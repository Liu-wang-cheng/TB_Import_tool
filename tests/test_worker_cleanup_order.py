"""Worker 必须先完成清理（断开 VPN/关闭客户端）再通知界面。

否则 GUI 会提前解除忙碌状态，新任务可能与旧线程的 VPN 断开操作并发，
且迟到的 progress 信号会把已复位的进度条重新置为滚动模式。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeStats:
    def __str__(self):
        return "stats"


def _patch_sync_deps(monkeypatch, events):
    import gui.workers as wmod

    class FakeVpn:
        def __init__(self, cfg):
            pass

        def connect(self):
            events.append("vpn_connect")
            return True, "ok"

        def disconnect(self):
            events.append("vpn_disconnect")

    class FakeSource:
        source_type = "zentao"

        def authenticate(self):
            pass

        def close(self):
            events.append("source_close")

    class FakeTb:
        project_id = "p1"

        def authenticate(self):
            pass

        def close(self):
            events.append("tb_close")

    class FakeResolver:
        def __init__(self, *args, **kwargs):
            pass

        def resolve(self):
            pass

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, dry_run, progress_callback):
            progress_callback(0, 0, "阶段")
            return FakeStats()

    monkeypatch.setattr("src.vpn_client.VpnClient", FakeVpn)
    monkeypatch.setattr("src.config_resolver.ConfigResolver", FakeResolver)
    monkeypatch.setattr("src.sync_engine.SyncEngine", FakeEngine)
    monkeypatch.setattr(wmod, "_init_clients",
                        lambda cfg: (FakeSource(), FakeTb()))
    return wmod


def test_sync_worker_finishes_after_cleanup(qapp, monkeypatch):
    events = []
    wmod = _patch_sync_deps(monkeypatch, events)
    worker = wmod.SyncWorker(
        {"vpn": {"enabled": True, "disconnect_after": True},
         "teambition": {}}, dry_run=True)
    worker.progress.connect(lambda c, t, m: events.append(f"progress:{m}"))
    worker.finished.connect(lambda s: events.append("finished"))
    worker.error.connect(lambda m, d: events.append(f"error:{m}"))

    worker.run()

    assert events[-1] == "finished"
    assert events.index("vpn_disconnect") < events.index("finished")
    assert events.index("source_close") < events.index("finished")
    assert events.index("tb_close") < events.index("finished")
    # 断开 VPN 的提示也必须在完成信号之前
    assert (events.index("progress:VPN 已断开")
            < events.index("finished"))


def test_sync_worker_vpn_failure_emits_after_disconnect(qapp, monkeypatch):
    import gui.workers as wmod
    events = []

    class FakeVpn:
        def __init__(self, cfg):
            pass

        def connect(self):
            events.append("vpn_connect")
            return False, "超时"

        def disconnect(self):
            events.append("vpn_disconnect")

    monkeypatch.setattr("src.vpn_client.VpnClient", FakeVpn)
    worker = wmod.SyncWorker(
        {"vpn": {"enabled": True, "disconnect_after": True}})
    worker.finished.connect(lambda s: events.append("finished"))
    worker.error.connect(lambda m, d: events.append(f"error:{m}"))

    worker.run()

    assert "finished" not in events
    assert events[-1] == "error:VPN 连接失败: 超时"
    assert events.index("vpn_disconnect") < events.index(
        "error:VPN 连接失败: 超时")


def test_list_worker_vpn_failure_emits_after_disconnect(qapp, monkeypatch):
    import gui.workers as wmod
    events = []

    class FakeVpn:
        def __init__(self, cfg):
            pass

        def connect(self):
            events.append("vpn_connect")
            return False, "拒绝"

        def disconnect(self):
            events.append("vpn_disconnect")

    monkeypatch.setattr("src.vpn_client.VpnClient", FakeVpn)
    worker = wmod.ListBugsWorker(
        {"vpn": {"enabled": True, "disconnect_after": True},
         "source": {"platform": "zentao"}})
    worker.finished.connect(lambda bugs: events.append("finished"))
    worker.error.connect(lambda m, d: events.append(f"error:{m}"))

    worker.run()

    assert "finished" not in events
    assert events[-1].startswith("error:VPN 连接失败")
    assert events.index("vpn_disconnect") < len(events) - 1


def test_parse_worker_emits_after_vpn_disconnect(qapp, monkeypatch):
    import gui.workers as wmod
    events = []

    class FakeVpn:
        def __init__(self, cfg):
            pass

        def connect(self):
            return True, "ok"

        def disconnect(self):
            events.append("vpn_disconnect")

    class FakeBug:
        id = 123
        title = "t"
        product = "1"
        module = "2"
        moduleName = "m"
        project = "3"
        projectName = "p"

    class FakeSource:
        def authenticate(self):
            pass

        def fetch_bug_detail(self, bug_id):
            return FakeBug()

        def close(self):
            events.append("source_close")

    monkeypatch.setattr("src.vpn_client.VpnClient", FakeVpn)
    monkeypatch.setattr("src.source_factory.create_source_client",
                        lambda cfg: FakeSource())
    worker = wmod.ParseBugWorker(
        {"vpn": {"enabled": True, "disconnect_after": True}}, 123)
    worker.finished.connect(lambda info: events.append("finished"))
    worker.error.connect(lambda m, d: events.append(f"error:{m}"))

    worker.run()

    assert events[-1] == "finished"
    assert events.index("vpn_disconnect") < events.index("finished")
    assert events.index("source_close") < events.index("finished")
