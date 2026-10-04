"""Noise XX responder + ServiceFrame session, server side.

This module reuses the client SDK's own noise package instead of
re-implementing the protocol. The pattern is `Noise_XX_25519_AESGCM_SHA256`,
and the SDK ships both `NoiseXXInitiator` and `NoiseXXResponder` plus the
ServiceFrame protobuf codec, so the server is a mirror of the client and
byte-compatibility is structural rather than maintained by hand.

One asymmetry matters: `NoiseXXResponder.split()` returns `(c2, c1)`, so the
server's send cipher is `c2` and its receive cipher is `c1` -- the mirror of
the initiator's `split() -> (c1, c2)`.

Wire shape, from the client's point of view:

    device -> server   ServiceRequest(ServiceFrame(request, ...))
    server -> device   ServiceResponse(ServiceFrame(response|body_chunk, ...))

`NoiseTransport.decrypt_frame` on the client expects a ServiceResponse
envelope, which is why the server writes with `encode_response_envelope`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from musegadget.noise import (
    ApplicationResponse,
    BodyChunk,
    CipherState,
    Header,
    NoiseFrameDecoder,
    NoiseProtocolError,
    NoiseTransport,
    NoiseXXResponder,
    Reset,
    ResetCode,
    ServiceFrame,
    decode_service_frame,
    decode_service_request,
    encode_noise_frames,
)

# Not re-exported from musegadget.noise.__init__, but this is the SDK's own
# helper for the server->client envelope -- exactly the direction we need.
from musegadget.noise.transport import encode_response_envelope

log = logging.getLogger(__name__)

EMPTY_AD = b""
HANDSHAKE_TIMEOUT_S = 20
MAX_INBOUND_MESSAGE = 4 * 1024 * 1024

# Stream ids the firmware reserves. See esp32/main/noise_control.cpp:
#   1 -> control, 2 -> L3 tunnel, plus an identity stream and REQ_STREAM_BASE.
CONTROL_STREAM_ID = 1
TUNNEL_STREAM_ID = 2

CONTROL_PATH = "/link-control"
CHAT_PATH = "/chat/stream"
TUNNEL_PATH = "/link-tunnel"
IDENTITY_PATH = "/identity"


def encode_message(obj: dict) -> bytes:
    """Length-prefixed JSON, the control stream's framing.

    Mirrors `link_client.encode_message`: little-endian u32 length followed by
    the UTF-8 JSON body. A zero length is a keepalive.
    """
    data = json.dumps(obj, separators=(",", ":")).encode()
    return struct.pack("<I", len(data)) + data


class MessageDecoder:
    """Reassembles length-prefixed JSON messages from body chunks."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        out: list[dict] = []
        while len(self._buf) >= 4:
            (length,) = struct.unpack_from("<I", self._buf)
            if length > MAX_INBOUND_MESSAGE:
                raise ValueError(f"inbound message too large: {length}")
            if len(self._buf) < 4 + length:
                break
            raw = bytes(self._buf[4 : 4 + length])
            del self._buf[: 4 + length]
            if not raw:
                continue  # keepalive
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("dropping malformed control message (%d bytes)", length)
                continue
            if isinstance(message, dict):
                out.append(message)
        return out


@dataclass
class DeviceState:
    """What the server knows about one connected device."""

    vm_id: str
    node_id: str = ""
    display_name: str = ""
    registered: bool = False
    registered_at: Optional[float] = None
    platform: str = ""
    model_id: str = ""

    def describe(self) -> dict:
        return {
            "vm_id": self.vm_id,
            "node_id": self.node_id,
            "display_name": self.display_name,
            "platform": self.platform,
            "model_id": self.model_id,
            "registered": self.registered,
            "uptime_s": None if self.registered_at is None else round(time.monotonic() - self.registered_at, 1),
        }


# (command, params, timeout_ms) -> {"ok": bool, ...}
CommandHandler = Callable[[str, dict, Optional[int]], Awaitable[dict]]


