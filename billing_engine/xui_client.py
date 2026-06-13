import logging
import time
from typing import Any, Dict, Optional

import requests
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

from billing_engine.models import XuiServer

logger = logging.getLogger(__name__)


class XuiAPIException(Exception):
    pass


class XuiAPIClient:
    """
    3x-ui v3+ API client — Bearer token auth.
    Token: Panel Settings -> Security -> API Token.

    UNIT CONTRACT
    -------------
    totalGB  : plain integer gigabytes  (e.g. 10 = 10 GB, 0 = unlimited).
               Never multiply/divide by 1024**3 — the panel stores GB, not bytes.
    expiryTime: Unix milliseconds. 0 = never expires.

    API SURFACE (v3+)
    -----------------
    Clients live under  /panel/api/clients/  (NOT /inbounds/ any more).
      GET  /panel/api/clients/get/:email          -> fetch one client
      POST /panel/api/clients/add                 -> create client
      POST /panel/api/clients/update/:email       -> update client  ← flat JSON body
      POST /panel/api/clients/del/:email          -> delete client
      POST /panel/api/clients/resetTraffic/:email -> reset traffic

    Inbounds (read-only for us):
      GET  /panel/api/inbounds/list

    The old v2 endpoints (/inbounds/addClient, /inbounds/updateClient/:uuid,
    /inbounds/:id/delClient/:uuid) are GONE in v3 and return 404.
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
                "Go to Panel Settings -> Security -> API Token."
            )
        return {
            "Authorization": f"Bearer {self.server.api_token}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Perform an HTTP request with retries.
        Raises XuiAPIException on any transport error, HTTP error, or when
        the panel returns {"success": false, "msg": "..."}.
        """
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
                        "Check the API token permissions."
                    )
                if response.status_code == 404:
                    raise XuiAPIException(
                        f"Endpoint not found (404): {url}. "
                        "Ensure your 3x-ui panel is v3+ and the base path is correct."
                    )
                response.raise_for_status()

                data = response.json()

                # 3x-ui v3 wraps every response in {success: bool, msg: str, obj: …}
                # A 200 OK with success=false is still a panel-level failure.
                if isinstance(data, dict) and data.get("success") is False:
                    msg = data.get("msg") or "unknown panel error"
                    raise XuiAPIException(
                        f"Panel returned success=false for {url}: {msg}"
                    )

                return data

            except (Timeout, ConnectionError) as e:
                if attempt == self.max_retries:
                    raise XuiAPIException(
                        f"Failed after {self.max_retries} attempts: {e}"
                    ) from e
                logger.warning(
                    "Retry %d/%d for %s in %ds", attempt, self.max_retries, url, sleep_duration
                )
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
    # Inbounds (read)
    # ------------------------------------------------------------------

    def get_inbounds(self) -> Dict[str, Any]:
        """Return the full inbounds list from /panel/api/inbounds/list."""
        return self._request("GET", "panel/api/inbounds/list")

    # ------------------------------------------------------------------
    # Clients — v3 API  (/panel/api/clients/)
    # ------------------------------------------------------------------

    def get_client_by_email(self, email: str) -> Dict[str, Any]:
        """
        Fetch a single client record by email.
        Returns the full client dict (id, email, totalGB, expiryTime, enable,
        subId, tgId, limitIp, flow, reset, comment, …).
        Raises XuiAPIException if not found or on any error.
        """
        data = self._request("GET", f"panel/api/clients/get/{email}")
        # Response shape: {success: true, obj: {client: {...}, inboundIds: [...]}}
        obj = data.get("obj") or {}
        client = obj.get("client") or obj  # some builds nest, some don't
        if not client:
            raise XuiAPIException(f"Client '{email}' not found on server '{self.server.name}'.")
        return client

    def add_client(
        self,
        inbound_ids: list,
        client_uuid: str,
        email: str,
        total_gb: int = 0,
        expiry_time_ms: int = 0,
        sub_id: str = "",
    ) -> Dict[str, Any]:
        """
        Create a new client on one or more inbounds.

        inbound_ids   : list of int inbound IDs to attach the client to.
        client_uuid   : VLESS/VMess UUID string.
        email         : unique client email (used as identifier in v3 API).
        total_gb      : plain GB cap. 0 = unlimited.
        expiry_time_ms: Unix ms. 0 = never.
        sub_id        : subscription UUID (generated by panel if omitted).
        """
        payload = {
            "inboundIds": [int(i) for i in inbound_ids],
            "client": {
                "id": client_uuid,
                "email": email,
                "totalGB": int(total_gb),
                "expiryTime": int(expiry_time_ms),
                "enable": True,
                "limitIp": 0,
                "tgId": 0,
                "subId": sub_id,
                "reset": 0,
                "comment": "",
            },
        }
        return self._request("POST", "panel/api/clients/add", json_data=payload)

    def update_client(
        self,
        email: str,
        total_gb: int = 0,
        expiry_time_ms: int = 0,
        enable: bool = True,
        inbound_ids: Optional[list] = None,
    ) -> Dict[str, Any]:
        """
        Update an existing client identified by email.

        Fetches the current client record first so all existing fields
        (subId, tgId, limitIp, flow, reset, comment, UUID, …) are preserved.
        Only totalGB, expiryTime, and enable are overwritten.

        email         : client email (panel identifier in v3).
        total_gb      : plain GB cap. 0 = unlimited.
        expiry_time_ms: Unix ms. 0 = never.
        enable        : whether the client is active.
        inbound_ids   : optional list of int IDs to restrict update scope
                        (passed as ?inboundIds=1,2 query param).
        """
        # Fetch current record so we don't clobber unrelated fields.
        current = self.get_client_by_email(email)

        # Build the merged payload — preserve every existing field.
        merged = dict(current)
        merged["totalGB"] = int(total_gb)
        merged["expiryTime"] = int(expiry_time_ms)
        merged["enable"] = bool(enable)

        endpoint = f"panel/api/clients/update/{email}"
        if inbound_ids:
            ids_str = ",".join(str(i) for i in inbound_ids)
            endpoint = f"{endpoint}?inboundIds={ids_str}"

        logger.debug(
            "update_client: server=%s email=%s total_gb=%s expiry_ms=%s enable=%s",
            self.server.name, email, total_gb, expiry_time_ms, enable,
        )
        return self._request("POST", endpoint, json_data=merged)

    def delete_client(self, email: str) -> Dict[str, Any]:
        """
        Delete a client by email.
        v3 API: POST /panel/api/clients/del/:email
        """
        return self._request("POST", f"panel/api/clients/del/{email}")

    def reset_client_traffic(self, email: str) -> Dict[str, Any]:
        """
        Reset traffic counters for a client.
        v3 API: POST /panel/api/clients/resetTraffic/:email
        """
        return self._request("POST", f"panel/api/clients/resetTraffic/{email}")

    def get_client_traffic(self, email: str) -> Dict[str, Any]:
        """Get traffic stats for a client by email."""
        return self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")

    def sync_existing_clients(self) -> list:
        """
        Return a flat list of all clients across all inbounds.
        Each entry: {inbound_id, protocol, email, uuid, enable}
        """
        result = self.get_inbounds()
        clients = []
        for inbound in result.get("obj", []):
            inbound_id = inbound.get("id")
            protocol = inbound.get("protocol", "").upper()
            try:
                import json
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
