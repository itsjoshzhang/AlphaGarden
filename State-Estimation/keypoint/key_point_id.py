#!/usr/bin/env python
# coding: utf-8
## General Imports
import os
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
import sys
# sys.path.insert(1, '..')
import keypoint.key_point_pipeline_utils as keypoint
import utils.full_auto_utils as fau
import time

## Heatmap Imports
import keypoint.keypoint_model as mdl
import torch
from torchvision import transforms
import numpy.linalg as la

## Clustering Imports
import scipy.cluster.hierarchy as hcluster
from sklearn.neighbors import NearestCentroid
from sklearn.decomposition import PCA
import cv2
import numpy as np
import pathlib

## Repo Imports
from keypoint.key_point_decision import *
from utils.crop_img_ind import *
from utils.constants import *

# path = pathlib.Path(__file__).parent.resolve()
# mask_path = os.path.join(path,'210805/snc-21080508141400.png')
# priors_path = os.path.join(path,'210805/priors210805.p')
# overhead_path = os.path.join(path,'210805/snc-21080508141400.jpg')
# date = '21080508141400'
shrink = 2

MODEL_PATH = './models/leaf_keypoints.pth'

def naive_centroid(arr):
        """ Find the centroid for a set of points
        Args:
            arr (numpy array of [[int,int], ...]): Set of points to cluster
        Returns:
            (1,2) numpy array for the centroid
        """
        length = arr.shape[0]
        sum_x = np.sum(arr[:, 0])
        sum_y = np.sum(arr[:, 1])
        return np.array((sum_x/length, sum_y/length)).reshape((1,2))

def find_point_clusters(points, thresh, name = 1):
    """ Find the Centroids for a given set of points by distance threshold
    Args:
        points: (numpy array with each element(col,row))): Set of points to cluster
        thresh (float): Max distance for clustering
    
    Returns:
        numpy array of centers for the clusters
    """
    if len(points) > 1:
        clusters = hcluster.fclusterdata(points, thresh, criterion="distance")
        unique = len(np.unique(clusters))
        if unique > 1:
            clf = NearestCentroid()
            clf.fit(points,clusters)
            return clf.centroids_,clusters, clf.classes_
        elif unique == 1:
            return naive_centroid(points), np.zeros((len(points),1))
    elif len(points) == 1:
        return points, np.zeros((len(points),1))
    return np.empty((0,2),dtype=np.int), np.empty((0,1))


transf = transforms.Compose([transforms.Resize((256,256)),transforms.ToTensor()])
def init_model(model_path=MODEL_PATH,  cuda_device = 'cuda'):
    '''Initialize the Model
    Params
        :string model_path: .pth location for the model
        
    Return
        :CountEstimate: initialized model

    Sample usage:
    >>> init_model()
    CountEstimate class instance
    '''
    komatsuna_leaf_count = mdl.CountEstimate()
    device = torch.device(cuda_device if torch.cuda.is_available() else 'cpu')
    komatsuna_leaf_count.load_state_dict(torch.load(model_path, map_location=device))
    print(device)
    komatsuna_leaf_count.to(device)
    return komatsuna_leaf_count
def eval_image(img_arr, model, transf = transf, device = 0):
    '''Apply the model on the image
    Params
        :numpy arr image_arr: image to apply model on
        :CountEstimate model: model to apply
        :TorchVision Transform transf: transform to use for image,
                                leave this as default for most uses
        
    Return
        :numpy arr: heatmap output
        :list y_hat: gpu attached output from the model

    Sample usage:
    >>> eval_image(test_im, model)
    heatmap, gpu_output
    '''
    img_arr = Image.fromarray(img_arr)
    if next(model.parameters()).is_cuda:
        y_hat,_ = model(transf(img_arr).unsqueeze(0).cuda(device))
    else:
        y_hat,_ = model(transf(img_arr).unsqueeze(0).cpu())
    return y_hat[0,0].cpu().detach().numpy(), y_hat


