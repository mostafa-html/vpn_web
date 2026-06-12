import logging
import time
from typing import Any, Dict, Optional
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError, HTTPError
from django.core.cache import cache
from billing_engine.models import XuiServer

logger = logging.getLogger(__name__)


class XuiAPIException(Exception):
    pass


class XuiAPIClient:
    """
    3x-ui API client with support for:
    - HTTP and HTTPS (self-signed certs)
    - Custom secret base paths (Panel Settings → Panel Path)
    - Hostname-based connections (instead of raw IP)
    """
    def __init__(self, server: XuiServer):
        self.server = server
        protocol = "https" if server.use_ssl else "http"
        host = server.get_host()          # domain or IP
        self.base_path = server.get_base_path()   # e.g. /4bfAPdC269HYSj1c24/
        self.base_url = f"{protocol}://{host}:{server.api_port}"
        self.verify_ssl = False           # accept self-signed certs
        self.cache_key = f"xui_session_{server.id}"
        self.max_retries = 3
        self.backoff_factor = 2

    def _url(self, path: str) -> str:
        """
        Build a full URL by prepending the secret base path.
        path should start without a slash, e.g. 'login' or 'panel/api/inbounds/list'
        """
        # base_path is always /something/ so strip leading slash from path
        path = path.lstrip('/')
        return f"{self.base_url}{self.base_path}{path}"

    def _get_session_cookie(self) -> Dict[str, str]:
        cookies = cache.get(self.cache_key)
        if cookies:
            return cookies

        login_url = self._url('login')
        logger.info(f"Authenticating with 3x-ui at {login_url}")
        payload = {
            "username": self.server.admin_username,
            "password": self.server.admin_password,
        }
        try:
            response = requests.post(login_url, data=payload, timeout=10.0, verify=self.verify_ssl)
            response.raise_for_status()
            json_data = response.json()
            if not json_data.get("success", True):
                raise XuiAPIException(f"Auth rejected: {json_data.get('msg')}")
            cookie_dict = response.cookies.get_dict()
            if not cookie_dict:
                raise XuiAPIException("Login OK but no session cookie returned.")
            cache.set(self.cache_key, cookie_dict, timeout=3600)
            return cookie_dict
        except RequestException as e:
            logger.error(f"Auth failure for server {self.server.id}: {e}")
            raise XuiAPIException(f"Failed to authenticate with 3x-ui: {e}") from e

    def _request(self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._url(endpoint)
        for attempt in range(1, self.max_retries + 1):
            sleep_duration = self.backoff_factor ** attempt
            try:
                cookies = self._get_session_cookie()
                response = requests.request(
                    method=method, url=url, json=json_data,
                    cookies=cookies, timeout=10.0, verify=self.verify_ssl,
                )
                if response.status_code in (401, 403):
                    cache.delete(self.cache_key)
                    raise ConnectionError("Stale session.")
                response.raise_for_status()
                return response.json()
            except (Timeout, ConnectionError) as e:
                if attempt == self.max_retries:
                    raise XuiAPIException(f"Failed after {self.max_retries} attempts: {e}") from e
                logger.warning(f"Retry {attempt} for {url} in {sleep_duration}s")
                time.sleep(sleep_duration)
            except HTTPError as e:
                status_code = e.response.status_code if e.response is not None else 500
                if status_code >= 500 and attempt < self.max_retries:
                    time.sleep(sleep_duration)
                    continue
                raise XuiAPIException(f"HTTP error: {e}") from e
            except RequestException as e:
                raise XuiAPIException(f"Request failed: {e}") from e
        return {}

    def get_inbounds(self) -> Dict[str, Any]:
        return self._request("GET", "panel/api/inbounds/list")

    def add_client(self, inbound_id: int, client_uuid: str, email: str) -> Dict[str, Any]:
        payload = {
            "id": inbound_id,
            "settings": f'{{"clients": [{{"id": "{client_uuid}", "email": "{email}"}}]}}',
        }
        return self._request("POST", "panel/api/inbounds/addClient", json_data=payload)

    def get_client_traffic(self, email: str) -> Dict[str, Any]:
        return self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")

    def sync_existing_clients(self) -> list:
        import json
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
