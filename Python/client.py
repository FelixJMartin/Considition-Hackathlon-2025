import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()
BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("API_KEY")

# If you must ignore TLS (JS sets NODE_TLS_REJECT_UNAUTHORIZED="0"), set verify=False below.
SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "x-api-key": API_KEY
})
TIMEOUT = 15


def _request(method: str, path: str, **kwargs):
    url = f"{BASE_URL}{path}"
    for attempt in range(4):
        try:
            r = SESSION.request(method, url, timeout=TIMEOUT,
                                **kwargs)  # , verify=False
            if r.status_code in (429, 502, 503, 504):
                time.sleep(0.4 * (2**attempt))
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            # Return None like the JS starter instead of exploding
            return None
        except requests.RequestException:
            return None
    return None


def get_map(map_name: str):
    # GET /map?mapName=...
    return _request("GET", "/map", params={"mapName": str(map_name)})


def post_game(input_dto: dict, save_game: bool = False):
    # POST /game?saveGame=...
    return _request("POST", "/game", params={"saveGame": str(bool(save_game)).lower()},
                    json=input_dto, headers={"Content-Type": "application/json"})
