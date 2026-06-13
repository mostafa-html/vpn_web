import json
import logging
import time
from typing import Any, Dict, Optional
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError, HTTPError
from billing_engine.models import XuiServer

logger = logging.getLogger(__name__)


class XuiAPIException(Exception):
    pass


class XuiAPIClient:
    """
    3x-ui v3.3.1+ API client using Bearer token auth.
    Token: Panel Settings -> Security -> API Token
    URL layout (example base_path=/secret/):
      API -> https://host:port/secret/panel/api/...
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

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------

    def get_inbounds(self) -> Dict[str, Any]:
        return self._request("GET", "panel/api/inbounds/list")

    def get_client_traffic(self, email: str) -> Dict[str, Any]:
        return self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")

    # ------------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------------

    def add_client(self, inbound_id: int, client_uuid: str, email: str,
                   total_gb: int = 0, expiry_time_ms: int = 0) -> Dict[str, Any]:
        """
        Add a new client to an inbound.
        total_gb      : traffic cap in BYTES (0 = unlimited)
        expiry_time_ms: expiry as Unix ms    (0 = never)
        """
        client_payload = {
            "id": client_uuid,
            "email": email,
            "totalGB": total_gb,
            "expiryTime": expiry_time_ms,
            "enable": True,
        }
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client_payload]}),
        }
        return self._request("POST", "panel/api/inbounds/addClient", json_data=payload)

    # ------------------------------------------------------------------
    # UPDATE
    # ------------------------------------------------------------------

    def update_client(self, inbound_id: int, client_uuid: str, email: str,
                      total_gb: int = 0, expiry_time_ms: int = 0,
                      enable: bool = True) -> Dict[str, Any]:
        """
        Update an existing client.
        total_gb      : traffic cap in BYTES (0 = unlimited)
        expiry_time_ms: expiry as Unix ms    (0 = never)
        enable        : whether the client is active
        """
        client_payload = {
            "id": client_uuid,
            "email": email,
            "totalGB": total_gb,
            "expiryTime": expiry_time_ms,
            "enable": enable,
        }
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client_payload]}),
        }
        return self._request(
            "POST",
            f"panel/api/inbounds/updateClient/{client_uuid}",
            json_data=payload,
        )

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------

    def delete_client(self, inbound_id: int, client_uuid: str) -> Dict[str, Any]:
        """Remove a client from an inbound entirely."""
        return self._request(
            "POST",
            f"panel/api/inbounds/{inbound_id}/delClient/{client_uuid}",
        )

    # ------------------------------------------------------------------
    # MISC
    # ------------------------------------------------------------------

    def reset_client_traffic(self, inbound_id: int, email: str) -> Dict[str, Any]:
        """Reset a client's up/down counters to zero."""
        return self._request(
            "POST",
            f"panel/api/inbounds/{inbound_id}/resetClientTraffic/{email}",
        )

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
