"""更新器版本一致性守卫：防止发布窗口内 CDN 旧元数据把旧包当新版本安装"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestReleaseTagFromUrl:
    def test_parses_release_tag(self):
        from gui.workers import _release_tag_from_url
        url = ("https://github.com/o/r/releases/download/"
               "v2.8.16/SmartDefectPlatform.zip")
        assert _release_tag_from_url(url) == "2.8.16"

    def test_non_release_url_returns_empty(self):
        from gui.workers import _release_tag_from_url
        assert _release_tag_from_url("https://mirror.example/pkg.zip") == ""
        assert _release_tag_from_url("") == ""


class TestValidateUpdateVersion:
    def test_mismatched_url_is_rejected(self):
        from gui.workers import _validate_update_version
        err = _validate_update_version(
            "https://github.com/o/r/releases/download/v2.8.16/x.zip",
            "2.8.17")
        assert "v2.8.17" in err and "v2.8.16" in err

    def test_matching_url_passes(self):
        from gui.workers import _validate_update_version
        assert _validate_update_version(
            "https://github.com/o/r/releases/download/v2.8.17/x.zip",
            "2.8.17") == ""

    def test_url_without_tag_passes(self):
        from gui.workers import _validate_update_version
        assert _validate_update_version(
            "https://mirror.example/pkg.zip", "2.8.17") == ""

    def test_package_version_mismatch_is_rejected(self, tmp_path):
        from gui.workers import _validate_update_version
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "VERSION").write_text("2.8.16", encoding="utf-8")
        err = _validate_update_version(
            "https://github.com/o/r/releases/download/v2.8.17/x.zip",
            "2.8.17", str(tmp_path))
        assert "实际 v2.8.16" in err

    def test_package_version_match_passes(self, tmp_path):
        from gui.workers import _validate_update_version
        (tmp_path / "VERSION").write_text("2.8.17\n", encoding="utf-8")
        assert _validate_update_version(
            "https://github.com/o/r/releases/download/v2.8.17/x.zip",
            "2.8.17", str(tmp_path)) == ""

    def test_package_without_version_passes(self, tmp_path):
        from gui.workers import _validate_update_version
        assert _validate_update_version(
            "https://github.com/o/r/releases/download/v2.8.17/x.zip",
            "2.8.17", str(tmp_path)) == ""


def test_download_worker_blocks_before_download(qapp):
    """meta 版本与下载地址不一致时，不发起任何下载直接报错"""
    import gui.workers as wmod

    class Info:
        version = "2.8.17"
        sha256 = "old-sha"
        download_url = ("https://github.com/o/r/releases/download/"
                        "v2.8.16/SmartDefectPlatform.zip")

    events = []
    worker = wmod.UpdateDownloadWorker({}, Info(), None, [])
    worker.error.connect(lambda msg, detail: events.append(msg))
    worker.finished.connect(lambda path: events.append("finished"))

    worker.run()

    assert "finished" not in events
    assert events and "版本信息不一致" in events[0]
