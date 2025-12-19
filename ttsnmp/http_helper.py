import requests

BASE_URL = "http://localhost:8001/"

def http_get(url):
    try:
        resp = requests.get(url)

        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"Error : {resp.status_code}")
            return None
    except Exception as e:
        print(e)


def pdu_info() -> tuple:
    url = BASE_URL + "settings/pdu-info/"
    ret = http_get(url)
    if ret == None:
        return None, None
    return (6, ret['outlet_count'])

def input_data(line_id: int) -> tuple:
    url = BASE_URL + f"inputs/{line_id}/data"
    ret = http_get(url)
    if ret == None:
        return None, None
    return ret['current'], ret['active_power']

def output_data(line_id: int) -> dict:
    url = BASE_URL + f"outputs/{line_id}/data"
    ret = http_get(url)
    if ret == None:
        return None, None
    return ret['current'], ret['active_power']

def get_license() -> str:
    url = BASE_URL + "settings/license";
    ret = http_get(url)
    if ret == None:
        return "A1"
    return ret['type_id']
