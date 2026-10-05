# Hardware notes: porting Muse to a non-Meta ESP32-S3 board

Notes from getting Meta's firmware onto a third-party board. These are
conclusions and method, not a lab diary — if you want the parts I got wrong
first, that is not in here.

Tested on the **xiaozhi ESP32-S3 N16R8** dev board: an ESP32-S3-WROOM-1
(16 MB flash / 8 MB PSRAM) with a 240x240 ST7789 on SPI, an INMP441 I2S mic and
a MAX98357A I2S amp, sitting on a stacked bottom board that carries an I2C
RGB LED, a CH343P USB-serial bridge, an XL6009 boost converter and a passive
buzzer.

---

## 1. The board is already supported at the chip level

Meta ships 16 supported boards, and the constraints line up:

| Requirement | This board | Source |
|---|---|---|
| 16 MB flash | 16 MB, quad, 3.3 V | `esptool flash_id` |
| PSRAM for the home-network tunnel | 8 MB, embedded | `esptool flash_id` |
| ST7789 driver | already present in 4 upstream boards | `board_aipi.c:161`, `board_cardputer_adv.c:189`, `board_stickc_plus2.c`, `board_sticks3.c` |
| SPI panel IO | matches `esp_lcd_new_panel_io_spi` | — |

Confirm your own board rather than trusting a silkscreen:

```sh
python -m esptool --chip esp32s3 -p <PORT> --before default_reset --after no_reset flash_id
```

Expect `Embedded PSRAM 8MB` and `Detected flash size: 16MB` on an N16R8.

### Partition table: do not override it

16 MB boards do **not** override the partition table upstream. `partitions.csv`
ends at `0x422000` (4.13 MB) and simply leaves the rest unallocated, which is
fine on a 16 MB part. The only cost is 2 MB OTA slots instead of 4 MB.

Switching to `partitions_muse.csv` gains the bigger slots but is a **re-flash
boundary**: `prod_data`/`prod_bak` sit at fixed offsets (`0x420000` /
`0x421000`) written during manufacturing, and a device flashed with the old
table cannot OTA across the change. Decide before the first flash.

---

## 2. GPIO0 as the talk button is upstream practice

Pairing confirms via the talk key, not a BOOT-specific path:

```c
// esp32/main/app.c:2314
bool app_confirm_pairing_press(void) {
    ...
    ESP_LOGI(TAG, "talk button confirmed active pairing session");
}
```

Four official boards already set `CONFIG_HOMEHUB_BUTTON_GPIO=0` — `ideaspark`,
`espressif-box-3`, `cardputer-adv`, `home-assistant-voice`. So:

```
CONFIG_HOMEHUB_BUTTON_GPIO=0
```

is all a board with a GPIO0 button needs. `button.c:59` reads
`gpio_get_level(BTN_GPIO) == 0`, and `muse_gpio_button_init()` sets up the
active-low internal pull-up, which suits a strapping pin that is pulled up
anyway.

**One operational caveat.** GPIO0 is an ESP32-S3 strapping pin, so holding the
button while powering on or resetting selects download mode. Pressing it while
running is harmless.

---

## 3. The two schematics are one stacked pair, and they do not conflict

If your board is a top/bottom stack, check every pin on both before assuming
there is a conflict. Here the only overlap is GPIO0, and it is the same
physical key:

| GPIO | Bottom board | Top board |
|---|---|---|
| **GPIO0** | BOOT switch | wake button — **same pad** |
| GPIO8 / GPIO9 | I2C RGB LED | — |
| GPIO19 / GPIO20 | USB-OTG | — |
| GPIO46 | TS3A serial mux | — |
| GPIO4/5/6/7/15/16 | — | INMP441 + MAX98357A |
| GPIO10-14 | — | ST7789 SPI |
| GPIO39 / GPIO40 | — | volume keys |

Everything else is disjoint. A single
`CONFIG_HOMEHUB_BUTTON_GPIO=0` covers both boards.

---

## 4. Flashing Muse is a layout change, not an app overwrite

Dumping the partition table of the firmware that was already on the board
showed why this matters:

| Label | Offset | Size |
|---|---|---|
| `nvs` | `0x009000` | 16 KB |
| `otadata` | `0x00d000` | 8 KB |
| `phy_init` | `0x00f000` | 4 KB |
| `model` | `0x010000` | 960 KB |
| `ota_0` | `0x100000` | 6144 KB |
| `ota_1` | `0x700000` | 6144 KB |

