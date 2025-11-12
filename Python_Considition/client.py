import json
import requests
from typing import Any, Dict, Optional


class ConsiditionClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0, verify_tls: bool = False):
        """
        base_url: e.g. "http://localhost:8080" for Docker, or cloud URL if provided by organizers
        api_key:  your team key
        timeout:  request timeout seconds
        verify_tls: verify HTTPS certificates (False is convenient for local dev)
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {"x-api-key": self.api_key}
        self.timeout = timeout
        self.verify = verify_tls

    def post_game(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/game", json=data)

    def get_map(self, map_name: str) -> Dict[str, Any]:
        return self._request("GET", "/map", params={"mapName": map_name})

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        try:
            resp = requests.request(
                method,
                url,
                headers=self.headers,
                timeout=self.timeout,
                verify=self.verify,
                **kwargs,
            )
            # Raise for HTTP errors
            resp.raise_for_status()
            # Parse JSON (and be resilient if server returns text)
            try:
                return resp.json()
            except json.JSONDecodeError:
                txt = resp.text or ""
                print("Non-JSON response body:", txt[:500])
                return {}
        except requests.HTTPError as e:
            # Print server error body to discover expected schema / validation messages
            body = ""
            try:
                body = e.response.text
            except Exception:
                pass
            print(f"[HTTP ERROR] {e}\nBody (first 1000 chars):\n{body[:1000]}")
            raise
        except requests.RequestException as e:
            print(f"[REQUEST ERROR] {e}")
            raise
