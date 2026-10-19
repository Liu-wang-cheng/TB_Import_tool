"""界面方案配置：文件存储 + 主窗口快照/恢复逻辑

覆盖：新建/覆盖保存、损坏容错、原子写；全部界面配置项的序列化与恢复、
占位项不触发恢复、恢复后联动处理器被调用。
"""
import os
from unittest.mock import MagicMock

from PyQt6.QtCore import QDate, QTime, Qt

from gui.ui_profiles import load_profiles, save_profile


class TestUIProfileStore:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_profiles(str(tmp_path / "nope.yaml")) == []

    def test_save_and_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "ui_profiles.yaml")
        prof = {"name": "禅道-11", "platform": "zentao",
                "fields": {"server": "http://x", "account": "u",
                           "password": "p", "product": "11",
                           "module": "136", "status": "激活",
                           "assignees": ["IOT-陈斌"]}}
        assert save_profile(path, prof) is True
        assert load_profiles(path) == [prof]

    def test_upsert_overwrites_same_name(self, tmp_path):
        path = str(tmp_path / "ui_profiles.yaml")
        save_profile(path, {"name": "A", "platform": "zentao", "fields": {}})
        save_profile(path, {"name": "B", "platform": "zentao", "fields": {}})
        save_profile(path, {"name": "A", "platform": "teambition",
                            "fields": {"product": "20"}})
        got = load_profiles(path)
        assert [p["name"] for p in got] == ["A", "B"]
        assert got[0]["platform"] == "teambition"
        assert got[0]["fields"]["product"] == "20"

    def test_corrupt_file_ignored(self, tmp_path):
        path = tmp_path / "ui_profiles.yaml"
        path.write_text("- not: a dict\n", encoding="utf-8")
        assert load_profiles(str(path)) == []
        path.write_text(":: bad: [yaml", encoding="utf-8")
        assert load_profiles(str(path)) == []

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        path = str(tmp_path / "ui_profiles.yaml")
        save_profile(path, {"name": "A", "platform": "zentao", "fields": {}})
        assert not os.path.exists(path + ".tmp")
        assert os.path.exists(path)

    def test_empty_name_rejected(self, tmp_path):
        path = str(tmp_path / "ui_profiles.yaml")
        assert save_profile(path, {"name": "  ", "fields": {}}) is False
        assert not os.path.exists(path)


# ── 轻量假控件（避免测试依赖 QApplication） ──────────────────

class _FakeLine:
    def __init__(self, text=""):
        self._text = text
        self.blocked = None

    def text(self):
        return self._text

    def setText(self, v):
        self._text = v

    def blockSignals(self, flag):
        self.blocked = flag


class _FakeCombo:
    def __init__(self, items, idx=0):
        self.items = list(items)
        self.idx = idx

    def currentText(self):
        return self.items[self.idx] if 0 <= self.idx < len(self.items) else ""

    def currentIndex(self):
        return self.idx

    def findText(self, text):
        return self.items.index(text) if text in self.items else -1

    def setCurrentIndex(self, idx):
        self.idx = idx

    def count(self):
        return len(self.items)

    def blockSignals(self, flag):
        pass


class _FakeCheck:
    def __init__(self, checked=False):
        self.checked = checked

    def isChecked(self):
        return self.checked

    def setChecked(self, v):
        self.checked = bool(v)

    def blockSignals(self, flag):
        pass


class _FakeDate:
    def __init__(self, text=""):
        self.min = QDate(2000, 1, 1)
        self.d = QDate.fromString(text, "yyyy-MM-dd") if text else self.min

    def date(self):
        return self.d

    def minimumDate(self):
        return self.min

    def setDate(self, d):
        self.d = d

    def blockSignals(self, flag):
        pass


class _FakeTime:
    def __init__(self, text="09:00"):
        self.t = QTime.fromString(text, "HH:mm")

    def time(self):
        return self.t

    def setTime(self, t):
        self.t = t

    def blockSignals(self, flag):
        pass


