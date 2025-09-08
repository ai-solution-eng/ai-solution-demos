# Bring your own dataset

You can reuse the training script and display application from this demo with your own dataset, provided it corresponds to a 2D segmentation problem and is following a specific file/folder structure.

This file explains how to shape your data in a way that will make it compatible with this demo, but alternatively, you can also have a look on the provided datasets and replicate their format.

# Dataset Prerequisites

## Folder Structure

* **Single channel segmentation**: If your dataset only has one output channel/one type of instance to segment:
```
├── dataset_dir
│   ├── train
│   │   ├── images
│   │   │   ├── image1 file
│   │   │   ├── image2 file
│   │   │   ├── ...
│   │   ├── categorie_name (e.g. 'mask')
│   │   │   ├── image1 file
│   │   │   ├── image2 file
│   │   │   ├── ...
│   ├── val
│   │   ├── images
│   │   ├── (if single channel segmentation) categorie_name (e.g. 'mask')
│   │   ├── ...
│   ├── (optional) test
│   │   ├── ...
```
* **Semantic segmentation**: If your dataset has multiple output channels/distinct categories to identify:
```
├── dataset_dir
│   ├── train
│   │   ├── images
│   │   │   ├── image1 file
│   │   │   ├── image2 file
│   │   │   ├── ...
│   │   ├── categorie_masks (folder name read from json file)
│   │   │   ├── image1.npy file
│   │   │   ├── image2.npy file
│   │   │   ├── ...
│   ├── val
│   │   ├── images
│   │   ├── categorie_masks (folder name read from json file)
│   ├── (optional) test
│   │   ├── ...
│   ├── json file
```
## Explanation

* **train** and **val** folders are mandatory, and must have these names. **test** folder is optional, it will not be used during training.
* **images** folder must be present in each of train, val and test folders, and must have this specific name. It is expected to hold the original images, on which the models will be trained, and for which they will make their predictions.
* **image files under images folder** must be RGB images, that can be opened with `Image.open()` (from PIL library).
* For single channel segmentation:
  * **categorie_name** folders can have any name, but it must be the same under train, val and test folders. They are expected to hold grayscale images that defines masks the model will try to generate as output.
  * **images files under categorie_name folder** must share the same name and extension as images files under the images folder, so that the script can link input image with corresponding mask. They should be **grayscale images, each or their pixel value being either 0 or 255**.
* For semantic segmentation:
  * A **json file** (its name doesn't matter) must be defined directly under data_dir. Its values should include:
    * mask_folder: name of categorie_masks folder
    * N_categories: number of distinct categories to segment
    * categories: a list of dictionaries, each of them corresponding to one of the distinct classes to segment. Each of these dictionaries must define:
      * name: the name of the categorie
      * color: a list of the three RGB values that will be used for visualizing masks
  * **categorie_masks** (name defined in the json file, must be the same under train, val and test folder). This folder should contain .npy files, containing an np.array of shape (H, W), where H and W are the height and width of the corresponding image. Each value should be an integer, corresponding to the class to segment at this position. Name of .npy files should match images file name (before extension). 

## Other prerequisites:
 * **Images' height and width must both be divisible by 8**. Otherwise, training the 'unet' type of model will fail.
