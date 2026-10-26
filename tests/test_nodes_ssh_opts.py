"""ssh_opts must not be able to turn a config edit into code execution.

Node sampling shells out as:

    ssh -o BatchMode=yes ... *extra -- <target> sh -s

The operator-supplied list is spliced BEFORE the "--" separator, so whatever it
contains is parsed by ssh as options, not as a hostname. -o ProxyCommand,
-o LocalCommand (with PermitLocalCommand) and -o RemoteCommand make ssh run a
LOCAL process, so a writable data/config.json becomes arbitrary execution as
the dashboard user -- the same blast radius as the recipes endpoint, but with no
password gate in front of it. Node configs are also the kind of thing people
paste from a blog post.

The policy is an allowlist, and unknown options are refused rather than passed
through, because "we did not think it was dangerous" is not a security property.
"""
import pytest

from app.nodes import sanitise_ssh_opts


def _kept(raw):
    return sanitise_ssh_opts(raw)[0]


def _rejected(raw):
    return sanitise_ssh_opts(raw)[1]


@pytest.mark.parametrize("opt", [
    "-o ProxyCommand=nc -X connect %h %p",
    "-oProxyCommand=sh -c id",
    "-o PermitLocalCommand=yes",
    "-o LocalCommand=touch /tmp/pwned",
    "-o RemoteCommand=id",
    "-o ProxyJump=attacker.example",
    "-o IdentityFile=/root/.ssh/id_rsa",
    "-o UserKnownHostsFile=/dev/null",
    "-o ControlPath=/tmp/x",
    "-o ForwardAgent=yes",
])
def test_code_executing_and_key_leaking_options_are_refused(opt):
    kept, rejected = sanitise_ssh_opts([opt])
    assert kept == [], f"{opt!r} reached argv"
    assert rejected == [opt]


@pytest.mark.parametrize("opt", [
    "; rm -rf /",
    "$(id)",
    "`id`",
    "a|b",
    "a\nb",
    "--oProxyCommand=x",
    "--unknown-flag",
    "-X11",
])
def test_metacharacters_and_unknown_flags_are_refused(opt):
    kept, _ = sanitise_ssh_opts([opt])
    assert kept == [], f"{opt!r} reached argv"


@pytest.mark.parametrize("opt", [
    "-v", "-4", "-6", "-q",
    "-o BatchMode=yes",
    "-o ConnectTimeout=5",
    "-o ServerAliveInterval=10",
    "-o StrictHostKeyChecking=accept-new",
    "-o LogLevel=ERROR",
    "-o Port=2222",
])
def test_ordinary_diagnostics_options_still_work(opt):
    """The point is to block execution, not to break remote monitoring."""
    kept, rejected = sanitise_ssh_opts([opt])
    assert kept == [opt] and rejected == [], f"{opt!r} blocked: {rejected}"


def test_mixed_list_keeps_the_safe_part_and_reports_the_rest():
    kept, rejected = sanitise_ssh_opts(
        ["-o", "BatchMode=yes", "-o", "ProxyCommand=nc evil", "-v"])
    assert kept == ["-o", "BatchMode=yes", "-v"], kept      # safe pair survives
    assert rejected == ["-o", "ProxyCommand=nc evil"], rejected


def test_empty_and_none_input_are_noops():
    assert sanitise_ssh_opts(None) == ([], [])
    assert sanitise_ssh_opts([]) == ([], [])


def test_sampler_refuses_to_exec_when_options_were_rejected(tmp_path, monkeypatch):
    """Reporting is not enough: the thread must never call subprocess.run at all
    once a config value has been vetted away."""
    import subprocess
    from app import nodes

    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("ssh ran")))
    n = nodes.NodeSampler({"id": "n1", "ssh": "host.example",
                           "ssh_opts": ["-o", "ProxyCommand=nc evil"]}, {}, _Broker())
    assert n.error and "refused unsafe ssh_opts" in n.error
    n._tick()                              # must return before exec
    assert called == [], "subprocess.run was reached with rejected options present"


class _Broker:
    def publish(self, *a, **k):
        pass
