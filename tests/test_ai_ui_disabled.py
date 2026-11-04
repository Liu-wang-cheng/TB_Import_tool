"""AI 分析界面暂时停用：置灰、强制关闭，且配置内存同步为 false"""
from unittest.mock import MagicMock


def test_feature_flag_disabled():
    from gui.feature_flags import AI_ANALYSIS_UI_ENABLED
    assert AI_ANALYSIS_UI_ENABLED is False


def test_update_ai_sub_switches_disabled_even_if_checked():
    from gui.main_window import MainWindow
    mw = MagicMock()
    mw.chk_ai_analysis.isChecked.return_value = True
    MainWindow._update_ai_sub_switches(mw)
    for chk in (mw.chk_fault_pattern, mw.chk_specialized_prompt,
                mw.chk_knowledge_base):
        chk.setEnabled.assert_called_with(False)


def test_force_ai_off_sets_ui_and_config_false():
    from gui.main_window import MainWindow
    mw = MagicMock()
    mw.config = {"ai_analysis": {"enabled": True}}
    MainWindow._force_ai_analysis_ui_off(mw)
    mw.chk_ai_analysis.setChecked.assert_called_with(False)
    mw.chk_ai_analysis.setEnabled.assert_called_with(False)
    assert mw.config["ai_analysis"]["enabled"] is False
    for chk in (mw.chk_fault_pattern, mw.chk_specialized_prompt,
                mw.chk_knowledge_base):
        chk.setEnabled.assert_called_with(False)


def test_on_ai_switch_changed_forces_off_and_saves():
    from gui.main_window import MainWindow
    mw = MagicMock()
    mw.config = {}
    mw._force_ai_analysis_ui_off = (
        lambda: MainWindow._force_ai_analysis_ui_off(mw))
    mw._save_ai_switches = MagicMock()
    MainWindow._on_ai_switch_changed(mw, 2)
    mw.chk_ai_analysis.setChecked.assert_called_with(False)
    mw._save_ai_switches.assert_called_once()


def test_save_ai_switches_writes_false(monkeypatch):
    import gui.main_window as mw_mod
    from gui.main_window import MainWindow
    monkeypatch.setattr(mw_mod.os.path, "exists", lambda p: False)
    mw = MagicMock()
    mw.config = {}
    mw.chk_ai_analysis.isChecked.return_value = True
    mw._force_ai_analysis_ui_off = (
        lambda: MainWindow._force_ai_analysis_ui_off(mw))
    MainWindow._save_ai_switches(mw)
    assert mw.config["ai_analysis"]["enabled"] is False
