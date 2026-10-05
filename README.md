# Muse Forge

**An open-source stand-in for Meta's Muse gadget service.** A stock ESP32
running Meta's own firmware pairs with it, completes the Noise XX handshake,
registers, and exchanges messages — against a backend you control.

Built as a proof of concept during stage 0 of an evaluation of Meta's
[Muse Gadgets SDK](https://github.com/facebookincubator/muse-gadget-sdk)
(Apache-2.0, released 2026-10-02). The firmware is Meta's; everything on this
side of the wire is ours.

```
   ESP32 (stock Meta firmware)          Muse Forge (this repo)
   ────────────────────────────         ────────────────────────
   BLE pairing  ──────────���────────►  (not implemented yet)
   /fetch_vms   ──── HTTPS ────────►  lease a VM + token
   /v1/noise    ──── WSS  ─────────►  Noise XX session
     link.register ────────────────►  register the node
     /chat/stream ────────────────►  your agent
```

## Status: stage 0

Working, and verified end to end against Meta's own client code:

- [x] `Noise_XX_25519_AESGCM_SHA256` three-message handshake
- [x] ServiceFrame multiplexing (`request` / `response` / `body_chunk` / `reset`)
- [x] `link.register` + acknowledgement
- [x] `/chat/stream` request/response
- [x] `link.invoke` routing (dispatch is wired; execution is off by default)
- [ ] L3 home-network tunnel (`/link-tunnel`) — stream is accepted, not routed
- [ ] BLE pairing — this is the hard part, see [Pairing](#pairing)
- [ ] Real agent — replace `EchoAgent` with your LLM
- [ ] ESP32 on-hardware test — verified against Meta's Linux client only

## Quick start

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# self-test: starts a server, runs Meta's client against it
python verify.py
```

Expected output:

```
1. bootstrap: mint a device token
  [PASS] mint returns HTTP 2xx
  [PASS] mint returns access + refresh tokens
2. bootstrap: fetch_vms leases a VM
  [PASS] fetch_vms returns a leaseable VM
  [PASS] VM carries vm_id and vm_auth_token
  [PASS] VM advertises a wss:// URL
3. noise session: handshake + register
  [PASS] link.register acknowledged
4. chat: a message round-trips
  [PASS] send_chat got HTTP 2xx
  [PASS] agent received the message
RESULT: all checks passed -- the server is byte-compatible
```

`verify.py` imports `musegadget.link_client.LinkSession` — Meta's real client,
not a reimplementation. A pass means the server is byte-compatible with stock
firmware. Point it elsewhere with `MUSEGADGET_SRC=/path/to/muse-gadget-sdk/linux/src`.

## Running it

```sh
python -m muse_forge --host 0.0.0.0 --port 8443 \
    --cert cert.pem --key key.pem --noise-host forge.local:8443
```

The ESP32 wants `wss://`, so TLS is effectively mandatory for real hardware.
For LAN experiments a self-signed CA works if you add it to the firmware's
trust store.

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + connected devices |
| `GET /fetch_vms` | lease a VM (`vm_id` + `vm_auth_token`) |
| `POST /device_token/mint` | SDK token → device token pair |
| `POST /device_token/refresh` | rotate the device token pair |
| `WS /v1/noise` | Noise XX session; bearer in the `Authorization` header |

## How it works

The interesting part is how little there is. Meta published the client SDK
along with the firmware, so **the server is a mirror of the client** rather
than something reverse-engineered. The protocol is:

```
device → server   ServiceRequest(ServiceFrame(ApplicationRequest, ...))
server → device   ServiceResponse(ServiceFrame(ApplicationResponse | BodyChunk, ...))
```

Handshake, from `linux/src/musegadget/noise/noise_xx.py`:

1. client → `e` (32 bytes, unencrypted)
2. server → `e, enc(s), enc(payload)`
3. client → `enc(s), enc(payload)` — empty payload; the bearer on the
   WebSocket upgrade is what authenticates, not anything in message 3

Then `HKDF` splits the chaining key into two AES-GCM ciphers.

## Four things that will bite you

These cost me most of the debugging time, and none of them are obvious from
reading the client code.

### 1. `split()` returns ciphers in opposite order for each role

```python
# initiator
send, recv = initiator.split()      # (c1, c2)

# responder — the SDK already returns (c2, c1), i.e. already in send, recv order
send, recv = responder.split()
```

If you "helpfully" swap them again you get a session that handshakes fine and
then fails every decrypt with `CipherState: decrypt failed`. The handshake
succeeding is not evidence the keys are right for data.

### 2. The envelope names the direction, not the semantics

`NoiseTransport.decrypt_frame` decodes a **ServiceResponse** envelope. So
everything the server sends — including `link.invoke`, which is logically a
request — is wrapped in `ServiceResponse`. Use the SDK's own
`encode_response_envelope()` from `musegadget.noise.transport`; it is not
re-exported from `musegadget.noise.__init__`, so import it from
`.transport`. Wrapping in `ServiceRequest` gets you
`empty ServiceResponse payload` on the client.

### 3. Message 3 must be valid protobuf — and empty is valid

Meta's firmware comment on this is worth repeating: sending a bare token
string as message 3's payload makes their edge proxy drop the session
*after* the crypto handshake completes. The device sees "session
established", sends `link.register`, and gets closed ~60 ms later with
nothing logged server-side. The correct owner payload is an empty
`NoiseHandshakeMessage3`, which encodes to zero bytes.

### 4. aiohttp server and client WS APIs are not symmetric

This cost me three debugging cycles, so if you write a test harness in the
same process as the server:

| | client (`websockets`) | server (`aiohttp`) |
|---|---|---|
| send | `ws.send(bytes)` | `ws.send_bytes(data)` |
| receive | `raw = await ws.recv()` → `bytes` | `msg = await ws.receive()` → `WSMessage` |

`WebSocketResponse` has no `.send()`. And `receive()` returns a `WSMessage`
object in aiohttp ≥ 3.9 — test with `msg.type` against `WSMsgType.TEXT` /
`WSMsgType.BINARY`, and read `msg.data`. `bytes(msg)` on the object itself
raises `'bytes' object cannot be interpreted as an integer`.

Also: if your test server shares the event loop with the test client, do not
use blocking `urllib` to call it. It deadlocks. Use `aiohttp.ClientSession`.

## Pairing

The firmware announces itself over BLE and waits for the Muse app to discover
it, with developer mode enabled in that app. That app is Meta's, so out of the
box a device cannot reach this server — the NVS keys `noise_host` and
`api_url_v2` have to point here first.

They are runtime configuration, not compile-time constants (`app.c` writes
them with `config_set_str`), so there are two honest options:

- **Pre-seed NVS** and skip the app entirely. Fine for a fixed deployment;
  this is the path stage 0 assumes.
- **Change the firmware's BLE pairing** and write your own app. Real work —
  `link_pairing.c` is ~41 KB — but it is the only way to get arbitrary
  devices pairing normally.

## Using a real agent

`EchoAgent` in `__main__.py` is the seam. It takes the request dict and returns
a JSON-serialisable dict; the device renders `reply` as captions.

```python
class MyAgent:
    async def __call__(self, payload: dict) -> dict:
        reply = await my_llm(payload["message"])
        return {"reply": reply}

app = build_app(ForgeState(), MyAgent())
```

Private deployment is the interesting case here: point the agent at a local
model and nothing leaves the building. That is the property Meta's service
cannot offer you, and it is the reason this project is worth building.

## Security notes

This is a proof of concept and it is honest about its limits:

- **Pairing has no manufacturer verification** and cannot stop an active
  MITM. Same caveat Meta gives.
- **Enrollment is open** — any SDK token is accepted. Set
  `ForgeState.open_enrollment = False` before exposing this anywhere.
- **`link.invoke` executes nothing.** The handler refuses every command. Wire
  it to a real executor behind an allowlist; `system.run` on a freshly paired
  box is not a sane default.
- **State is in memory.** Restart and every pairing is gone.

## Porting to your own board

[`docs/HARDWARE-NOTES.md`](docs/HARDWARE-NOTES.md) covers what it takes to run
this firmware on a board Meta does not list, based on an ESP32-S3 N16R8:

- which `muse_board_t` constraints are already met by a generic S3 board
- why `CONFIG_HOMEHUB_BUTTON_GPIO=0` is upstream practice, not a hack
- how to check a stacked board pair for GPIO conflicts
- **flashing Muse is a layout change, not an app overwrite** — dump the
  partition table first, keep a backup
- why neither upstream LED backend fits an I2C RGB LED
- the audio architecture gap (`muse_board_t` expects a codec chip; bare I2S
  parts need a custom `esp_codec_dev` data interface)

There is also a `xiaozhi-s3-light.sdkconfig` overlay in this repo — 22 symbols,
21 cross-checked against official 16 MB ESP32-S3 overlays — as a starting
point for a no-display, no-audio bring-up.

## License

Apache-2.0, matching the Muse SDK. `muse-forge/` is my own work; it imports
`musegadget` from Meta's SDK rather than vendoring it.
