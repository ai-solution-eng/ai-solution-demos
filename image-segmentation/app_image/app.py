import argparse
import os
import base64
import mlflow

import numpy as np
from PIL import Image
from app_utils import *
import streamlit as st


# adding the following row to avoid
# RuntimeError: Tried to instantiate class '__path__._path',
# but it does not exist! Ensure that it is registered via torch::class_
# ?
torch.classes.__path__ = []

top_bar_logo = "hpe_pri_wht_rev_rgb.png"
top_bar_color = "#00B188" 

file_ = open(top_bar_logo, "rb")
contents = file_.read()
data_url = base64.b64encode(contents).decode("utf-8")
file_.close()

parser = argparse.ArgumentParser()
parser.add_argument(
    "--datasets-path",
    type=str,
    default="/home/user/namespace/datasets", # replace this
    help="Path to the folder containing the different datasets",
)
parser.add_argument(
    "--checkpoints-path",
    type=str,
    default="/home/user/namespace/checkpoints", # replace this
    help="Path to the folder containing the models' checkpoints after local training",
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
parser.add_argument(
    "--tracking-uri",
    type=str,
    default="http://mlflow.mlflow.svc.cluster.local:5000",
    help="Tracking server uri for logging with MLFlow",
)
args = parser.parse_args()

secret_file_path = args.secret_file_path
mlflow_s3_endpoint_url = args.mlflow_s3_endpoint_url

datasets_path = args.datasets_path
checkpoints_path = args.checkpoints_path

os.environ["MLFLOW_S3_ENDPOINT_URL"] = args.mlflow_s3_endpoint_url
mlflow.set_tracking_uri(uri=args.tracking_uri)

st.set_page_config(
    layout="wide", page_title="Segmentation Demo App"
)

with open("static/style.css") as css:
    st.markdown(f"<style>{css.read()}</style>", unsafe_allow_html=True)

######

# CSS for formatting top bar
css_string = """
<style>
.top-bar {
    background-color: """
css_string += top_bar_color+";"
css_string += """
    padding: 15px;
    color: white;
    margin-top: -70px;
}
</style>
"""

st.markdown(css_string, unsafe_allow_html=True)

# Create top bar
st.markdown(
    f"""
    <div class="top-bar">
        <img src="data:image/png;base64,{data_url}" alt="Logo" height="55">  
    </div>
    """,
    unsafe_allow_html=True,
)
######

st.header("Image Segmentation Displayer", divider="gray")

if "model" not in st.session_state:
    st.session_state['model'] = None
    st.session_state['get_model_pred'] = None


col1, col2, col3, col4 = st.columns(spec=[0.25,0.25,0.25,0.25])

col2.caption("Image")
col3.caption("Mask")
col4.caption("Model Prediction")

def update_dataset_features():
    st.session_state["dataset_with_path"] = os.path.join(datasets_path, st.session_state["dataset_choice"])
    st.session_state["categories"], st.session_state["json_data"] = get_categories_and_json(st.session_state["dataset_with_path"])
    st.session_state["N_categories"] = len(st.session_state["categories"])
    if st.session_state["json_data"]:
        st.session_state["color_map"] = get_color_map_from_dict(st.session_state["json_data"])
    else:
        st.session_state["color_map"] = None

    subset_list = os.listdir(st.session_state["dataset_with_path"])
    possible_subsets = ["train", "val", "test"]
    st.session_state["subset_list"] = list(set(subset_list) & set(possible_subsets))

dataset_list = os.listdir(datasets_path)
dataset_choice = col1.selectbox("Available datasets", dataset_list,
                                on_change=update_dataset_features, key="dataset_choice")

if "categories" not in st.session_state:
    update_dataset_features()

def update_subset_file_list():
    st.session_state["subset_with_path"] = os.path.join(datasets_path, st.session_state["dataset_choice"],
                                                        st.session_state["subset_choice"])
    st.session_state["subset_with_path_images_folder"] = os.path.join(st.session_state["subset_with_path"], "images")
    st.session_state["image_list"] = os.listdir(st.session_state["subset_with_path_images_folder"])

subset_choice = col1.selectbox("Subset", st.session_state["subset_list"],
                                on_change=update_subset_file_list, key="subset_choice")

if "images_list" not in st.session_state:
    update_subset_file_list()

selected_images = col1.multiselect("Which images would you like to display?", st.session_state["image_list"])

if "model" not in st.session_state:
    st.session_state['model'] = None
    st.session_state['get_model_pred'] = None

for im in selected_images:
    im_with_path = os.path.join(datasets_path, dataset_choice, subset_choice, "images", im)
    opened_image = Image.open(im_with_path)
    col2.image(opened_image, caption=im, width=256)

    mask, mask_file = get_mask_and_filename(im, st.session_state["subset_with_path"],
                     st.session_state["categories"], st.session_state["json_data"], st.session_state["color_map"])
    col3.image(mask, caption=mask_file, width=256)
    
    if st.session_state['model']:
        model_output = get_model_pred_on_img(st.session_state['model'], st.session_state['get_model_pred'], opened_image)
        if not st.session_state["json_data"]: # means single channel segmentation
            predicted_mask = model_output[0][0]
        else:
            predicted_mask = st.session_state["color_map"][np.argmax(model_output[0], axis=0)]
        col4.image(predicted_mask, caption=mask_file, width=256)

def load_model():

    if st.session_state['local_mlflow_pytorch_choice'] == "Default PyTorch model":
        st.session_state['model'], st.session_state['get_model_pred'] = get_and_adapt_pretrained_model(st.session_state['architecture_choice'], st.session_state['N_categories'])
        col1.caption(f":green[Successfully loaded default PyTorch {architecture_choice} model]")
        col1.caption(f":red[Warning: This model has not been fine-tuned on selected dataset!]")
        return 0
    
    if st.session_state['local_mlflow_pytorch_choice'] == "Local checkpoint":
        load_from_mlflow = False
        st.session_state['complete_path_to_model'] = os.path.join(checkpoints_path, st.session_state['model_choice'])

    if st.session_state['local_mlflow_pytorch_choice'] == "MLflow model":
        load_from_mlflow = True
        mlflow_model_choice = st.session_state['model_choice'] 
        mlflow_version_choice = st.session_state['model_version_choice'] 
        st.session_state['complete_path_to_model'] = f"models:/{mlflow_model_choice}/{mlflow_version_choice}"
    
    try:
        st.session_state['model'], st.session_state['get_model_pred'] = load_model_checkpoint(st.session_state['architecture_choice'], st.session_state['complete_path_to_model'], st.session_state['N_categories'], mlf=load_from_mlflow)
        if st.session_state['local_mlflow_pytorch_choice'] == "MLflow model":
            col1.caption(f":green[Successfully loaded {mlflow_model_choice} model version {mlflow_version_choice} from MLflow!]")
        if st.session_state['local_mlflow_pytorch_choice'] == "Local checkpoint":
            architecture = st.session_state['architecture_choice']
            checkpoint_file = st.session_state['model_choice']
            col1.caption(f":green[Successfully loaded {architecture} model from {checkpoint_file} from checkpoint file!]")
    except Exception as e:
        col1.caption(f":red[Failed to load model]")
        col1.caption(f":red[{str(e)}]")
        col1.caption(f":red[Are you sure this model uses is based on your selected model architecture: {st.session_state['architecture_choice']}? For the same dataset?]")

model_list = ["None", "unet", "fcn_resnet50", "deeplabv3_resnet50"]
architecture_choice = col1.selectbox("Segmentation model architecture", model_list, key="architecture_choice")

if architecture_choice != "None":
    local_mlflow_pytorch = ["Default PyTorch model", "MLflow model", "Local checkpoint"]
    local_mlflow_pytorch_choice = col1.selectbox("Where do you want to load your model from?", local_mlflow_pytorch, key="local_mlflow_pytorch_choice")

    display_load_button = True

    if local_mlflow_pytorch_choice == "MLflow model":
        refresh_token(args.secret_file_path)
        client = mlflow.MlflowClient()
        mlflow_registered_models = client.search_registered_models()
        model_options = [model.name for model in mlflow_registered_models]
        model_selectbox_description = "MLflow registered models"
    elif local_mlflow_pytorch_choice == "Local checkpoint":
        model_options = os.listdir(checkpoints_path)
        model_selectbox_description = "Local checkpoints"
    
    if local_mlflow_pytorch_choice in ["MLflow model", "Local checkpoint"]:
        no_checkpoints = len(model_options) == 0
        if no_checkpoints:
            display_load_button = False
            col1.caption(f":red[No model or checkpoint found]")
        else:
            model_choice = col1.selectbox(model_selectbox_description, model_options, key="model_choice")

    if local_mlflow_pytorch_choice == "MLflow model" and not no_checkpoints:
        refresh_token(args.secret_file_path)
        model_versions_data = client.search_model_versions(filter_string =f"name='{model_choice}'", order_by=["version_number DESC"])
        versions = [mvd.version for mvd in model_versions_data]
        model_version_choice = col1.selectbox("MLflow model versions", versions, key="model_version_choice")
        chosen_model_version = model_versions_data[[i for i, n in enumerate(model_versions_data) if n.version == model_version_choice][0]]
        if chosen_model_version.description == '':
            col1.caption("No description is available for this model version")
        else:
            col1.caption(chosen_model_version.description)
    
    if display_load_button:
        model_load_button = st.button("Load model", on_click=load_model)















