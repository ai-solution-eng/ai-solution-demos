import requests

def upload_video(api_host,vid_file):
    # Upload Video
    upload_url = "http://" + api_host + "/api/utils/video/upload"
    files = {
        'file': (vid_file.name, vid_file.getvalue(), 'video/mp4')
    }
    
    response = requests.post(upload_url,files=files)
    response_json = response.json()
    sess_id = response_json['session_id']
    video_path = response_json['video_path']
    return sess_id, video_path


def delete_video(api_host,video_path,output_path):
    # Delete Video
    url = "http://" + api_host + "/api/utils/video/delete"
    json = {
        'video_path': f'{video_path}',
        'output_path': f'{output_path}',
    }
    
    response = requests.delete(url,json=json)
    response_json = response.json()
    # print(response_json)
    return response_json["message"],response_json["success"]