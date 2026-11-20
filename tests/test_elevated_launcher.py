"""管理员权限启动：桌面快捷方式直指 exe，并清理旧版免 UAC 计划任务"""
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    import gui_main as gm
    monkeypatch.setattr(gm, "_is_frozen", lambda: True)
    monkeypatch.setattr(gm, "_is_admin", lambda: True)
    calls = []

    class R:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append(args)
        return R()

    monkeypatch.setattr(gm.subprocess, "run", fake_run)

    # 模拟桌面目录
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(gm.os.path, "expanduser", lambda p: str(tmp_path))

    lnk_holder = {}

    class _FakeLnk:
        def Save(self):
            self.saved = True

    def fake_dispatch(name):
        shell = MagicMock()
        def create(path):
            lnk_holder["path"] = path
            fake = _FakeLnk()
            fake.saved = False
            lnk_holder["lnk"] = fake
            return fake
        shell.CreateShortCut.side_effect = create
        return shell

    import win32com.client
    monkeypatch.setattr(win32com.client, "Dispatch", fake_dispatch)
    return gm, calls, lnk_holder, desktop


class TestDesktopShortcut:
    def test_creates_shortcut_pointing_to_exe(self, launcher):
        gm, calls, lnk_holder, desktop = launcher
        assert gm._ensure_desktop_shortcut() is True
        lnk = lnk_holder["lnk"]
        assert lnk.TargetPath == gm.sys.executable
        assert lnk.Arguments == ""
        assert lnk.WorkingDirectory == os.path.dirname(gm.sys.executable)
        assert lnk.Save is not None and lnk.saved is True
        assert lnk_holder["path"].endswith(".lnk")

    def test_migrates_old_schtasks_shortcut(self, launcher):
        """旧快捷方式指向 schtasks，启动后应被改写为直接指向 exe"""
        gm, calls, lnk_holder, desktop = launcher
        (desktop / gm._ELEVATED_LNK).write_text("x", encoding="utf-8")
        assert gm._ensure_desktop_shortcut() is False
        assert lnk_holder["lnk"].TargetPath == gm.sys.executable
        assert lnk_holder["lnk"].saved is True

    def test_noop_when_not_admin(self, monkeypatch):
        import gui_main as gm
        monkeypatch.setattr(gm, "_is_frozen", lambda: True)
        monkeypatch.setattr(gm, "_is_admin", lambda: False)
        assert gm._ensure_desktop_shortcut() is False

    def test_noop_when_not_frozen(self, monkeypatch):
        import gui_main as gm
        monkeypatch.setattr(gm, "_is_frozen", lambda: False)
        monkeypatch.setattr(gm, "_is_admin", lambda: True)
        assert gm._ensure_desktop_shortcut() is False


class TestLegacyTaskCleanup:
    def test_disables_running_task_and_removes_idle_task(self, launcher):
        gm, calls, _, _ = launcher
        gm._cleanup_legacy_elevated_task()
        assert len(calls) == 1
        cmd = " ".join(calls[0])
        assert "Disable-ScheduledTask" in cmd       # 正在运行 → 只禁用
        assert "Unregister-ScheduledTask" in cmd    # 未运行 → 删除
        assert gm._ELEVATED_TASK in cmd

    def test_errors_are_swallowed(self, monkeypatch):
        import gui_main as gm

        def boom(*a, **k):
            raise OSError("powershell missing")

        monkeypatch.setattr(gm.subprocess, "run", boom)
        gm._cleanup_legacy_elevated_task()  # 不抛异常


class TestShortcutFailure:
    def test_dispatch_failure_returns_false(self, launcher, monkeypatch):
        gm, _, _, _ = launcher
        import win32com.client

        def boom(name):
            raise OSError("no desktop")

        monkeypatch.setattr(win32com.client, "Dispatch", boom)
        assert gm._ensure_desktop_shortcut() is False

    def test_shortcut_uses_plain_name(self, launcher):
        gm, _, lnk_holder, _ = launcher
        gm._ensure_desktop_shortcut()
        assert os.path.basename(lnk_holder["path"]) == "智能缺陷管理平台.lnk"