class ForgeSession:
    """One device's Noise session plus virtual-HTTP routing."""

    def __init__(
        self,
        *,
        ws: Any,
        vm_id: str,
        command_handler: Optional[CommandHandler] = None,
        on_chat: Optional[Callable[[dict], Awaitable[dict]]] = None,
    ) -> None:
        self._ws = ws
        self._state = DeviceState(vm_id=vm_id)
        self._command_handler = command_handler
        self._on_chat = on_chat
        self.state = self._state
        # send/recv ciphers, held directly so we can frame both directions
        self._send: Optional[CipherState] = None
        self._recv: Optional[CipherState] = None
        self._in_decoder = NoiseFrameDecoder()
        self._control_decoder = MessageDecoder()
        self._register_id = ""
        self._send_lock = asyncio.Lock()
        self.closed = False

    # -- handshake -----------------------------------------------------------

    async def handshake(self) -> None:
        responder = NoiseXXResponder()
        responder.initialize()

        msg1 = await asyncio.wait_for(self._recv_raw(), HANDSHAKE_TIMEOUT_S)
        msg2 = responder.read_message1_and_write_message2(msg1)
        await self._ws.send_bytes(msg2)
        log.info("noise: msg2 sent (%d bytes)", len(msg2))

        msg3 = await asyncio.wait_for(self._recv_raw(), HANDSHAKE_TIMEOUT_S)
        responder.read_message3(msg3)
        log.info("noise: msg3 read (%d bytes)", len(msg3))

        # Responder.split() returns (c2, c1) -- already in send, recv order.
        # c1 is the initiator's send cipher, so it is ours for receiving; c2 is
        # the initiator's receive cipher, so it is ours for sending. The
        # initiator does `send, recv = initiator.split()` and gets (c1, c2).
        self._send, self._recv = responder.split()
        log.info("noise: session established for vm_id=%s", self._state.vm_id)

    async def _recv_raw(self) -> bytes:
        # aiohttp >= 3.9 returns a WSMessage, not bare bytes. Unwrap it here so
        # the rest of the module can work with plain bytes.
        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            raise ConnectionError("noise handshake got a text frame")
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
            raise ConnectionError("noise handshake: peer closed")
        return bytes(msg.data)

    # -- inbound -------------------------------------------------------------

    async def _recv_frame(self) -> Optional[ServiceFrame]:
        """Read one encrypted ServiceFrame, or None if the peer closed."""
        if self._recv is None:
            raise RuntimeError("session not handshaked")
        while True:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                log.warning("ignoring text frame on the noise connection")
                continue
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                return None
            plain = self._recv.decrypt_with_ad(EMPTY_AD, bytes(msg.data))
            reassembled = self._in_decoder.decode(plain)
            if reassembled is None:
                continue  # more chunks pending
            request = decode_service_request(reassembled)
            return decode_service_frame(request.payload)

    # -- outbound ------------------------------------------------------------

    async def _send_service_request(self, frame: ServiceFrame) -> None:
        """Encrypt a ServiceFrame toward the device.

        Direction matters. The SDK client reads everything with
        `NoiseTransport.decrypt_frame`, which decodes a *ServiceResponse*
        envelope -- and `encode_response_envelope` is the SDK's own helper for
        producing exactly that. The device sends ServiceRequest and expects
        ServiceResponse back, even for frames that are logically "requests"
        (link.invoke), because the envelope names the direction of travel, not
        the semantics of the frame inside.
        """
        if self._send is None:
            raise RuntimeError("session not handshaked")
        async with self._send_lock:
            for chunk in encode_noise_frames(encode_response_envelope(frame)):
                await self._ws.send_bytes(self._send.encrypt_with_ad(EMPTY_AD, chunk))

    async def _send_application_response(self, stream_id: int, status: int, body: bytes) -> None:
        """Answer a request stream with a complete HTTP response.

        The device reassembles responses from `response` frames on the same
        stream id, so a single frame with end_body set closes the stream.
        """
        response = ApplicationResponse(
            status=status,
            headers=[Header("Content-Type", "application/json")],
            body=body,
            end_body=True,
        )
        await self._send_service_request(ServiceFrame.response(stream_id, response))

    async def _send_response_headers(self, stream_id: int, status: int) -> None:
        """Open a response stream: status and headers, body still to come."""
        response = ApplicationResponse(
            status=status,
            headers=[Header("Content-Type", "application/json")],
            body=b"",
            end_body=False,
        )
        await self._send_service_request(ServiceFrame.response(stream_id, response))

    async def _send_body_chunk(self, stream_id: int, data: bytes, end: bool = False) -> None:
        await self._send_service_request(
            ServiceFrame.body_chunk(stream_id, BodyChunk(data=data, end_body=end))
        )

    async def _send_control_message(self, message: dict) -> None:
        await self._send_body_chunk(CONTROL_STREAM_ID, encode_message(message))

    async def _send_reset(self, stream_id: int, reason: str, code: ResetCode = ResetCode.PROTOCOL_ERROR) -> None:
        await self._send_service_request(
            ServiceFrame.reset(stream_id, Reset(code=code, reason=reason))
        )

    # -- routing -------------------------------------------------------------

    async def run(self) -> None:
        """Serve frames until the peer goes away."""
        log.info("session %s: entering service loop", self._state.vm_id)
        try:
            while not self.closed:
                try:
                    frame = await self._recv_frame()
                except NoiseProtocolError as exc:
                    log.error("session %s: decrypt failed: %s", self._state.vm_id, exc)
                    return
                if frame is None:
                    log.info("session %s: closed by peer", self._state.vm_id)
                    return
                try:
                    await self._on_frame(frame)
                except Exception as exc:  # noqa: BLE001 - one bad request must not kill the session
                    log.error("session %s: handler error: %s", self._state.vm_id, exc)
        finally:
            self.closed = True

    async def _on_frame(self, frame: ServiceFrame) -> None:
        kind = frame.kind
        if kind == "request":
            await self._on_request(frame)
        elif kind == "body_chunk":
            await self._on_body_chunk(frame)
        elif kind == "reset":
            log.warning("device reset stream %s: %s", frame.stream_id, frame.value.reason)
        elif kind == "response":
            # A device answering a request we made (e.g. a tunnel read).
            log.debug("device response on stream %s: HTTP %s", frame.stream_id, frame.value.status)
        else:
            log.warning("unexpected frame kind %r on stream %s", kind, frame.stream_id)

    async def _on_request(self, frame: ServiceFrame) -> None:
        request = frame.value
        path = request.path
        log.info("request %s %s (stream %s)", request.verb, path, frame.stream_id)

        if path == CONTROL_PATH:
            await self._send_response_headers(frame.stream_id, 200)
            return
        if path == IDENTITY_PATH:
            body = json.dumps(
                {
                    "node_id": self._state.node_id or f"forge-{self._state.vm_id[:8]}",
                    "display_name": self._state.display_name or "Muse Forge device",
                    "platform": self._state.platform or "unknown",
                    "version": self._state.model_id or "0.1.0",
                },
                separators=(",", ":"),
            ).encode()
            await self._send_application_response(frame.stream_id, 200, body)
            return
        if path == CHAT_PATH:
            await self._on_chat_request(frame)
            return
        if path == TUNNEL_PATH:
            # L3 tunnel. Stage 0 does not implement the home-network side; accept
            # the stream so the device's tunnel task sees "up" and stops retrying.
            log.info("tunnel stream opened (not implemented in stage 0)")
            await self._send_response_headers(frame.stream_id, 501)
            await self._send_body_chunk(frame.stream_id, b"", end=True)
            return

        await self._send_application_response(
            frame.stream_id, 404, b'{"error":"no such path"}'
        )

    async def _on_chat_request(self, frame: ServiceFrame) -> None:
        request = frame.value
        try:
            payload = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            await self._send_application_response(
                frame.stream_id, 400, b'{"error":"bad json"}'
            )
            return
        if self._on_chat is None:
            await self._send_application_response(
                frame.stream_id, 501, b'{"error":"no agent attached"}'
            )
            return
        try:
            result = await self._on_chat(payload)
        except Exception as exc:  # noqa: BLE001
            log.error("chat handler failed: %s", exc)
            await self._send_application_response(
                frame.stream_id, 500, b'{"error":"agent failure"}'
            )
            return
        body = json.dumps(result, separators=(",", ":")).encode()
        await self._send_application_response(frame.stream_id, 200, body)

    async def _on_body_chunk(self, frame: ServiceFrame) -> None:
        chunk = frame.value
        if frame.stream_id != CONTROL_STREAM_ID:
            log.debug("body chunk on stream %s (%d bytes)", frame.stream_id, len(chunk.data))
            if chunk.end_body:
                await self._send_body_chunk(frame.stream_id, b"", end=True)
            return
        for message in self._control_decoder.feed(chunk.data):
            await self._on_control_message(message)
        if chunk.end_body:
            log.warning("device closed the control stream")

    async def _on_control_message(self, message: dict) -> None:
        method = message.get("method")
        if method == "link.register":
            await self._on_register(message)
            return
        if method == "link.invoke":
            asyncio.create_task(self._on_invoke(message))
            return
        # An ack we did not ask for, or something we do not model yet.
        if message.get("method") is None and message.get("id") == self._register_id:
            if message.get("error"):
                log.error("link.register rejected: %s", message["error"])
            return
        log.debug("control message: %s", json.dumps(message)[:200])

    async def _on_register(self, message: dict) -> None:
        """Handle link.register: record identity, then acknowledge.

        The device sends `{"type":"req","id":...,"method":"link.register",
        "params":{...}}` and waits for a reply carrying the same id with no
        method. We learn the id from the request -- the server never picks it.
        """
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        self._register_id = message.get("id") or ""
        self._state.node_id = params.get("node_id", "") or self._state.node_id
        self._state.display_name = params.get("display_name", "") or self._state.display_name
        self._state.platform = params.get("platform", "") or self._state.platform
        self._state.model_id = params.get("model_id", "") or self._state.model_id
        self._state.registered_at = time.monotonic()
        log.info(
            "device registered: node_id=%s name=%s platform=%s",
            self._state.node_id, self._state.display_name, self._state.platform,
        )
        # Acknowledge. The client treats the matching id with no method as the
        # registration reply; an "error" key would mark it rejected.
        await self._send_control_message({"id": self._register_id})
        # registered=True is set by the server only once the device has had a
        # chance to act on the ack; /health reports it for debugging.
        self._state.registered = True

    async def _on_invoke(self, message: dict) -> None:
        invoke_id = message.get("id")
        command = message.get("command") or ""
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        timeout_ms = message.get("timeout_ms")
        if not invoke_id:
            return
        if self._command_handler is None:
            await self._send_control_message(
                {"method": "link.result", "id": invoke_id, "ok": False,
                 "error": "no command handler attached"}
            )
            return
        log.info("invoke %s", command)
        try:
            result = await self._command_handler(command, params, timeout_ms)
        except Exception as exc:  # noqa: BLE001
            log.error("command %s failed: %s", command, exc)
            result = {"ok": False, "error": str(exc)}
        await self._send_control_message({"method": "link.result", "id": invoke_id, **result})
