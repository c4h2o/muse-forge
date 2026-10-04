"""Muse Forge: an open-source stand-in for Meta's Muse gadget service.

Speaks enough of the wire protocol for a stock ESP32 running Meta's
open-source firmware to pair, complete the Noise XX handshake, register, and
exchange messages -- against a backend you control.

Apache-2.0. See LICENSE.
"""

__version__ = "0.1.0"

# The Noise pattern this server implements, per Meta's client SDK.
PROTOCOL_NAME = "Noise_XX_25519_AESGCM_SHA256"