# Adapted From: https://stackoverflow.com/a/44874588
def create_circular_mask(h, w, center=None, radius=None, area_scale =4):
    '''Create a circular mask for the image
    Params
        :int h: height of image
        :int w: width of image
        :tuple center: center of mask circle
        :double radius: radius of the mask
        :double area_scale: scale for the area in the mask
        
    Return
        :numpy arr: output mask to use

    Sample usage:
    >>> create_circular_mask(256,256,(128,128), 4)
    mask_to_use
    '''
    if not radius is None:
        radius = radius/np.sqrt(area_scale)
    if center is None: # use the middle of the image
        center = (int(w/2), int(h/2))
    if radius is None: # use the smallest distance between the center and image walls
        radius = min(center[0], center[1], w-center[0], h-center[1])

    Y, X = np.ogrid[:h, :w]
    dist_from_center = np.sqrt((X - center[0])**2 + (Y-center[1])**2)

    mask = dist_from_center <= radius
    return mask
def shrink_im(im, inner_size = (128,128), outer_size = (256,256), which="RGB"):
    '''Shrink Image
    Params
        :numpy arr im: image to mask
        :tuple inner_size: the shrink size for image inside frame
        :tuple outer_size: size of the outside frame
        
    Return
        :numpy arr: output shrunk image

    Sample usage:
    >>> mask_im(im, mask)
    numpy array for image masked
    '''
    old = Image.fromarray(im)
    new = Image.new(which, outer_size)
    new.paste(old.resize(inner_size), ((outer_size[0]-inner_size[0])//2,(outer_size[1]-inner_size[1])//2))
    return np.array(new)
def mask_im(im, mask):
    '''Mask Image
    Params
        :numpy arr im: image to mask
        :numpy arr mask: mask to use 
        
    Return
        :numpy arr: output masked image

    Sample usage:
    >>> mask_im(im, mask)
    numpy array for image masked
    '''
    t2 = im.copy()
    t2[~mask] = 0
    return t2
    
def recursive_cluster(heatmap, leaves_remaining, masked, pts = np.empty((0,2)), thres = 0.3, flip_coords = False):
    '''Recursively Cluster the heatmap
    Params
        :numpy arr heatmap: heatmap output from the model
        :int leaves_remaining: number of leaves remaining to find
        :numpy arr masked: the masked plant image from the segmentation mask
        :numpy arr pts: all the discovered points so far
        :double thres: threshold for clustering
        :bool flip_coords: whether to swap x/y for the black point checking
        
    Return
        :numpy arr: array of keypoints from the heatmap, cv2 style

    Sample usage:
    >>> recursive_cluster(heatmap, 5, masked)
    array of points
    '''
    if leaves_remaining <= 0:
        return pts
    norm_map = (heatmap-np.min(heatmap))/(np.max(heatmap)-np.min(heatmap))
    pointmap = np.array(list(zip(*np.where(norm_map > thres))))
    clusterdata = find_point_clusters(pointmap,7)
    clusterpts = (clusterdata[0]).copy()
    for pt in clusterdata[0]:
        test_mask  = create_circular_mask(*heatmap.shape[:2], center = pt, radius = 7, area_scale=1)
        norm_map[np.argwhere(test_mask == 1)] = 0 
        pt2 = pt.astype(int)
        # print(tuple(masked[pt2[0],pt2[1]]),tuple(masked[pt2[1],pt2[0]]) )
        check = tuple(masked[pt2[0],pt2[1]]) if not flip_coords else tuple(masked[pt2[1],pt2[0]])
        if check == (0,0,0):
            clusterpts = np.delete(clusterpts,np.argwhere(clusterpts == pt)[:,0],axis=0)
    pts = np.vstack((pts,clusterpts))
    if len(clusterpts) == 0:
        return pts
    return recursive_cluster(norm_map,leaves_remaining-len(clusterpts), masked, pts)

def point_to_overhead(point, mask_center, input_size, scale = 4*1/0.75, orig_offset = (0,0)):
    '''Converts a point to the overhead space
    Params
        :tuple point: array location of the point
        :tuple point: array location of the plant in overhead
        :tuple input_size: size of the input image (square)
        :double scale: factor for the point scaling
        :tuple orig_offset: offset for the original plant, in case the mask was cut off
    Return
        :tuple: point containing (x,y) coords in matplotlib format ((0,0) is top left)

    Sample usage:
    >>> point_to_overhead((0,0),(0,0),(256,256))
    (-682.67,-682.67)
    '''
    center_scale = scale*(np.array(point) - np.array(input_size)//2) + np.array(orig_offset)
    return center_scale[::-1] + np.array(mask_center)

def remove_keypoints(points, mask, inner_thres = 10):
    '''Removes bad keypoints from a detection
    Params
        :tuple points: locations for the keypoints
        :np array mask: array location of the plant in overhead
        :float inner_thres: threshold to remove points too close to the edge of the mask
    Return
        :np array: array with the cleaned points list

    Sample usage:
    >>> point_to_overhead(pts, mask, 5)
    [(),(),(),...]
    '''
    assert inner_thres >= 0
    inner_pts = []
    for point in points:
        if mask[int(point[0]),int(point[1])] != 0:
            inner_pts.append(tuple(point) )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contours = sorted(contours, key=lambda x: cv2.contourArea(x))
    respectivepts = [[(point,cv2.pointPolygonTest(contour,point[::-1],True)) for point in inner_pts if 
                        cv2.pointPolygonTest(contour,point[::-1],True) >= 0] for contour in contours]
    ret_pts = set()
    for ct in respectivepts:
        for pt, dist in ct:
            if dist >= inner_thres:
                ret_pts.add(pt) 
    return list(ret_pts)

def get_keypoints(mask_path, overhead_path, priors_path, model_path, date = "00000000", save_raw = False, **kwargs):
    '''Generates a keypoint dictionary with all the data for the overhead
    Params
        :string mask_path: location of segmentation mask
        :string overhead_path: location of cropped overhead image to overlay points on
        :string priors_path: location for prioirs (.p) files
        :string model_path: location for the model path for leaf counting (.pth)
        :string date: date for the images, optional but useful
        
    Return
        :dict: Dictionary with data in the following format:
            [
                {
                plant_type
                mask_center (from priors)
                leaves: array of garden coordinates for all detected leaf center
                original: localized points from each individual prediction
                id_name: name of the date and plant index from the model
                radius: the radius of the plant on the overhead image
                },
                another plant...
            ]

    Sample usage:
    >>> get_keypoints("snc-1240234232.png", "snc-1240234232.jpg", "snc-1240234232.p", "model.pth", "20210624")
    dict with the above data
    '''
    print(mask_path, overhead_path, priors_path, model_path, date, kwargs)
    model = init_model(model_path=model_path, **kwargs)
    mask = fau.get_img(mask_path)[1]
    overhead = fau.get_img(overhead_path)[1]
    priors = keypoint.get_recent_priors(priors_path)
    plants = keypoint.get_individual_plants(priors, mask, overhead)
    vals = dict()
    for cut_idx, plant in enumerate(plants):
        if not plant[3] in vals:
            vals[plant[3]] = {}
        size = np.array(plant[0].shape[:2])
        plant[4][np.where(plant[4] != 0)] = 1
        vals[plant[3]][f'{date}_{cut_idx}'] = [shrink_im(plant[0], tuple(size//shrink), tuple(size)), 1.5* plant[2] //shrink, plant[1], 
                                    shrink_im(plant[4], tuple(size//shrink), tuple(size),which="L"), plant[5], plant[6]]
    leaf_centers  = []
    for pt in list(vals.keys()):    
        for rc in list(vals[pt].keys()):
            plant = vals[pt][rc]
            t = eval_image(plant[0],model)
            center = np.array(plant[0].shape)
            plant_mask  = create_circular_mask(*plant[0].shape[:2], center = center//2, radius = plant[1])
            t_arr = mask_im(np.asarray(t[0]), plant_mask)
            pts = recursive_cluster(t_arr, round(t[1].sum().item()), plant[0])
            pts = remove_keypoints(pts,plant[3],inner_thres=plant[1]*0.01)
            if save_raw:
                mask = np.copy(plant[0])
                for x,y in pts:
                    x,y = int(x), int(y)
                    mask = cv2.rectangle(mask,(x-1,y-1),(x+1,y+1), (255,255,255),-1)
            im_size = np.array(plant[0].shape[:2])
            converted_pts = np.array([point_to_overhead(pt, plant[2], plant[0].shape[:2],
                                    scale=min(im_size/(im_size//shrink))*plant[4], orig_offset = plant[5]) for pt in pts]).astype(int)
            leaf_centers.append({
                                    'plant_type':pt, 
                                    'mask_center': tuple(np.array(plant[2]).astype(int)), 
                                    'leaves': converted_pts, 
                                    'original':pts, 
                                    'id_name':rc, 
                                    'radius':plant[1]*shrink,
                                    'localized_im': mask if save_raw else None
                                })
    return leaf_centers

def generate_image(leaf_centers, overhead_path, raw_img = False):
    '''Generates Leaf Center Images from overhead image
    Params
        :dict leaf_centers: data generated from get_keypoints
        :string overhead_path: location of cropped overhead image to overlay points on
        
    Return
        :AxesImage: axes with the overlayed image

    Sample usage:
    >>> generate_images(leaf_centers, "snc-1240234232.jpg")
    matplotlib AxesImage with the data
    '''
    # file = overhead_path[-22:-4]
    load = cv2.imread(overhead_path)
    _ = plt.imshow(load, alpha = 0.5)
    # mask = np.zeros(load.shape, dtype = np.uint8)
    colors = {}
    for plant in leaf_centers:
        if not plant['plant_type'] in colors:
            cl = list(np.random.random(size=3) * 256)
            while cl in list(colors.values()):
                cl = list(np.random.random(size=3) * 256)
            colors[plant['plant_type']] = cl
        for pt in plant['leaves']:
            x,y = pt
            x,y = int(x), int(y)
            load = cv2.rectangle(load,(x-5,y-5),(x+5,y+5), colors[plant['plant_type']],-1)
    _ = plt.imshow(load)
    # _ = plt.imshow(mask, alpha = 0.5)
    # plt.imsave("./target_leaf_data/images/" + file + ".png", load) # For pruning
    # plt.imsave("./Experiments/" + file + ".png", load) # For experiments
    # print(colors)
    if raw_img:
        return plt, load
    else:
        return plt

def potted_plant_auto(overhead, mask):
    file = overhead[-22:-4]
    im = cv2.cvtColor(cv2.imread(overhead), cv2.COLOR_BGR2RGB)
    ## Identify all key points (need prior from get_points, and manual mask)
    print("INSTRUCTION: Label center then outer most point!")
    center, outer = get_points(im)
    dist = ((center[0] - outer[0])**2 + (center[1] - outer[1])**2)**0.5
    prior = {'external': [{'circle': (center, dist, outer), 'days_post_germ': 40}]}
    print("DUMPING")
    pkl.dump(prior, open(PRIORS + file + '.p', 'wb'))
    leaf_centers = get_keypoints(mask, overhead, os.path.join(PRIORS, file + '.p'), MODEL_PATH)
    generate_image(leaf_centers, overhead)
    ## Cut all points
    return leaf_centers

if __name__ == "__main__":
    ## Experiments
    # leaf_centers = potted_plant_auto("Experiments/snc-21081309141400.jpg", "Experiments/snc-21081309141400_mask.png")
    # print(leaf_centers)

    ## Generation
    file = "snc-21090119150000"
    leaf_centers = get_keypoints(PROCESSED_IMAGES + file + ".png", os.path.join(CROPPED_LOC, file + ".jpg"), os.path.join(PRIORS + "left/priors210901.p"), MODEL_PATH)
    pkl.dump(leaf_centers, open("./target_leaf_data/data/" + file + "_unfiltered.p", "wb"))
    print("./target_leaf_data/data/" + file + "_unfiltered.p")
    with open("./target_leaf_data/data/" + file + "_unfiltered.p", "rb") as f:
        leaf_centers = pkl.load(f)
        fg = generate_image(leaf_centers, "./cropped/" + file + ".jpg", raw_img=True)
        fg[0].imsave(f'./target_leaf_data/images/{file}.png', fg[1])

    ##Simulator
    pkl.dump([], open("plants_to_prune.p", "wb"))
    os.system('python3 ../Learning/create_state.py ' + 'l') #choose side
    os.system('python3 ../Learning/eval_policy.py -p ba -d 2')
    time.sleep(10)

    ## Selection
    select = SelectPoint(leaf_centers, 'l') #choose side
    target_list = select.center_target()
    pkl.dump(leaf_centers, open("./target_leaf_data/data/" + file + ".p", "wb"))
    print(target_list)

