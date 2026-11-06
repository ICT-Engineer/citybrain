"""Offline decision vault: decisions are never lost when the link to the Twin drops.

Two append-only JSON-lines files under data/ (created automatically):

  data/offline/pending_decisions.jsonl  every decision whose commands could not be delivered
                                        (state PENDING), and later its fate (SENT / EXPIRED / FAILED)
  data/journal/decisions.jsonl          every decision the Brain takes, delivered or not

Each line is written, flushed and fsync-ed before the Brain moves on, so a crash or power cut
right after a decision still leaves it on disk. Lines are hash-chained and HMAC-sealed with the
Brain's key: editing, deleting or reordering a line breaks the chain and `verify()` reports it.

When the link comes back the Brain re-sends PENDING commands with the SAME idempotent
request_id (the Twin applies a request_id only once), unless the decision is too old or its
incident has already cleared, in which case it is marked EXPIRED and kept for the audit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

PENDING_TTL_S = float(os.environ.get("OFFLINE_DECISION_TTL_S", "300"))


class SealedLog:
    def __init__(self, path: Path, key: bytes) -> None:
        self.path = path
        self.key = key
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "GENESIS"
        last = "GENESIS"
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    last = json.loads(line)["hash"]
                except (ValueError, KeyError):
                    continue
        return last

    def append(self, record: dict) -> dict:
        with self.lock:
            body = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
            h = hashlib.sha256((self.prev + body).encode()).hexdigest()
            line = {"prev": self.prev, "hash": h, "seal": hmac.new(self.key, h.encode(), hashlib.sha256).hexdigest(),
                    "record": record}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.prev = h
            return line

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"corrupt": line[:80]})
        return out

    def verify(self) -> dict:
        prev, n = "GENESIS", 0
        for i, line in enumerate(self.read()):
            if "corrupt" in line:
                return {"ok": False, "lines": i, "error": f"line {i + 1} is not valid JSON"}
            body = json.dumps(line["record"], sort_keys=True, separators=(",", ":"), default=str)
            h = hashlib.sha256((prev + body).encode()).hexdigest()
            if line["prev"] != prev or line["hash"] != h:
                return {"ok": False, "lines": i, "error": f"chain broken at line {i + 1}"}
            if not hmac.compare_digest(line["seal"], hmac.new(self.key, h.encode(), hashlib.sha256).hexdigest()):
                return {"ok": False, "lines": i, "error": f"seal invalid at line {i + 1}"}
            prev, n = h, n + 1
        return {"ok": True, "lines": n}


class OfflineVault:
    def __init__(self, root: Path, key: bytes) -> None:
        self.pending_log = SealedLog(root / "offline" / "pending_decisions.jsonl", key)
        self.journal = SealedLog(root / "journal" / "decisions.jsonl", key)
        self.items: dict[str, dict] = {}               # vault_id -> current state
        for line in self.pending_log.read():            # rebuild state after a restart
            r = line.get("record") or {}
            if r.get("vault_id"):
                self.items.setdefault(r["vault_id"], {}).update(r)
        self._n = len(self.items)

    def save(self, decision: dict, commands: list[dict], sensor_id: str) -> dict:
        self._n += 1
        vid = f"V{int(time.time())}-{self._n:04d}"
        rec = {"vault_id": vid, "state": "PENDING", "saved_at": time.time(), "sensor_id": sensor_id,
               "correlation_id": decision.get("correlation_id"), "incident": decision.get("incident"),
               "commands": commands, "attempts": 0, "reason": "TWIN_UNREACHABLE"}
        self.pending_log.append(rec)
        self.items[vid] = dict(rec)
        return rec

    def update(self, vid: str, **changes) -> None:
        self.items[vid].update(changes)
        self.pending_log.append({"vault_id": vid, **changes, "at": time.time()})

    def pending(self) -> list[dict]:
        return [v for v in self.items.values() if v.get("state") == "PENDING"]

    def summary(self) -> dict:
        states: dict[str, int] = {}
        for v in self.items.values():
            states[v["state"]] = states.get(v["state"], 0) + 1
        return {"states": states, "items": sorted(self.items.values(), key=lambda v: -v.get("saved_at", 0))[:20],
                "file": str(self.pending_log.path), "journal": str(self.journal.path),
                "integrity": self.pending_log.verify(), "journal_integrity": self.journal.verify(), "ttl_s": PENDING_TTL_S}
