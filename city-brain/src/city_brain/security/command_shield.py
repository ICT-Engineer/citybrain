"""Command shield: protects every piece of data the Brain sends to the Twin.

Layers, applied in this order before a command leaves the Brain:
  1. Allowlist validation   - device exists in the shared Twin catalog, is supported, the command
                              is one it accepts, parameters have the exact keys, types and ranges.
                              Anything else is blocked locally and never sent.
  2. Rate limit             - at most RATE_LIMIT commands per device per minute (stops flapping or
                              a runaway loop from hammering an actuator).
  3. Transport check        - partner URLs must be HTTPS unless they point at this machine.
  4. Signature              - HMAC-SHA256 over method, path, timestamp, nonce and the SHA-256 of
                              the exact JSON body. Sent as X-CityBrain-* headers. A receiver can
                              check integrity (body not altered), authenticity (shared key) and
                              freshness (timestamp window + single-use nonce, so no replays).
The Twin layer does not check these headers yet; the Brain's built-in simulator does, to show
how the Twin should. Secrets never appear in logs or on the dashboard (only a key id).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from city_brain import catalog

RATE_LIMIT = 6                      # commands per device per 60 s
MAX_SKEW_S = 30                     # accepted clock difference for a signed request
KEY_FILE = Path(os.environ.get("BRAIN_KEY_FILE", str(Path(os.environ.get("BRAIN_DATA_DIR", "data")) / "secrets" / "brain_signing.key")))

PARAM_RULES = {
    ("pump", "start"): {"speed_pct": (0, 100)},
    ("pump", "set_speed"): {"speed_pct": (0, 100)},
    ("pump", "stop"): {},
    ("valve", "set_opening"): {"open_pct": (0, 100)},
    ("signal", "set_priority_corridor"): {},
    ("signal", "set_mode"): {"mode": ("normal", "priority", "stop")},
    ("signal", "set_phase"): {"phase": ("all_red",)},
    ("feeder", "open"): {}, ("feeder", "close"): {},
    ("responder", "dispatch"): {"destination_node_id": "id", "route_road_ids": "route", "mission_ref": "id?"},
    ("responder", "release"): {"mission_ref": "id"},
}


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_key() -> bytes:
    """BRAIN_SIGNING_KEY env var, else a key file created once with owner-only permissions."""
    env = os.environ.get("BRAIN_SIGNING_KEY")
    if env:
        return env.encode("utf-8")
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            os.chmod(KEY_FILE, 0o600)
        except OSError:
            pass
    return KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


def key_id(key: bytes) -> str:
    return "cb-" + hashlib.sha256(key).hexdigest()[:10]


def sign(key: bytes, method: str, path: str, body: dict, ts: int | None = None, nonce: str | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or secrets.token_hex(12)
    digest = hashlib.sha256(canonical(body)).hexdigest()
    msg = f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{digest}".encode()
    return {
        "X-CityBrain-Key-Id": key_id(key),
        "X-CityBrain-Timestamp": str(ts),
        "X-CityBrain-Nonce": nonce,
        "X-CityBrain-Body-SHA256": digest,
        "X-CityBrain-Signature": hmac.new(key, msg, hashlib.sha256).hexdigest(),
    }


class SignatureVerifier:
    """Receiver side (used by the built-in Twin simulator; reference for the Twin team)."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, method: str, path: str, body: dict, headers) -> tuple[bool, str]:
        h = {k.lower(): v for k, v in headers.items()}
        try:
            ts, nonce, sig = int(h["x-citybrain-timestamp"]), h["x-citybrain-nonce"], h["x-citybrain-signature"]
        except (KeyError, ValueError):
            return False, "SIGNATURE_MISSING"
        if abs(time.time() - ts) > MAX_SKEW_S:
            return False, "SIGNATURE_EXPIRED"
        digest = hashlib.sha256(canonical(body)).hexdigest()
        if not hmac.compare_digest(digest, h.get("x-citybrain-body-sha256", "")):
            return False, "BODY_TAMPERED"
        expected = hmac.new(self.key, f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{digest}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False, "SIGNATURE_INVALID"
        with self._lock:
            now = time.time()
            self._seen = {n: t for n, t in self._seen.items() if now - t < 2 * MAX_SKEW_S}
            if nonce in self._seen:
                return False, "NONCE_REPLAYED"
            self._seen[nonce] = now
        return True, "OK"


class CommandShield:
    def __init__(self) -> None:
        self.key = load_key()
        self.key_id = key_id(self.key)
        self._recent: dict[str, deque] = {}
        self._counted: dict[str, float] = {}          # request_ids already counted (retries are not new actions)
        self.stats = {"validated": 0, "signed": 0, "blocked_invalid": 0, "blocked_rate": 0, "last_block": None,
                      "last_signature": None}

    def validate(self, device_id: str, command: str, params: dict) -> str | None:
        """Return None if the command is allowed, else a reason code."""
        act = catalog.actuators().get(device_id)
        if act is None:
            return "UNKNOWN_DEVICE"
        if not act.supported or act.target_id is None:
            return "UNSUPPORTED_DEVICE"
        if command not in act.commands:
            return "COMMAND_NOT_ALLOWED"
        rules = PARAM_RULES.get((act.family, command))
        if rules is None:
            return "COMMAND_NOT_ALLOWED"
        required = {k for k, r in rules.items() if r != "id?"}
        if not required <= set(params) or set(params) - set(rules):
            return "BAD_PARAMETERS"
        for k, rule in rules.items():
            if k not in params:
                continue
            v = params[k]
            if isinstance(rule, tuple) and len(rule) == 2 and all(isinstance(x, (int, float)) for x in rule):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not rule[0] <= v <= rule[1]:
                    return "PARAMETER_OUT_OF_RANGE"
            elif isinstance(rule, tuple):
                if v not in rule:
                    return "PARAMETER_NOT_ALLOWED"
            elif rule in ("id", "id?"):
                if not isinstance(v, str) or not _ident(v):
                    return "BAD_IDENTIFIER"
            elif rule == "route":
                roads = catalog.road_graph()
                if not isinstance(v, list) or len(v) > 5 or any(r not in roads for r in v):
                    return "BAD_ROUTE"
        return None

    def rate_ok(self, device_id: str) -> bool:
        now = time.time()
        q = self._recent.setdefault(device_id, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True

    def check(self, device_id: str, command: str, params: dict, request_id: str | None = None) -> str | None:
        reason = self.validate(device_id, command, params)
        now = time.time()
        self._counted = {k: t for k, t in self._counted.items() if now - t < 600}
        if reason:
            self.stats["blocked_invalid"] += 1
        elif request_id is not None and request_id in self._counted:
            pass                                       # re-send of the same idempotent request: not a new action
        elif not self.rate_ok(device_id):
            reason = "RATE_LIMITED"
            self.stats["blocked_rate"] += 1
        else:
            self.stats["validated"] += 1
            if request_id is not None:
                self._counted[request_id] = now
        if reason:
            self.stats["last_block"] = {"device_id": device_id, "command": command, "reason": reason, "ts": time.time()}
        return reason

    def headers(self, method: str, path: str, body: dict) -> dict[str, str]:
        h = sign(self.key, method, path, body)
        self.stats["signed"] += 1
        self.stats["last_signature"] = {"nonce": h["X-CityBrain-Nonce"], "body_sha256": h["X-CityBrain-Body-SHA256"][:16],
                                        "ts": int(h["X-CityBrain-Timestamp"])}
        return h

    @staticmethod
    def transport(url: str) -> dict:
        u = urlparse(url)
        local = u.hostname in ("127.0.0.1", "localhost", "::1")
        return {"scheme": u.scheme, "local": local, "ok": u.scheme == "https" or local,
                "note": "HTTPS" if u.scheme == "https" else ("same machine" if local else "PLAIN HTTP over the network: use TLS")}


def _ident(v: str) -> bool:
    import re
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", v) is not None


_shield: CommandShield | None = None


def get_shield() -> CommandShield:
    global _shield
    if _shield is None:
        _shield = CommandShield()
    return _shield
