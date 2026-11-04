"""VPN 客户端：命令构建、指纹解析、TOFU、缺失依赖降级、断开清理"""
import socket
import threading
from unittest.mock import MagicMock

import pytest

from src.vpn_client import (
    VpnClient, find_openconnect, is_reachable, parse_certificate_pin,
    parse_host_port,
)


class TestParsers:
    def test_parse_host_port_variants(self):
        assert parse_host_port("10.10.3.67:8080") == ("10.10.3.67", 8080)
        assert parse_host_port("http://10.10.3.67:8080/index.php?m=bug") == (
            "10.10.3.67", 8080)
        assert parse_host_port("vpn.example.com") == ("vpn.example.com", 0)
        assert parse_host_port("") == ("", 0)

    def test_parse_certificate_pin(self):
        out = ("None of the 1 fingerprint(s) specified via --servercert match "
               "server's certificate: pin-sha256:LS2XrKkkw3qy5u8NLsW+qNi1N9"
               "vCMpKo2Cq+51OKxIY=\n")
        assert parse_certificate_pin(out) == (
            "pin-sha256:LS2XrKkkw3qy5u8NLsW+qNi1N9vCMpKo2Cq+51OKxIY=")
        assert parse_certificate_pin("no cert info") is None


class TestFindOpenconnect:
    def test_prefers_existing_path(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod.os.path, "isfile",
                            lambda p: p.endswith("openconnect.exe"))
        assert find_openconnect().endswith("openconnect.exe")

    def test_missing_returns_none(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(mod.shutil, "which", lambda n: None)
        assert find_openconnect() is None


class TestReachability:
    def test_reachable_with_local_server(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def accept_once():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                pass

        threading.Thread(target=accept_once, daemon=True).start()
        assert is_reachable("127.0.0.1", port, timeout=2) is True
        srv.close()

    def test_unreachable_port(self):
        assert is_reachable("127.0.0.1", 1, timeout=0.5) is False


# ── 用假进程验证命令构建与 TOFU ──────────────────────

class _FakeStdout:
    def __init__(self, lines):
        self._lines = [ln.encode("utf-8") + b"\n" for ln in lines]

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    def flush(self):
        pass


class _FakeProc:
    def __init__(self, cmd, lines, exit_code=0, **kwargs):
        self.cmd = cmd
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self._alive = True
        self._exit_code = exit_code
        self.terminated = False

    def poll(self):
        # 保持"运行中"直到被 terminate：测试用短超时控制失败分支，
        # 避免读取线程与断言之间的竞态
        return None if self._alive else self._exit_code

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return self._exit_code

    def kill(self):
        self._alive = False


def _client(**overrides):
    cfg = {
        "enabled": True,
        "server": "vpn.example.com",
        "port": 50443,
        "username": "u",
        "password": "secret-pw",
        "servercert": "",
        "probe_host": "10.0.0.5",
        "probe_port": 8080,
        "timeout": 3,
        "auto_trust_cert": True,
    }
    cfg.update(overrides)
    return VpnClient(cfg)


class TestConnect:
    def test_disabled_returns_ok(self):
        c = VpnClient({"enabled": False})
        assert c.connect() == (True, "未启用 VPN")

    def test_incomplete_config_returns_error(self):
        ok, msg = VpnClient({"enabled": True, "server": ""}).connect()
        assert ok is False and "不完整" in msg

    def test_missing_openconnect_graceful(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod, "find_openconnect", lambda: None)
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: False)
        ok, msg = _client().connect()
        assert ok is False
        assert "未检测到 OpenConnect" in msg
        assert "choco install openconnect" in msg

    def test_already_reachable_skips(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: True)
        ok, msg = _client().connect()
        assert ok is True and "已可达" in msg

    def test_password_not_in_argv_and_success(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod, "find_openconnect", lambda: "oc.exe")
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: True)
        procs = []

        def fake_popen(cmd, **kwargs):
            p = _FakeProc(cmd, ["Configured as 10.0.0.2, with SSL disconnected and DTLS established"],
                          exit_code=0, **kwargs)
            procs.append(p)
            return p

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        ok, msg = _client(probe_host="", probe_port=0).connect()
        assert ok is True
        cmd = procs[0].cmd
        assert all("secret-pw" not in str(part) for part in cmd)  # 密码不入 argv
        assert b"secret-pw" in procs[0].stdin.written            # 密码走 stdin
        assert "--protocol=fortinet" in cmd
        assert "vpn.example.com:50443" in cmd
        assert "--non-inter" in cmd

    def test_tofu_retry_with_detected_pin(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod, "find_openconnect", lambda: "oc.exe")
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: True)
        mismatch = ("None of the 1 fingerprint(s) specified via --servercert "
                    "match server's certificate: pin-sha256:AAAA=")
        procs = []
        outputs = [[mismatch], ["Configured as 10.0.0.2, with SSL disconnected and DTLS established"]]

        def fake_popen(cmd, **kwargs):
            p = _FakeProc(cmd, outputs[len(procs)], exit_code=1, **kwargs)
            procs.append(p)
            return p

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        client = _client(probe_host="", probe_port=0, timeout=1)
        # 第一次探测让循环快速退出：is_reachable True 但需先看到成功标记；
        # 首个输出无成功标记且进程退出 → 失败 → 解析指纹重试
        ok, msg = client.connect()
        assert ok is True
        assert client.servercert == "pin-sha256:AAAA="
        assert len(procs) == 2
        # 第一次用占位指纹探测真实指纹，第二次用探测到的指纹
        assert any("--servercert=pin-sha256:AAAAAAAAAAAA" in str(p.cmd)
                   for p in procs[:1])
        assert any("--servercert=pin-sha256:AAAA=" in p.cmd for p in procs)

    def test_failure_message_hint_for_admin(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod, "find_openconnect", lambda: "oc.exe")
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: False)

        def fake_popen(cmd, **kwargs):
            return _FakeProc(cmd, ["Failed to create Wintun adapter",
                                   "Access is denied"], exit_code=1, **kwargs)

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        ok, msg = _client(probe_host="", probe_port=0, timeout=1).connect()
        assert ok is False
        assert "管理员" in msg


