"""Negative controls: prove verify.py can actually fail.

A test suite that only ever prints PASS is worthless. This injects known bugs
into the protocol layer and checks that `verify.py` notices each one.

    python test_negative.py

Each case monkeypatches one thing, runs the real acceptance test, and asserts
that it reports a failure. If a case ever starts passing, the test has stopped
protecting the thing it was written to protect.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# verify.py sets up sys.path for both the server package and Meta's SDK, and
# raises a clear error if the SDK is missing. Import it first for both reasons.
import verify  # noqa: E402

import musegadget.noise.noise_xx as nxx  # noqa: E402


def _run_verify_quiet() -> tuple[int, str]:
    """Run verify.main() with logging suppressed; return (exit code, output)."""
    buf = io.StringIO()
    previous = logging.getLogger().level
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(buf):
            rc = asyncio.run(verify.main())
    finally:
        logging.disable(previous)
        logging.getLogger().setLevel(previous)
    return rc, buf.getvalue()


# -- the cases ---------------------------------------------------------------

def case_split_order() -> tuple[str, callable]:
    """Responder.split() must return (c2, c1).

    Breaking it yields a session that handshakes fine and then fails every
    decrypt -- the most confusing failure mode in this protocol.
    """
    def apply():
        def broken(self):
            c1, c2 = self._ss.split()
            self._phase = nxx._Phase.SPLIT
            return c1, c2  # wrong: should be (c2, c1)
        nxx.NoiseXXResponder.split = broken
    return "responder split() returns (c1, c2) instead of (c2, c1)", apply


def case_envelope_direction() -> tuple[str, callable]:
    """The server must wrap outbound frames in ServiceResponse.

    Wrapping in ServiceRequest is the natural mistake and produces
    `empty ServiceResponse payload` on the client.
    """
    def apply():
        import musegadget.noise.transport as t

        def wrong_envelope(frame):
            return t.encode_service_request(
                t.ServiceRequest(service=t.ServiceType.SERVICE_DAEMON,
                                 payload=t.encode_service_frame(frame))
            )
        t.encode_response_envelope = wrong_envelope
    return "server wraps outbound frames in ServiceRequest", apply


def case_message3_payload() -> tuple[str, callable]:
    """Message 3's payload must be empty on the owner path.

    A non-empty payload is what makes Meta's proxy drop sessions after the
    crypto handshake, and it is the documented cause of "established, then
    closed 60ms later with nothing in the log".
    """
    def apply():
        original = nxx.NoiseXXResponder.read_message3

        def strict(self, msg3):
            # reject any payload the way a strict proxy would
            original(self, msg3)
        # instead, break the *initiator* so it sends a payload
        orig_init = nxx.NoiseXXInitiator.write_message3

        def with_payload(self):
            self._s = nxx._generate_x25519_key_pair()
            enc_s = self._ss.encrypt_and_hash(self._s.public_key_bytes)
            se = nxx._x25519_dh(self._s.private_key, self._re)
            self._ss.mix_key(se)
            enc_payload = self._ss.encrypt_and_hash(b"surprise")
            self._phase = nxx._Phase.MSG3_SENT
            return enc_s + enc_payload
        nxx.NoiseXXInitiator.write_message3 = with_payload
    return "initiator sends a non-empty message 3 payload", apply


CASES = [
    case_split_order,
    case_envelope_direction,
    case_message3_payload,
]


def _restore() -> None:
    """Reload the noise modules so every case starts from a clean import."""
    for name in list(sys.modules):
        if name.startswith("muse_forge") or name.startswith("musegadget"):
            del sys.modules[name]
    import musegadget.noise.noise_xx  # noqa: F401
    import musegadget.noise.transport  # noqa: F401
    import muse_forge.noise_session  # noqa: F401
    import verify  # noqa: F401


def main() -> int:
    print("negative controls: each case injects a bug and expects verify.py to fail\n")
    survivors = []
    for factory in CASES:
        label, apply = factory()
        _restore()
        apply()
        try:
            rc, out = _run_verify_quiet()
        except Exception as exc:  # noqa: BLE001 - an exception is a detection too
            rc, out = 1, f"raised {type(exc).__name__}: {exc}"
        caught = rc != 0
        mark = "\033[92mCAUGHT\033[0m" if caught else "\033[91mMISSED\033[0m"
        print(f"  [{mark}] {label}")
        if not caught:
            survivors.append(label)

    print()
    if survivors:
        print(f"{len(survivors)} case(s) slipped through -- the suite is not protecting them:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print(f"all {len(CASES)} negative controls behaved correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
