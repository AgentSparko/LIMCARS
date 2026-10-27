#!/usr/bin/env python3
"""Prove the write gates from a non-loopback peer.

The server binds 0.0.0.0 and exempts loopback from several gates, so a test that connects to
127.0.0.1 proves nothing about what a host on the LAN can do. This connects with the source address
bound to the machine's own LAN IP, so the peer really is remote.

Expectations are read from the policy in app/server.py, not invented:
  gated (must be refused: 401/403/428)   recipe add/update/delete (write gate),
                                         services start|stop and engine stop (control gate),
                                         POST /api/config pricing (pricing gate),
                                         /api/session-tokens (trace-read gate)
  open by design (must be allowed)       snapshot/counters/history/storage/... and the numeric
                                         config + settings keys — ungating those on the LAN is the
                                         owner's canary decision, not a hole to be "fixed".

Nothing here sends a body that could change state if a gate turned out to be missing, except the
recipe add, which uses a marked name and is deleted again if it ever gets through.

    python3 scripts/limcars_gate_proof.py [--port 8951]
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import subprocess
import sys

MARK = "gate-proof-ignore-me"

GATED = [
    ("POST", "/api/recipes", {"name": MARK, "cmd": "echo gate-proof"}),
    ("POST", "/api/recipes/does-not-exist/update", {"name": "x"}),
    ("POST", "/api/recipes/does-not-exist/delete", {}),
    ("POST", "/api/services/nonexistent-svc/start", {}),
    ("POST", "/api/services/nonexistent-svc/stop", {}),
    ("POST", "/api/engines-live/999999/stop", {}),
    ("POST", "/api/config", {"pricing": {"input": 99.0}}),
    ("GET", "/api/session-tokens?engine=flashnext", None),
]
OPEN = ["/api/snapshot?seconds=60", "/api/counters?window=all&engine=flashnext", "/api/health",
        "/api/storage", "/api/history?seconds=60", "/api/savings-history?days=7",
        "/api/lost-potential", "/api/clients?window=1800", "/api/settings",
        "/api/recipes", "/api/engines-live", "/api/auth/status"]
# POST-only handlers: a GET here must be 404, which is also not a way in
POST_ONLY = ["/api/config", "/api/services/x/start", "/api/probe"]


def call(host, port, src, method, path, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=15, source_address=(src, 0))
    try:
        payload = json.dumps(body) if body is not None else None
        hdrs = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=hdrs)
        r = conn.getresponse()
        return r.status, r.read()[:300]
    finally:
        conn.close()


def lan_ip() -> str:
    out = subprocess.run(["bash", "-c",
                          "ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \\K[0-9.]+' | head -1"],
                         capture_output=True, text=True).stdout.strip()
    return out if out and not out.startswith("127.") else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=8951)
    a = ap.parse_args()

    src = lan_ip()
    if not src:
        print("no non-loopback source address found — refusing to claim a proof")
        return 2
    target = a.host or src
    print(f"peer={src} (non-loopback)  target={target}:{a.port}\n")

    bad, created = [], False
    for method, path, body in GATED:
        try:
            code, raw = call(target, a.port, src, method, path, body)
        except Exception as exc:
            bad.append((path, f"connection failed: {exc}"))
            print(f"  FAIL  {method:5s} {path:40s} {exc}")
            continue
        refused = code in (401, 403, 423, 428)
        print(f"  {'ok  ' if refused else 'FAIL'} {method:5s} {path:40s} -> {code} {raw[:70]!r}")
        if not refused:
            bad.append((path, f"{code}: a remote peer was allowed through a gate"))
            if path == "/api/recipes" and MARK.encode() in raw:
                created = True

    for path in OPEN:
        try:
            code, _ = call(target, a.port, src, "GET", path)
        except Exception as exc:
            bad.append((path, f"connection failed: {exc}"))
            print(f"  FAIL  GET   {path:40s} {exc}")
            continue
        ok = code == 200
        print(f"  {'ok  ' if ok else 'FAIL'} GET   {path:40s} -> {code}")
        if not ok:
            bad.append((path, f"{code}: a read that is open by design is refusing"))

    if created:      # the write gate failed and left a recipe behind — clean up and shout
        try:
            data = json.loads(call(target, a.port, src, "GET", "/api/recipes")[1])
            for r in data.get("recipes", data if isinstance(data, list) else []):
                if isinstance(r, dict) and r.get("name") == MARK:
                    call(target, a.port, src, "POST", f"/api/recipes/{r.get('id')}/delete", {})
        except Exception:
            pass

    for path in POST_ONLY:
        try:
            code, _ = call(target, a.port, src, "GET", path)
        except Exception as exc:
            bad.append((path, f"connection failed: {exc}"))
            continue
        ok = code == 404
        print(f"  {'ok  ' if ok else 'FAIL'} GET   {path:40s} -> {code} (POST-only route)")
        if not ok:
            bad.append((path, f"{code}: a POST-only route answered a GET"))

    print(f"\n{'GATE PROOF FAILED: ' + str(len(bad)) + ' problem(s)' if bad else 'GATES HOLD from a non-loopback peer'}")
    for p, why in bad:
        print(f"  {p}: {why}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
