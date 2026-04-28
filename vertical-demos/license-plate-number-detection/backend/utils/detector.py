from PIL import Image
import io
import numpy as np
import cv2 as cv
import easyocr
from ultralytics import YOLO
from utils.license_format import license_complies_format,format_license
from utils.logging import logger
import yaml

with open("./config.yaml", "r") as f:
    config = yaml.safe_load(f)

MODEL_ENDPOINT = config["detectors"]["server_url"]
VEHICLE_MODEL_NAME = config["detectors"]["vehicle_detector"]
LICENSE_MODEL_NAME = config["detectors"]["license_detector"]

vehicles_id = [2,3,5,7]

def get_model_names():
    vehicle_detector = YOLO(MODEL_ENDPOINT + '/' + VEHICLE_MODEL_NAME, task='detect')
    license_detector = YOLO(MODEL_ENDPOINT + '/' + LICENSE_MODEL_NAME, task='detect')

    vehicle_detector_names = vehicle_detector.names
    license_detector_names = license_detector.names
    return vehicle_detector_names, license_detector_names

def vehicle_detect_bytes(image,conf: float = 0.25,classes: list = vehicles_id):
    """
    Do Predict, Return the result as Byte format
    
    Args:
    image (cv2 image) : Input from User
    conf (int) : Confidence Threshold
    classes ( list ) : Detection Target List

    Returns:
    bytes : BytesIO object that contains the image in JPEG format with quality 95
    """
    vehicle_detector = YOLO(MODEL_ENDPOINT + '/' + VEHICLE_MODEL_NAME, task='detect')
    results = vehicle_detector(image,conf=conf,classes=classes)
    prediction = results[0].plot()
    return_bytes = get_bytes_from_prediction(prediction,quality=65)
    return return_bytes

def license_detect_bytes(image,conf: float = 0.25):
    """
    Do Predict, Return the result as Byte format
    
    Args:
    image (cv2 image) : Input from User
    conf (int) : Confidence Threshold
    
    Returns:
    bytes : BytesIO object that contains the image in JPEG format with quality 95
    """
    license_detector = YOLO(MODEL_ENDPOINT + '/' + LICENSE_MODEL_NAME, task='detect')
    results = license_detector(image,conf=conf)
    prediction = results[0].plot()
    return_bytes = get_bytes_from_prediction(prediction,quality=65)
    return return_bytes

def license_detect_number(image):
    """
    Do OCR by EasyOCR
    
    Args:
    image (Fastapi UploadFile) : Input from User
    
    Return:
    dict : {
        lic_text : "detected License texts",
        lic_score : "detection score"
    }
    """
    plate_reader = easyocr.Reader(['en'],gpu=False)
    file = image.file.read()
    ocr_detections = plate_reader.readtext(file)
    lic_text, lic_score = reformat_license_number(ocr_detections)
    return lic_text, lic_score

def get_bytes_from_prediction(prediction: np.ndarray,quality: int) -> bytes:
    """
    Convert YOLO's prediction to Bytes
    
    Args:
    prediction (np.ndarray): A YOLO's prediction
    
    Returns:
    bytes : BytesIO object that contains the image in JPEG format with quality 95
    """
    im_rgb = prediction[...,::-1]
    return_image = Image.fromarray(im_rgb)
    return_bytes = io.BytesIO()
    return_image.save(return_bytes, format='JPEG', quality=quality)
    return_bytes.seek(0)
    return return_bytes

def reformat_license_number(detections):
    """
    check format compliance and formatting the results
    
    Args:
    detections (list): Results of EasyOCR
    
    Returns:
    tuple : { text , score }
    """
    for detection in detections:
        bbox, text, score = detection
        text = text.upper().replace(' ', '')
        # return text, score
        # verify that text is conform to a standard license plate
        if license_complies_format(text):
            # bring text into the default license plate format
            return format_license(text), score

    return None, None

def crop_vehicle_license_then_read(vehicle_tracker,license_detector,plate_reader,input_image,vehicle_conf: float = 0.25,license_conf: float = 0.25,frame_number: int = 0):
    """
    Get the image or frame of video and return the results as a form of list of dicts
    
    Args:
    input_image (cv2 image): Input for Detection
    vehicle_conf: Confidence Threshold for vehicle
    license_conf: Confidence Threshold for license plate
    frame_number: Frame number of input_image

    Returns:
    list of Dicts : 
    [
        {
            'frame_number': frame_number,
            'track_id': track_id,
            'vehicle_bbox': [x1, y1, x2, y2],
            'vehicle_bbox_score': score,
            'lp_bbox': [plate_x1 , plate_y1, plate_x2, plate_y2],
            'lp_bbox_score': plate_score,
            'lp_number': lic_text,Add commentMore actions
            'lp_text_score': lic_score
        },
    ]
    """
    frame_results = [] # change return value type as list

    vehicle_results = vehicle_tracker.track(input_image, persist=True,conf=vehicle_conf,classes=vehicles_id)[0]
    for vehicle_result in vehicle_results.boxes.data.tolist():
        x1, y1, x2, y2, track_id, score, class_id = vehicle_result
        
        vehicle_bounding_boxes = []
        vehicle_bounding_boxes.append([x1, y1, x2, y2, track_id, score])
        for bbox in vehicle_bounding_boxes: 
            roi = input_image[int(y1):int(y2), int(x1):int(x2)] # crop the vehicle
            license_plates = license_detector(roi,conf=license_conf)[0]
            for license_plate in license_plates.boxes.data.tolist():
                # print(track_id)
                plate_x1, plate_y1, plate_x2, plate_y2, plate_score, _ = license_plate

                # crop license plate
                plate = roi[int(plate_y1):int(plate_y2), int(plate_x1):int(plate_x2)]
                # de-colorize
                plate_gray = cv.cvtColor(plate, cv.COLOR_BGR2GRAY)
                # posterize
                _, plate_treshold = cv.threshold(plate_gray, 64, 255, cv.THRESH_BINARY_INV)

                ### try rgb and gray and get the best
                rgb_detections = plate_reader.readtext(plate)
                gray_detections = plate_reader.readtext(plate_treshold)
                rgb_text, rgb_score = reformat_license_number(rgb_detections)
                gray_text, gray_score = reformat_license_number(gray_detections)
                
                candidates = [(rgb_text,rgb_score),(gray_text,gray_score)]
                valid_candidates = [(text,score) for text,score in candidates if score is not None]
                lic_text, lic_score = max(valid_candidates,default=(None,None))
               
                frame_results.append({
                    "frame_number": frame_number,
                    "track_id": track_id,
                    "vehicle_bbox": [x1, y1, x2, y2],
                    "vehicle_bbox_score": score,
                    "lp_bbox": [plate_x1 , plate_y1, plate_x2, plate_y2],
                    "lp_bbox_score": plate_score,
                    "lp_number": lic_text,
                    "lp_text_score": lic_score
                })
    
    return frame_results


# def reset_tracker(vehicle_tracker):
#     if len(vehicle_tracker.predictor.trackers) > 0:
#         vehicle_tracker.predictor.trackers[0].reset()
#         # print(vehicle_tracker.predictor.trackers[0])
#         return True
#     else:
#         logger.info('tracker does not exists')