class TestUIProfileWidgetCodec:
    def test_text_and_combo_set(self):
        line = _FakeLine("old")
        self._set(line, "text", "new")
        assert line.text() == "new"
        combo = _FakeCombo(["激活", "已关闭"], 0)
        self._set(combo, "text_sel", "已关闭")
        assert combo.currentIndex() == 1

    def test_index_clamped(self):
        combo = _FakeCombo(["a", "b"], 0)
        self._set(combo, "index", 99)
        assert combo.currentIndex() == 1
        self._set(combo, "index", "bad")
        assert combo.currentIndex() == 0

    def test_date_empty_means_unlimited(self):
        d = _FakeDate("2026-09-01")
        self._set(d, "date", "")
        assert d.date() == d.minimumDate()
        self._set(d, "date", "2026-10-01")
        assert d.date().toString("yyyy-MM-dd") == "2026-10-01"

    def test_time_roundtrip(self):
        t = _FakeTime("09:00")
        self._set(t, "time", "18:30")
        assert t.time().toString("HH:mm") == "18:30"
        self._set(t, "time", "bad")
        assert t.time().toString("HH:mm") == "18:30"  # 非法值不覆盖

    def test_bools_list(self):
        chks = [_FakeCheck(True), _FakeCheck(False)]
        self._set(chks, "bools", [False, True])
        assert [c.isChecked() for c in chks] == [False, True]

    @staticmethod
    def _set(widget, kind, value):
        from gui.main_window import MainWindow
        MainWindow._ui_profile_widget_set(widget, kind, value)


