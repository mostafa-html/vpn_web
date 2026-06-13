import json
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
    totalGB   : plain integer gigabytes (e.g. 10 = 10 GB, 0 = unlimited).
    expiryTime: Unix milliseconds. 0 = never expires.
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
        url = self._url(endpoint)

        logger.warning("[XUI_REQ]  %s  %s", method, url)
        if json_data is not None:
            try:
                logger.warning("[XUI_BODY] %s", json.dumps(json_data, ensure_ascii=False, default=str))
            except Exception:
                logger.warning("[XUI_BODY] <could not serialize body>")

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

                logger.warning(
                    "[XUI_RESP] status=%s  body=%s",
                    response.status_code,
                    response.text[:1000],
                )

                if response.status_code == 401:
                    raise XuiAPIException(
                        f"[401] API token rejected for server '{self.server.name}'. "
                        "Regenerate the token in Panel Settings -> Security -> API Token."
                    )
                if response.status_code == 403:
                    raise XuiAPIException(
                        f"[403] Access forbidden for server '{self.server.name}'."
                    )
                if response.status_code == 404:
                    raise XuiAPIException(
                        f"[404] Endpoint not found: {url}. "
                        "Check your 3x-ui version and base path."
                    )
                response.raise_for_status()

                data = response.json()

                if isinstance(data, dict) and data.get("success") is False:
                    msg = data.get("msg") or "unknown panel error"
                    raise XuiAPIException(
                        f"[PANEL_ERR] success=false for {url} — {msg}"
                    )

                return data

            except (Timeout, ConnectionError) as e:
                if attempt == self.max_retries:
                    raise XuiAPIException(f"Failed after {self.max_retries} attempts: {e}") from e
                logger.warning("[XUI_RETRY] attempt %d/%d for %s in %ds", attempt, self.max_retries, url, sleep_duration)
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
        return self._request("GET", "panel/api/inbounds/list")

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    def get_client_by_email(self, email: str) -> Dict[str, Any]:
        """
        Fetch live client fields using the correct v3 endpoint.
        Returns the raw client dict from the panel (id, email, totalGB,
        expiryTime, enable, limitIp, …).
        """
        # FIX: correct v3 endpoint is getClientTraffics/<email>, not clients/get/<email>
        data = self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")
        obj = data.get("obj")
        if not obj:
            raise XuiAPIException(f"Client '{email}' not found on server '{self.server.name}'.")
        # getClientTraffics returns traffic stats, not the full client config.
        # We need the full config (totalGB, expiryTime, enable) — scan inbounds for it.
        inbounds_data = self.get_inbounds()
        for inbound in inbounds_data.get("obj", []):
            try:
                settings_obj = json.loads(inbound.get("settings", "{}"))
            except (json.JSONDecodeError, TypeError):
                settings_obj = {}
            for client in settings_obj.get("clients", []):
                if client.get("email", "").strip().lower() == email.strip().lower():
                    return client
        raise XuiAPIException(f"Client config for '{email}' not found in any inbound on server '{self.server.name}'.")

    def add_client(
        self,
        inbound_ids: list,
        client_uuid: str,
        email: str,
        total_gb: int = 0,
        expiry_time_ms: int = 0,
        sub_id: str = "",
    ) -> Dict[str, Any]:
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
        client_uuid: str,
        total_gb: int = 0,
        expiry_time_ms: int = 0,
        enable: bool = True,
        inbound_ids: Optional[list] = None,
    ) -> Dict[str, Any]:
        """
        FIX: 3x-ui v3 update endpoint requires the client UUID in the URL,
        NOT the email.  Endpoint: POST /panel/api/clients/update/<uuid>
        Body must include inboundIds so the panel knows which inbounds to
        apply the change to.

        We fetch the current client config first so we only overwrite the
        fields we explicitly change and preserve everything else (limitIp,
        subId, tgId, etc.).
        """
        # Fetch current record to preserve all unchanged fields.
        current = self.get_client_by_email(email)
        merged = dict(current)
        merged["id"] = client_uuid          # ensure UUID is present in body
        merged["email"] = email
        merged["totalGB"] = int(total_gb)
        merged["expiryTime"] = int(expiry_time_ms)
        merged["enable"] = bool(enable)

        # FIX: URL key is the UUID, not the email.
        payload = {
            "inboundIds": [int(i) for i in inbound_ids] if inbound_ids else [],
            "client": merged,
        }
        return self._request("POST", f"panel/api/clients/update/{client_uuid}", json_data=payload)

    def disable_client(
        self,
        inbound_id: int,
        client_uuid: str,
    ) -> Dict[str, Any]:
        """
        Disable (block) a client by setting enable=False via the update endpoint.
        Used by _deprovision_subscription() in tasks.py.
        We fetch the current client config from the inbound to get the email,
        then call update_client with enable=False.
        """
        inbounds_data = self.get_inbounds()
        for inbound in inbounds_data.get("obj", []):
            if int(inbound.get("id", -1)) != int(inbound_id):
                continue
            try:
                settings_obj = json.loads(inbound.get("settings", "{}"))
            except (json.JSONDecodeError, TypeError):
                settings_obj = {}
            for client in settings_obj.get("clients", []):
                if client.get("id", "") == client_uuid:
                    email = client.get("email", "")
                    return self.update_client(
                        email=email,
                        client_uuid=client_uuid,
                        total_gb=int(client.get("totalGB", 0)),
                        expiry_time_ms=int(client.get("expiryTime", 0)),
                        enable=False,
                        inbound_ids=[inbound_id],
                    )
        raise XuiAPIException(
            f"Client UUID '{client_uuid}' not found in inbound {inbound_id} "
            f"on server '{self.server.name}'."
        )

    def delete_client(self, email: str) -> Dict[str, Any]:
        return self._request("POST", f"panel/api/clients/del/{email}")

    def reset_client_traffic(self, email: str) -> Dict[str, Any]:
        return self._request("POST", f"panel/api/clients/resetTraffic/{email}")

    def get_client_traffic(self, email: str) -> Dict[str, Any]:
        return self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")

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
