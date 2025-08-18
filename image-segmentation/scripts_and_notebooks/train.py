from utils import *
import argparse

parser = argparse.ArgumentParser()
parser.add_argument(
    "--data-dir", 
    type=str,
    default="data",
    help="Path to dataset directory"
)
parser.add_argument(
    "--model-name",
    type=str,
    default="unet",
    help="Name of pretrained Pytorch model",
)
parser.add_argument(
    "--save-path",
    type=str,
    default="",
    help="Filename for fine-tuned model weights (e.g. checkpoint/model.pth). If not set, model weights will not be saved.",
)
parser.add_argument(
    "--batch-size",
    type=int,
    default=16,
    help="Batch size",
)
parser.add_argument(
    "--learning-rate",
    type=float,
    default=0.01,
    help="Learning rate",
)
parser.add_argument(
    "--epochs",
    default=5,
    type=int,
    help="Number of epochs",
)
parser.add_argument(
    "--max-rotation",
    type=int,
    default=15,
    help="Maximum rotation in degrees applied during training for data augmentation",
)
parser.add_argument(
    "--max-translation",
    type=float,
    default=0.1,
    help="Maximum translation ratio applied during training for data augmentation",
)
parser.add_argument(
    "--min-scale",
    type=float,
    default=0.8,
    help="Smallest image ratio kept during training for data augmentation",
)
parser.add_argument(
    "--max-scale",
    type=float,
    default=1.2,
    help="Largest image ratio kept during training for data augmentation",
)
parser.add_argument(
    "--disable-data-augmentation",
    action="store_true",
    help="Set flag to disable data augmentation during training"
)
parser.add_argument(
    "--disable-affine-transform",
    action="store_true",
    help="Set flag to remove affine transformation from data augmentation during training"
)
parser.add_argument(
    "--disable-horizontal-flip",
    action="store_true",
    help="Set flag to disable data augmentation during training"
)
parser.add_argument(
    "--disable-mlflow-tracking",
    action="store_true",
    help="Set flag not to track the training with MLFlow"
)
parser.add_argument(
    "--tracking-uri",
    type=str,
    default="http://mlflow.mlflow.svc.cluster.local:5000",
    help="Tracking server uri for logging with MLFlow",
)
parser.add_argument(
    "--exp-name",
    type=str,
    default="Segmentation Demo Experiment",
    help="MLFlow experiment name",
)
parser.add_argument(
    "--run-name",
    type=str,
    default=None,
    help="MLFlow run name",
)
parser.add_argument(
    "--secret-file-path",
    type=str,
    default="/etc/secrets/ezua/.auth_token",
    help="Path to the file containing auth token for MLFlow",
)
parser.add_argument(
    "--mlflow-s3-endpoint-url",
    type=str,
    default="http://local-s3-service.ezdata-system.svc.cluster.local:30000",
    help="MLflow s3 endpoint url to save model",
)

args = parser.parse_args()

data_dir = args.data_dir

learning_rate = args.learning_rate
batch_size = args.batch_size
epochs = args.epochs

params = {
    "Dataset": data_dir,
    "Learning Rate": learning_rate,
    "Batch Size": batch_size,
    "Epochs": epochs
}

enable_data_augmentation = True
enable_affine_transform = True
enable_horizontal_flip = True

if args.disable_data_augmentation:
    enable_data_augmentation = False
    params["Data Augmentation"] = "No"
else:
    params["Data Augmentation"] = "Yes"

if args.disable_affine_transform:
    enable_affine_transform = False
    if params["Data Augmentation"] == "Yes":
        params["Affine Transformation"] = "No"
elif params["Data Augmentation"] == "Yes":
    params["Affine Transformation"] = "Yes"

if args.disable_horizontal_flip:
    enable_horizontal_flip = False
    if params["Data Augmentation"] == "Yes":
        params["Horizontal Flip"] = "No"
elif params["Data Augmentation"] == "Yes":
    params["Horizontal Flip"] = "Yes"

