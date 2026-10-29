"""Tests for docker_recipe: container-owning pid -> complete `docker run`."""
import io

from app import docker_recipe


# ---------------- container_id ----------------

class FakeProc:
    def __init__(self, monkeypatch, content):
        self.content = content
        monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(self.content))

def test_container_id_cgroup_v2(monkeypatch):
    FakeProc(monkeypatch, "0::/system.slice/docker-" + "ab" * 32 + ".scope\n")
    assert docker_recipe.container_id(1) == "ab" * 32

def test_container_id_cgroup_v1(monkeypatch):
    FakeProc(monkeypatch, "12:pids:/docker/" + "cd" * 32 + "\n")
    assert docker_recipe.container_id(1) == "cd" * 32

def test_container_id_not_a_container(monkeypatch):
    FakeProc(monkeypatch, "0::/user.slice/user-1000.slice/session-2.scope\n")
    assert docker_recipe.container_id(1) is None

def test_container_id_missing_file(monkeypatch):
    def boom(*a, **k):
        raise OSError
    monkeypatch.setattr("builtins.open", boom)
    assert docker_recipe.container_id(999999) is None


# ---------------- build_from_inspect ----------------

def _ctr(**over):
    c = {
        "Name": "/my-engine",
        "Image": "myllmbox/engine:v1",
        "Config": {
            "Image": "myllmbox/engine:v1",
            "Env": ["PATH=/usr/bin", "NVIDIA_VISIBLE_DEVICES=all", "EXTRA=1",
                    "SPACED=two words"],
            "Entrypoint": ["/usr/bin/python3", "-B", "-m", "engine.cli"],
            "Cmd": ["serve", "/models/x", "--port", "8000"],
        },
        "HostConfig": {
            "NetworkMode": "host", "IpcMode": "host",
            "Binds": ["/home/a/models:/models:ro", "/home/a/cache:/cache"],
            "RestartPolicy": {"Name": "unless-stopped"},
            "Ulimits": [{"Name": "memlock", "Hard": -1, "Soft": -1}],
            "DeviceRequests": [{"Count": -1, "Capabilities": [["gpu"]]}],
            "CpusetCpus": "0-9", "ShmSize": 67108864,
        },
    }
    c.update(over)
    return c


IMG = [{"Config": {"Env": ["PATH=/usr/bin", "NVIDIA_VISIBLE_DEVICES=all"],
                  "Entrypoint": ["/usr/bin/python3", "-B", "-m", "engine.cli"]}}]


def build(ctr, img=IMG):
    cmd = docker_recipe.build_from_inspect([ctr], image_lookup=lambda ref: img)
    assert cmd is not None
    return cmd


def test_foreground_named():
    cmd = build(_ctr())
    assert cmd.startswith("docker run --name my-engine")
    assert " -d " not in " " + cmd  # Stop must reach it via killpg


def test_flags_and_mounts_present():
    cmd = build(_ctr())
    for frag in ["--restart unless-stopped", "--ipc host", "--network host",
                 "--gpus all", "--ulimit memlock=-1:-1", "--cpuset-cpus 0-9",
                 '-v /home/a/models:/models:ro', "-v /home/a/cache:/cache"]:
        assert frag in cmd, frag
    assert "--shm-size" not in cmd  # docker default is omitted


def test_env_dedup_and_quoting():
    cmd = build(_ctr())
    assert "-e EXTRA=1" in cmd
    assert "-e 'SPACED=two words'" in cmd
    assert "PATH=" not in cmd and "NVIDIA_VISIBLE_DEVICES" not in cmd  # image-provided


def test_entrypoint_and_cmd_tail():
    cmd = build(_ctr())
    assert "--entrypoint" not in cmd  # matches image entrypoint -> omitted
    assert cmd.endswith("myllmbox/engine:v1 serve /models/x --port 8000")


def test_entrypoint_override():
    ctr = _ctr()
    # docker flattens `--entrypoint /bin/sh -c 'exec ...'` into
    # Entrypoint=[/bin/sh], Cmd=[-c, exec ...]; rebuild must replay argv exactly
    ctr["Config"]["Entrypoint"] = ["/bin/sh"]
    ctr["Config"]["Cmd"] = ["-c", "exec vllm serve x"]
    cmd = build(ctr)
    assert "--entrypoint /bin/sh" in cmd
    assert cmd.endswith("myllmbox/engine:v1 -c 'exec vllm serve x'")


def test_restart_no_omitted():
    ctr = _ctr()
    ctr["HostConfig"]["RestartPolicy"] = {"Name": "no"}
    assert "--restart" not in build(ctr)


def test_port_bindings_only_when_not_host_net():
    ctr = _ctr()
    ctr["HostConfig"]["NetworkMode"] = "bridge"
    ctr["HostConfig"]["PortBindings"] = {"8000/tcp": [{"HostIp": "", "HostPort": "8000"}]}
    assert "-p 8000:8000/tcp" in build(ctr)


def test_empty_or_bad_input():
    assert docker_recipe.build_from_inspect([]) is None
    assert docker_recipe.build_from_inspect(None) is None
    assert docker_recipe.build_from_inspect([{}]) is None  # no image
