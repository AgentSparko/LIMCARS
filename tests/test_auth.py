"""AuthGate: password verification + the per-IP lockout ladder.

The lockout tests are written as the policy, not as magic numbers, so tightening
`auth_max_attempts` in config.json does not silently invalidate them. The three that came out
of a real lockout (2026-10-28) are named for what the owner experienced.
"""
from app.auth import CHANGE, WRITE, AuthGate


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Gate(AuthGate):
    """AuthGate with a clock the tests can move (declared here so it stays type-visible)."""

    def __init__(self, path: str, **policy):
        super().__init__(path, **policy)
        self.clock = FakeClock()
        self._now = self.clock


def make_gate(tmp_path, **policy):
    return Gate(str(tmp_path / "auth.json"), **policy)


def burn(g, ip, n, scope=WRITE, pw="nope"):
    """n wrong attempts; returns the locks applied along the way."""
    return [g.attempt(ip, pw, scope=scope)[1] for _ in range(n)]


# ---------------------------------------------------------------- basics

def test_disabled_gate_accepts_everything(tmp_path):
    g = make_gate(tmp_path)
    assert not g.enabled
    assert g.verify("anything") is False
    assert g.locked_for("1.2.3.4") == 0.0


def test_set_verify_change(tmp_path):
    g = make_gate(tmp_path)
    assert g.set_password("beach123")
    assert g.enabled
    assert g.verify("beach123")
    assert not g.verify("Beach123")
    # change requires current when given
    assert not g.set_password("newpw", current="wrong")
    assert g.verify("beach123")           # unchanged
    assert g.set_password("newpw", current="beach123")
    assert g.verify("newpw")
    # empty disables
    assert g.set_password(None)
    assert not g.enabled


# ---------------------------------------------------------------- the ladder

def test_first_lock_needs_the_whole_budget(tmp_path):
    """Three typos used to be enough to freeze an hour of work. It is a policy now."""
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.10"
    assert g.max_attempts >= 8, "a human mistyping must get a real number of tries"
    for i in range(1, g.max_attempts):
        assert g.attempt(ip, "nope") == (False, 0.0), f"locked after only {i} typos"
    assert g.locked_for(ip) == 0.0
    assert g.attempt(ip, "nope") == (False, g.lock_steps[0])
    assert g.locked_for(ip) == g.lock_steps[0]


def test_lock_duration_steps_up_for_repeat_offenders(tmp_path):
    """Back-to-back burned budgets escalate, then hold at the last step."""
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.13"
    got = []
    for i in range(len(g.lock_steps) + 2):
        burn(g, ip, g.max_attempts)                      # never waiting it out: one burst
        got.append(g.locked_for(ip))
        g.clock.t += g.locked_for(ip) + 1                 # serve the lock, then keep going
    assert got[:len(g.lock_steps)] == list(g.lock_steps), got
    assert set(got[len(g.lock_steps):]) == {g.lock_steps[-1]}, got


def test_a_served_lock_that_expires_gives_a_clean_slate(tmp_path):
    """THE complaint: waiting the lock out used to change nothing.

    The old ladder counted failures since the last *success* forever, so each expiry handed
    you straight into the next, longer tier: 60 s, 5 m, 30 m, then 2 h every time. A person
    who mistyped a few times was finished for the day. A lock that has been served and has
    expired must put the whole budget back.
    """
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.14"
    burn(g, ip, g.max_attempts)                          # first lock
    first = g.locked_for(ip)
    assert first == g.lock_steps[0]
    g.clock.t += first + 1                               # wait it out
    assert g.locked_for(ip) == 0.0
    # a full fresh budget is available, and the first typo of it is not a lock
    assert g.attempts_left(ip) == g.max_attempts
    assert g.attempt(ip, "nope") == (False, 0.0)
    assert g.locked_for(ip) == 0.0
    # and the right password still works immediately
    assert g.attempt(ip, "s3cret") == (True, 0.0)


def test_quiet_period_clears_the_escalation(tmp_path):
    """Escalation is about persistence, so it must fall back when nobody is trying."""
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.15"
    for _ in range(3):                                   # climb to the 30 min step
        burn(g, ip, g.max_attempts)
        g.clock.t += g.locked_for(ip) + 1
    g.clock.t += g.quiet_reset_s + 1                     # a quiet evening
    assert g.attempt(ip, "nope") == (False, 0.0)
    burn(g, ip, g.max_attempts - 1)
    assert g.attempt(ip, "nope") == (False, g.lock_steps[0]), "still stuck on the old step"


def test_retries_during_a_lock_do_not_extend_it(tmp_path):
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.16"
    burn(g, ip, g.max_attempts)
    until = g.ips[ip][WRITE]["until"]
    for _ in range(5):
        assert g.attempt(ip, "nope") == (False, g.lock_steps[0])
    assert g.ips[ip][WRITE]["until"] == until, "hammering the door lengthened the lock"


# ---------------------------------------------------------------- two budgets

