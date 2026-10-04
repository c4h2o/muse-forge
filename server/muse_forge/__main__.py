"""Muse Forge: an open-source stand-in for Meta's Muse gadget service.

Speaks enough of the wire protocol for a stock ESP32 running Meta's
open-source firmware to pair, complete the Noise XX handshake, register, and
exchange messages -- against a backend you control.

    python -m muse_forge --host 0.0.0.0 --port 8443

Endpoints
    GET  /fetch_vms             lease a VM
    POST /device_token/mint     SDK token -> device token
    POST /device_token/refresh  rotate device tokens
    WS   /v1/noise              Noise XX session (bearer in the header)
    GET  /health                liveness + connected devices
"""

from __future__ import annotations

import argparse
import asyncio

import logging

import ssl
from typing import Optional

from aiohttp import web

from .bootstrap import ForgeState, configure_routes
from .noise_session import ForgeSession

log = logging.getLogger("muse_forge")


class EchoAgent:
    """A deliberately boring agent, so the protocol path is testable.

    Replace this with a real LLM call. The contract is: take the request dict,
    return a JSON-serialisable dict. The device shows `reply` as captions.
    """

    def __init__(self) -> None:
        self.turns = 0

    async def __call__(self, payload: dict) -> dict:
        self.turns += 1
        message = payload.get("message", "")
        log.info("agent turn %d: %r", self.turns, message)
        return {
            "reply": f"Muse Forge heard you: {message}",
            "turn": self.turns,
            "device_id": payload.get("device_id", ""),
        }


def build_app(state: Optional[ForgeState] = None, agent=None) -> web.Application:
    state = state or ForgeState()
    agent = agent or EchoAgent()
    # live sessions, for /health and for tests
    app = web.Application()
    app["state"] = state
    app["agent"] = agent
    app["sessions"] = {}

    configure_routes(app, state)

    async def health(request: web.Request) -> web.Response:
        sessions = app["sessions"]
        return web.json_response(
            {
                "ok": True,
                "service": "muse-forge",
                "protocol": "Noise_XX_25519_AESGCM_SHA256",
                "devices": [s.state.describe() for s in sessions.values()],
            }
        )

    async def noise(request: web.Request) -> web.WebSocketResponse:
        vm_id = request.query.get("vm_id", "")
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""

        # The bearer authenticates at the upgrade, exactly like Meta's edge.
        if not vm_id or not state.vm_is_valid(vm_id, token):
            log.warning("noise: rejected vm_id=%r", vm_id)
            raise web.HTTPUnauthorized(text="invalid vm credentials")

        ws = web.WebSocketResponse(max_msg_size=0, heartbeat=None)
        await ws.prepare(request)

        session = ForgeSession(
            ws=ws,
            vm_id=vm_id,
            command_handler=_make_command_handler(),
            on_chat=agent,
        )
        app["sessions"][vm_id] = session
        log.info("noise: session opened for vm_id=%s", vm_id)
        try:
            await session.handshake()
            await session.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("noise: session for %s failed: %s", vm_id, exc)
        finally:
            app["sessions"].pop(vm_id, None)
            log.info("noise: session closed for vm_id=%s", vm_id)
        return ws

    app.router.add_get("/health", health)
    app.router.add_get("/v1/noise", noise)
    return app


def _make_command_handler():
    """Handle `link.invoke` from the device.

    The Linux SDK's device side can call system.run / file.read / file.write /
    device.health. We answer with a refusal rather than executing anything:
    stage 0 is about the protocol, and silently running shell commands on a box
    that just paired is not a good default. Wire this to a real executor only
    behind an explicit allowlist.
    """
    allowed: set[str] = set()

    async def handle(command: str, params: dict, timeout_ms: Optional[int]) -> dict:
        if command not in allowed:
            return {
                "ok": False,
                "error": f"command {command!r} is not enabled on this server",
            }
        raise NotImplementedError  # pragma: no cover - placeholder for real work

    return handle


def _ssl_context(cert: Optional[str], key: Optional[str]) -> Optional[ssl.SSLContext]:
    if not cert or not key:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


def main() -> None:
    parser = argparse.ArgumentParser(description="Muse Forge server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--noise-host", default=None,
                        help="host clients should dial back on, e.g. forge.local:8443")
    parser.add_argument("--cert", default=None, help="TLS certificate (PEM)")
    parser.add_argument("--key", default=None, help="TLS private key (PEM)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    state = ForgeState()
    if args.noise_host:
        state.noise_host = args.noise_host
    elif args.cert:
        state.noise_host = f"localhost:{args.port}"

    app = build_app(state)
    context = _ssl_context(args.cert, args.key)
    scheme = "https" if context else "http"
    log.info("muse-forge listening on %s://%s:%d", scheme, args.host, args.port)
    log.info("devices should dial wss://%s/v1/noise", state.noise_host)
    if not context:
        log.warning("no TLS cert given -- the ESP32 firmware expects wss://")
    web.run_app(app, host=args.host, port=args.port, ssl_context=context)


if __name__ == "__main__":
    main()
