import logging
import time
from typing import Any, Dict, Optional
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError, HTTPError
from django.core.cache import cache
from billing_engine.models import XuiServer

logger = logging.getLogger(__name__)


class XuiAPIException(Exception):
    """Base exception for all 3x-ui client communication errors."""
    pass


class XuiAPIClient:
    """
    Resilient, cached, fault-tolerant API Client for 3x-ui panels.
    Supports both HTTP and HTTPS (including self-signed certificates).
    """
    def __init__(self, server: XuiServer):
        self.server = server
        protocol = "https" if getattr(server, 'use_ssl', False) else "http"
        self.base_url = f"{protocol}://{server.ip_address}:{server.api_port}"
        self.verify_ssl = False  # 3x-ui panels commonly use self-signed certs
        self.cache_key = f"xui_session_{server.id}"
        self.max_retries = 3
        self.backoff_factor = 2

    def _get_session_cookie(self) -> Dict[str, str]:
        cookies = cache.get(self.cache_key)
        if cookies:
            logger.debug(f"Cache HIT for 3x-ui server ID {self.server.id}")
            return cookies

        logger.info(f"Cache MISS for 3x-ui server ID {self.server.id}. Initiating login.")
        login_url = f"{self.base_url}/login"
        payload = {
            "username": self.server.admin_username,
            "password": self.server.admin_password,
        }

        try:
            response = requests.post(
                login_url, data=payload, timeout=10.0, verify=self.verify_ssl
            )
            response.raise_for_status()

            json_data = response.json()
            if not json_data.get("success", True):
                raise XuiAPIException(
                    f"Authentication rejected by 3x-ui: {json_data.get('msg')}"
                )

            cookie_dict = response.cookies.get_dict()
            if not cookie_dict:
                raise XuiAPIException("Login successful but no session cookies returned.")

            cache.set(self.cache_key, cookie_dict, timeout=3600)
            return cookie_dict

        except RequestException as e:
            logger.error(f"Auth failure for server {self.server.id}: {e}")
            raise XuiAPIException(f"Failed to authenticate with 3x-ui: {e}") from e

    def _request(
        self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"

        for attempt in range(1, self.max_retries + 1):
            sleep_duration = self.backoff_factor ** attempt
            try:
                cookies = self._get_session_cookie()
                response = requests.request(
                    method=method,
                    url=url,
                    json=json_data,
                    cookies=cookies,
                    timeout=10.0,
                    verify=self.verify_ssl,
                )

                if response.status_code in (401, 403):
                    logger.warning("Session stale. Evicting cache and retrying.")
                    cache.delete(self.cache_key)
                    raise ConnectionError("Stale session token.")

                response.raise_for_status()
                return response.json()

            except (Timeout, ConnectionError) as e:
                if attempt == self.max_retries:
                    raise XuiAPIException(
                        f"Communication failed after {self.max_retries} attempts: {e}"
                    ) from e
                logger.warning(f"Transient error on {endpoint} (attempt {attempt}). Retrying in {sleep_duration}s.")
                time.sleep(sleep_duration)

            except HTTPError as e:
                status_code = e.response.status_code if e.response is not None else 500
                if status_code >= 500 and attempt < self.max_retries:
                    logger.warning(f"Server error {status_code}. Retrying in {sleep_duration}s.")
                    time.sleep(sleep_duration)
                    continue
                raise XuiAPIException(f"HTTP error: {e}") from e

            except RequestException as e:
                raise XuiAPIException(f"Request failed: {e}") from e

        return {}

    def get_inbounds(self) -> Dict[str, Any]:
        return self._request("GET", "/panel/api/inbounds/list")

    def add_client(self, inbound_id: int, client_uuid: str, email: str) -> Dict[str, Any]:
        payload = {
            "id": inbound_id,
            "settings": f'{{"clients": [{{"id": "{client_uuid}", "email": "{email}"}}]}}',
        }
        return self._request("POST", "/panel/api/inbounds/addClient", json_data=payload)

    def get_client_traffic(self, email: str) -> Dict[str, Any]:
        """Fetch traffic stats for a specific client by their email identifier."""
        return self._request("GET", f"/panel/api/inbounds/getClientTraffics/{email}")

    def sync_existing_clients(self) -> list:
        """
        Pull all inbounds and their clients from the 3x-ui panel.
        Returns a flat list of dicts: {inbound_id, protocol, email, uuid, up, down, total, enable}
        Useful for importing clients that already exist on the panel.
        """
        result = self.get_inbounds()
        clients = []
        for inbound in result.get("obj", []):
            inbound_id = inbound.get("id")
            protocol = inbound.get("protocol", "").upper()
            import json
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
                    "up": inbound.get("up", 0),
                    "down": inbound.get("down", 0),
                    "total": inbound.get("total", 0),
                    "enable": client.get("enable", True),
                })
        return clients
