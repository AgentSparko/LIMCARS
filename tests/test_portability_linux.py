"""Non-Linux process discovery, exercised on BOTH implementations.

The portability suite we inherited had nine tests, eight driving the Windows
path, and never once ran the Linux branch
(procs.LINUX_PROC is chosen by os.path.isdir("/proc/self") at import). So the
code nobody runs was covered and the code everybody runs -- every Linux host,
including the DGX Spark this dashboard was built for -- was verified only by
being used in production.

These tests do two things the old suite could not: drive the /proc branch
directly, and require the /proc branch and the psutil branch to return the SAME
answer for the same pid, which is the property that makes a portable dashboard
trustworthy. Assertions use the real API (pids/argv/alive/ppid/ancestors/
kill_tree); a test that quietly passes because an attribute is missing is worse
than no test, so nothing here defaults to a convenient value.

Scope note: this file is POSIX-only (see pytestmark). Running the suite on Windows
skips it and relies on tests/test_portability.py instead.
"""
import os
import subprocess
import time

import pytest

from app import procs

# Every test here drives the /proc branch, so the file is POSIX by construction:
# os.fork is absent on Windows and /proc is not a path there. CI runs a windows-latest
# matrix, and without this guard the six fork/proc tests surfaced as 3 failures and 3
# errors -- not a product defect, but a suite crashing on a platform it never tested.
# Windows is covered by tests/test_portability.py, which drives the Windows code path;
# this file is its Linux counterweight, and that division is the honest scope.
pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "fork"),
    reason="drives the /proc branch via os.fork; POSIX-only by construction "
           "(Windows coverage lives in tests/test_portability.py)",
)


def _spawn_sleeper(seconds=30):
    """Real child process, returns its pid. Caller must reap it."""
    pid = os.fork()
    if pid == 0:
        try:
            time.sleep(seconds)
        finally:
            os._exit(0)
    time.sleep(0.15)
    return pid


def _reap(pid):
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    for _ in range(50):
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done:
                return
        except ChildProcessError:
            return
        time.sleep(0.05)


@pytest.fixture
def child():
    pid = _spawn_sleeper()
    yield pid
    _reap(pid)


def _both_branches(fn, *args):
    """Run fn once on the /proc branch and once on the psutil branch."""
    orig = procs.LINUX_PROC
    try:
        procs.LINUX_PROC = True
        a = fn(*args)
        procs.LINUX_PROC = False
        b = fn(*args)
    finally:
        procs.LINUX_PROC = orig
    return a, b


def test_alive_and_ppid_agree_across_branches(child):
    (a_alive, b_alive), (a_ppid, b_ppid) = _both_branches(procs.alive, child), _both_branches(procs.ppid, child)
    assert a_alive is True and b_alive is True, (a_alive, b_alive)
    assert a_ppid == b_ppid == os.getpid(), (a_ppid, b_ppid)


def test_argv_agrees_across_branches(child):
    (a, b) = _both_branches(procs.argv, child)
    assert isinstance(a, list) and isinstance(b, list), (type(a), type(b))
    assert a[0] == b[0], f"argv diverges between /proc and psutil: {a[:1]} vs {b[:1]}"


def test_ancestors_walk_back_to_this_process(child):
    (a, b) = _both_branches(procs.ancestors, child)
    assert os.getpid() in a, f"/proc ancestors of {child} = {a}"
    assert os.getpid() in b, f"psutil ancestors of {child} = {b}"


def test_pids_lists_this_process_on_both_branches():
    (a, b) = _both_branches(procs.pids)
    assert os.getpid() in a and os.getpid() in b


def test_dead_pid_is_absent_on_both_branches():
    """No half-populated dict, no exception: a reaped pid must simply not be
    alive, identically on both code paths."""
    pid = _spawn_sleeper(0.01)
    _reap(pid)
    time.sleep(0.1)
    (a_alive, b_alive) = _both_branches(procs.alive, pid)
    assert a_alive is False and b_alive is False, (a_alive, b_alive)


def test_kill_tree_leaves_no_orphan_grandchild():
    """The previous version of this test announced grandchild reaping and then
    asserted only that the *parent* died -- the grandchild pid was created inside
    the forked child and thrown away, so kill_tree could orphan every worker and
    the test stayed green. Here the grandchild reports its own pid over a pipe and
    is asserted dead, which is the property that actually matters: a stray worker
    on this box keeps its KV allocation.
    """
    if not procs.LINUX_PROC:
        pytest.skip("no /proc on this host")
    r_fd, w_fd = os.pipe()
    parent = os.fork()
    if parent == 0:
        gc = os.fork()
        if gc == 0:
            os.close(r_fd)
            os.write(w_fd, str(os.getpid()).encode())
            os.close(w_fd)
            time.sleep(60)
            os._exit(0)
        os.close(r_fd)
        time.sleep(60)
        os._exit(0)
    os.close(w_fd)
    try:
        gc_pid = int(os.read(r_fd, 32).decode().strip())
    finally:
        os.close(r_fd)
    time.sleep(0.25)
    assert procs.alive(gc_pid), "grandchild never came up; the test would prove nothing"

    procs.kill_tree(parent, grace=2.0)
    time.sleep(0.4)

    try:
        os.waitpid(parent, 0)
    except ChildProcessError:
        pass
    assert not procs.alive(parent), f"parent {parent} survived kill_tree"
    assert not procs.alive(gc_pid), (
        f"grandchild {gc_pid} survived kill_tree -- an orphaned worker holds GPU "
        f"memory and the next engine launch fails on allocation")


def test_kill_tree_on_the_psutil_branch():
    """The /proc branch and the psutil branch are separate implementations; only
    one of them was ever executed, so the other could have been wrong forever."""
    if not hasattr(procs, "kill_tree"):
        pytest.skip("procs has no kill_tree")
    p = subprocess.Popen(["sleep", "60"], start_new_session=True)
    time.sleep(0.2)
    orig = procs.LINUX_PROC
    try:
        procs.LINUX_PROC = False
        procs.kill_tree(p.pid, grace=2.0)
    finally:
        procs.LINUX_PROC = orig
    assert p.wait(timeout=5) is not None, "psutil-branch kill_tree did not reap the child"


def test_argv_branches_agree_on_every_argument():
    """Comparing argv[0] only proves the executable matches; ports, --model and
    every other flag -- the things engine classification actually reads -- could
    diverge between /proc and psutil and the old assertion stayed green."""
    a, b = _both_branches(procs.argv, os.getpid())
    assert isinstance(a, list) and isinstance(b, list)
    assert a == b, f"argv diverges beyond argv[0]:\n  /proc  ={a}\n  psutil={b}"


def test_kill_tree_reaps_child_and_grandchild_on_linux():
    """A stopped engine must not leave GPU-holding grandchildren: on this box a
    stray worker keeps its KV allocation and the next launch fails on memory.

    Verified by reaping first -- an unreaped child is a zombie and still answers
    kill(pid, 0), which is what made an earlier version of this test report a
    false failure.
    """
    if not procs.LINUX_PROC:
        pytest.skip("no /proc on this host")
    parent = os.fork()
    if parent == 0:                       # child -> spawns a grandchild
        gc = os.fork()
        if gc == 0:
            time.sleep(60)
            os._exit(0)
        time.sleep(60)
        os._exit(0)
    time.sleep(0.25)

    procs.kill_tree(parent, grace=2.0)

    # reap so a zombie does not masquerade as alive
    try:
        os.waitpid(parent, 0)
    except ChildProcessError:
        pass
    with pytest.raises(ProcessLookupError):
        os.kill(parent, 0)
