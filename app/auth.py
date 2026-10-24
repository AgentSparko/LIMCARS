"""Password gate for mutating endpoints, with escalating per-IP lockout.

Design (user's spec): one shared password; 3 wrong tries -> 60 s freeze,
next 3 -> 5 min, next 3 -> 30 min, further -> 2 h cap. Counter resets on a
successful auth. State persists in data/auth.json so restarts do not clear
locks. Localhost is exempt (checked server-side); this box is the admin
domain. Passwords are stored salted-SHA256, compared with hmac.compare_digest.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# (min_failures_since_success, lock_seconds) — highest matching tier wins
TIERS = ((12, 7200.0), (9, 1800.0), (6, 300.0), (3, 60.0))


class AuthGate:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._now = time.time
        self.salt: str | None = None
        self.hash: str | None = None
        self.ips: dict[str, dict] = {}   # ip -> {"n": failures, "until": ts}
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.salt, self.hash = d.get("salt"), d.get("hash")
            self.ips = {k: {"n": int(v.get("n", 0)), "until": float(v.get("until", 0.0))}
                        for k, v in (d.get("ips") or {}).items()}
        except (OSError, ValueError):
            pass

    # --- persistence ---

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # audit: 0664 left the scrypt hash + lockout state world-readable.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"salt": self.salt, "hash": self.hash, "ips": self.ips}, f, indent=1)
        os.replace(tmp, self.path)

    # --- password ---

    @property
    def enabled(self) -> bool:
        return bool(self.hash)

    def _digest(self, pw: str) -> str:
        return hashlib.sha256(((self.salt or "") + pw).encode("utf-8")).hexdigest()

    def _digest_scrypt(self, pw: str) -> str:
        # audit L: single-round sha256 is brute-forceable offline if
        # data/auth.json leaks; new hashes use scrypt. "s2:" prefix marks the
        # format so pre-existing sha256 entries keep verifying until reset
        return "s2:" + hashlib.scrypt(
            pw.encode("utf-8"), salt=(self.salt or "").encode("utf-8"),
            n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024).hex()

    def _matches(self, pw: str) -> bool:
        want = self.hash or ""
        got = (self._digest_scrypt(pw) if want.startswith("s2:")
               else self._digest(pw))
        return hmac.compare_digest(want, got)

    def verify(self, pw: str) -> bool:
        if not self.enabled or not pw:
            return False
        return self._matches(pw)

    def set_password(self, new: str | None, current: str | None = None) -> bool:
        """Set/change/disable. Changing an existing password requires `current`
        (callers enforce this for remote IPs; admin/localhost may overwrite).
        Empty/None `new` disables the gate. Returns False if current is wrong."""
        with self._lock:
            if self.enabled and current is not None and not self.verify(current):
                return False
            if new:
                self.salt = secrets.token_hex(16)
                self.hash = self._digest_scrypt(new)
            else:
                self.salt = self.hash = None
            self.ips = {}
            self._save()
            return True

    # --- lockout ---

    def locked_for(self, ip: str) -> float:
        """Seconds of lockout remaining for ip (0.0 when free)."""
        with self._lock:
            ent = self.ips.get(ip)
            if not ent:
                return 0.0
            return max(0.0, ent["until"] - self._now())

    def failure(self, ip: str) -> float:
        """Record a wrong password; returns the lock just applied (0 if none)."""
        with self._lock:
            ent = self.ips.setdefault(ip, {"n": 0, "until": 0.0})
            ent["n"] += 1
            lock = 0.0
            for mn, secs in TIERS:
                if ent["n"] >= mn and (ent["n"] - mn) % 3 == 0:
                    lock = secs
                    break
            if lock:
                ent["until"] = self._now() + lock
            self._save()
            return lock

    def success(self, ip: str) -> None:
        with self._lock:
            if self.ips.pop(ip, None) is not None:
                self._save()

    def attempt(self, ip: str, pw: str) -> tuple[bool, float]:
        """Lockout-check + verify + ladder update in ONE lock hold.

        The server used to call locked_for() then verify() then failure() as
        three separate lock acquisitions; with ThreadingHTTPServer's
        thread-per-connection, parallel guesses read the same failure count
        and cap the ladder at level 1 forever (audit H1). This is the only
        auth path mutating endpoints may use. Returns (ok, locked_remaining).
        """
        with self._lock:
            ent = self.ips.get(ip)
            if ent:
                rem = max(0.0, ent["until"] - self._now())
                if rem > 0:
                    return False, rem
            if self.enabled and pw and self._matches(pw):
                if self.ips.pop(ip, None) is not None:
                    self._save()
                return True, 0.0
            ent = self.ips.setdefault(ip, {"n": 0, "until": 0.0})
            ent["n"] += 1
            lock = 0.0
            for mn, secs in TIERS:
                if ent["n"] >= mn and (ent["n"] - mn) % 3 == 0:
                    lock = secs
                    break
            if lock:
                ent["until"] = self._now() + lock
            self._save()
            return False, lock
