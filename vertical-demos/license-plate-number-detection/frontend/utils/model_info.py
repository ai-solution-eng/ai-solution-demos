import requests

def model_info(api_host):
    url = "http://" + api_host + "/api/utils/models/info"
    response = requests.get(url)
    response_json = response.json()
    return response_json