"""免 UAC 启动：计划任务与桌面快捷方式创建逻辑"""
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    import gui_main as gm
    monkeypatch.setattr(gm, "_is_frozen", lambda: True)
    monkeypatch.setattr(gm, "_is_admin", lambda: True)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0)

    monkeypatch.setattr(gm.subprocess, "run", fake_run)

    # 模拟桌面目录
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(gm.os.path, "expanduser", lambda p: str(tmp_path))

    lnk_holder = {}

    class _FakeLnk:
        def __init__(self):
            self.__dict__["saved"] = False

        def Save(self):
            self.__dict__["saved"] = True

    def fake_dispatch(name):
        shell = MagicMock()
        def create(path):
            lnk_holder["path"] = path
            fake = _FakeLnk()
            lnk_holder["lnk"] = fake
            return fake
        shell.CreateShortCut.side_effect = create
        shell.SpecialFolders = MagicMock(return_value=str(desktop))
        return shell

    import win32com.client
    monkeypatch.setattr(win32com.client, "Dispatch", fake_dispatch)
    return gm, calls, lnk_holder, desktop


class TestElevatedLauncher:
    def test_creates_task_and_shortcut(self, launcher):
        gm, calls, lnk_holder, desktop = launcher
        assert gm._ensure_elevated_launcher() is True
        assert len(calls) == 1
        args = calls[0]
        assert args[:3] == ["schtasks", "/create", "/tn"]
        assert "/RL" in args and "HIGHEST" in args
        assert "/IT" in args and "/F" in args
        assert any("SmartDefectPlatform_Elevated" in str(a) for a in args)
        assert lnk_holder["path"].endswith(".lnk")
        assert lnk_holder["lnk"].saved is True

    def test_skips_when_shortcut_exists(self, launcher):
        gm, calls, lnk_holder, desktop = launcher
        (desktop / gm._ELEVATED_LNK).write_text("x", encoding="utf-8")
        assert gm._ensure_elevated_launcher() is False
        # 任务仍会刷新（/F 幂等），但不再重复创建快捷方式
        assert len(calls) == 1

    def test_noop_when_not_admin(self, monkeypatch):
        import gui_main as gm
        monkeypatch.setattr(gm, "_is_frozen", lambda: True)
        monkeypatch.setattr(gm, "_is_admin", lambda: False)
        assert gm._ensure_elevated_launcher() is False

    def test_noop_when_not_frozen(self, monkeypatch):
        import gui_main as gm
        monkeypatch.setattr(gm, "_is_frozen", lambda: False)
        monkeypatch.setattr(gm, "_is_admin", lambda: True)
        assert gm._ensure_elevated_launcher() is False

    def test_schtasks_failure_returns_false(self, monkeypatch, tmp_path):
        import gui_main as gm
        monkeypatch.setattr(gm, "_is_frozen", lambda: True)
        monkeypatch.setattr(gm, "_is_admin", lambda: True)

        def boom(*a, **k):
            raise OSError("schtasks blocked by policy")

        monkeypatch.setattr(gm.subprocess, "run", boom)
        assert gm._ensure_elevated_launcher() is False


class TestShortcutNameAndFailure:
    def test_shortcut_uses_plain_name(self, launcher):
        gm, calls, lnk_holder, desktop = launcher
        gm._ensure_elevated_launcher()
        assert os.path.basename(lnk_holder["path"]) == "智能缺陷管理平台.lnk"

    def test_schtasks_nonzero_does_not_create_shortcut(self, monkeypatch,
                                                        tmp_path):
        import gui_main as gm
        monkeypatch.setattr(gm, "_is_frozen", lambda: True)
        monkeypatch.setattr(gm, "_is_admin", lambda: True)
        monkeypatch.setattr(gm.os.path, "expanduser", lambda p: str(tmp_path))
        monkeypatch.setattr(gm.subprocess, "run",
                            lambda *a, **k: MagicMock(returncode=1))
        dispatch = MagicMock()
        import win32com.client
        monkeypatch.setattr(win32com.client, "Dispatch", dispatch)
        assert gm._ensure_elevated_launcher() is False
        dispatch.assert_not_called()
