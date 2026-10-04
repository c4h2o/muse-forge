"""End-to-end check: the SDK's own client against the Forge server.

This is the stage-0 acceptance test. It uses `muse_gadget`'s real
`LinkSession` -- not a reimplementation -- so a pass means the server is
byte-compatible with stock firmware.

    python verify.py                       # starts its own server on a free port
    python verify.py --server https://host:port   # test a running server

What it proves, in order:
  1. /device_token/mint then /fetch_vms hand out a usable VM
  2. the Noise XX three-message handshake completes
  3. link.register is accepted and acknowledged
  4. a message round-trips through /chat/stream
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import ssl
import sys
import uuid
from typing import Optional

# The client SDK lives in the Muse repo; make it importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK = os.environ.get("MUSEGADGET_SRC")
if not _SDK:
    # verify.py -> muse-forge/ -> workspace root -> muse-gadget-sdk/linux/src
    _SDK = os.path.abspath(
        os.path.join(_HERE, os.pardir, "muse-gadget-sdk", "linux", "src")
    )
if not os.path.isdir(os.path.join(_SDK, "musegadget")):
    raise SystemExit(
        f"cannot find the musegadget package under {_SDK}\n"
        "set MUSEGADGET_SRC to the SDK's linux/src directory"
    )
sys.path.insert(0, _SDK)

from musegadget.link_client import DeviceDescription, LinkSession, Outcome  # noqa: E402


sys.path.insert(0, os.path.join(_HERE, "server"))
from muse_forge.__main__ import build_app  # noqa: E402
from muse_forge.bootstrap import ForgeState  # noqa: E402

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


class ChatCollector:
    """Agent stub that records what the device sent."""

    def __init__(self) -> None:
        self.turns: list[dict] = []

    async def __call__(self, payload: dict) -> dict:
        self.turns.append(payload)
        return {
            "reply": f"ack: {payload.get('message', '')}",
            "turn": len(self.turns),
        }


def _describe() -> DeviceDescription:
    return DeviceDescription(
        node_id=f"forge-test-{uuid.uuid4().hex[:8]}",
        display_name="Forge Verify Node",
        version="0.1.0",
        commands={
            "system.run": {"description": "run a shell command"},
            "device.health": {"description": "report health"},
        },
    )


async def run_against(base_url: str, noise_host: str, agent: ChatCollector) -> int:
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if ok:
            print(f"  [{PASS}] {label}")
        else:
            failures += 1
            print(f"  [{FAIL}] {label}" + (f" -- {detail}" if detail else ""))



    print("\n1. bootstrap: mint a device token")
    # aiohttp client, not urllib: the server runs on this same event loop, so a
    # blocking urllib call would deadlock the server we are trying to reach.
    import aiohttp

    async with aiohttp.ClientSession(base_url=base_url) as http:
        async with http.post("/device_token/mint", json={"sdk_token": "mgst_stage0_verify"}) as resp:
            check("mint returns HTTP 2xx", resp.status == 200, f"status={resp.status}")
            tokens = await resp.json()
    check("mint returns access + refresh tokens",
          bool(tokens.get("access_token")) and bool(tokens.get("refresh_token")),
          json.dumps(tokens)[:120])

    print("\n2. bootstrap: fetch_vms leases a VM")
    vms, status = await _fetch_vms(aiohttp, base_url, tokens["access_token"])
    check("fetch_vms returns a leaseable VM", bool(vms), f"status={status} vms={len(vms)}")
    if not vms:
        print("\ncannot continue without a VM")
        return 1
    vm = vms[0]
    vm_id = vm["vm_id"]
    vm_token = vm["vm_auth_token"]
    check("VM carries vm_id and vm_auth_token", bool(vm_id) and bool(vm_token))
    check("VM advertises a wss:// URL", vm["vm_url"].startswith("wss://"), vm["vm_url"])

    print("\n3. noise session: handshake + register")
    device = _describe()
    session = LinkSession(
        noise_host=noise_host,
        vm_id=vm_id,
        vm_auth_token=vm_token,
        device=device,
        run_command=lambda command, params, timeout_ms: _run_command(command, params),
        connect=_connector(base_url),
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(session.run(stop))
    try:
        for _ in range(100):
            await asyncio.sleep(0.1)
            if session.registered_at is not None:
                break
        check("link.register acknowledged", session.registered_at is not None,
              "no registration within 10s")

        print("\n4. chat: a message round-trips")
        result = await session.send_chat("what is the air speed velocity of a swallow?")
        check("send_chat got HTTP 2xx", bool(result.get("ok")),
              f"status={result.get('status')} response={result.get('response')}")
        check("agent received the message",
              bool(agent.turns) and agent.turns[0].get("message", "").startswith("what is"),
              json.dumps(agent.turns[:1])[:160])
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()

    print(f"\n{'=' * 56}")
    if failures:
        print(f"RESULT: {failures} check(s) failed")
    else:
        print("RESULT: all checks passed -- the server is byte-compatible")
    print("=" * 56)
    return 1 if failures else 0


async def _fetch_vms(aiohttp, base_url: str, access_token: str):
    """Mirror of the SDK's fetch_vms_with_status, but async.

    We re-implement the call rather than reuse the SDK's urllib version,
    because this test server shares the event loop.
    """
    async with aiohttp.ClientSession(base_url=base_url) as http:
        async with http.get(
            "/fetch_vms",
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-API-Version": "1.0.0",
            },
        ) as resp:
            status = resp.status
            data = await resp.json(content_type=None)
    if not isinstance(data, dict):
        return [], status
    if data.get("error_title") or data.get("backend_error_code"):
        return [], status
    out = []
    for entry in data.get("vm_list") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("vm_ws_url") or entry.get("vm_url")
        token = entry.get("vm_auth_token")
        if url and token:
            out.append({
                "vm_url": url,
                "vm_auth_token": token,
                "vm_name": entry.get("vm_name", ""),
                "vm_id": entry.get("vm_id", ""),
            })
    return out, status


def _run_command(command: str, params: dict) -> dict:
    """The device-side command executor. Stage 0 only reports."""
    return {"ok": False, "error": "stage 0 verify: execution disabled"}


def _connector(base_url: str):
    """Build a connect() that talks http/wss to the local test server."""
    import websockets
    from websockets.asyncio.client import connect

    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")

    async def connect_(url: str, headers: dict):
        # the SDK builds an https/wss URL; rewrite whatever host it used
        path = url.split("/", 3)[3] if url.count("/") >= 3 else "/v1/noise"
        target = f"{ws_base}/{path}"
        try:
            return await connect(target, additional_headers=headers, max_size=None,
                                 open_timeout=20, user_agent_header="muse-forge-verify")
        except ssl.SSLError:
            return await connect(target, additional_headers=headers, max_size=None,
                                 open_timeout=20, ssl=None,
                                 user_agent_header="muse-forge-verify")

    return connect_


async def start_local_server() -> tuple[web.AppRunner, str, str, object]:
    import socket

    from aiohttp import web

    # Bind a socket first so we know the port before starting the site. Using
    # port 0 and reading back runner.addresses is racy across aiohttp versions.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    state = ForgeState()
    agent = ChatCollector()
    app = build_app(state, agent)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    state.noise_host = f"127.0.0.1:{port}"
    base = f"http://127.0.0.1:{port}"
    return runner, base, state.noise_host, agent


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=None,
                        help="base URL of a running server; omit to start one")
    parser.add_argument("--noise-host", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    runner = None
    agent = ChatCollector()
    if args.server:
        base = args.server.rstrip("/")
        noise_host = args.noise_host or base.split("://", 1)[1]
    else:
        print("starting a local server for the test...")
        runner, base, noise_host, agent = await start_local_server()
        print(f"  listening on {base}  (devices dial {noise_host})")

    try:
        return await run_against(base, noise_host, agent)
    finally:
        if runner is not None:
            await runner.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
