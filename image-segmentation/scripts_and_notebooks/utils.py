import os
import json
import numpy as np
import torch

import mlflow

from torch import nn, optim
from torch.utils.data import Dataset, DataLoader

import torchvision.transforms.functional as TF
from torchvision import transforms
from torchvision.models.segmentation import fcn_resnet50, FCN_ResNet50_Weights
from torchvision.models.segmentation import deeplabv3_resnet50, DeepLabV3_ResNet50_Weights

from PIL import Image

import matplotlib.pyplot as plt

from custom_unet import UNet_without_activation

def refresh_token(secret_file_path):
    with open(secret_file_path, "r") as file:
    	token = file.read().strip()
    os.environ['MLFLOW_TRACKING_TOKEN'] = token
    print("Token successfully refreshed")

def get_categories_list(data_dir, return_json_if_defined=False):
    
    subfiles = os.listdir(data_dir)
    defined_by_json = False
    
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
    
    if defined_by_json and return_json_if_defined:
        return categories, data
        
    return categories

class Segmentation_Dataset(Dataset):
    def __init__(self, data_dir, transform=None, json_data=None):
        self.data_dir = data_dir
        self.transform = transform
        self.json_data = json_data
        
        # base image directory is always "images"
        self.images_dir = os.path.join(self.data_dir,"images")
        self.file_names = os.listdir(self.images_dir)
        
        if self.json_data:
            self.N_categories = json_data["N_categories"]
            self.categories = [json_data["categories"][i]["name"] for i in range(json_data["N_categories"])]
            self.masks_dir = os.path.join(self.data_dir, json_data["mask_folder"])
            self.mask_files = os.listdir(self.masks_dir) # mask files should be named the same as images, but with .npy extension
            
        else:
            self.categories = os.listdir(data_dir)
            self.categories.remove("images")

            self.N_categories = len(self.categories)
            
            self.masks_dirs = []
            for categorie in self.categories:
                self.masks_dirs.append(os.path.join(self.data_dir, categorie))

        
    def __len__(self):
        return len(self.file_names)
    
    def __getitem__(self, idx):
        
        img_path = os.path.join(self.images_dir, self.file_names[idx])
        image = Image.open(img_path)
        
        if self.json_data:
            mask_path = os.path.join(self.masks_dir, self.mask_files[idx])
            mask = np.load(mask_path)
            
            # (H, W) image, where pixel value actually corresponds to classes
            # that way, it can go through PairedRandomHorizontalFlip
            masks = Image.fromarray(mask) 
            
        else:
            masks_paths = []
            for i in range(self.N_categories):
                categorie = self.categories[i]
                masks_paths.append(os.path.join(self.masks_dirs[i], self.file_names[idx]))

            masks = [Image.open(mask_path) for mask_path in masks_paths]

        sample = (image, masks)

        if self.transform:
            sample = self.transform(sample)
            
        return sample


class PairedToTensor():
    
    def __init__(self, category_encoded_mask=False, N_categories=0):
        self.category_encoded_mask = category_encoded_mask
        self.N_categories = N_categories
    
    def __call__(self, sample):
        
        img, masks = sample
        
        img = np.array(img)
        img = np.moveaxis(img, -1, 0) # needs to be of shape (channels, H, W)
        img = torch.FloatTensor(img)
        img = img/255

        #masks = np.array(masks)
        #masks = np.array([np.array(mask) for mask in masks])
        
        if not self.category_encoded_mask:

            masks = np.array([np.array(mask) for mask in masks])

            # masks is already of shape (channels,H,W)
            #masks = np.expand_dims(masks, -1) 
            #masks = np.moveaxis(masks, -1, 0)

            masks = torch.FloatTensor(masks)
            masks = masks/255
        
        # need to one hot encode masks
        else:
            masks = np.array(masks)
            masks = torch.nn.functional.one_hot(torch.FloatTensor(masks).to(torch.int64), num_classes=self.N_categories).to(torch.float)
            masks = masks.movedim(-1,0)
            
        return img, masks


class PairedRandomHorizontalFlip():
    def __init__(self, p=0.5, category_encoded_mask=False):
        self.p = p
        self.category_encoded_mask = category_encoded_mask
        
    def __call__(self, sample):
        img, masks = sample
        
        if np.random.random() < self.p:
            if not self.category_encoded_mask:
                img = TF.hflip(img)
                masks = [TF.hflip(mask) for mask in masks]
            
            else:
                # only one (H,W) "image" if category encoded mask - keeping maskS variable to save code lines
                img, masks = TF.hflip(img), TF.hflip(masks)
            
        return img, masks


