# Security policy

LIMCARS is a monitoring dashboard you run yourself, and it is also a **control plane**: the
recipe runner executes stored commands with `subprocess(shell=True)` as the user running
LIMCARS, and the same server can start/stop systemd services and engine processes and rewrite
the cloud-price table that every "money saved" figure is measured against. That combination is
why we take security reports seriously and why the defaults below exist.

## Reporting a vulnerability

**Use the Security tab → “Report a vulnerability”** on
[AgentSparko/LIMCARS](https://github.com/AgentSparko/LIMCARS/security). That opens a **private
advisory** — the finding is not visible to anyone but you and the maintainers until you agree to
publish it. Please do not open a public issue, discussion or PR for a vulnerability.

What makes a report actionable:

- **Version**: output of `GET /api/health` (`{"ok":true,"version":"1.1.0",...}`) **and**
  `git rev-parse HEAD` of the checkout you tested.
- **How it is running**: the `host`/`port` in `data/config.json` (or `LIMCARS_HOST` /
  `LIMCARS_PORT`), whether a password was set, and whether `LIMCARS_TOKEN` is in use.
- **Steps**, **impact**, and a minimal PoC — a script or a single `curl` is ideal.
- **Redact your own secrets**: if your `data/config.json`, pricing table or engine URLs hold
  real API keys or prices, scrub them before attaching anything.

This is a one-person project with no formal SLA. We aim to acknowledge within a week, and we
credit reporters in the advisory unless you ask not to be.

## In scope — we want to hear about these

- **Reaching the recipe runner or control-plane verbs from a non-loopback client without the
  password** — recipe add/edit/delete, service start/stop, engine stop, price-table edits —
  including any way to get past the fail-closed `428` when no password is configured.
- **Authentication / lockout bypass**: forging or spoofing the client identity the gate keys on,
  racing the ladder into a permanent level-1 lockout, leaking one budget into the other, or any
  weakness in how the scrypt hash is compared.
- **Injection / path traversal**: reading or writing outside the intended paths via static
  serving or transcript endpoints; SQL injection into the history database; SSRF through an
  engine URL or proxy target supplied over HTTP.
- **Stored XSS in the dashboard or the subagent-watch page** — both render text that came from an
  engine, a log line or another machine.
- **Remote corruption or loss of history** — anything that lets an unauthenticated or
  under-privileged request damage the money ladder (`counter_buckets` / `counter_hour` /
  `counter_day`), the self-tidy path, or restore a stale file over a live database. Data-loss
  bugs are treated as security issues here, not just bugs.
- **Credential exposure**: secrets written world-readable, echoed into the API, dashboard, logs
  or the database where they should not be (`data/auth.json` is deliberately created `0600`).

## Out of scope — these are design decisions, already documented

| Report | Why it isn't a finding |
|---|---|
| “Loopback needs no password” | Localhost is exempt **by design**: the normal case is you, on your own box. `README → Security`. |
| “I set `LIMCARS_HOST=0.0.0.0` / `host: 0.0.0.0` with no password and got RCE” | That configuration is documented as a remote-code-execution path and refused for writes until a password is set. **If the gate fails open *with* a password configured, that IS a finding.** |
| “Settings writes are not password-gated on the LAN” | Deliberate owner canary, documented. Findings in the gated verbs still count. |
| “No CSP / no clickjacking header” | Built for a trusted LAN, not for untrusted browsing. A working XSS is in scope; the missing header by itself is not. |
| DoS on your own instance, or locking yourself out with wrong passwords | Operator self-harm; the lockout ladder is tunable (`auth_max_attempts`, `auth_lock_steps`, `auth_quiet_reset_s`). |
| Vulnerabilities in the engines it watches | vLLM, SGLang, llama.cpp, TandemLLM etc. are third-party — report upstream. LIMCARS-side bugs in how it parses or exposes them are in scope. |
| Anything needing existing shell/user access to the box | At that point the attacker already owns the process and its files. |

## Hardening already in place (so you know where to look)

- Binds `127.0.0.1:8951` by default; `0.0.0.0` is an explicit operator choice.
- Non-local writes **fail closed** with `428` when no password is set; there is deliberately no
  environment variable that turns this off.
- Passwords: salted **scrypt** (`n=2¹⁴,r=8,p=1`), verified in constant time, pre-existing
  single-round SHA-256 entries still verify until reset.
- Per-IP escalating lockout with **independent budgets** for the write gate and the
  change-password form, checked + counted + persisted under a **single atomic lock** (parallel
  guesses used to freeze the escalation at level 1).
- Gate keys on the real socket peer (`client_address`), not on a spoofable `X-Real-IP`/`X-Forwarded-For`.
- API responses send `Cache-Control: no-store`.
- `data/auth.json` written `0600`; history introspection endpoints are read-only.

## Safe harbour

We consider security research on LIMCARS authorised when you: test against an instance you run
yourself (use `LIMCARS_DATA=/tmp/limcars-test` so you never touch a live install — a second
process pointed at the real data dir can write its `config.json`/`auth.json`), report to us
promptly, act in good faith, and do not exfiltrate other people's data, threaten or demand
payment, or scan infrastructure you do not own. We will not pursue or initiate legal action
against research conducted within this policy.

— Thanks. The history ladder, the sampler and the auth gate are measurably better because
outside reviewers read them line by line.
