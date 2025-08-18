This file contains examples of commands calling the train.py script to start training different models on different architectures.

**These commands will not necessarily train good models, they are simply provided to quickly start trainings**, without having to think too much about the script parameters.
Main parameters to consider exploring for improving models' performances are:
  * Number of epochs (the more, the better, at the cost of longer training)
  * Batch size (also have an impact on training duration)
  * Learning rate
  * Enabling affine transformation for data augmentation (recommneded for Brain MRI dataset, not recommended for the others)

**Model registering with MLflow only happens at the end of the training, so don't hesitate to reduce the number of epochs if you quickly want a MLflow model, regardless of its performances.**

For a complete list of hyperparameters, please refer to the train.py script, or call `python train.py --help`

# Metal nut dataset trainings

## Unet architecture
`python train.py --data-dir /mnt/user/datasets/metal_nut_dataset --model-name unet --save-path /mnt/user/checkpoints/metalnut_unet.pth --epochs 10 --batch-size 16 --disable-affine-transform`

## Deeplabv3_Resnet50 architecture
`python train.py --data-dir /mnt/user/datasets/metal_nut_dataset --model-name deeplabv3_resnet50 --save-path /mnt/user/checkpoints/metalnut_deeplabv3_resnet50.pth --epochs 10 --batch-size 16 --disable-affine-transform`

## FCN_Resnet50 architecture
`python train.py --data-dir /mnt/user/datasets/metal_nut_dataset --model-name fcn_resnet50 --save-path /mnt/user/checkpoints/metalnut_fcn_resnet50.pth --epochs 10 --batch-size 16 --disable-affine-transform`

# Cityscapes dataset trainings

## Unet architecture
`python train.py --data-dir /mnt/user/datasets/cityscapes_dataset --model-name unet --save-path /mnt/user/checkpoints/cityscapes_unet.pth --epochs 10 --batch-size 32 --disable-affine-transform`

## Deeplabv3_Resnet50 architecture
`python train.py --data-dir /mnt/user/datasets/cityscapes_dataset --model-name deeplabv3_resnet50 --save-path /mnt/user/checkpoints/cityscapes_deeplabv3_resnet50.pth --epochs 10 --batch-size 32 --disable-affine-transform`

## FCN_Resnet50 architecture
`python train.py --data-dir /mnt/user/datasets/cityscapes_dataset --model-name fcn_resnet50 --save-path /mnt/user/checkpoints/cityscapes_fcn_resnet50.pth --epochs 10 --batch-size 32 --disable-affine-transform`


# Brain MRI dataset trainings

## Unet architecture
`python train.py --data-dir /mnt/user/datasets/brain_mri_dataset --model-name unet --save-path /mnt/user/checkpoints/brain_mri_unet.pth --epochs 10 --batch-size 16 --disable-affine-transform`

## Deeplabv3_Resnet50 architecture
`python train.py --data-dir /mnt/user/datasets/brain_mri_dataset --model-name deeplabv3_resnet50 --save-path /mnt/user/checkpoints/brain_mri_deeplabv3_resnet50.pth --epochs 10 --batch-size 16 --disable-affine-transform`

## FCN_Resnet50 architecture
`python train.py --data-dir /mnt/user/datasets/brain_mri_dataset --model-name fcn_resnet50 --save-path /mnt/user/checkpoints/brain_mri_fcn_resnet50.pth --epochs 10 --batch-size 16 --disable-affine-transform`
