import json
import logging
import time
from typing import Any, Dict, Optional
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError, HTTPError
from billing_engine.models import XuiServer

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024 ** 3


class XuiAPIException(Exception):
    pass


class XuiAPIClient:
    """
    3x-ui v3.3.1+ API client using Bearer token auth.
    Token: Panel Settings -> Security -> API Token

    IMPORTANT — unit contract
    -------------------------
    The 3x-ui API field is named `totalGB` but it stores raw BYTES.
    - add_client / update_client: pass total_bytes (int, raw bytes). 0 = unlimited.
    - expiryTime: Unix milliseconds. 0 = never.
    When reading back from get_inbounds, cli['totalGB'] is also raw bytes.
    """

    def __init__(self, server: XuiServer):
        self.server = server
        protocol = "https" if server.use_ssl else "http"
        host = server.get_host()
        self.base_path = server.get_base_path()
        self.base_url = f"{protocol}://{host}:{server.api_port}"
        self.verify_ssl = False
        self.max_retries = 3
        self.backoff_factor = 2

    def _url(self, path: str) -> str:
        return f"{self.base_url}{self.base_path}{path.lstrip('/')}"

    def _headers(self) -> Dict[str, str]:
        if not self.server.api_token:
            raise XuiAPIException(
                f"No API token set for server '{self.server.name}'. "
                "Go to 3x-ui Panel Settings -> Security -> API Token."
            )
        return {
            "Authorization": f"Bearer {self.server.api_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._url(endpoint)
        for attempt in range(1, self.max_retries + 1):
            sleep_duration = self.backoff_factor ** attempt
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    json=json_data,
                    headers=self._headers(),
                    timeout=10.0,
                    verify=self.verify_ssl,
                )
                if response.status_code == 401:
                    raise XuiAPIException(
                        f"API token rejected (401) for server '{self.server.name}'. "
                        "Regenerate the token in Panel Settings -> Security -> API Token."
                    )
                if response.status_code == 403:
                    raise XuiAPIException(
                        f"Access forbidden (403) for server '{self.server.name}'. "
                        "Check the API token has the required permissions."
                    )
                response.raise_for_status()
                return response.json()
            except (Timeout, ConnectionError) as e:
                if attempt == self.max_retries:
                    raise XuiAPIException(f"Failed after {self.max_retries} attempts: {e}") from e
                logger.warning("Retry %d/%d for %s in %ds", attempt, self.max_retries, url, sleep_duration)
                time.sleep(sleep_duration)
            except HTTPError as e:
                status_code = e.response.status_code if e.response is not None else 500
                if status_code >= 500 and attempt < self.max_retries:
                    time.sleep(sleep_duration)
                    continue
                raise XuiAPIException(f"HTTP {status_code}: {e}") from e
            except XuiAPIException:
                raise
            except RequestException as e:
                raise XuiAPIException(f"Request failed: {e}") from e
        return {}

    def get_inbounds(self) -> Dict[str, Any]:
        return self._request("GET", "panel/api/inbounds/list")

    def get_client_traffic(self, email: str) -> Dict[str, Any]:
        return self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")

    def add_client(self, inbound_id: int, client_uuid: str, email: str,
                   total_bytes: int = 0, expiry_time_ms: int = 0) -> Dict[str, Any]:
        """
        total_bytes    : raw bytes cap (e.g. 2*1024**3 for 2 GB). 0 = unlimited.
        expiry_time_ms : Unix ms. 0 = never.
        """
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [{
                "id": client_uuid,
                "email": email,
                "totalGB": total_bytes,
                "expiryTime": expiry_time_ms,
                "enable": True,
            }]}),
        }
        return self._request("POST", "panel/api/inbounds/addClient", json_data=payload)

    def update_client(self, inbound_id: int, client_uuid: str, email: str,
                      total_bytes: int = 0, expiry_time_ms: int = 0,
                      enable: bool = True) -> Dict[str, Any]:
        """
        total_bytes    : raw bytes cap. 0 = unlimited.
        expiry_time_ms : Unix ms. 0 = never.
        """
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [{
                "id": client_uuid,
                "email": email,
                "totalGB": total_bytes,
                "expiryTime": expiry_time_ms,
                "enable": enable,
            }]}),
        }
        return self._request("POST", f"panel/api/inbounds/updateClient/{client_uuid}", json_data=payload)

    def delete_client(self, inbound_id: int, client_uuid: str) -> Dict[str, Any]:
        return self._request("POST", f"panel/api/inbounds/{inbound_id}/delClient/{client_uuid}")

    def reset_client_traffic(self, inbound_id: int, email: str) -> Dict[str, Any]:
        return self._request("POST", f"panel/api/inbounds/{inbound_id}/resetClientTraffic/{email}")

    def sync_existing_clients(self) -> list:
        result = self.get_inbounds()
        clients = []
        for inbound in result.get("obj", []):
            inbound_id = inbound.get("id")
            protocol = inbound.get("protocol", "").upper()
            try:
                settings_obj = json.loads(inbound.get("settings", "{}"))
            except (json.JSONDecodeError, TypeError):
                settings_obj = {}
            for client in settings_obj.get("clients", []):
                clients.append({
                    "inbound_id": inbound_id,
                    "protocol": protocol,
                    "email": client.get("email", ""),
                    "uuid": client.get("id", ""),
                    "enable": client.get("enable", True),
                })
        return clients