max_rotation = args.max_rotation
max_translation = args.max_translation
min_scale = args.min_scale
max_scale = args.max_scale

if params["Data Augmentation"] == "Yes":
    params["Max Rotation"] = max_rotation
    params["Max Translation"] = max_translation
    params["Min Scale"] = min_scale
    params["Max Scale"] = max_scale

model_name = args.model_name
params["Model Architecture"] = model_name

save_path = args.save_path

args_dict = vars(args)
print("Passed args:")
for key in args_dict:
    print(key + ": " + str(args_dict[key]))

categories = get_categories_list(data_dir)
N_categories = len(categories)

tags = {"Dataset": data_dir.split("/")[-1], "Model": model_name, "Categories": N_categories}

if N_categories == 1:
    print(f"Single channel segmentation problem: {categories[0]}")
else:
    print(f"This segmentation problem has {N_categories} distinct categorie(s):")
    for categorie in categories:
        print(f"  - {categorie}")

print("Building data loaders from data directory...")
loaders = get_data_loaders(data_dir,
                           enable_data_augmentation=enable_data_augmentation,
                           enable_affine_transform=enable_affine_transform,
                           enable_horizontal_flip=enable_horizontal_flip,
                           batch_size=batch_size,
                           max_rotation=max_rotation,
                           max_translation=max_translation,
                           min_scale=min_scale,
                           max_scale=max_scale)

# test loader may not exist
train_loader = loaders[0]
val_loader = loaders[1]
print("Data loaders ready")

print("Loading and adapting pretrained model...")
model, get_model_pred = get_and_adapt_pretrained_model(model_name, N_categories)
print("Model loaded")

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
model = model.to(device)
print(f"Model loaded on device: {device}")

optimizer = optim.Adam(model.parameters(), lr=learning_rate)
scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=3)
criterion = nn.BCELoss(reduction='mean')



if not args.disable_mlflow_tracking:

    refresh_token(args.secret_file_path)

    os.environ["MLFLOW_S3_ENDPOINT_URL"] = args.mlflow_s3_endpoint_url
    mlflow.set_tracking_uri(uri=args.tracking_uri)
    mlflow.set_experiment(args.exp_name)
    
    with mlflow.start_run(run_name=args.run_name):
        mlflow.log_params(params)

        for tag in tags:
            mlflow.set_tag(tag, tags[tag])

        print("Start model training...")
        best_epoch_metrics = train(model, get_model_pred, optimizer, criterion, scheduler, train_loader, val_loader,
                        num_epochs=epochs, device=device, save_path=save_path, track_with_mlflow=True, secret_file_path=args.secret_file_path)
        print("Model training done")

        mlflow_model_name = tags["Model"] + "_" + tags["Dataset"]
        print("Logging model to MLflow...")
        mlflow.pytorch.log_model(model, "model", registered_model_name=mlflow_model_name)
        print("Model successfully logged to MLflow!")

        print("Adding model description...")
        client = mlflow.MlflowClient()
        dataset_name = tags["Dataset"]
        client.update_registered_model(name=mlflow_model_name, description=f"{model_name} model trained on {dataset_name} data")
        mlflow_data = client.search_model_versions(filter_string =f"name='{mlflow_model_name}'", order_by=["version_number DESC"])
        current_model_version = mlflow_data[0].version
        
        display_string = ""
        for metric in best_epoch_metrics:
            rounded_metric_value = "{:.3f}".format(best_epoch_metrics[metric])
            display_string += f"{metric}: {rounded_metric_value}\n"

        description_string = f"Metric values from best epoch are: \n\n{display_string}"
        
        client.update_model_version(name=mlflow_model_name, version=current_model_version, description=description_string)
        print("Done!")
            
    print("Experiment successfully tracked by MLFlow")
    
else:
    print("Start model training...")
    best_epoch_metrics = train(model, get_model_pred, optimizer, criterion, scheduler, train_loader, val_loader, num_epochs=epochs, device=device, save_path=save_path)
    print("Model training done")
