"""Agentless node sampler: remote script output parsing (no SSH needed)."""
from app.nodes import NodeSampler, _sections


class _B:
    def __init__(self):
        self.pub = []

    def publish(self, topic, data):
        self.pub.append((topic, data))


def _out(user, idle, rx, tx, gpu=""):
    return f"""@@stat
cpu  {user} 0 100 {idle} 50 0 0 0 0 0
@@mem
MemTotal:       131072000 kB
MemFree:         1000000 kB
MemAvailable:   100663296 kB
SwapTotal:       2097152 kB
SwapFree:        1048576 kB
@@load
1.50 0.75 0.40 2/900 1234
@@net
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 999999 10 0 0 0 0 0 0 999999 10 0 0 0 0 0 0
  eth0: {rx} 10 0 0 0 0 0 0 {tx} 10 0 0 0 0 0 0
@@df
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/nvme0n1p2  1048576000 524288000 524288000      50% /
@@temp
acpitz temp1 61500
@@gpu
{gpu}"""


def _node():
    return NodeSampler({"id": "spark-2", "ssh": "u@h"}, {}, _B())


def test_sections_split():
    s = _sections(_out(1, 2, 3, 4))
    assert s["load"] == ["1.50 0.75 0.40 2/900 1234"] and s["gpu"] == []


def test_first_tick_static_fields_then_rates():
    n = _node()
    r1 = n.parse(_out(1000, 8000, 0, 0, "0, 96, 63, 41.0, [N/A], 2463, 3003, [N/A], [N/A], NVIDIA GB10"), 100.0)
    assert r1["mem_total_gb"] == 125.0 and r1["mem_avail_gb"] == 96.0 and r1["mem_used_gb"] == 29.0
    assert r1["swap_used_gb"] == 1.0 and r1["load1"] == 1.5 and r1["cpu_temp_c"] == 61.5
    assert r1["disk_total_gb"] == 1000.0 and r1["disk_used_gb"] == 500.0
    assert "cpu_pct" not in r1 and "net_r_mbs" not in r1          # rates need two ticks
    assert r1["gpu_util"] == 96 and "vram_used_gb" not in r1     # unified memory
    assert n.gpus == [{"index": 0, "name": "NVIDIA GB10", "vram_gb": None}]
    r2 = n.parse(_out(1250, 8750, 2 * 2**20, 2**20), 101.0)
    assert r2["cpu_pct"] == 25.0                                  # 250 busy of 1000 jiffies
    assert r2["net_r_mbs"] == 2.0 and r2["net_w_mbs"] == 1.0      # lo excluded


def test_counter_reset_gives_no_rate_not_negative():
    n = _node()
    n.parse(_out(1000, 8000, 5 * 2**20, 5 * 2**20), 100.0)
    r = n.parse(_out(1100, 8100, 0, 0), 101.0)
    assert "net_r_mbs" not in r and "net_w_mbs" not in r


def test_ssh_argv_never_prompts():
    a = _node().argv()
    assert a[0] == "ssh" and "BatchMode=yes" in a and a[-3:] == ["u@h", "sh", "-s"]


def test_option_like_ssh_target_is_rejected():
    from app.nodes import start_nodes
    assert start_nodes({"nodes": [{"id": "x", "ssh": "-oProxyCommand=touch /tmp/pwn"},
                                  {"id": "y", "ssh": "a b"}]}, _B()) == []
    assert "--" in _node().argv()
