# import json
# from typing import Any, Dict

# import requests


# class ConsiditionClient:
#     """
#     Minimal HTTP client for the Considition game engine.
#     Includes a 404 fallback that toggles /api prefix automatically.
#     """

#     def __init__(self, base_url: str, api_key: str, timeout: float = 30.0, verify_tls: bool = False):
#         """
#         base_url: e.g. "http://localhost:8080" or "http://localhost:8080/api"
#         api_key : your team API key (x-api-key header)
#         timeout: request timeout (seconds)
#         verify_tls: set True for proper HTTPS verification (False convenient for local dev)
#         """
#         self.base_url = base_url.rstrip("/")
#         self.api_key = api_key
#         self.headers = {"x-api-key": self.api_key}
#         self.timeout = timeout
#         self.verify = verify_tls

#     # ---------- public API ----------

#     def post_game(self, data: Dict[str, Any]) -> Dict[str, Any]:
#         return self._request_with_fallback("POST", "/game", json=data)

#     def get_map(self, map_name: str) -> Dict[str, Any]:
#         return self._request_with_fallback("GET", "/map", params={"mapName": map_name})

#     # ---------- internals ----------

#     def _request_with_fallback(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
#         """
#         Try base URL as-is. If we get a 404, retry once with toggled /api prefix.
#         """
#         try:
#             return self._request(method, self.base_url, endpoint, **kwargs)
#         except requests.HTTPError as e:
#             if e.response is not None and e.response.status_code == 404:
#                 alt_base = self._toggle_api_prefix(self.base_url)
#                 if alt_base != self.base_url:
#                     try:
#                         resp = self._request(
#                             method, alt_base, endpoint, **kwargs)
#                         # If alt worked, stick to it for future calls.
#                         self.base_url = alt_base
#                         return resp
#                     except requests.HTTPError:
#                         pass
#             raise

#     def _request(self, method: str, base: str, endpoint: str, **kwargs) -> Dict[str, Any]:
#         url = f"{base}{endpoint}"
#         resp = requests.request(
#             method,
#             url,
#             headers=self.headers,
#             timeout=self.timeout,
#             verify=self.verify,
#             **kwargs,
#         )
#         resp.raise_for_status()
#         try:
#             return resp.json()
#         except json.JSONDecodeError:
#             txt = resp.text or ""
#             print("Non-JSON response body (first 500 chars):", txt[:500])
#             return {}

#     @staticmethod
#     def _toggle_api_prefix(base: str) -> str:
#         return base[:-4] if base.endswith("/api") else base + "/api"


import requests


class ConsiditionClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self.headers = {"x-api-key": self.api_key}

    def post_game(self, data: object):
        return self.request("POST", "/api/game", json=data)

    def get_map(self, map_name: str):
        return self.request("GET", "/api/map", params={"mapName": map_name})

    def request(self, method: str, endpoint: str, **kwargs):
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(
                method, url, headers=self.headers, verify=False, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error making request to {url}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Error response status: {e.response.status_code}")
                print(f"Error response body: {e.response.text}")
            raise
