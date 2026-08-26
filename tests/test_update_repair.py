"""测试 gui_main 更新自愈逻辑（回滚/清理/继续更新）"""
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gui_main


@pytest.fixture
def app_dir(monkeypatch, tmp_path):
    """构造假安装目录并让 gui_main._get_app_dir 指向它"""
    d = str(tmp_path)
    monkeypatch.setattr(gui_main, "_get_app_dir", lambda: d)
    monkeypatch.setattr(gui_main, "_is_frozen", lambda: True)
    return d


def _mkdir(p):
    os.makedirs(p, exist_ok=True)
    return p


class TestRepairRollback:
    def test_rollback_when_internal_missing(self, app_dir):
        """_internal 缺失 + _internal_old 存在 → 自动回滚恢复"""
        _mkdir(os.path.join(app_dir, "_internal_old"))
        assert gui_main._repair_update_state() is False
        assert os.path.isdir(os.path.join(app_dir, "_internal"))
        assert not os.path.exists(os.path.join(app_dir, "_internal_old"))

    def test_cleanup_old_when_both_exist(self, app_dir):
        """新旧 _internal 都存在 → 清理 _internal_old，保留 _internal"""
        _mkdir(os.path.join(app_dir, "_internal"))
        _mkdir(os.path.join(app_dir, "_internal_old"))
        assert gui_main._repair_update_state() is False
        assert os.path.isdir(os.path.join(app_dir, "_internal"))
        assert not os.path.exists(os.path.join(app_dir, "_internal_old"))

    def test_noop_when_clean(self, app_dir):
        _mkdir(os.path.join(app_dir, "_internal"))
        assert gui_main._repair_update_state() is False


class TestRepairRetry:
    def test_retry_when_user_confirms(self, app_dir, monkeypatch):
        """bat 残留 + 解压目录有更高版本 exe + 用户确认 → 重新生成并进入更新"""
        extract = _mkdir(os.path.join(app_dir, "_update_extracted", "new"))
        open(os.path.join(extract, "新程序.exe"), "w").close()
        _mkdir(os.path.join(extract, "_internal"))
        open(os.path.join(extract, "_internal", "VERSION"), "w").write("2.8.0")
        bat = os.path.join(app_dir, "_update_replace.bat")
        open(bat, "w").close()
        monkeypatch.setattr(gui_main, "_current_version", lambda: "2.7.9")
        monkeypatch.setattr(gui_main, "_ask_retry_update", lambda: True)
        monkeypatch.setattr(gui_main, "_restart_to_update", lambda: True)
        assert gui_main._repair_update_state() is True

    def test_cancel_removes_stale_bat(self, app_dir, monkeypatch):
        """用户拒绝 → 删除残留 bat，正常启动"""
        extract = _mkdir(os.path.join(app_dir, "_update_extracted", "new"))
        open(os.path.join(extract, "新程序.exe"), "w").close()
        bat = os.path.join(app_dir, "_update_replace.bat")
        open(bat, "w").close()
        monkeypatch.setattr(gui_main, "_ask_retry_update", lambda: False)
        assert gui_main._repair_update_state() is False
        assert not os.path.exists(bat)
        # 解压目录被清理
        assert not os.path.exists(os.path.join(app_dir, "_update_extracted"))

    def test_no_retry_without_exe(self, app_dir, monkeypatch):
        """解压目录无 exe（下载损坏）→ 不询问，直接清理"""
        _mkdir(os.path.join(app_dir, "_update_extracted", "broken"))
        bat = os.path.join(app_dir, "_update_replace.bat")
        open(bat, "w").close()
        asked = []
        monkeypatch.setattr(gui_main, "_ask_retry_update",
                            lambda: asked.append(1) or True)
        assert gui_main._repair_update_state() is False
        assert asked == []
        assert not os.path.exists(bat)
        assert not os.path.exists(os.path.join(app_dir, "_update_extracted"))


class TestRepairNoFalseRetry:
    """更新成功后的残留不应触发"继续更新"弹窗"""

    def test_same_version_no_retry(self, app_dir, monkeypatch):
        """解压目录版本 == 当前版本（更新已完成，bat 尚未自删）→ 静默清理"""
        extract = _mkdir(os.path.join(app_dir, "_update_extracted", "new"))
        open(os.path.join(extract, "程序.exe"), "w").close()
        _mkdir(os.path.join(extract, "_internal"))
        open(os.path.join(extract, "_internal", "VERSION"), "w").write("2.7.9")
        open(os.path.join(app_dir, "_update_replace.bat"), "w").close()
        # 当前运行版本与解压目录一致
        monkeypatch.setattr(gui_main, "_current_version", lambda: "2.7.9")
        asked = []
        monkeypatch.setattr(gui_main, "_ask_retry_update",
                            lambda: asked.append(1) or True)
        assert gui_main._repair_update_state() is False
        assert asked == []  # 不弹窗
        assert not os.path.exists(os.path.join(app_dir, "_update_replace.bat"))
        assert not os.path.exists(os.path.join(app_dir, "_update_extracted"))

    def test_newer_version_still_asks(self, app_dir, monkeypatch):
        """解压目录版本高于当前版本 → 正常询问"""
        extract = _mkdir(os.path.join(app_dir, "_update_extracted", "new"))
        open(os.path.join(extract, "程序.exe"), "w").close()
        _mkdir(os.path.join(extract, "_internal"))
        open(os.path.join(extract, "_internal", "VERSION"), "w").write("2.8.0")
        open(os.path.join(app_dir, "_update_replace.bat"), "w").close()
        monkeypatch.setattr(gui_main, "_current_version", lambda: "2.7.9")
        asked = []
        monkeypatch.setattr(gui_main, "_ask_retry_update",
                            lambda: asked.append(1) or True)
        monkeypatch.setattr(gui_main, "_restart_to_update", lambda: True)
        assert gui_main._repair_update_state() is True
        assert asked == [1]

    def test_version_tuple_compares_numerically(self):
        assert gui_main._version_tuple("2.10.0") > gui_main._version_tuple("2.7.9")
        assert gui_main._version_tuple("2.7.9") > gui_main._version_tuple("2.7.8")
        assert gui_main._version_tuple("abc") == ()


class TestFindNewExe:
    def test_find_exe_recursive(self, tmp_path):
        d = str(tmp_path)
        _mkdir(os.path.join(d, "a", "b"))
        p = os.path.join(d, "a", "b", "程序.exe")
        open(p, "w").close()
        assert gui_main._find_new_exe(d) == p

    def test_none_when_no_exe(self, tmp_path):
        _mkdir(os.path.join(str(tmp_path), "a"))
        assert gui_main._find_new_exe(str(tmp_path)) is None