def test_changing_the_password_does_not_burn_the_login_budget(tmp_path):
    """THE complaint: the Settings form shared the write gate's counter, so fumbling the
    'current password' field spent the tries you needed to edit a recipe."""
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "192.0.2.5"
    for _ in range(g.max_attempts - 1):                  # nearly exhaust the change budget
        assert g.attempt(ip, "wrong-current", scope=CHANGE) == (False, 0.0)
    assert g.locked_for(ip, scope=WRITE) == 0.0
    assert g.attempts_left(ip, scope=WRITE) == g.max_attempts, "the write budget was eaten"
    assert g.attempt(ip, "s3cret", scope=WRITE) == (True, 0.0)


def test_locked_out_of_writes_you_can_still_prove_the_current_password(tmp_path):
    """The way out of a lockout is setting a new password (set_password clears every lock).
    That door must never be welded shut by the lock it is meant to escape."""
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "192.0.2.6"
    burn(g, ip, g.max_attempts)                          # locked out of writes
    assert g.locked_for(ip, scope=WRITE) > 0
    assert g.attempt(ip, "s3cret", scope=CHANGE) == (True, 0.0), "escape hatch blocked"
    assert g.set_password("brand-new", current="s3cret")
    assert g.locked_for(ip, scope=WRITE) == 0.0, "the change did not clear the lock"


def test_the_change_budget_is_rate_limited_on_its_own(tmp_path):
    """Separating the budgets must not create an unthrottled oracle for `current`."""
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "192.0.2.7"
    burn(g, ip, g.max_attempts, scope=CHANGE)
    assert g.locked_for(ip, scope=CHANGE) == g.lock_steps[0]
    assert g.attempt(ip, "s3cret", scope=CHANGE) == (False, g.lock_steps[0])
    assert g.locked_for(ip, scope=WRITE) == 0.0, "the two budgets leaked into each other"


# ---------------------------------------------------------------- misc

def test_empty_password_does_not_climb_the_ladder(tmp_path):
    """A request carrying no password (the traces page polling behind its password prompt)
    is not a guess. Three of them used to lock the IP out for 60 s, so the right password
    typed straight after was refused, and recipe edits and engine control with it."""
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.12"
    for _ in range(g.max_attempts * 2):
        assert g.attempt(ip, "") == (False, 0.0)
    assert g.locked_for(ip) == 0.0
    assert g.attempt(ip, "s3cret") == (True, 0.0)
    # wrong passwords still count, and an empty one still reports a lock already running
    burn(g, ip, g.max_attempts - 1)
    assert g.attempt(ip, "nope") == (False, g.lock_steps[0])
    assert g.attempt(ip, "") == (False, g.lock_steps[0])


def test_success_resets_counter(tmp_path):
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.11"
    burn(g, ip, g.max_attempts - 1)
    assert g.attempt(ip, "s3cret") == (True, 0.0)
    assert g.attempts_left(ip) == g.max_attempts
    # a whole new budget is there again, so the lock only comes after max_attempts more
    burn(g, ip, g.max_attempts - 1)
    assert g.locked_for(ip) == 0.0
    assert g.attempt(ip, "nope") == (False, g.lock_steps[0])


def test_attempts_left_warns_before_the_lock(tmp_path):
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.17"
    assert g.attempts_left(ip) == g.max_attempts
    burn(g, ip, 3)
    assert g.attempts_left(ip) == g.max_attempts - 3
    burn(g, ip, g.max_attempts - 3)
    assert g.attempts_left(ip) == 0


def test_policy_is_configurable(tmp_path):
    """A box that faces further than the LAN tightens this in config.json, not in code."""
    g = make_gate(tmp_path, max_attempts=3, lock_steps=[5, 10], quiet_reset_s=60)
    g.set_password("s3cret")
    ip = "198.51.100.4"
    assert g.attempt(ip, "nope") == (False, 0.0)
    assert g.attempt(ip, "nope") == (False, 0.0)
    assert g.attempt(ip, "nope") == (False, 5.0)
    g.clock.t += 6.0
    burn(g, ip, 3)
    assert g.locked_for(ip) == 10.0                       # second step


def test_lockout_persists_across_reload(tmp_path):
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    burn(g, "9.9.9.9", g.max_attempts)
    g2 = Gate(str(tmp_path / "auth.json"))
    g2.clock = g.clock                      # same fake clock as the "old" gate
    g2._now = g.clock
    assert g2.locked_for("9.9.9.9") > g.lock_steps[0] - 5  # survived "restart"


def test_old_state_file_shape_still_loads(tmp_path):
    """Pre-scopes auth.json held ip -> {n, until}. A lock already served must not strand
    anyone mid-upgrade, and the old counter must not resume escalating from where it was."""
    import json
    p = str(tmp_path / "auth.json")
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"salt": g.salt, "hash": g.hash,
                   "ips": {"7.7.7.7": {"n": 40, "until": g.clock.t + 3600.0}}}, f)
    g2 = Gate(p)
    g2.clock = g.clock
    g2._now = g.clock
    assert g2.locked_for("7.7.7.7") == 3600.0, "an old lock was dropped on upgrade"
    g2.clock.t += 3601.0
    assert g2.locked_for("7.7.7.7") == 0.0
    assert g2.attempts_left("7.7.7.7") == g2.max_attempts, "inherited the old runaway counter"