class TestMainWindowProfile:
    def _mw(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._ui_profile_widget_get = MainWindow._ui_profile_widget_get
        mw._ui_profile_widget_set = MainWindow._ui_profile_widget_set
        mw.chk_weekdays = []
        return mw

    def test_collect_full_snapshot(self):
        from gui.main_window import MainWindow
        mw = self._mw()
        line = _FakeLine("11")
        combo = _FakeCombo(["激活", "已关闭"], 0)
        check = _FakeCheck(True)
        date = _FakeDate("2026-09-01")
        tm = _FakeTime("18:30")
        mw._ui_profile_widgets = lambda: [
            ("product", line, "text"),
            ("status", combo, "text_sel"),
            ("reopen", check, "bool"),
            ("date_from", date, "date"),
            ("schedule_time", tm, "time"),
        ]
        mw.filter_platform.currentIndex.return_value = 0
        mw.filter_assigned.count.return_value = 2
        i1 = MagicMock()
        i1.checkState.return_value = Qt.Checked
        i1.text.return_value = "IOT-陈斌"
        i2 = MagicMock()
        i2.checkState.return_value = Qt.Unchecked
        i2.text.return_value = "算法-张三"
        mw.filter_assigned.item.side_effect = lambda i: [i1, i2][i]

        prof = MainWindow._collect_ui_profile(mw, "方案A")
        assert prof["name"] == "方案A"
        assert prof["platform"] == "zentao"
        f = prof["fields"]
        assert f["product"] == "11"
        assert f["status"] == "激活"
        assert f["reopen"] is True
        assert f["date_from"] == "2026-09-01"
        assert f["schedule_time"] == "18:30"
        assert f["assignees"] == ["IOT-陈斌"]

    def test_apply_restores_all_and_triggers_handlers(self):
        from gui.main_window import MainWindow
        mw = self._mw()
        line = _FakeLine("old")
        combo = _FakeCombo(["激活", "已关闭"], 0)
        check = _FakeCheck(False)
        date = _FakeDate("2026-09-01")
        tm = _FakeTime("09:00")
        mw._ui_profile_widgets = lambda: [
            ("product", line, "text"),
            ("status", combo, "text_sel"),
            ("reopen", check, "bool"),
            ("date_from", date, "date"),
            ("schedule_time", tm, "time"),
        ]
        mw.filter_platform.currentIndex.return_value = 0
        mw.cmb_executor_mode.currentIndex.return_value = 1
        mw.filter_date_mode.currentIndex.return_value = 0
        mw.chk_ai_analysis.checkState.return_value = Qt.Checked
        profile = {"name": "P", "platform": "zentao", "fields": {
            "product": "20", "status": "已关闭", "reopen": True,
            "date_from": "", "schedule_time": "18:30"}}

        MainWindow._apply_ui_profile(mw, profile)
        assert line.text() == "20"
        assert combo.currentIndex() == 1
        assert check.isChecked() is True
        assert date.date() == date.minimumDate()   # 空串恢复为"不限"
        assert tm.time().toString("HH:mm") == "18:30"
        mw._restore_profile_assignees.assert_called_once()
        mw._on_executor_mode_changed.assert_called_once_with(1)
        mw._on_date_mode_changed.assert_called_once_with(0)
        mw._save_reopen_switch.assert_called_once()
        mw._on_ai_switch_changed.assert_called_once()
        mw._on_scheduled_switch_changed.assert_called_once()

    def test_apply_switches_platform_first(self):
        from gui.main_window import MainWindow
        mw = self._mw()
        mw._ui_profile_widgets = lambda: []
        mw.filter_platform.currentIndex.return_value = 0
        MainWindow._apply_ui_profile(
            mw, {"name": "TB", "platform": "teambition", "fields": {}})
        mw.filter_platform.setCurrentIndex.assert_called_once_with(1)

    def test_selected_ignores_placeholder(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._ui_profiles = [{"name": "A"}, {"name": "B"}]
        MainWindow._on_ui_profile_selected(mw, 0)   # 占位项
        mw._apply_ui_profile.assert_not_called()
        MainWindow._on_ui_profile_selected(mw, 2)   # 第二个方案
        mw._apply_ui_profile.assert_called_once_with({"name": "B"})
        mw._log.assert_called_once()

    def test_refresh_loads_names_without_auto_apply(self, tmp_path,
                                                    monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._ui_profiles_path.return_value = str(tmp_path / "p.yaml")

        class _Combo:
            def __init__(self):
                self.items = []
                self.idx = None

            def blockSignals(self, flag):
                pass

            def clear(self):
                self.items = []

            def addItem(self, text):
                self.items.append(text)

            def findText(self, text):
                return self.items.index(text) if text in self.items else -1

            def setCurrentIndex(self, idx):
                self.idx = idx

        mw.cmb_ui_profile = _Combo()
        monkeypatch.setattr(mw_mod, "load_profiles",
                            lambda p: [{"name": "方案A"}, {"name": "方案B"}])
        MainWindow._refresh_ui_profiles(mw)
        assert mw.cmb_ui_profile.items == ["（选择方案载入…）", "方案A", "方案B"]
        assert mw.cmb_ui_profile.idx == 0
        mw._apply_ui_profile.assert_not_called()
        MainWindow._refresh_ui_profiles(mw, select_name="方案B")
        assert mw.cmb_ui_profile.idx == 2


class TestSaveProfileFlow:
    """保存按钮流程：命名→写盘→刷新；取消不写；重名需确认"""

    def _mw(self, tmp_path):
        mw = MagicMock()
        mw._ui_profiles_path.return_value = str(tmp_path / "p.yaml")
        mw._ui_profiles = []
        mw._collect_ui_profile.return_value = {
            "name": "新方案", "platform": "zentao",
            "fields": {"product": "11"}}
        return mw

    def test_save_new_profile_writes_file(self, tmp_path, monkeypatch):
        from gui import qt_compat
        from gui.main_window import MainWindow
        mw = self._mw(tmp_path)
        fake = MagicMock()
        fake.getText.return_value = ("新方案", True)
        monkeypatch.setattr(qt_compat, "QInputDialog", fake)
        MainWindow._on_save_profile(mw)
        got = load_profiles(str(tmp_path / "p.yaml"))
        assert got and got[0]["name"] == "新方案"
        mw._refresh_ui_profiles.assert_called_once_with(select_name="新方案")
        mw._log.assert_called_once()

    def test_cancel_dialog_writes_nothing(self, tmp_path, monkeypatch):
        from gui import qt_compat
        from gui.main_window import MainWindow
        mw = self._mw(tmp_path)
        fake = MagicMock()
        fake.getText.return_value = ("新方案", False)
        monkeypatch.setattr(qt_compat, "QInputDialog", fake)
        MainWindow._on_save_profile(mw)
        assert load_profiles(str(tmp_path / "p.yaml")) == []

    def test_overwrite_requires_confirm(self, tmp_path, monkeypatch):
        from gui import qt_compat, main_window as mw_mod
        from gui.main_window import MainWindow
        mw = self._mw(tmp_path)
        path = mw._ui_profiles_path.return_value
        save_profile(path, {"name": "新方案", "platform": "zentao",
                            "fields": {}})
        mw._ui_profiles = [{"name": "新方案"}]
        fake = MagicMock()
        fake.getText.return_value = ("新方案", True)
        monkeypatch.setattr(qt_compat, "QInputDialog", fake)
        box = MagicMock()
        monkeypatch.setattr(mw_mod, "QMessageBox", box)
        # 选"否"：不覆盖
        box.question.return_value = box.No
        MainWindow._on_save_profile(mw)
        assert load_profiles(path)[0]["fields"] == {}
        # 选"是"：覆盖
        box.question.return_value = box.Yes
        MainWindow._on_save_profile(mw)
        assert load_profiles(path)[0]["fields"] == {"product": "11"}
