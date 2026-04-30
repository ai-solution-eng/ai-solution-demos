# Ultralytics 🚀 AGPL-3.0 License - https://ultralytics.com/license
from ultralytics.utils.checks import check_requirements
import yaml

from utils.inference import vehicle_detection_image_file, vehicle_detection_video_file,license_number_video_visualize_file, license_number_image_infer, license_number_image_visualize, license_number_video_infer
from utils.model_info import model_info
from utils.file_request import upload_video, delete_video
from utils.logging import logger

import torch
torch.classes.__path__ = [] # To resolve -> Tried to instantiate class ‘__path__._path’, but it does not exist! Ensure that it is registered via torch::class_

class Inference:
    """
    A class to perform object detection, image classification, image segmentation and pose estimation inference.

    This class provides functionalities for loading models, configuring settings, uploading video files, and performing
    real-time inference using Streamlit and Ultralytics YOLO models.

    Attributes:
        st (module): Streamlit module for UI creation.
        source (str): Selected type of input source (image or video file).
        video_inference_ratio(int): Percentage of video frames to process for inference
        vehicle_conf (float): Confidence threshold for detection.
        license_conf (float): Confidence threshold for detection.
        iou (float): IoU threshold for non-maximum suppression.
        org_frame (Any): Container for the original frame to be displayed.
        ann_frame (Any): Container for the annotated frame to be displayed.
        vid_file (File_uploader): Return object of streamlit's File_uploader
        selected_ind (List[int]): List of selected class indices for detection.
        selected_classes (List[str]) : List of selected class name for detection.
        usecase (str): Use case of this app. Vehicle Detection or License Number Detection
        api_host (str): FastAPI host
        log_url (str): Logo Icon image path
        vehicle_detectors (YOLO) : YOLO model instance for vehicle detection
        license_detectors (YOLO) : YOLO model instance for license plate detection

    Methods:
        web_ui: Sets up the Streamlit web interface with custom HTML elements.
        usecase_sidebar: Configures the Streamlit sidebar for usecase settings.
        vehicle_sidebar: Configures the Streamlit sidebar for vehicle detction.
        license_sidebar: Configures the Streamlit sidebar for license number detection.
        source_upload: Handles video file uploads through the Streamlit interface.
        vehicle_configure: Configures the model classes for inference.
        license_configure: Configures the model classes for inference.
        get_model_info: get model information from FastAPI backend
        set_state: set state for steps of license number detection
        inference: Performs real-time object detection inference.

    Examples:
        >>> inf = Inference()
        >>> inf.inference()
    """

    def __init__(self,api_host,logo_url):
        """
        Initialize the Inference class, checking Streamlit requirements and setting up the model path.

        Args:
            **kwargs (Any): Additional keyword arguments for model configuration.
        """
        check_requirements("streamlit>=1.29.0")  # scope imports for faster ultralytics package load speeds
        import streamlit as st

        self.st = st  # Reference to the Streamlit module
        self.source = None  # Input source type selection (image or video file)
        self.video_inference_ratio = 0.2
        self.vehicle_conf = 0.25
        self.license_conf = 0.25
        self.iou = 0.45  # Intersection-over-Union (IoU) threshold for non-maximum suppression
        self.org_frame = None  # Container for the original frame display
        self.ann_frame = None  # Container for the annotated frame display
        self.vid_file = None
        self.selected_ind = []  # List of selected class indices for detection
        self.selected_classes = []
        self.usecase = None
        self.api_host = api_host
        self.logo_url = logo_url
        self.vehicle_detectors = None
        self.license_detectors = None
        
        if 'stage' not in self.st.session_state:
            self.st.session_state.stage = 0

    def web_ui(self):
        """Sets up the Streamlit web interface with custom HTML elements."""
        menu_style_cfg = """<style>MainMenu {visibility: hidden;}</style>"""  # Hide main menu style

        # Main title of streamlit application
        main_title_cfg = """<div><h1 style="color:#01A982; text-align:center; font-size:40px; margin-top:-50px;
        font-family: 'Archivo', sans-serif; margin-bottom:20px;">License Plate Number Detection Application</h1></div>"""

        # Set html page configuration and append custom HTML
        self.st.set_page_config(page_title="Licenses Number Detection App", layout="wide")
        self.st.markdown(menu_style_cfg, unsafe_allow_html=True)
        self.st.markdown(main_title_cfg, unsafe_allow_html=True)

    def usecase_sidebar(self):
        # Add LOGO
        self.st.sidebar.image(self.logo_url, width=250)

        self.st.sidebar.title("Use Case")  # Add elements to vertical setting menu
        self.usecase = self.st.sidebar.selectbox(
            "Select Use Case",
            ("Vehicle Detection", "License Number Detection"),
            on_change=self.set_state,args=[0]
        )

    def vehicle_sidebar(self):
        """Configure the Streamlit sidebar for model and inference settings."""
        self.st.sidebar.title("Configuration")  # Add elements to vertical setting menu
        self.source = self.st.sidebar.selectbox(
            "Data Type",
            ("Image", "Video"),
            on_change=self.set_state,args=[0]
        )  # Add source selection dropdown
        
        self.vehicle_conf = float(
            self.st.sidebar.slider("Confidence Threshold", 0.0, 1.0, self.vehicle_conf, 0.01,on_change=self.set_state,args=[0])
        )  # Slider for confidence
        # self.iou = float(self.st.sidebar.slider("IoU Threshold", 0.0, 1.0, self.iou, 0.01))  # Slider for NMS threshold

    def license_sidebar(self):
        """Configure the License Detection Usecase sidebar."""
        self.st.sidebar.title("Configuration")  # Add elements to vertical setting menu
        self.source = self.st.sidebar.selectbox(
            "Data Type",
            ("Image", "Video"),
            on_change=self.set_state,args=[0]
        )  # Add source selection dropdown
        
        self.vehicle_conf = float(
            self.st.sidebar.slider("vehicle Confidence Threshold", 0.0, 1.0, self.vehicle_conf, 0.01,on_change=self.set_state,args=[0])
        )
        self.license_conf = float(
            self.st.sidebar.slider("License Confidence Threshold", 0.0, 1.0, self.license_conf, 0.01,on_change=self.set_state,args=[0])
        )  # Slider for confidence
        # self.iou = float(self.st.sidebar.slider("IoU Threshold", 0.0, 1.0, self.iou, 0.01))  # Slider for NMS threshold

    def source_upload(self):
        """Handle video file uploads through the Streamlit interface."""
        col1, col2 = self.st.columns(2)  # Create two columns for displaying frames
        self.org_frame = col1.empty()  # Container for original frame
        self.ann_frame = col2.empty()  # Container for annotated frame

        if self.vid_file is None:
            if self.source == "Video":
                self.vid_file = self.st.sidebar.file_uploader("Upload Video File", type=["mp4", "mov", "avi", "mkv"],on_change=self.set_state,args=[0])
                if self.vid_file is not None:
                    self.org_frame.video(self.vid_file,muted=True)    
                    self.video_inference_ratio = float(self.st.sidebar.slider("Video length Ratio for Inference",0.0,1.0,self.video_inference_ratio,0.01,on_change=self.set_state,args=[0]))
                    
            elif self.source == "Image":
                self.vid_file = self.st.sidebar.file_uploader("Upload Image File", type=["jpeg", "png"],on_change=self.set_state,args=[0])
                if self.vid_file is not None:
                    self.org_frame.image(self.vid_file)

    def vehicle_configure(self):
        """Configure the model and load selected classes for inference."""
        available_models = [self.vehicle_detectors, self.license_detectors]
        selected_model = self.st.sidebar.selectbox("Model", available_models)

        vehicle_class_names = list(self.vehicle_detectors_names.values())  # Convert dictionary to list of class names
        license_class_names = list(self.license_detectors_names.values())  # Convert dictionary to list of class names
        
        if selected_model == available_models[0]:
            self.selected_classes = self.st.sidebar.multiselect("Classes", vehicle_class_names, default=[vehicle_class_names[i] for i in [2,3,5,7]])   
            self.selected_ind = [vehicle_class_names.index(option) for option in self.selected_classes]
        else:
            self.selected_classes = self.st.sidebar.multiselect("Classes", license_class_names, default=license_class_names[:3])
            self.selected_ind = [license_class_names.index(option) for option in self.selected_classes]
        
        logger.info(f"Selected Classes : {self.selected_ind}")
        if not isinstance(self.selected_ind, list):  # Ensure selected_options is a list
            self.selected_ind = list(self.selected_ind)

    def license_configure(self):
        """Get the model info for inference."""
        self.st.sidebar.text("Vehicle Detector")
        self.st.sidebar.code(self.vehicle_detectors)
        self.st.sidebar.text("License Plate Detector")
        self.st.sidebar.code(self.license_detectors)

    def get_model_info(self):
        models = model_info(api_host)
        self.vehicle_detectors = models["VEHICLE_MODEL"]
        self.license_detectors = models["LICENSE_MODEL"]
        self.vehicle_detectors_names = models["VEHICLE_MODEL_NAME"]
        self.license_detectors_names = models["LICENSE_MODEL_NAME"]

    def set_state(self,i):
        '''
        stage 0 : intial stage
        stage 1 : Do inference and Get the Inference results
        stage 2 : Visualize the outputs
        '''
        self.st.session_state.stage = i

    def inference(self):
        """Perform vehicle detection or license plate number inference on video or image."""
        self.get_model_info()
        self.web_ui()  # Initialize the web interface
        self.usecase_sidebar()
        if self.usecase == "Vehicle Detection" :
            self.vehicle_sidebar()  # Create the sidebar
            self.source_upload()  # Upload the video source
            self.vehicle_configure()  # Configure the app
            side_left, side_right = self.st.sidebar.columns(2)

            if side_left.button("Start",use_container_width=True):
                side_right.button("Clear",use_container_width=True)  # Button to stop the inference
                if self.source == "Image":
                    annotated_result = vehicle_detection_image_file(
                        self.api_host,
                        self.selected_classes,
                        self.vid_file,
                        self.vehicle_conf,
                        self.selected_ind
                    )
                    self.ann_frame.image(annotated_result)

                elif self.source == "Video":
                    # Upload Video
                    sess_id, video_path = upload_video(self.api_host,self.vid_file)
                    output_path = 'out_' + video_path
                    # Do inference
                    with self.st.spinner("Wait for Inferencing...", show_time=True):
                        annotated_result = vehicle_detection_video_file(
                                self.api_host,
                                video_path,
                                output_path,
                                self.selected_classes,
                                self.vehicle_conf,
                                self.selected_ind,
                                self.video_inference_ratio,
                        )
                    
                    self.ann_frame.video(annotated_result,autoplay=True)
                    message, success = delete_video(self.api_host,video_path,output_path)
                    logger.info(f"{message}")
    
                        
        elif self.usecase == 'License Number Detection':
            self.license_sidebar()
            self.source_upload()
            self.license_configure()
            side_left, side_right = self.st.sidebar.columns(2)
            side_left.button("Start",on_click=self.set_state, args=[1],use_container_width=True)
            side_right.button("Clear",on_click=self.set_state, args=[0],use_container_width=True)

            if self.st.session_state.stage == 0:
                self.st.session_state.detection_result = None
                logger.info(f"Stage 0 : {self.st.session_state}")
            
            if self.st.session_state.stage == 1:
                if self.source == "Image":
                    response_info_json = license_number_image_infer(self.api_host,self.vid_file,self.vehicle_conf,self.license_conf)

                    logger.info(response_info_json)
                    self.st.session_state.detection_result = response_info_json
                    self.st.dataframe(response_info_json)
                    self.st.button("Visualize",on_click=self.set_state, args=[2])

                elif self.source == "Video":
                    sess_id, video_path = upload_video(self.api_host,self.vid_file)
                    with self.st.spinner("Wait for Inferencing...", show_time=True):
                        results_list = license_number_video_infer(
                            self.api_host,
                            video_path,
                            self.vehicle_conf,
                            self.license_conf,
                            self.video_inference_ratio
                        )
              
                    self.st.session_state.detection_result = results_list
                    self.st.dataframe(self.st.session_state.detection_result)
                    self.st.button("Visualize",on_click=self.set_state, args=[2])

            if self.st.session_state.stage == 2:
                if self.source == "Image":
                    annotated_result = license_number_image_visualize(self.api_host,self.vid_file,self.st.session_state.detection_result,)

                    self.ann_frame.image(annotated_result)
                    self.st.dataframe(self.st.session_state.detection_result)
                    self.st.button("Visualize",on_click=self.set_state, args=[2])

                elif self.source == "Video":
                    sess_id, video_path = upload_video(self.api_host,self.vid_file)
                    output_path = 'out_' + video_path

                    with self.st.spinner("Wait for Visualizing...", show_time=True):
                        annotated_result = license_number_video_visualize_file(self.api_host,video_path,output_path,self.st.session_state.detection_result)
                
                    self.ann_frame.video(annotated_result,autoplay=True)
                    message, success = delete_video(self.api_host,video_path,output_path)
                    logger.info(f"{message}")
                    self.st.dataframe(self.st.session_state.detection_result)
                    self.st.button("Visualize",on_click=self.set_state, args=[2])

if __name__ == "__main__":
    with open('./config.yaml','r') as file:
        config = yaml.safe_load(file)
        api_host = config["api_host"]
        logo_url = config["logo_url"]

    inf = Inference(api_host,logo_url)
    inf.inference()