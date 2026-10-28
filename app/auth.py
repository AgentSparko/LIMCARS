"""Password gate for mutating endpoints, with escalating per-IP lockout.

Design (owner's spec, revised 2026-10-28 after a real lockout that cost them an hour):
one shared password; `max_attempts` wrong tries (default 10) earn the first lock, then the
duration steps up 60 s -> 5 m -> 30 m -> 1 h -> 2 h (cap) for repeat offenders.

Two rules make this survivable for a human who simply mistyped:

* **A served lock buys back a full budget.** The old ladder counted failures *since the last
  success* and never let that number fall, so waiting a lock out did not help: the next three
  typos jumped straight to the next, longer tier. An owner mistyping a few times in a row rode
  60 s -> 5 m -> 30 m -> 2 h and stayed at the cap, which reads as "the timer never resets, it
  just grows". Once a lock has actually been served and has expired the counter is cleared, so
  escalation only climbs while someone burns budgets back to back, and it decays back to the
  first step after a quiet period.
* **Changing the password is its own budget.** Proving `current` on the Settings form used to
  run through the same counter as a write, so fiddling with that form ate the login tries, and
  — worse — an IP already locked out of writes could not use the one door that gets it out
  (setting a new password clears every lock). `scope="change"` keeps the two apart, and both
  stay rate-limited on their own.

Localhost is exempt (checked server-side); this box is the admin domain. Passwords are
scrypt-hashed (with a verified sha256 path for pre-existing entries) and compared with
hmac.compare_digest. A request that carries no password is not a guess (#11) and does not
touch the ladder; it still fails closed at the caller.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# Policy defaults. Each is overridable from config.json (auth_max_attempts, auth_lock_steps,
# auth_quiet_reset_s) so an operator can tighten them on a box that reaches further than the
# LAN without a code change.
DEFAULT_MAX_ATTEMPTS = 10                       # wrong tries before the first lock
DEFAULT_LOCK_STEPS = (60.0, 300.0, 1800.0, 3600.0, 7200.0)
DEFAULT_QUIET_RESET_S = 6 * 3600.0              # this long with no failure -> level back to 0

# The two budgets. "write" gates recipe/engine/service verbs; "change" gates proving the
# current password on the Settings form. Per IP, and deliberately independent.
WRITE, CHANGE = "write", "change"


class AuthGate:
    def __init__(self, path: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 lock_steps=DEFAULT_LOCK_STEPS, quiet_reset_s: float = DEFAULT_QUIET_RESET_S):
        self.path = path
        self._lock = threading.Lock()
        self._now = time.time
        self.max_attempts = max(1, int(max_attempts))
        steps = tuple(float(s) for s in (lock_steps or DEFAULT_LOCK_STEPS) if float(s) > 0)
        self.lock_steps = steps or DEFAULT_LOCK_STEPS
        self.quiet_reset_s = max(0.0, float(quiet_reset_s))
        self.salt: str | None = None
        self.hash: str | None = None
        # ip -> {scope -> {"n": failures this cycle, "until": lock expiry,
        #                  "level": which lock_step the next lock earns, "last": last failure}
        self.ips: dict[str, dict] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.salt, self.hash = d.get("salt"), d.get("hash")
            self.ips = {k: self._normalise(v) for k, v in (d.get("ips") or {}).items()}
        except (OSError, ValueError):
            pass

    @staticmethod
    def _normalise(v) -> dict:
        """Accept the pre-2026-10-28 shape (ip -> {n, until}) as the write scope."""
        if not isinstance(v, dict):
            return {}
        if "n" in v or "until" in v:                       # old single-budget format
            v = {WRITE: v}
        out: dict = {}
        for scope, ent in v.items():
            if not isinstance(ent, dict):
                continue
            out[scope] = {"n": int(ent.get("n", 0) or 0),
                          "until": float(ent.get("until", 0.0) or 0.0),
                          "level": int(ent.get("level", 0) or 0),
                          "last": float(ent.get("last", 0.0) or 0.0)}
        return out

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
        Empty/None `new` disables the gate. Returns False if current is wrong.

        Wiping every lock is deliberate: an owner who has forgotten the password and is
        locked out of the write gate must still be able to reach this path.
        """
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

    def _entry(self, ip: str, scope: str) -> dict:
        return self.ips.setdefault(ip, {}).setdefault(
            scope, {"n": 0, "until": 0.0, "level": 0, "last": 0.0})

    def _settle(self, ent: dict, now: float) -> float:
        """Apply the two rules that make the ladder survivable; return time still locked.

        Called under the lock before any decision: a long gap with no failure clears the
        escalation, and a lock that has been served and has expired hands back a full budget.
        """
        if self.quiet_reset_s and (now - float(ent.get("last") or 0.0)) > self.quiet_reset_s:
            ent["n"], ent["level"] = 0, 0
        rem = max(0.0, float(ent.get("until", 0.0)) - now)
        if rem == 0.0 and ent.get("until"):
            ent["until"] = 0.0                            # punishment served...
            ent["n"] = 0                                  # ...so the budget is whole again
        return rem

    def _record_failure(self, ent: dict, now: float) -> float:
        """Count one wrong password; return the lock just applied (0 if none)."""
        ent["n"] += 1
        ent["last"] = now
        if ent["n"] < self.max_attempts:
            return 0.0
        lock = self.lock_steps[min(ent["level"], len(self.lock_steps) - 1)]
        ent["until"] = now + lock
        ent["level"] += 1
        ent["n"] = 0                                      # a served lock buys back the budget
        return lock

    def locked_for(self, ip: str, scope: str = WRITE) -> float:
        """Seconds of lockout remaining for ip (0.0 when free). A pure read."""
        with self._lock:
            ent = self.ips.get(ip, {}).get(scope)
            if not ent:
                return 0.0
            return max(0.0, float(ent.get("until", 0.0)) - self._now())

    def attempts_left(self, ip: str, scope: str = WRITE) -> int:
        """Tries remaining before the next lock, so a UI can warn instead of springing it.

        Settles first: a budget that was restored by a lock expiring must read as restored,
        even if nothing has attempted since.
        """
        with self._lock:
            ent = self.ips.get(ip, {}).get(scope)
            if not ent:
                return self.max_attempts
            if self._settle(ent, self._now()) > 0:
                return 0
            return max(0, self.max_attempts - int(ent.get("n", 0)))

    def failure(self, ip: str, scope: str = WRITE) -> float:
        """Record a wrong password; returns the lock just applied (0 if none)."""
        with self._lock:
            now = self._now()
            ent = self._entry(ip, scope)
            if self._settle(ent, now) > 0:                # already locked: nothing to add
                return 0.0
            lock = self._record_failure(ent, now)
            self._save()
            return lock

    def success(self, ip: str, scope: str | None = None) -> None:
        """Clear the budget. No scope clears the whole IP (a success proves the password)."""
        with self._lock:
            if scope is None:
                changed = self.ips.pop(ip, None) is not None
            else:
                ent = self.ips.get(ip)
                changed = bool(ent) and ent.pop(scope, None) is not None
                if ent is not None and not ent:
                    self.ips.pop(ip, None)            # no empty shells left behind
            if changed:
                self._save()

    def attempt(self, ip: str, pw: str, scope: str = WRITE) -> tuple[bool, float]:
        """Lockout-check + verify + ladder update in ONE lock hold.

        The server used to call locked_for() then verify() then failure() as three separate
        lock acquisitions; with ThreadingHTTPServer's thread-per-connection, parallel guesses
        read the same failure count and cap the ladder at level 1 forever (audit H1). This is
        the only auth path mutating endpoints may use. Returns (ok, locked_remaining).

        `scope` separates the write gate from proving the current password on the Settings
        form: honest mistakes in that form must not spend the login budget, and a lockout on
        one door must not weld the other one shut.
        """
        with self._lock:
            now = self._now()
            ent = self._entry(ip, scope)
            rem = self._settle(ent, now)
            if rem > 0:
                return False, rem
            if not pw:
                # no password sent is not a guess: pollers behind the password
                # prompt would otherwise lock the IP out of every gate
                return False, 0.0
            if self.enabled and self._matches(pw):
                if self.ips.get(ip, {}).pop(scope, None) is not None:
                    if not self.ips.get(ip):
                        self.ips.pop(ip, None)
                    self._save()
                return True, 0.0
            lock = self._record_failure(ent, now)
            self._save()
            return False, lock
