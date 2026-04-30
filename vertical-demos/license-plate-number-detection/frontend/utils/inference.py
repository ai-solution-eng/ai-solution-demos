import requests
import json
import io
from utils.logging import logger  


def vehicle_detection_image_file(api_host, selected_classes, vid_file,vehicle_conf,selected_ind):
    if selected_classes[0] == 'License_Plate': # license plate detection usecase
        url = "http://" + api_host + "/api/image/plates/detect/annotatedImage"
    else: # vehicle detction usecase
        url = "http://" + api_host + "/api/image/vehicles/detect/annotatedImage"
    # Do inference
    files = {
            'image': (vid_file.name, vid_file.getvalue(), 'image/jpeg'),
        }
    data = {
        'conf': f'{vehicle_conf}'
    }
    params = [('classes', str(n)) for n in selected_ind]

    response = requests.post(url,files=files,data=data,params=params,stream=True)
    annotated_result = io.BytesIO(response.content)
    return annotated_result


def vehicle_detection_video_file(api_host,video_path,output_path,selected_classes,vehicle_conf,selected_ind,video_inference_ratio):
    if selected_classes[0] == 'License_Plate':
        url = f"http://{api_host}/api/video/plates/detect/annotatedVideo"
    else:
        url = f"http://{api_host}/api/video/vehicles/detect/annotatedVideo"
    
    payload = {
        'video_path' : f'{video_path}',
        'output_path' : f'{output_path}',
        'conf': f'{vehicle_conf}',
        'ratio': f'{video_inference_ratio}',
    }
    
    params = [('classes', str(n)) for n in selected_ind]
    response = requests.post(url,json=payload,params=params,stream=True)
    if response.status_code == 200:
        logger.info("Video download Done!!")
        annotated_result = io.BytesIO(response.content)
        return annotated_result
    else:
        return


def license_number_image_infer(api_host,vid_file,vehicle_conf,license_conf):
    files = {
        'image': (vid_file.name, vid_file.getvalue(), 'image/jpeg'),
    }
    data = {
        'vehicle_conf': f'{vehicle_conf}',
        'license_conf': f'{license_conf}',
    }
    
    url = "http://" + api_host + "/api/image/plate_number/crop/detect/info"
    response_info = requests.post(url,files=files,data=data)
    response_info_json = response_info.json()
    return response_info_json

def license_number_image_visualize(api_host,vid_file,detection_result):
    files = {
        'image': (vid_file.name, vid_file.getvalue(), 'image/jpeg'),
    }
    data = {
        'item_json': json.dumps(detection_result),
    }

    url = 'http://' + api_host + '/api/utils/draw/annotatedImage'
    response = requests.post(url,files=files,data=data,stream=True)
    annotated_result = io.BytesIO(response.content)
    return annotated_result


def license_number_video_infer(api_host,video_path,vehicle_conf,license_conf,video_inference_ratio):
    url = f"http://{api_host}/api/video/plate_number/crop/detect/info"

    payload = {
        "video_path": f"{video_path}",
        "vehicle_conf": f"{vehicle_conf}",
        "license_conf": f"{license_conf}",
        "ratio": f"{video_inference_ratio}"
    }
    
    response_info = requests.post(url,json=payload)
    response_info_json = response_info.json()
    return response_info_json

def license_number_video_visualize_file(api_host,video_path,output_path,detection_result):
    url = f"http://{api_host}/api/utils/draw/annotatedVideo"
    
    payload = {
        'video_path' : video_path,
        'output_path' : output_path,
        'detection_result': detection_result,
    }
    
    response = requests.post(url,json=payload,stream=True)
    if response.status_code == 200:
        logger.info("Video download Done!!")
        annotated_result = io.BytesIO(response.content)
        return annotated_result
    else:
        return