class TestErrorHints:
    def test_success_output_not_misjudged_as_auth_failure(self):
        text = ("Configured as 10.0.0.2\n"
                "Session authentication will expire at 2026-11-04")
        assert VpnClient._error_hint(text) == ""

    def test_failed_authentication_hint(self):
        assert "账号或密码" in VpnClient._error_hint(
            "Failed to complete authentication")

    def test_session_terminated_hint(self):
        assert "会话" in VpnClient._error_hint("Session terminated by server")

    def test_wintun_failure_hint(self):
        assert "虚拟网卡" in VpnClient._error_hint(
            "Failed to create Wintun adapter")


class TestProbeFailureMessage:
    def test_tunnel_up_but_probe_unreachable(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod, "find_openconnect", lambda: "oc.exe")
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: False)

        def fake_popen(cmd, **kwargs):
            return _FakeProc(cmd, [
                "Configured as 10.0.0.2, with SSL disconnected",
                "Session authentication will expire at 2026-11-04",
            ], exit_code=0, **kwargs)

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        ok, msg = _client(timeout=1).connect()
        assert ok is False
        assert "隧道已建立" in msg
        assert "10.0.0.5:8080" in msg
        assert "账号或密码" not in msg


class TestDisconnect:
    def test_disconnect_terminates_process(self, monkeypatch):
        import src.vpn_client as mod
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: False)
        client = _client()
        proc = _FakeProc("oc.exe", ["x"], exit_code=0)
        client._proc = proc
        ok, msg = client.disconnect()
        assert ok is True
        assert proc.terminated is True
        assert client._proc is None


class TestProbeTargetParsing:
    def test_https_without_port_defaults_443(self):
        from src.vpn_client import parse_probe_target
        assert parse_probe_target("https://zentao.example.com/zentao") == (
            "zentao.example.com", 443)

    def test_http_without_port_defaults_80(self):
        from src.vpn_client import parse_probe_target
        assert parse_probe_target("http://10.10.3.67/index.php") == (
            "10.10.3.67", 80)

    def test_explicit_port_kept(self):
        from src.vpn_client import parse_probe_target
        assert parse_probe_target("http://10.10.3.67:8080/x") == (
            "10.10.3.67", 8080)

    def test_no_scheme_keeps_zero(self):
        from src.vpn_client import parse_probe_target
        assert parse_probe_target("10.0.0.1") == ("10.0.0.1", 0)


class TestBundledScript:
    def test_script_passed_when_bundled(self, tmp_path, monkeypatch):
        import src.vpn_client as mod
        exe = tmp_path / "openconnect.exe"
        exe.write_text("", encoding="utf-8")
        script = tmp_path / "vpnc-script-win.js"
        script.write_text("// script", encoding="utf-8")
        monkeypatch.setattr(mod, "find_openconnect", lambda: str(exe))
        monkeypatch.setattr(mod, "is_reachable", lambda *a, **k: True)
        procs = []

        def fake_popen(cmd, **kwargs):
            p = _FakeProc(cmd, ["Configured as 10.0.0.2, with SSL disconnected and DTLS established"], **kwargs)
            procs.append(p)
            return p

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        ok, _ = _client(probe_host="", probe_port=0).connect()
        assert ok is True
        assert any(f"--script={script}" in p.cmd for p in procs)