class PairedRandomAffine():
    
    def __init__(self, degrees= None, translate=None, scale_ranges=None,
                shears=None, category_encoded_mask=False):
        self.category_encoded_mask = category_encoded_mask
        self.params = {
            'degree': degrees,
            'translate': translate,
            'scale_ranges':scale_ranges,
            'shears':shears
        }
    def __call__(self, sample):
        img, masks = sample
        w, h = img.size
        
        angle, translations, scale, shear = transforms.RandomAffine.get_params(
            self.params['degree'], self.params['translate'],
            self.params['scale_ranges'], self.params['shears'],
            (w,h)
        )
        
        img = TF.affine(img, angle, translations, scale, shear)
        if self.category_encoded_mask:
            masks = TF.affine(masks, angle, translations, scale, shear)
        else:
            masks = [TF.affine(mask, angle, translations, scale, shear) for mask in masks]
        
        return img, masks

def show_sample(sample, title=None):
    
    # sample from SegmentationDataset with transform
    if type(sample[0]) == torch.Tensor:
        
        original_image = np.moveaxis(sample[0].numpy(), 0, -1)
        N_categories = sample[1].shape[0]
        
        fig, ax = plt.subplots(1, 1+N_categories)
        ax[0].imshow(original_image)
        for i in range(1,1+N_categories):
            ax[i].imshow(sample[1].numpy()[i-1], cmap="gray")
        
    # sample from SegmentationDataset without transform
    else:
        N_categories = len(sample[1])
        fig, ax = plt.subplots(1, 1+N_categories)
        ax[0].imshow(sample[0])
        for i in range(1,1+N_categories):
            ax[i].imshow(sample[1][i-1], cmap="gray")
            
    if title:
        fig.suptitle(title)
    plt.show()


def iou(pred, label):
    intersection = (pred * label).sum()
    union = pred.sum() + label.sum() - intersection
    if pred.sum() == 0 and label.sum() == 0:
        return 1
    return intersection / union


def dice_coef_metric(pred, label):
    intersection = 2.0 * (pred * label).sum()
    union = pred.sum() + label.sum()
    if pred.sum() == 0 and label.sum() == 0:
        return 1
    return intersection / union


def train_loop(model, get_model_pred, optimizer, criterion, train_loader, device):
    running_loss = 0
    model.train()
    
    final_dice_coef = 0 
    final_iou = 0

    for i, data in enumerate(train_loader, 0):
        
        imgs, masks = data

        imgs = imgs.to(device)
        masks = masks.to(device)
        
        # forward
        out = get_model_pred(model, imgs)
        loss = criterion(out, masks)
        running_loss += loss.item() * imgs.shape[0]
        
        out_cut = np.copy(out.detach().cpu().numpy())
        out_cut[np.nonzero(out_cut < 0.5)] = 0.0
        out_cut[np.nonzero(out_cut >= 0.5)] = 1.0
            
        train_dice = dice_coef_metric(out_cut, masks.data.cpu().numpy())
        final_dice_coef += train_dice 
        
        train_iou = iou(out_cut, masks.data.cpu().numpy())
        final_iou += train_iou
        
        # optimize
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        
    running_loss /= len(train_loader.sampler)
    return {'train_dice_coef':final_dice_coef/len(train_loader), 'train_iou': final_iou/len(train_loader), 'train_loss':running_loss}


def eval_loop(model, get_model_pred, criterion, eval_loader, device):
    
    running_loss = 0
    final_dice_coef = 0 
    final_iou = 0
    
    model.eval()
    with torch.no_grad():

        for i, data in enumerate(eval_loader, 0):
            
            imgs, masks = data
            
            imgs = imgs.to(device)
            masks = masks.to(device)
            
            #out = model(imgs)['aux']
            out = get_model_pred(model, imgs)
            
            loss = criterion(out, masks)
            running_loss += loss.item() * imgs.shape[0]
            
            out_cut = np.copy(out.detach().cpu().numpy())
            out_cut[np.nonzero(out_cut < 0.5)] = 0.0
            out_cut[np.nonzero(out_cut >= 0.5)] = 1.0
            
            valid_dice = dice_coef_metric(out_cut, masks.data.cpu().numpy())
            final_dice_coef += valid_dice 
            
            valid_iou = iou(out_cut, masks.data.cpu().numpy())
            final_iou += valid_iou
            
    running_loss /= len(eval_loader.sampler)   
    return {'val_dice_coef':final_dice_coef/len(eval_loader), 'val_iou': final_iou/len(eval_loader), 'val_loss':running_loss}


