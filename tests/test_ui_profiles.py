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

    def test_selected_ignores_placeholder(self, monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow
        saved = MagicMock()
        monkeypatch.setattr(mw_mod, "save_last_selected", saved)
        mw = MagicMock()
        mw._ui_profiles = [{"name": "A"}, {"name": "B"}]
        MainWindow._on_ui_profile_selected(mw, 0)   # 占位项
        mw._apply_ui_profile.assert_not_called()
        saved.assert_not_called()
        MainWindow._on_ui_profile_selected(mw, 2)   # 第二个方案
        mw._apply_ui_profile.assert_called_once_with({"name": "B"})
        saved.assert_called_once()
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
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._ui_profiles_path.return_value = str(tmp_path / "p.yaml")
        mw._ui_profiles = []
        mw._collect_ui_profile.side_effect = (
            lambda n: {"name": n, "platform": "zentao",
                       "fields": {"product": "11"}})
        # MagicMock 会遮蔽 self 方法调用，显式绑定真实实现驱动流程
        mw._current_ui_profile_name = (
            lambda: MainWindow._current_ui_profile_name(mw))
        mw._prompt_profile_name = (
            lambda title, initial="": MainWindow._prompt_profile_name(
                mw, title, initial))
        mw._save_profile_snapshot = (
            lambda name, select: MainWindow._save_profile_snapshot(
                mw, name, select))
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
        mw.cmb_ui_profile.currentIndex.return_value = 0   # 未关联当前方案
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


class TestScheduleProfileHelpers:
    """定时方案辅助：按名解析（保序/缺失）与按钮摘要文本"""

    def test_resolve_profiles_order_and_missing(self):
        from gui.ui_profiles import resolve_profiles
        all_p = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        found, missing = resolve_profiles(all_p, ["C", "X", "A"])
        assert [p["name"] for p in found] == ["C", "A"]
        assert missing == ["X"]
        found2, missing2 = resolve_profiles(all_p, [])
        assert found2 == [] and missing2 == []

    def test_format_profiles_summary(self):
        from gui.ui_profiles import format_profiles_summary
        assert format_profiles_summary([]) == "（按当前界面配置）"
        assert format_profiles_summary(["A", "B"]) == "A → B"
        long = format_profiles_summary(["方案" + str(i) for i in range(10)], 12)
        assert len(long) <= 12 and long.endswith("…")


class TestApplySkipScheduleFields:
    """定时任务按方案执行时：方案里的定时同步项必须被跳过"""

    def test_skip_schedule_fields(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._ui_profile_widget_set = MainWindow._ui_profile_widget_set
        mw._SCHEDULE_FIELD_KEYS = MainWindow._SCHEDULE_FIELD_KEYS
        mw.chk_weekdays = []
        product = _FakeLine("old")
        sched_chk = _FakeCheck(True)      # 当前正在启用的定时
        sched_time = _FakeTime("09:00")
        mw._ui_profile_widgets = lambda: [
            ("product", product, "text"),
            ("scheduled", sched_chk, "bool"),
            ("schedule_time", sched_time, "time"),
        ]
        mw.filter_platform.currentIndex.return_value = 0
        mw.cmb_executor_mode.currentIndex.return_value = 0
        mw.filter_date_mode.currentIndex.return_value = 0
        mw.chk_ai_analysis.checkState.return_value = Qt.Unchecked
        profile = {"name": "P", "platform": "zentao", "fields": {
            "product": "20", "scheduled": False, "schedule_time": "18:30"}}

        MainWindow._apply_ui_profile(mw, profile, skip_schedule_fields=True)
        assert product.text() == "20"
        assert sched_chk.isChecked() is True          # 定时开关未被方案覆盖
        assert sched_time.time().toString("HH:mm") == "09:00"
        mw._on_scheduled_switch_changed.assert_not_called()
        mw._on_time2_changed.assert_not_called()
        mw._on_schedule_mode_changed.assert_not_called()
        mw._on_ai_switch_changed.assert_called_once()  # 业务开关仍按方案生效


class TestScheduledProfileChain:
    """多方案定时导入：按顺序执行、失败继续、结束后恢复运行前界面"""

    def _mw(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._scheduled_chain = None
        mw._schedule_profiles = []
        mw._ui_profiles_path.return_value = "/tmp/none.yaml"
        mw._worker = None
        mw.log_text = MagicMock()
        mw.status_label = MagicMock()
        mw.progress_bar = MagicMock()
        mw.chk_scheduled_notify.isChecked.return_value = False
        mw._apply_filters_to_config = MagicMock()
        mw._apply_ui_profile = MagicMock()
        mw._collect_ui_profile.return_value = {"name": "__pre_schedule__"}
        mw._start_worker.return_value = True
        mw._worker_finished = MagicMock()
        mw._save_config_to_yaml = MagicMock()
        mw._set_busy = MagicMock()
        # 用真实实现驱动执行链
        mw._run_next_scheduled_profile = (
            lambda: MainWindow._run_next_scheduled_profile(mw))
        mw._finish_scheduled_chain = (
            lambda: MainWindow._finish_scheduled_chain(mw))
        return mw

    def test_profiles_run_in_order_then_restore(self, monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow
        mw = self._mw()
        profiles = [
            {"name": "A", "platform": "zentao", "fields": {"product": "11"}},
            {"name": "B", "platform": "teambition", "fields": {"module": "p1"}},
        ]
        monkeypatch.setattr(mw_mod, "load_profiles", lambda p: profiles)
        monkeypatch.setattr(mw_mod, "SyncWorker", MagicMock())
        mw._schedule_profiles = ["A", "B"]

        assert MainWindow._run_scheduled_sync(mw) is True
        assert mw._scheduled_chain is not None
        assert mw._scheduled_chain["total"] == 2
        mw._apply_ui_profile.assert_called_with(
            profiles[0], skip_schedule_fields=True)
        assert mw._start_worker.call_count == 1
        assert mw.status_label.setText.called

        # 第一个方案完成 → 自动执行第二个
        MainWindow._on_scheduled_profile_result(mw, "A: 新建 1")
        mw._apply_ui_profile.assert_called_with(
            profiles[1], skip_schedule_fields=True)
        assert mw._start_worker.call_count == 2

        # 第二个完成 → 收尾：恢复运行前快照
        MainWindow._on_scheduled_profile_result(mw, "B: 新建 2")
        assert mw._scheduled_chain is None
        mw._apply_ui_profile.assert_called_with(
            {"name": "__pre_schedule__"}, skip_schedule_fields=True)
        mw._save_config_to_yaml.assert_called_once()
        logs = "\n".join(str(c) for c in mw._log.call_args_list)
        assert "A: 新建 1" in logs and "B: 新建 2" in logs

    def test_profile_failure_continues_to_next(self, monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow
        mw = self._mw()
        profiles = [{"name": "A"}, {"name": "B"}]
        monkeypatch.setattr(mw_mod, "load_profiles", lambda p: profiles)
        monkeypatch.setattr(mw_mod, "SyncWorker", MagicMock())
        mw._schedule_profiles = ["A", "B"]
        MainWindow._run_scheduled_sync(mw)
        MainWindow._on_scheduled_profile_error(mw, "boom", "")
        # A 失败后 B 仍启动
        mw._apply_ui_profile.assert_called_with(
            profiles[1], skip_schedule_fields=True)
        assert mw._start_worker.call_count == 2

    def test_empty_selection_runs_current_ui(self, monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow
        mw = self._mw()
        monkeypatch.setattr(mw_mod, "SyncWorker", MagicMock())
        mw._schedule_profiles = []
        assert MainWindow._run_scheduled_sync(mw) is True
        assert mw._scheduled_chain is None
        mw._apply_filters_to_config.assert_called_once()
        mw._apply_ui_profile.assert_not_called()

    def test_missing_profiles_skip_without_fallback(self, monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow
        mw = self._mw()
        monkeypatch.setattr(mw_mod, "load_profiles", lambda p: [])
        mw._schedule_profiles = ["已删除的方案"]
        assert MainWindow._run_scheduled_sync(mw) is True  # 消费去重键
        assert mw._scheduled_chain is None
        mw._start_worker.assert_not_called()
        mw._apply_filters_to_config.assert_not_called()    # 不回退当前界面


class TestScheduledProfilesConfig:
    """定时方案选择持久化到 sync.yaml"""

    def test_save_scheduled_config_includes_profiles(self, tmp_path):
        import yaml
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._project_root = str(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "sync.yaml").write_text(
            "scheduled_sync: null\n", encoding="utf-8")
        mw.chk_scheduled.isChecked.return_value = True
        t1 = MagicMock()
        t1.toString.return_value = "09:00"
        mw.time_schedule.time.return_value = t1
        mw.chk_time2.isChecked.return_value = False
        t2 = MagicMock()
        t2.toString.return_value = "18:00"
        mw.time_schedule2.time.return_value = t2
        mw.chk_scheduled_notify.isChecked.return_value = True
        mw.cmb_schedule_mode.currentIndex.return_value = 0
        mw.chk_weekdays = []
        mw._schedule_profiles = ["方案A", "方案B"]
        mw.config = {}
        MainWindow._save_scheduled_config(mw)
        data = yaml.safe_load(
            (tmp_path / "configs" / "sync.yaml").read_text(encoding="utf-8"))
        assert data["scheduled_sync"]["profiles"] == ["方案A", "方案B"]


class TestScheduledProfilesLoad:
    """启动时从 sync.yaml 恢复定时方案选择"""

    def test_load_restores_schedule_profiles(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw.config = {"sync": {"scheduled_sync": {
            "enabled": True, "time": "10:30",
            "profiles": ["方案A", "方案B"]}}}
        mw.chk_weekdays = []
        mw._update_schedule_profiles_button = MagicMock()
        MainWindow._load_scheduled_config(mw)
        assert mw._schedule_profiles == ["方案A", "方案B"]
        mw._update_schedule_profiles_button.assert_called_once()

    def test_load_without_profiles_defaults_empty(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw.config = {"sync": {"scheduled_sync": {
            "enabled": False, "time": "09:00"}}}
        mw.chk_weekdays = []
        mw._schedule_profiles = ["旧选择"]
        mw._update_schedule_profiles_button = MagicMock()
        MainWindow._load_scheduled_config(mw)
        assert mw._schedule_profiles == []


class TestRememberLastSelected:
    """记住上次选择：选择后持久化，重新打开下拉框恢复选中"""

    def test_save_and_load_last_selected_preserves_profiles(self, tmp_path):
        from gui.ui_profiles import (
            save_profile, save_last_selected, load_last_selected,
            load_profiles,
        )
        path = str(tmp_path / "p.yaml")
        save_profile(path, {"name": "A", "platform": "zentao", "fields": {}})
        assert save_last_selected(path, "A") is True
        assert load_last_selected(path) == "A"
        assert [p["name"] for p in load_profiles(path)] == ["A"]
        # 再次保存方案不得丢掉 last_selected
        save_profile(path, {"name": "B", "platform": "zentao", "fields": {}})
        assert load_last_selected(path) == "A"
        assert [p["name"] for p in load_profiles(path)] == ["A", "B"]

    def test_missing_file_last_selected_empty(self, tmp_path):
        from gui.ui_profiles import load_last_selected
        assert load_last_selected(str(tmp_path / "none.yaml")) == ""

    def test_selecting_profile_remembered_and_config_saved(self, monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow
        saved = MagicMock()
        monkeypatch.setattr(mw_mod, "save_last_selected", saved)
        mw = MagicMock()
        mw._ui_profiles = [{"name": "A"}, {"name": "B"}]
        MainWindow._on_ui_profile_selected(mw, 1)
        mw._apply_ui_profile.assert_called_once_with({"name": "A"})
        saved.assert_called_once()
        assert saved.call_args[0][1] == "A"
        mw._save_config_to_yaml.assert_called_once()

    def test_refresh_restores_last_selected(self, tmp_path, monkeypatch):
        from gui import main_window as mw_mod
        from gui.main_window import MainWindow

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

        mw = MagicMock()
        mw._ui_profiles_path.return_value = str(tmp_path / "p.yaml")
        mw.cmb_ui_profile = _Combo()
        monkeypatch.setattr(mw_mod, "load_profiles",
                            lambda p: [{"name": "方案A"}, {"name": "方案B"}])
        monkeypatch.setattr(mw_mod, "load_last_selected", lambda p: "方案B")
        MainWindow._refresh_ui_profiles(mw)     # 不传 select_name
        assert mw.cmb_ui_profile.idx == 2       # 占位(0) + A(1) + B(2)
        mw._apply_ui_profile.assert_not_called()  # 只恢复选中，不重复应用


class TestApplyExternalTBProfile:
    """外部TB 方案：先切平台，再恢复 URL/项目ID/账号密码等字段"""

    def test_apply_teambition_profile_restores_fields(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._ui_profile_widget_set = MainWindow._ui_profile_widget_set
        mw._SCHEDULE_FIELD_KEYS = MainWindow._SCHEDULE_FIELD_KEYS
        mw.chk_weekdays = []
        url = _FakeLine("")
        module = _FakeLine("")
        account = _FakeLine("")
        password = _FakeLine("")
        mw._ui_profile_widgets = lambda: [
            ("url", url, "text"),
            ("module", module, "text"),
            ("account", account, "text"),
            ("password", password, "text"),
        ]
        mw.filter_platform.currentIndex.return_value = 0   # 当前在禅道
        mw.cmb_executor_mode.currentIndex.return_value = 0
        mw.filter_date_mode.currentIndex.return_value = 0
        mw.chk_ai_analysis.checkState.return_value = Qt.Unchecked
        profile = {"name": "TB方案", "platform": "teambition", "fields": {
            "url": "https://www.teambition.com",
            "module": "6306e205,6306e206",
            "account": "u_tb", "password": "p_tb"}}

        MainWindow._apply_ui_profile(mw, profile, skip_schedule_fields=True)
        mw.filter_platform.setCurrentIndex.assert_called_once_with(1)
        assert url.text() == "https://www.teambition.com"
        assert module.text() == "6306e205,6306e206"
        assert account.text() == "u_tb"
        assert password.text() == "p_tb"


class TestSaveVsSaveAs:
    """保存=覆盖当前方案；另存为=新建方案；未关联方案时另存为置灰"""

    def _mw(self, tmp_path, current_idx=0, profiles=None):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw._ui_profiles_path.return_value = str(tmp_path / "p.yaml")
        mw._ui_profiles = list(profiles or [])
        mw.cmb_ui_profile.currentIndex.return_value = current_idx
        mw._collect_ui_profile.side_effect = (
            lambda n: {"name": n, "platform": "zentao",
                       "fields": {"product": "99"}})
        mw._current_ui_profile_name = (
            lambda: MainWindow._current_ui_profile_name(mw))
        mw._prompt_profile_name = (
            lambda title, initial="": MainWindow._prompt_profile_name(
                mw, title, initial))
        mw._save_profile_snapshot = (
            lambda name, select: MainWindow._save_profile_snapshot(
                mw, name, select))
        return mw

    def test_save_overwrites_current_without_dialog(self, tmp_path,
                                                    monkeypatch):
        from gui import qt_compat
        from gui.main_window import MainWindow
        save_profile(str(tmp_path / "p.yaml"),
                     {"name": "A", "platform": "zentao", "fields": {}})
        mw = self._mw(tmp_path, current_idx=1,
                      profiles=[{"name": "A"}])
        fake = MagicMock()
        monkeypatch.setattr(qt_compat, "QInputDialog", fake)
        MainWindow._on_save_profile(mw)
        fake.getText.assert_not_called()          # 直接覆盖，不弹命名框
        got = load_profiles(str(tmp_path / "p.yaml"))
        assert [p["name"] for p in got] == ["A"]
        assert got[0]["fields"] == {"product": "99"}
        mw._log.assert_called_once()

    def test_save_as_creates_new_profile(self, tmp_path, monkeypatch):
        from gui import qt_compat
        from gui.main_window import MainWindow
        save_profile(str(tmp_path / "p.yaml"),
                     {"name": "A", "platform": "zentao", "fields": {}})
        mw = self._mw(tmp_path, current_idx=1,
                      profiles=[{"name": "A"}])
        fake = MagicMock()
        fake.getText.return_value = ("B", True)
        monkeypatch.setattr(qt_compat, "QInputDialog", fake)
        MainWindow._on_save_profile_as(mw)
        got = load_profiles(str(tmp_path / "p.yaml"))
        assert [p["name"] for p in got] == ["A", "B"]
        assert got[1]["fields"] == {"product": "99"}
        mw._refresh_ui_profiles.assert_called_once_with(select_name="B")
        mw._log.assert_called_once()

    def test_save_as_noop_without_current(self, tmp_path, monkeypatch):
        from gui import qt_compat
        from gui.main_window import MainWindow
        mw = self._mw(tmp_path, current_idx=0, profiles=[{"name": "A"}])
        fake = MagicMock()
        monkeypatch.setattr(qt_compat, "QInputDialog", fake)
        MainWindow._on_save_profile_as(mw)
        fake.getText.assert_not_called()
        assert load_profiles(str(tmp_path / "p.yaml")) == []

    def test_buttons_state_follows_current_profile(self, tmp_path):
        from gui.main_window import MainWindow
        # 未关联方案 → 另存为置灰
        mw = self._mw(tmp_path, current_idx=0, profiles=[{"name": "A"}])
        mw.btn_save_profile = MagicMock()
        mw.btn_save_profile_as = MagicMock()
        MainWindow._update_profile_buttons(mw)
        mw.btn_save_profile_as.setEnabled.assert_called_once_with(False)
        # 关联方案 → 另存为可用
        mw2 = self._mw(tmp_path, current_idx=1, profiles=[{"name": "A"}])
        mw2.btn_save_profile = MagicMock()
        mw2.btn_save_profile_as = MagicMock()
        MainWindow._update_profile_buttons(mw2)
        mw2.btn_save_profile_as.setEnabled.assert_called_once_with(True)
