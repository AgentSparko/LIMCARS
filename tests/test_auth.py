"""AuthGate: password verification + escalating per-IP lockout ladder."""
from app.auth import AuthGate


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_gate(tmp_path):
    g = AuthGate(str(tmp_path / "auth.json"))
    g._now = FakeClock()
    return g


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


def test_lockout_ladder(tmp_path):
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.10"
    # tries 1-2: free
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 0.0
    assert g.locked_for(ip) == 0.0
    # 3rd wrong -> 60 s
    assert g.failure(ip) == 60.0
    assert g.locked_for(ip) == 60.0
    # 4th/5th add no NEW lock while old still counts down
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 0.0
    # 6th -> 300 s
    assert g.failure(ip) == 300.0
    # 9th -> 1800 s
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 1800.0
    # 12th -> 7200 s cap
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 7200.0
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 7200.0        # capped, repeats every 3


def test_success_resets_counter(tmp_path):
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    ip = "203.0.113.11"
    g.failure(ip); g.failure(ip)          # n=2
    assert g.verify("s3cret")
    g.success(ip)
    # counter reset: 2 failures again must not lock
    assert g.failure(ip) == 0.0
    assert g.failure(ip) == 0.0
    assert g.locked_for(ip) == 0.0
    # and the next (3rd) does
    assert g.failure(ip) == 60.0


def test_lockout_persists_across_reload(tmp_path):
    g = make_gate(tmp_path)
    g.set_password("s3cret")
    for _ in range(3):
        g.failure("9.9.9.9")
    g2 = AuthGate(str(tmp_path / "auth.json"))
    g2._now = g._now                      # same fake clock as the "old" gate
    assert g2.locked_for("9.9.9.9") > 55  # survived "restart"