def train(model, get_model_pred, optimizer, criterion, scheduler, train_loader, val_loader, num_epochs,
          device, save_path="", track_with_mlflow=False, secret_file_path=None):
    
    train_loss_list = []
    train_dice_coef = []
    train_iou = []
    val_loss_list = []
    val_dice_coef = []
    val_iou = []
    
    val_loss_min = np.inf
    
    for e in range(num_epochs):

        if track_with_mlflow:
            refresh_token(secret_file_path)
        
        train_metrics = train_loop(model, get_model_pred, optimizer, criterion, train_loader, device)
        
        val_metrics = eval_loop(model, get_model_pred, criterion, val_loader, device)
        
        scheduler.step(val_metrics['val_dice_coef'])
        
        train_loss_list.append(train_metrics['train_loss']) 
        train_dice_coef.append(train_metrics['train_dice_coef'])
        train_iou.append(train_metrics['train_iou'])
        val_loss_list.append(val_metrics['val_loss'])
        val_dice_coef.append(val_metrics['val_dice_coef'])
        val_iou.append(val_metrics['val_iou'])
        
        
        print_string = f"\nEpoch: {e+1}\n"
        print_string += f"Train Loss: {train_metrics['train_loss']:.5f}\n"
        print_string += f"Train Dice Coef: {train_metrics['train_dice_coef']:.5f}\n"
        print_string += f"Train IoU: {train_metrics['train_iou']:.5f}\n"
        print_string += f"Valid Loss: {val_metrics['val_loss']:.5f}\n"
        print_string += f"Valid Dice Coef: {val_metrics['val_dice_coef']:.5f}\n"
        print_string += f"Valid IoU: {val_metrics['val_iou']:.5f}\n"
        print(print_string)

        all_metrics = train_metrics | val_metrics

        if track_with_mlflow:
            refresh_token(secret_file_path)
            mlflow.log_metrics(all_metrics, step=e)
        
        if val_metrics["val_loss"] <= val_loss_min:
            best_epoch_metrics = all_metrics
            
            if save_path:
                torch.save(model.state_dict(), save_path)
                print(f"Model checkpoint saved as {save_path}")
                val_loss_min = val_metrics["val_loss"]
                
            if track_with_mlflow:
                best_weights = model.state_dict()
                
                
    if track_with_mlflow:
        refresh_token(secret_file_path)
        print("Loading back best weights to the model")
        model.load_state_dict(best_weights)
        print("Best weights loaded successfully")
        
    return best_epoch_metrics
    
        #print("Logging model to MLflow...")
        #mlflow.pytorch.log_model(model, "model")
        #print("Model successfully logged to MLflow!")

                
            #best_epoch = e+1
            #tloss_at_best_epoch = train_metrics['loss']
            #tdice_at_best_epoch = train_metrics['dice coef']
            #tiou_at_best_epoch = train_metrics['iou']
            #vloss_at_best_epoch = val_metrics['loss']
            #vdice_at_best_epoch = val_metrics['dice coef']
            #viou_at_best_epoch = val_metrics['iou']

    #metrics_at_best_epoch = {
    #    "Best epoch": best_epoch,
    #    "Train Loss": tloss_at_best_epoch,
    #    "Train Dice Coefficient": tdice_at_best_epoch,
    #    "Train IoU": tiou_at_best_epoch,
    #    "Validation Loss": vloss_at_best_epoch,
    #    "Validation Dice Coefficient": vdice_at_best_epoch,
    #    "Validation IoU": viou_at_best_epoch,
    #}
    #return metrics_at_best_epoch
    

def get_color_map(data_dir):
    
    subfiles = os.listdir(data_dir)
    json_file_found = False
    
    for f in subfiles:
        if ".json" in f:
            json_file = os.path.join(data_dir,f)
            json_file_found = True
            print(f"Reading categories from json file: {json_file}")
            break
    
    if not json_file_found:
        print("No JSON file found, returning None")
        return None
    
    with open(json_file, 'r') as fp:
        data = json.load(fp)
    
    colors = np.array([data["categories"][i]["color"] for i in range(data["N_categories"])])
    
    return colors

