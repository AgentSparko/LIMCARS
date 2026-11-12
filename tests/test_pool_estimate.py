"""Install-path autodetection for scripts/pool_estimate.py.

The KV reference line must follow the engine wherever the user installed it:
the running process (its argv[0] and cwd through /proc) is the authority, and
--engine-dir only stands in when nothing serves the port. These tests plant
fake install trees at arbitrary locations — the marker file src/server/app.py
is what identifies a TandemLLM root, never a configured path.
"""
import importlib.util
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pool_estimate", ROOT / "scripts" / "pool_estimate.py")
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)


def _fake_install(tmp: pathlib.Path, rel: str = "opt/whatever") -> pathlib.Path:
    """A TandemLLM-shaped tree at any location, with an executable venv python."""
    root = tmp / rel
    (root / "src" / "server").mkdir(parents=True)
    (root / "src" / "server" / "app.py").write_text("fake marker\n", encoding="utf-8")
    py = root / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("exit 0\n", encoding="utf-8")
    py.chmod(0o755)
    return root


def test_install_detected_from_run_dir_cwd(tmp_path):
    root = _fake_install(tmp_path)
    got = pe.install_from_proc(str(root / "venv" / "bin" / "python"), str(root / "src"))
    assert got is not None
    install, py = got
    assert install == os.path.realpath(str(root))
    assert py == str(root / "venv" / "bin" / "python")


def test_install_detected_from_install_root_cwd(tmp_path):
    root = _fake_install(tmp_path, rel="home/user/TandemLLM")
    got = pe.install_from_proc(str(root / "venv" / "bin" / "python"), str(root))
    assert got is not None and got[0] == os.path.realpath(str(root))


def test_install_detected_from_argv0_alone(tmp_path):
    """Even a wrong cwd must not mislead: argv[0] carries the whole location."""
    root = _fake_install(tmp_path, rel="srv/odd/place")
    got = pe.install_from_proc(str(root / "venv" / "bin" / "python"), "/nowhere")
    assert got is not None and got[0] == os.path.realpath(str(root))


def test_non_install_process_is_rejected(tmp_path):
    assert pe.install_from_proc("/usr/bin/python3", "/tmp") is None


def test_venv_python_falls_back_when_argv0_unusable(tmp_path):
    root = _fake_install(tmp_path)
    got = pe.install_from_proc("python", str(root / "src"))   # relative argv[0]
    assert got is not None
    assert got[0] == os.path.realpath(str(root))
    assert got[1] == str(root / "venv" / "bin" / "python")


def test_no_engine_on_port_detects_nothing():
    # port 1: nothing on this box can match the engine cmdline pattern there
    assert pe.detect_running_install(1) is None


def test_reexec_argv_inserts_engine_dir():
    argv = ["--from-running", "--port", "8020", "--json"]
    assert pe._reexec_argv(argv, "/x/TandemLLM") == \
        ["--from-running", "--port", "8020", "--json", "--engine-dir", "/x/TandemLLM"]


def test_reexec_argv_replaces_existing_engine_dir():
    argv = ["--engine-dir", "/old/0.15.2", "--from-running"]
    assert pe._reexec_argv(argv, "/new/0.15.3") == \
        ["--engine-dir", "/new/0.15.3", "--from-running"]
    argv = ["--engine-dir=/old", "--json"]
    assert pe._reexec_argv(argv, "/new") == ["--engine-dir=/new", "--json"]


def test_main_without_engine_and_without_dir_fails_cleanly(tmp_path, capsys):
    # no engine on port 1, no fallback dir: exit 2 with a named reason, no exec
    assert pe.main(["--from-running", "--port", "1", "--json"]) == 2
    err = capsys.readouterr().err
    assert "no TandemLLM process on port 1" in err
    assert "no --engine-dir" in err


def test_main_without_any_source_fails_cleanly(capsys):
    assert pe.main(["--json"]) == 2
    assert "pass --engine-dir" in capsys.readouterr().err

def test_launch_env_switches_the_fp8_kv_component():
    m = {"components": [
        {"id": "kv_fp8", "kv_bytes_per_token": 18944, "knobs": {"QWEN38_FN_KV_FP8": {"on": "1", "off": "0"}}},
        {"id": "vision", "knobs": {"QWEN38_VISION": {"on": "1", "off": "0"}}},
    ], "parallelism": {"kv_bytes_per_token": 31040}}
    assert pe.launch_components(m, {"QWEN38_FN_KV_FP8": "1", "QWEN38_VISION": "1"}) == {"kv_fp8": True}
    assert pe.launch_components(m, {"QWEN38_FN_KV_FP8": "0"}) == {"kv_fp8": False}
    assert pe.launch_components(m, {}) == {}
    assert pe.kv_bytes_per_token(m, pe.launch_components(m, {"QWEN38_FN_KV_FP8": "1"})) == 18944
    assert pe.kv_bytes_per_token(m, {}) == 31040
