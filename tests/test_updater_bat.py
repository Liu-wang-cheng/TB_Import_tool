"""测试更新 bat 模板：动态路径、占用重试、唯一旧目录名、更新日志"""
import os
import tempfile
import shutil

from gui.workers import (
    _generate_updater_bat, _remove_tree_with_retries,
    find_other_app_instances,
)


def make_fake_dir():
    tmp = tempfile.mkdtemp()
    open(os.path.join(tmp, "智能缺陷管理平台.exe"), "w").close()
    return tmp


class TestUpdaterBatNoAbsoluteChinesePath:
    def test_bat_uses_dynamic_paths_only(self):
        tmp = make_fake_dir()
        try:
            extract = os.path.join(tmp, "_update_extracted")
            bat = _generate_updater_bat(
                tmp, os.path.join(extract, "智能缺陷管理平台"),
                12345, extract)
            # 不嵌入安装绝对路径
            assert tmp not in bat
            # 不嵌入中文 exe 名作为路径引用
            assert '"智能缺陷管理平台.exe"' not in bat
            # 全部动态解析
            assert "cd /d \"%~dp0\"" in bat
            assert "%NEW_DIR%" in bat
            assert "%EXE_NAME%" in bat
        finally:
            shutil.rmtree(tmp)

    def test_bat_locates_new_exe_in_extract_dir(self):
        tmp = make_fake_dir()
        try:
            extract = os.path.join(tmp, "_update_extracted")
            os.makedirs(os.path.join(extract, "智能缺陷管理平台"), exist_ok=True)
            bat = _generate_updater_bat(
                tmp, os.path.join(extract, "智能缺陷管理平台"),
                12345, extract)
            # 递归遍历 _update_extracted 找新 exe，而非写死路径
            assert 'for /r "_update_extracted" %%f in (*.exe)' in bat
            assert 'set "NEW_DIR=%%~dpf"' in bat
        finally:
            shutil.rmtree(tmp)


class TestUpdaterBatResilience:
    """文件占用场景：唯一旧目录名、重试、回滚、日志、VPN 子进程清理"""

    def _bat(self):
        tmp = make_fake_dir()
        extract = os.path.join(tmp, "_update_extracted")
        bat = _generate_updater_bat(
            tmp, os.path.join(extract, "智能缺陷管理平台"), 12345, extract)
        shutil.rmtree(tmp)
        return bat

    def test_unique_old_dir_avoids_stale_name_conflict(self):
        bat = self._bat()
        # 旧目录必须唯一命名，不能复用可能被上一次更新残留占用的固定名
        assert 'set "OLD_DIR=_internal_old_%RANDOM%%RANDOM%"' in bat
        assert 'rename "_internal" "%OLD_DIR%"' in bat

    def test_rename_and_copy_have_retries(self):
        bat = self._bat()
        assert ":rename_try" in bat
        assert "RENAME_TRY+=1" in bat
        assert "/r:5 /w:2" in bat          # robocopy 重试
        assert ":copy_exe_try" in bat

    def test_failure_rolls_back_old_version(self):
        bat = self._bat()
        assert ":rollback" in bat
        assert 'rename "%OLD_DIR%" "_internal"' in bat
        assert ":rollback_stuck" in bat    # 回滚也失败时提示重启自愈

    def test_current_exe_passed_via_env(self):
        bat = self._bat()
        # 当前 exe 由调用方通过环境变量传入，避免目录里有多个 exe 时选错
        assert "%TB_UPDATE_EXE%" in bat
        assert "for %%f in (\"%~dp0*.exe\")" in bat  # 兜底逻辑保留

    def test_kills_only_bundled_openconnect(self):
        bat = self._bat()
        assert ":kill_bundled_openconnect" in bat
        assert "$env:TB_APP_DIR" in bat
        assert "openconnect.exe" in bat

    def test_writes_update_log(self):
        bat = self._bat()
        assert "logs\\update.log" in bat
        assert "call :log" in bat

    def test_pause_on_errors_keeps_window_visible(self):
        bat = self._bat()
        assert "timeout /t 20 /nobreak" in bat


class TestRemoveTreeWithRetries:
    def test_removes_existing_dir(self, tmp_path):
        target = tmp_path / "_update_extracted"
        target.mkdir()
        (target / "a.dll").write_text("x", encoding="utf-8")
        assert _remove_tree_with_retries(str(target), attempts=1) is True
        assert not target.exists()

    def test_missing_dir_is_success(self, tmp_path):
        assert _remove_tree_with_retries(str(tmp_path / "nope")) is True


class TestFindOtherInstances:
    def test_non_frozen_returns_empty(self, monkeypatch):
        import gui.workers as workers
        monkeypatch.setattr(workers.sys, "frozen", False, raising=False)
        assert find_other_app_instances() == []

    def test_parses_pids_and_excludes_self(self, monkeypatch):
        import gui.workers as workers
        import subprocess

        class R:
            stdout = f"{os.getpid()}\n999999\n"

        monkeypatch.setattr(workers.sys, "frozen", True, raising=False)
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
        assert find_other_app_instances() == [999999]

    def test_powershell_failure_returns_empty(self, monkeypatch):
        import gui.workers as workers
        import subprocess

        def boom(*a, **k):
            raise OSError("powershell missing")

        monkeypatch.setattr(workers.sys, "frozen", True, raising=False)
        monkeypatch.setattr(subprocess, "run", boom)
        assert find_other_app_instances() == []