def plot_pred(model, get_model_pred, dataset, sample_idx, device, color_map=None):
    sample_img, sample_mask = dataset[sample_idx]
    sample_img_tensor = torch.FloatTensor(np.expand_dims(sample_img, 0))
    sample_img_tensor = sample_img_tensor.to(device)

    sample_img = torch.Tensor(sample_img).permute(1,2,0)
    
    model.eval()
    sample_pred = get_model_pred(model,sample_img_tensor)
    sample_pred = sample_pred.cpu().detach().numpy()
    
    
    fig, ax = plt.subplots(1, 3, figsize=(20,10))
    ax[0].title.set_text("Original image")
    ax[0].imshow(sample_img)
    
    # color_map is None only if output is a single channel
    if not type(color_map) == np.ndarray:
        ax[1].title.set_text("Ground truth")
        ax[1].imshow(sample_mask[0], cmap="gray")

        ax[2].title.set_text("Prediction")
        ax[2].imshow(sample_pred[0][0], cmap="gray")
        
    else:
        
        colored_mask = color_map[np.argmax(sample_mask.numpy(), axis=0)]
        colored_pred = color_map[np.argmax(sample_pred[0], axis=0)]
        
        ax[1].title.set_text("Ground truth")
        ax[1].imshow(colored_mask)

        ax[2].title.set_text("Prediction")
        ax[2].imshow(colored_pred)
    
    plt.show()


def get_data_loaders(data_dir,
                     batch_size,
                     enable_data_augmentation=True,
                     enable_affine_transform=True,
                     max_rotation=15,
                     max_translation=0.1,
                     min_scale=0.8,
                     max_scale=1.2,
                     enable_horizontal_flip=True,
                     return_datasets=False):
    
    subfiles = os.listdir(data_dir)
    
    defined_by_json = False
    category_encoded_mask = False
    jsondata = None
    
    #try:
    #    categories, jsondata = get_categories_list(data_dir, return_json_if_defined=True)
    #    category_encoded_mask = True
    #except:
    #    categories = get_categories_list(data_dir, return_json_if_defined=True)
    
    categories_output = get_categories_list(data_dir, return_json_if_defined=True)
    try:
        categories, jsondata = categories_output
        category_encoded_mask = True
    except:
        categories = categories_output
        
        
    N_categories = len(categories)
            
    train_dir = os.path.join(data_dir, "train")
    val_dir = os.path.join(data_dir, "val")
    test_dir = os.path.join(data_dir, "test")
    
    contains_test = False
    
    if os.path.isdir(test_dir):
        contains_test = True
    
    if not enable_data_augmentation:
        train_dataset = Segmentation_Dataset(train_dir, transform=PairedToTensor(category_encoded_mask=category_encoded_mask, N_categories=N_categories), json_data=jsondata)
        
    else:
        train_transform_list = []
        
        if enable_horizontal_flip:
            train_transform_list.append(PairedRandomHorizontalFlip(category_encoded_mask=category_encoded_mask))
            
        if enable_affine_transform:
            train_transform_list.append(PairedRandomAffine(
            degrees=(-max_rotation, max_rotation),
            translate=(max_translation, max_translation),
            scale_ranges=(min_scale, max_scale),
            category_encoded_mask=category_encoded_mask
            ))
            
        train_transform_list.append(PairedToTensor(category_encoded_mask=category_encoded_mask, N_categories=N_categories))
        train_transforms = transforms.Compose(train_transform_list)
        train_dataset = Segmentation_Dataset(train_dir, transform=train_transforms, json_data=jsondata)

    
    val_dataset = Segmentation_Dataset(val_dir, transform=PairedToTensor(category_encoded_mask=category_encoded_mask, N_categories=N_categories), json_data=jsondata)
    
    if contains_test:
        test_dataset = Segmentation_Dataset(test_dir, transform=PairedToTensor(category_encoded_mask=category_encoded_mask, N_categories=N_categories), json_data=jsondata)
        
        if return_datasets: 
            return (train_dataset, val_dataset, test_dataset)
        
    elif return_datasets:
        return (train_dataset, val_dataset)
    
    train_loader = DataLoader(train_dataset, batch_size=batch_size,
                             shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size,
                             shuffle=False)
    
    if contains_test:
        test_loader = DataLoader(test_dataset, batch_size=batch_size,
                                 shuffle=False)
        return (train_loader, val_loader, test_loader)
    else:
        return (train_loader, val_loader)

# also returns a function to call for getting models predictions
def get_and_adapt_pretrained_model(model_name, N_categories):
    
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
            custom_model =  UNet_without_activation(in_channels=3, out_channels=1, init_features=32)
            custom_model.load_state_dict(model.state_dict())
            
            custom_model.conv = nn.Sequential(
                nn.Conv2d(32, N_categories, kernel_size=(1, 1), stride=(1, 1)),
                nn.Softmax(dim=1))
            
            model = custom_model

        def get_model_pred(model, imgs):
            return model(imgs)
    
    else:
        print("Error model not recognized/tested")
    
    return model, get_model_pred

def load_model_checkpoint(model_name, checkpoint_path, N_categories=1):
    
    model, _ = get_and_adapt_pretrained_model(model_name, N_categories)
    model.load_state_dict(torch.load(checkpoint_path))
    model.eval()
    
    return model
        
    