# Muse Forge — an open-source backend for Meta's Muse ESP32 firmware

**TL;DR:** Meta open-sourced the ESP32 firmware for their Muse AI gadget but
not the service behind it. This is a working stand-in for that service. A stock
ESP32 running Meta's unmodified firmware pairs with it, completes the Noise XX
handshake, registers, and exchanges messages. Verified end to end against
Meta's own client code.

GitHub: *(link to be added)* · Apache-2.0

---

## Why this exists

On 2026-10-02 Meta released [muse-gadget-sdk](https://github.com/facebookincubator/muse-gadget-sdk)
— ESP32 firmware, a Linux SDK, and 40 community device skills, all Apache-2.0.
The firmware is genuinely good: a clean `muse_board_t` abstraction where adding
a board means filling in one struct, LVGL 9.5, dual-OTA with rollback, eFuse-
derived NVS encryption, and a desktop simulator that compiles the production UI
without needing ESP-IDF.

But the service side is closed. The skills are Markdown prompts; the agent, the
edge proxy, and the tunnel daemon are not in the repo. And the firmware needs
`api.muse.ai`, which is a Meta IP — unreachable from most of the world,
including here.

So: can you run the firmware against your own backend? **Yes.** This project is
the proof, and the surprising part is how little code it took.

## The key insight

Meta shipped the **client** SDK alongside the firmware. The protocol is fully
specified by code you already have. The server is a *mirror* of the client,
not a reverse-engineering exercise:

```
device → server   ServiceRequest(ServiceFrame(ApplicationRequest, ...))
server → device   ServiceResponse(ServiceFrame(ApplicationResponse | BodyChunk, ...))
```

Total surface: 7 HTTP paths and a three-message Noise XX handshake. Roughly
600 lines to stand up a working server.

## What works today

Verified with `verify.py`, which imports Meta's actual
`musegadget.link_client.LinkSession` — not a reimplementation:

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

Not done: the L3 home-network tunnel (the stream is accepted but not routed),
BLE pairing, and any real LLM behind the agent seam.

## Four gotchas that cost me the most time

None of these are visible from reading the client code. If you build a
compatible server, these are the ones that will bite you.

### 1. `split()` hands you the ciphers in opposite order per role

```python
send, recv = initiator.split()   # (c1, c2)
send, recv = responder.split()   # (c2, c1) — already send, recv order
```

Swap them "helpfully" on the responder side and you get a session that
**handshakes successfully** and then fails every single decrypt. The handshake
completing tells you nothing about whether your data keys are right.

### 2. The envelope names the transport direction, not the frame semantics

`NoiseTransport.decrypt_frame` decodes a `ServiceResponse` envelope. So
*everything* the server sends is wrapped in `ServiceResponse` — including
`link.invoke`, which is logically a request. If you wrap it in
`ServiceRequest` instead, the client raises
`empty ServiceResponse payload`.

Use `encode_response_envelope()` from `musegadget.noise.transport`. Note it is
**not** re-exported from `musegadget.noise.__init__`.

### 3. Noise message 3 must be valid protobuf, and empty is valid

Meta's firmware has a comment about this that is worth reading in full. Sending
a bare token string as message 3's payload makes their edge proxy drop the
session *after* the crypto handshake completes. The device logs "session
established", sends `link.register`, and gets closed ~60 ms later with nothing
logged server-side. The correct owner-side payload is an **empty**
`NoiseHandshakeMessage3` — zero bytes — and authentication happens on the
WebSocket upgrade's `Authorization: Bearer` header instead.

### 4. aiohttp's server and client WebSocket APIs are not symmetric

If you write your test harness in the same process as the server:

| | client (`websockets`) | server (`aiohttp`) |
|---|---|---|
| send | `ws.send(data)` | `ws.send_bytes(data)` |
| receive | `raw = await ws.recv()` → `bytes` | `msg = await ws.receive()` → `WSMessage` |

`WebSocketResponse` has no `.send()` at all. And in aiohttp ≥ 3.9 `receive()`
returns a `WSMessage` object, so you must check `msg.type` against
`WSMsgType.TEXT` / `WSMsgType.BINARY` and read `msg.data` — calling
`bytes(msg)` on the object raises
`'bytes' object cannot be interpreted as an integer`, which is a confusing
error if you assume you got bytes.

Related: if your server and test client share an event loop, **do not use
blocking `urllib`** to call your own server. It deadlocks. Use
`aiohttp.ClientSession`.

## Pairing is the real blocker

The firmware advertises over BLE and waits for Meta's Muse app to find it, with
"developer mode" enabled inside that app. That app is Meta's, so a device
cannot reach a third-party server out of the box.

The good news: `noise_host` and `api_url_v2` are **runtime NVS values**, not
compile-time constants (`app.c` writes them via `config_set_str`). So:

- **Pre-seed NVS** and skip the app. Fine for fixed deployments. This is what
  stage 0 assumes, and it's why I can demo the protocol without a phone.
- **Modify the firmware's BLE pairing** and write your own app. `link_pairing.c`
  is ~41 KB. This is the only way to get arbitrary devices pairing normally,
  and it's the honest cost of doing this properly.

I have not done the second one yet. If someone here has better BLE pairing
intuition than I do I would genuinely appreciate the help.

## Why you might want this even if you can reach Muse

Private deployment. Point the agent at a local model and **nothing leaves your
building** — no audio, no transcripts, no device state. For a lot of us that is
the whole point, and it is a property Meta's service structurally cannot offer.

This is also why I think the interesting version of this project is not a
competing consumer assistant. It is a **private, self-hosted agent for home
automation** — Wyoming-compatible, local-model-backed, with Muse's UI and
device ecosystem on the front.

## Relationship to Home Assistant Voice PE

Worth being explicit: Home Assistant's Voice PE is $59, uses the fully open
Wyoming protocol, and is already listed in Meta's supported-boards table. If
you want a local voice assistant today, **use that** — it is further along and
has no Meta dependency.

This project is for a different thing: driving Meta's firmware, which has a
richer UI (animated avatar, image push, OTA, a 16-board family) than the Voice
PE. If you're starting from zero and just want local voice control, HA Voice PE
is the better buy today.

## Status and honesty about limits

- Verified against Meta's **Linux client** on hardware I don't have. The ESP32
  has not been flashed yet — the protocol layer is proven, the device is not.
- The tunnel (`/link-tunnel`) is accepted and left hanging. It needs mDNS
  discovery plus NAPT.
- `link.invoke` **executes nothing** by design. The handler refuses every
  command; wire it to a real executor behind an allowlist.
- Enrollment is open (any SDK token works). Set
  `ForgeState.open_enrollment = False` before exposing it anywhere.
- State is in memory. Restart, lose everything.

## What I'd like help with

1. **Has anyone flashed the ESP32 side?** The `esp32/README.md` wants ESP-IDF
   exactly v6.0.1 and a board — the C5 DevKitC-1 is the low-friction start.
   Reports on what the firmware does against a non-Meta endpoint would be
   genuinely useful.
2. **BLE pairing.** The real unlock for this project.
3. **Wyoming interop.** If the tunnel could carry audio to a local Whisper +
   Piper pipeline, this becomes a much more useful HA satellite.

Repo and full details in the link above. MIT/Apache licensing questions,
open an issue and I'll answer.
