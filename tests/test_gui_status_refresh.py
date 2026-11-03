"""状态码刷新：VPN 未连接时不得在 GUI 线程发起网络请求（避免卡顿）"""
from unittest.mock import MagicMock

import pytest


def _make_mw(vpn_enabled: bool):
    from gui.main_window import MainWindow
    mw = MagicMock()
    mw.config = {
        "source": {"platform": "zentao"},
        "zentao": {"base_url": "http://10.10.3.67:8080",
                   "account": "u", "password": "p",
                   "filters": {"product": "419"}},
        "sync": {},
    }
    mw._vpn_config = {"enabled": vpn_enabled}
    mw._current_source_server = lambda: "10.10.3.67:8080"
    mw._refresh_status_codes = (
        lambda: MainWindow._refresh_status_codes(mw))
    return mw


class TestStatusRefreshVpnGuard:
    def test_skips_when_vpn_unreachable(self, monkeypatch):
        import src.vpn_client as vpn_mod
        import src.zentao_client as zt_mod
        import src.source_factory as sf_mod

        calls = []
        monkeypatch.setattr(vpn_mod, "is_reachable",
                            lambda *a, **k: calls.append(a) or False)
        fake_cache = {}
        monkeypatch.setattr(sf_mod, "_CLIENT_CACHE", fake_cache)
        fake_client_cls = MagicMock()
        monkeypatch.setattr(zt_mod, "ZentaoClient", fake_client_cls)

        mw = _make_mw(vpn_enabled=True)
        mw._refresh_status_codes()
        assert calls, "应做过一次连通性探测"
        assert fake_cache == {}, "VPN 不可达时不应创建/认证客户端"
        fake_client_cls.assert_not_called()

    def test_refreshes_when_vpn_reachable(self, monkeypatch):
        import src.vpn_client as vpn_mod
        import src.zentao_client as zt_mod
        import src.source_factory as sf_mod

        monkeypatch.setattr(vpn_mod, "is_reachable", lambda *a, **k: True)
        fake_cache = {}
        monkeypatch.setattr(sf_mod, "_CLIENT_CACHE", fake_cache)
        client = MagicMock()
        client.fetch_status_groups.return_value = None
        client._cloud_browse_cache = {"x": 1}  # 跳过预热
        fake_client_cls = MagicMock(return_value=client)
        monkeypatch.setattr(zt_mod, "ZentaoClient", fake_client_cls)

        mw = _make_mw(vpn_enabled=True)
        mw._refresh_status_codes()
        # 缓存 key 必须与 source_factory 一致（base_url/account/password）
        assert ("http://10.10.3.67:8080", "u", "p") in fake_cache
        client.fetch_status_groups.assert_called_once()

    def test_no_vpn_refreshes_normally(self, monkeypatch):
        import src.zentao_client as zt_mod
        import src.source_factory as sf_mod

        fake_cache = {}
        monkeypatch.setattr(sf_mod, "_CLIENT_CACHE", fake_cache)
        client = MagicMock()
        client.fetch_status_groups.return_value = None
        client._cloud_browse_cache = {"x": 1}
        monkeypatch.setattr(zt_mod, "ZentaoClient",
                            MagicMock(return_value=client))
        mw = _make_mw(vpn_enabled=False)
        mw._refresh_status_codes()
        assert ("http://10.10.3.67:8080", "u", "p") in fake_cache


class TestProgressBarReset:
    """任务结束后不得残留不确定模式（滚动）"""

    def test_worker_finished_resets_indeterminate(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw.progress_bar.maximum.return_value = 0
        MainWindow._worker_finished(mw, save_config=False)
        mw.progress_bar.setRange.assert_called_with(0, 100)
        mw.progress_bar.setValue.assert_called_with(100)

    def test_list_result_resets_range(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._worker = None
        mw._worker_finished = MagicMock()
        mw.config = {}
        mw._log = MagicMock()
        mw.status_label = MagicMock()
        MainWindow._on_list_result(mw, [])
        mw.progress_bar.setRange.assert_called_with(0, 100)


class TestProgressWatchdog:
    """空闲看门狗：没有任务运行时强制清掉滚动状态"""

    def test_resets_when_idle(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._worker = None
        mw._update_download_worker = None
        mw._update_check_worker = None
        mw._collab_worker = None
        mw.progress_bar.maximum.return_value = 0
        MainWindow._reset_stuck_progress(mw)
        mw.progress_bar.setRange.assert_called_with(0, 100)
        mw.progress_bar.setValue.assert_called_with(0)

    def test_keeps_scrolling_while_worker_running(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        w = MagicMock()
        w.isRunning.return_value = True
        mw._worker = w
        mw._update_download_worker = None
        mw._update_check_worker = None
        mw._collab_worker = None
        mw.progress_bar.maximum.return_value = 0
        MainWindow._reset_stuck_progress(mw)
        mw.progress_bar.setRange.assert_not_called()


class TestProgressSignalGuards:
    """迟到的进度信号不得把已复位/已完成的任务重新置为滚动模式"""

    def test_progress_message_shows_indeterminate(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        w = MagicMock()
        w.isRunning.return_value = True
        mw._worker = w
        MainWindow._on_progress_message(mw, "获取到 100 条缺陷")
        mw.status_label.setText.assert_called_with("获取到 100 条缺陷")
        mw.progress_bar.setRange.assert_called_with(0, 0)
        mw.progress_bar.setFormat.assert_called_with("获取到 100 条缺陷")

    def test_stale_sync_progress_ignored_after_finish(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._worker = None
        MainWindow._on_sync_progress(mw, 0, 0, "VPN 已断开")
        mw.progress_bar.setRange.assert_not_called()
        mw.progress_bar.setFormat.assert_not_called()

    def test_stale_update_progress_ignored_after_finish(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._update_download_worker = None
        MainWindow._on_update_download_progress(mw, 0, 0, "下载完成")
        mw.progress_bar.setRange.assert_not_called()

    def test_start_worker_refuses_while_update_check_running(
            self, monkeypatch):
        import gui.main_window as mw_mod
        from gui.main_window import MainWindow
        monkeypatch.setattr(mw_mod.QMessageBox, "information",
                            lambda *a, **k: None)
        mw = MagicMock()
        mw._worker = None
        mw._update_download_worker = None
        checker = MagicMock()
        checker.isRunning.return_value = True
        mw._update_check_worker = checker
        assert MainWindow._start_worker(mw, MagicMock()) is False
        assert mw._worker is None