`ota_0` spans `0x100000`–`0x700000`, which is exactly where the Muse firmware
expects `prod_data`/`prod_bak` at `0x420000`. The existing firmware's table is
also at `0x8000` where Muse's is at `0x10000`.

**So Muse needs a full erase.** Keep a pre-flash backup; it is the only way back
to the original layout:

```sh
# back up first
python -m esptool --chip esp32s3 -p <PORT> --before default_reset --after no_reset \
  --baud 921600 read-flash 0x0 0x1000000 xiaozhi-backup.bin

# Muse will overwrite the layout, so erase before flashing
python -m esptool --chip esp32s3 -p <PORT> erase_flash
```

esptool 5.x takes underscore option names (`default_reset`, `no_reset`). Meta's
`esp32/README.md` uses the older hyphenated form, which still works but warns.

Verify the backup is real rather than trusting the file size — a dump of an
empty chip is all `0xFF`:

```sh
python -c "d=open('xiaozhi-backup.bin','rb').read(); print(len(d), sum(1 for b in d if b!=0xFF))"
```

---

## 5. The status LED needs a new backend

Neither upstream LED backend fits an I2C RGB LED. The board here has an
XL-4009-I2C part on SCL/SDA, with the three colours as register writes rather
than three PWM channels.

| Backend | How it drives the LED | Verdict |
|---|---|---|
| `PWM_RGB` | LEDC channels on pins **hardcoded** in `main/led_status.c:63-68` (24/25/26 production, 2/3/6 DVT) | ✗ wrong pins, and these are compile-time macros, not Kconfig symbols, so an overlay cannot change them |
| `DEVKIT_GPIO27` | single-wire addressable (WS2812-style) | ✗ wrong protocol |
| `NONE` | no LED | ✓ **the only safe setting without writing code** |

`CONFIG_HOMEHUB_LED_BACKEND_NONE=y` costs you nothing during bring-up: the
state machine still reports over the serial log — orange breathing for setup,
blue for the pairing press, green for connected. Lighting the LED later means
adding a backend that writes the XL-4009 PWM registers, roughly half a day.

Check what your LED actually is before assuming either backend applies.

---

## 6. Audio is the real porting job

This is the one genuine architecture difference, and it is worth knowing about
before you start.

`muse_board_t.audio_init()` expects an **audio codec chip**:

```c
// components/muse/boards/board_aipi.c:246-248
audio_codec_i2s_cfg_t i2s_cfg = { .port = I2S_NUM_0, .rx_handle = rx, .tx_handle = tx };
const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_cfg);
audio_codec_i2c_cfg_t i2c_cfg = { .port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, ... };
```

Upstream boards use ES8311 or ES7210, configured over I2C. A board with bare I2S
parts — an INMP441 mic, a MAX98357A amp — has no codec to configure and no I2C
control line, and there is no upstream driver for either part.

You can still do it: `esp_codec_dev` accepts a custom data interface, so wire
I2S TX to the amp and I2S RX to the mic, skip the I2C codec step, and handle
volume by toggling the MAX98357A `GAIN` pin (it exposes several levels) rather
than through the codec. Budget about a day.

**Do this last.** `audio_init` can return `ESP_ERR_NOT_SUPPORTED` and the UI
degrades. Get the protocol, pairing and tunnel working first — that is where
the actual risk is.

---

## 7. Suggested order

| Step | Scope | Why first |
|---|---|---|
| 1 | Light build: one button, no screen, no audio | Proves compile, flash, pairing, and the tunnel. Lowest risk. |
| 2 | Add the panel | Copy `board_aipi.c`, change resolution and pins. Offsets vary per module, expect to tune `invert_color` / `mirror`. |
| 3 | Add audio | The only real architecture work. |

`devices/sdkconfig.xiaozhi-s3-light` in this repo is a step-1 overlay: 22
symbols, 21 of them cross-checked against official 16 MB ESP32-S3 overlays.

---

## 8. Windows build

Meta's `esp32/README.md` names macOS and Linux only, and `tools/board.sh` is
bash. `idf.py` itself is cross-platform, so this looks like a docs gap rather
than a hard blocker, but it is unverified. If it does get in the way:

```sh
idf.py -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;devices/sdkconfig.<yourboard>" build
```

bypasses `board.sh` entirely — it only reads `sdkconfig.*` files. WSL2 is the
fallback if native Windows does not work out.
