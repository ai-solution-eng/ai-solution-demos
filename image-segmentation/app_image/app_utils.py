import os
import json
import numpy as np
from PIL import Image

import mlflow

import torch
from torch import nn

from torchvision.models.segmentation import fcn_resnet50, FCN_ResNet50_Weights
from torchvision.models.segmentation import deeplabv3_resnet50, DeepLabV3_ResNet50_Weights

from custom_unet import UNet_without_activation

def refresh_token(secret_file_path):
    with open(secret_file_path, "r") as file:
    	token = file.read().strip()
    os.environ['MLFLOW_TRACKING_TOKEN'] = token
    print("Token successfully refreshed")

def get_categories_and_json(data_dir):
    
    subfiles = os.listdir(data_dir)
    defined_by_json = False
    data = None
    
    for f in subfiles:
        if ".json" in f:
            json_file = os.path.join(data_dir,f)
            defined_by_json = True
            print(f"Reading categories from json file: {json_file}")
    
    if defined_by_json:
        with open(json_file, 'r') as fp:
            data = json.load(fp)
        assert "N_categories" in data, "N_categories must be defined in the json file"
        assert "categories" in data, "categories must be defined in the json file"
        assert len(data["categories"]) == data["N_categories"], "Number of elements in categories does not match N_categories"
        assert "name" in data["categories"][0], "name must be provided for each element within the categories array"
        assert "mask_folder" in data, "mask_folder must be defined in the json file"
        categories = [data["categories"][i]["name"] for i in range(data["N_categories"])]
        
    else:
        print("Getting categories from train subfolder (minus images folder)")
        train_dir = os.path.join(data_dir, "train")
        categories = os.listdir(train_dir)
        categories.remove("images")
        
    return categories, data

def get_color_map_from_dict(dict):
    colors = np.array([dict["categories"][i]["color"] for i in range(dict["N_categories"])])    
    return colors

def get_mask_and_filename(im, subset_with_path, categories, json_data, color_map):
    if json_data:
        mask_folder = json_data["mask_folder"]
        # Mask should have the same name as the image, but with .npy extension
        split_im_name = im.split(".")
        mask_file = ".".join(split_im_name[:-1])+".npy"
        mask_file_with_path = os.path.join(subset_with_path, mask_folder, mask_file)
        mask = color_map[np.load(mask_file_with_path)]
    else:
        assert len(categories) == 1, "A JSON config file must be provided for datasets with more than one categorie to segment"
        mask_folder = categories[0]
        mask_file = im # mask_file is named identically as image for single channel segmentation problems
        mask_file_with_path = os.path.join(subset_with_path, mask_folder, mask_file)
        mask = np.array(Image.open(mask_file_with_path))

    return mask, mask_file


def get_and_adapt_pretrained_model(model_name, N_categories, mlf_model=False, device=None):

    if mlf_model:
        
        if model_name in ["fcn_resnet50", "deeplabv3_resnet50"]:
            def get_model_pred(model, imgs):
                return model(imgs)['aux']
        else: # unet
            # load public pretrained model first, as "unet" isn't defined from mlflow model on its own
            if N_categories == 1:
                model = torch.hub.load('mateuszbuda/brain-segmentation-pytorch', 'unet', in_channels=3, out_channels=1, init_features=32, pretrained=True)
            def get_model_pred(model, imgs):
                return model(imgs)
        
        model = mlflow.pytorch.load_model(mlf_model, map_location=device)
        
        return model, get_model_pred

    if model_name in ["fcn_resnet50", "deeplabv3_resnet50"]:
        
        if model_name == "fcn_resnet50":
            weights = FCN_ResNet50_Weights.DEFAULT
            model = fcn_resnet50(weights=weights)
            model.classifier[4] = torch.nn.Conv2d(512, N_categories, kernel_size=(1,1), stride=(1,1))
        if model_name == "deeplabv3_resnet50":
            weights = DeepLabV3_ResNet50_Weights.DEFAULT
            model = deeplabv3_resnet50(weights=weights)
            model.classifier[4] = torch.nn.Conv2d(256, N_categories, kernel_size=(1,1), stride=(1,1))
        
        model.aux_classifier[4] = torch.nn.Conv2d(256, N_categories, kernel_size=(1,1), stride=(1,1))
        
        if N_categories == 1:
            model.classifier.append(torch.nn.Sigmoid())
            model.aux_classifier.append(torch.nn.Sigmoid())
        else:
            model.classifier.append(torch.nn.Softmax(dim=1))
            model.aux_classifier.append(torch.nn.Softmax(dim=1))
        
        def get_model_pred(model, imgs):
            return model(imgs)['aux']
    
    elif model_name == "unet":
        
        # loading original, pretrained unet model - it applies sigmoid to its output
        model = torch.hub.load('mateuszbuda/brain-segmentation-pytorch', 'unet', in_channels=3, out_channels=1, init_features=32, pretrained=True)
        
        # if more than one categorie to segment, we need to change the last layer
        # but also to apply softmax
        if N_categories != 1:
            
            # problem is that pretrained unet applies sigmoid to its output, and adding a softmax layer before does not prevent this issue
            # that's why we need a custom unet, that does not apply sigmoid
            custom_model = model_without_activation =  UNet_without_activation(in_channels=3, out_channels=1, init_features=32)
            custom_model.load_state_dict(model.state_dict())
            
            custom_model.conv = nn.Sequential(
                nn.Conv2d(32, N_categories, kernel_size=(1, 1), stride=(1, 1)),
                nn.Softmax(dim=1))
            
            model = custom_model

        def get_model_pred(model, imgs):
            return model(imgs)
    
    else:
        print("Error model not recognized/tested")

    model.eval()
    
    return model, get_model_pred

def load_model_checkpoint(model_name, checkpoint_path, N_categories=1, mlf=False):
    
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    if mlf:
        print("Loading model from MLflow...")
        model, get_model_pred = get_and_adapt_pretrained_model(model_name, N_categories, mlf_model=checkpoint_path, device=device)
        print("Successfully loaded model from MLflow!")

    else:
        print("Loading model local model...")
        model, get_model_pred = get_and_adapt_pretrained_model(model_name, N_categories)
        model.load_state_dict(torch.load(checkpoint_path, map_location=device))
        print("Successfully loaded local model!")

    model.eval()
    
    return model, get_model_pred

def get_model_pred_on_img(model, get_model_pred, opened_image):
    img = np.array(opened_image)
    img = np.moveaxis(img, -1, 0) # needs to be of shape (channels, H, W)
    img = np.expand_dims(img, 0) # add batch dimension
    img = torch.FloatTensor(img)
    img = img/255

    pred = get_model_pred(model, img).cpu().detach().numpy()
    return pred