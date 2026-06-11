import logging
import time
from typing import Any, Dict, Optional
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError, HTTPError
from django.core.cache import cache
from billing_engine.models import XuiServer  # True source of truth schema

logger = logging.getLogger(__name__)


class XuiAPIException(Exception):
    """Base exception for all 3x-ui client communication errors."""
    pass


class XuiAPIClient:
    """
    Resilient, cached, and fault-tolerant API Client wrapper
    aligned with the Django billing infrastructure schema.
    """
    def __init__(self, server: XuiServer):
        self.server = server
        self.base_url = f"http://{server.ip_address}:{server.api_port}"
        self.cache_key = f"xui_session_{server.id}"
        self.max_retries = 3
        self.backoff_factor = 2  # Seconds multiplier for exponential backoff

    def _get_session_cookie(self) -> Dict[str, str]:
        """
        Retrieves the 3x-ui session cookie from Django's cache backend.
        Performs a fresh HTTP login handshake only on a cache miss.
        """
        cookies = cache.get(self.cache_key)
        if cookies:
            logger.debug(f"Cache HIT for 3x-ui server ID {self.server.id}")
            return cookies

        logger.info(f"Cache MISS for 3x-ui server ID {self.server.id}. Initiating login handshake.")
        login_url = f"{self.base_url}/login"
        
        # Aligned to exact model definitions: admin_username and admin_password
        payload = {
            "username": self.server.admin_username,
            "password": self.server.admin_password
        }

        try:
            # Short, explicit timeout for authentication to avoid hanging workers
            response = requests.post(login_url, data=payload, timeout=5.0)
            response.raise_for_status()
            
            json_data = response.json()
            if not json_data.get("success", True):
                raise XuiAPIException(f"Authentication rejected by 3x-ui: {json_data.get('msg')}")

            # Extract cookie jar into a standard dictionary
            cookie_dict = response.cookies.get_dict()
            if not cookie_dict:
                raise XuiAPIException("Login successful but no session cookies returned.")

            # Commit token to cache for exactly 60 minutes (3600 seconds)
            cache.set(self.cache_key, cookie_dict, timeout=3600)
            return cookie_dict

        except RequestException as e:
            logger.error(f"Critical authentication failure for 3x-ui server {self.server.id}: {e}")
            raise XuiAPIException(f"Failed to authenticate with 3x-ui edge node: {e}") from e

    def _request(self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Centralized HTTP wrapper handling verb routing, token injection,
        and an automated exponential backoff retry loop.
        """
        url = f"{self.base_url}{endpoint}"
        
        for attempt in range(1, self.max_retries + 1):
            sleep_duration = self.backoff_factor ** attempt
            try:
                cookies = self._get_session_cookie()
                
                logger.debug(f"Sending {method} to {url} (Attempt {attempt}/{self.max_retries})")
                response = requests.request(
                    method=method,
                    url=url,
                    json=json_data,
                    cookies=cookies,
                    timeout=10.0  
                )

                # Instantly evict cache and loop back if token became stale downstream
                if response.status_code in (401, 403):
                    logger.warning("Session token invalidated by remote host. Evicting cache and retrying.")
                    cache.delete(self.cache_key)
                    raise ConnectionError("Stale session token encountered.")

                response.raise_for_status()
                return response.json()

            except (Timeout, ConnectionError) as e:
                # Always retry on transient network-layer dropouts
                if attempt == self.max_retries:
                    logger.critical(f"Network fault threshold exceeded for server {self.server.id} on {endpoint}.")
                    raise XuiAPIException(f"Remote node communication broken after {self.max_retries} attempts: {e}") from e
                
                logger.warning(f"Transient network glitch on {endpoint} (Attempt {attempt}). Retrying in {sleep_duration}s.")
                time.sleep(sleep_duration)

            except HTTPError as e:
                # Guard rails: Retry on 5xx Server Errors, but crash immediately on deterministic 4xx Errors
                status_code = e.response.status_code if e.response is not None else 500
                if status_code >= 500 and attempt < self.max_retries:
                    logger.warning(f"Remote server error {status_code}. Retrying in {sleep_duration}s.")
                    time.sleep(sleep_duration)
                    continue
                
                logger.error(f"Deterministic HTTP Error encountered: {e}")
                raise XuiAPIException(f"Non-transient API failure: {e}") from e

            except RequestException as e:
                raise XuiAPIException(f"Fatal request anomaly encountered: {e}") from e
                
        return {}

    def get_inbounds(self) -> Dict[str, Any]:
        """Issues a GET request to retrieve all active proxy inbounds."""
        return self._request("GET", "/panel/api/inbounds/list")

    def add_client(self, inbound_id: int, client_uuid: str, email: str) -> Dict[str, Any]:
        """Issues an authenticated POST request to register a new Xray client payload."""
        payload = {
            "id": inbound_id,
            "settings": f'{{"clients": [{{"id": "{client_uuid}", "email": "{email}"}}]}}'
        }
        return self._request("POST", "/panel/api/inbounds/addClient", json_data=payload)