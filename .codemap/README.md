# LIMCARS-NEXT codemap

Regenerate after edits: `python3 .codemap/regen-codemap.py .` (idempotent, instant).
Queries (jq is at /usr/bin/jq):
```
jq '.imports["app/server.py"]' .codemap/module-graph.json
jq '.symbols["app/sampler.py"]' .codemap/symbols.json
jq '."app/dialects.py"' .codemap/reverse-imports.json
```

## Layout & conventions
- `app/` = stdlib-only backend (psutil is the sole dep). Python ≥3.12, `/usr/bin/python3`.
- `web/js/dashboard.js` = the whole dashboard client (module script, cache-busted
  via `?v=` in web/dashboard.html — bump the letter after edits, server sends no-store).
- `web/subagent-watch/` = VENDORED Hermes traces UI — fetch wrapper at top of
  app.js is the password contract (X-Limcars-Pw from ?pw=; keep header MERGE,
  Range headers must survive).
- `data/` is runtime state (gitignored, EXCLUDED from map): sqlite db, auth.json,
  recipes.json, settings, pricing.json, config.json. NEVER map or publish it.
- `scripts/` start.sh + limcars.service + boot-test.sh. Entry module is `python3 -m app`.

## Hard constraints for reviewers
- STATIC review only: do NOT run the server, do NOT kill :8951, do NOT touch data/*,
  do NOT make outbound requests, do NOT modify any file.
- Server binds 0.0.0.0:8951 on a shared LAN — security findings must assume a hostile
  LAN client as attacker, but remember the USER's scope decision: password gates ONLY
  recipe add/edit/delete + trace DATA; Start/Stop/settings/probe are intentionally open.
- Numerical integrity is the product: findings about wrong/stale/averaged numbers
  count as bugs, not cosmetics.
- Cite findings as file:line with the exact snippet; never cite a path not in INDEX.md
  without grepping to confirm it exists.
