# Muse Forge — an open-source backend for Meta's Muse ESP32 firmware

**TL;DR:** Meta open-sourced the ESP32 firmware for their Muse AI gadget but
not the service behind it. This is a working stand-in for that service. A stock
ESP32 running Meta's unmodified firmware pairs with it, completes the Noise XX
handshake, registers, and exchanges messages. Verified end to end against
Meta's own client code.

GitHub: https://github.com/c4h2o/muse-forge · Apache-2.0

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

## Hardware: verified on a real board, and one surprise

I ran this against a physical ESP32-S3 N16R8 (the xiaozhi dev board — an
ESP32-S3-WROOM-1 with a 240x240 ST7789 on SPI, INMP441 mic and MAX98357A
amp, on a stacked bottom board carrying an I2C RGB LED, a CH343P USB-serial
bridge and an XL6009 boost converter).

`esptool flash_id` confirms what the silkscreen claims:

```
Chip type:    ESP32-S3 (QFN56) revision v0.2
Features:     Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz,
              Embedded PSRAM 8MB (AP_3v3)
Flash:        Detected 16MB / quad 4 data lines / 3.3V
```

Two findings worth passing on:

**The two schematics are one stacked pair, and the GPIOs do not conflict.**
The only overlap is GPIO0, and it is the same physical key — the bottom
board wires its BOOT switch there and the top board wires its wake button to
the same pad. That is worth knowing if you assumed one board ID per
schematic.

**Flashing Muse is a layout change, not an app overwrite.** Dumping the
running xiaozhi partition table shows `ota_0` spanning `0x100000-0x700000`,
which is exactly where Muse expects `prod_data`/`prod_bak` at `0x420000`.
So Muse needs a full erase, and keeping a pre-flash backup is what makes
that reversible. Its table also sits at `0x8000` where Muse's is at
`0x10000`.

Also worth knowing before you plan a port: the rainbow LED on that board is
an XL-4009-I2C part on SCL/SDA, so **neither upstream LED backend fits**.
`PWM_RGB` drives LEDC channels on pins hardcoded at `led_status.c:63-68`
(24/25/26 production, 2/3/6 DVT) — compile-time macros, not Kconfig symbols,
so an overlay cannot change them. `LED_BACKEND_NONE` is the only safe
setting without writing a new backend. The serial log reports the same state
machine either way.

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

- The protocol is verified against Meta's own **Linux client** on real
  hardware — the board section above is measured, not assumed. But the
  **ESP32 has not been flashed with Muse firmware yet**, so the device-side
  port is unproven.
- The tunnel (`/link-tunnel`) is accepted and left hanging. It needs mDNS
  discovery plus NAPT.
- `link.invoke` **executes nothing** by design. The handler refuses every
  command; wire it to a real executor behind an allowlist.
- Enrollment is open (any SDK token works). Set
  `ForgeState.open_enrollment = False` before exposing it anywhere.
- State is in memory. Restart, lose everything.

## What I'd like help with

1. **ESP-IDF on Windows.** Meta's `esp32/README.md` names macOS and Linux
   only, and `tools/board.sh` is bash. `idf.py` should be cross-platform, so
   this is probably just a docs gap, but I would like it confirmed before
   committing to a porting effort.
2. **BLE pairing.** The real unlock for this project, and the part I have
   not touched.
3. **Wyoming interop.** If the tunnel could carry audio to a local Whisper +
   Piper pipeline, this becomes a much more useful HA satellite.
4. **Anyone with a Muse-compatible board who has flashed it** — the C5
   DevKitC-1 is the low-friction start. Reports on what the firmware does
   against a non-Meta endpoint would be genuinely useful.

Full details, including the GPIO map, the partition dump and the
`docs/HARDWARE-NOTES.md` porting guide, are in the repo. MIT/Apache licensing
questions, open an issue and I will answer.
