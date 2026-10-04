"""Pairing bootstrap: the REST surface a device touches before Noise.

Three endpoints, all HTTPS in production (plain HTTP is fine for LAN testing):

    GET  /fetch_vms              -> lease a VM (vm_id + vm_auth_token)
    POST /device_token/mint      -> exchange an SDK token for a device token
    POST /device_token/refresh   -> rotate the device token pair

The response shapes come from `muse_api.py` in the client SDK; see
`fetch_vms_with_status` for the exact validation the client performs. In short
`/fetch_vms` must answer with a `vm_list` array, and each usable entry needs
both a `vm_ws_url` and a `vm_auth_token`.

State is in-memory by default so stage 0 has no database. Swap `ForgeState`
for a persistent backend when you actually have devices.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

SDK_TOKEN_PREFIX = "mgst_"


def _bearer(request) -> str:
    """Extract the bearer credential, or '' when absent."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""


@dataclass
class Device:
    """A paired device."""

    device_id: str
    sdk_token: str
    access_token: str
    refresh_token: str
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {
            "device_id": self.device_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
        }


class ForgeState:
    """In-memory pairing state.

    Deliberately simple. One dict per kind of record, no expiry logic beyond a
    timestamp, because stage 0 is about proving the protocol, not about running
    a fleet.
    """

    def __init__(self) -> None:
        # access_token -> Device
        self._devices: dict[str, Device] = {}
        # device_id -> Device
        self._by_device_id: dict[str, Device] = {}
        # vm_id -> vm_auth_token
        self._vms: dict[str, str] = {}
        # the host clients should dial, e.g. "forge.local:8443"
        self.noise_host: str = "localhost:8443"
        # accept any SDK token (stage 0 convenience; require one in production)
        self.open_enrollment = True

    # -- enrollment ---------------------------------------------------------

    def mint_device_token(self, sdk_token: str) -> Device:
        """Exchange an SDK token for a device token pair."""
        if not self.open_enrollment and not sdk_token.startswith(SDK_TOKEN_PREFIX):
            raise PermissionError("invalid SDK token")
        device = Device(
            device_id=str(uuid.uuid4()),
            sdk_token=sdk_token,
            access_token=f"hat_access_{secrets.token_urlsafe(24)}",
            refresh_token=f"hatch_refresh:{secrets.token_urlsafe(24)}",
        )
        self._devices[device.access_token] = device
        self._by_device_id[device.device_id] = device
        log.info("minted device token for %s", device.device_id)
        return device

    def refresh_device_token(self, refresh_token: str, device_id: str) -> Optional[Device]:
        """Rotate a device's tokens. The presented access token is never used.

        Mirroring the client: a refresh that is handed a valid access token is
        refused, because the server would answer 200 with replacements that no
        other endpoint accepts, silently overwriting working credentials.
        """
        device = self._by_device_id.get(device_id)
        if device is None:
            return None
        if not secrets.compare_digest(device.refresh_token, refresh_token):
            return None
        device.access_token = f"hat_access_{secrets.token_urlsafe(24)}"
        device.refresh_token = f"hatch_refresh:{secrets.token_urlsafe(24)}"
        self._devices[device.access_token] = device
        # drop the old access token so it can no longer authenticate
        for key, value in list(self._devices.items()):
            if value is device and key != device.access_token:
                del self._devices[key]
        log.info("rotated tokens for %s", device_id)
        return device

    def device_for_access_token(self, access_token: str) -> Optional[Device]:
        return self._devices.get(access_token)

    # -- VM leasing ---------------------------------------------------------

    def lease_vm(self, device: Device) -> dict:
        """Lease a VM for a device, creating one on first use."""
        vm_id = f"vm_{uuid.uuid4().hex[:16]}"
        vm_auth_token = f"vmhat_{secrets.token_urlsafe(24)}"
        self._vms[vm_id] = vm_auth_token
        log.info("leased VM %s to device %s", vm_id, device.device_id)
        return {
            "vm_ws_url": f"wss://{self.noise_host}/v1/noise",
            "vm_auth_token": vm_auth_token,
            "vm_name": "muse-forge",
            "vm_id": vm_id,
            "default": True,
        }

    def vm_token(self, vm_id: str) -> Optional[str]:
        return self._vms.get(vm_id)

    def vm_is_valid(self, vm_id: str, vm_auth_token: str) -> bool:
        expected = self._vms.get(vm_id)
        if expected is None:
            return False
        return secrets.compare_digest(expected, vm_auth_token)


def configure_routes(app, state: ForgeState) -> None:
    """Attach the bootstrap endpoints to an aiohttp app."""

    async def fetch_vms(request):
        token = _bearer(request)
        device = state.device_for_access_token(token)
        if device is None:
            log.warning("fetch_vms: unknown or missing access token")
            return _json({"error_title": "unauthorized", "backend_error_code": "bad_token"}, 401)
        return _json({"vm_list": [state.lease_vm(device)]})

    async def device_token_mint(request):
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return _json({"error_title": "bad request"}, 400)
        sdk_token = payload.get("sdk_token", "")
        if not sdk_token:
            return _json({"error_title": "missing sdk_token"}, 400)
        try:
            device = state.mint_device_token(sdk_token)
        except PermissionError:
            return _json({"error_title": "invalid sdk token"}, 403)
        return _json(device.public())

    async def device_token_refresh(request):
        auth = _bearer(request)
        if not auth.startswith("hatch_refresh:"):
            # The client never presents the access token here, by design.
            return _json({"error_title": "bad refresh credential"}, 401)
        refresh_token = auth.split(":", 1)[1]
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return _json({"error_title": "bad request"}, 400)
        device_id = payload.get("device_id", "")
        device = state.refresh_device_token(refresh_token, device_id)
        if device is None:
            return _json({"error_title": "refresh rejected"}, 401)
        return _json(device.public())

    app.router.add_get("/fetch_vms", fetch_vms)
    app.router.add_post("/device_token/mint", device_token_mint)
    app.router.add_post("/device_token/refresh", device_token_refresh)


def _json(payload: dict, status: int = 200):
    from aiohttp import web

    return web.json_response(payload, status=status)
