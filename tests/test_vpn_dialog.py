"""VPN 配置对话框：勾选启用后控件立即可编辑（无需重开）"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _dialog(enabled: bool):
    from gui.vpn_dialog import VpnDialog
    return VpnDialog({"enabled": enabled}, default_probe="10.0.0.1:8080")


class TestVpnDialogEnableToggle:
    def test_fields_disabled_when_unchecked(self, qapp):
        dlg = _dialog(False)
        assert dlg.edit_server.isEnabled() is False
        assert dlg.edit_password.isEnabled() is False
        assert dlg.btn_test.isEnabled() is False
        dlg.deleteLater()

    def test_toggle_enables_fields_immediately(self, qapp):
        dlg = _dialog(False)
        dlg.chk_enabled.setChecked(True)          # 用户勾选
        assert dlg.edit_server.isEnabled() is True
        assert dlg.edit_username.isEnabled() is True
        assert dlg.edit_password.isEnabled() is True
        assert dlg.edit_probe.isEnabled() is True
        assert dlg.edit_cert.isEnabled() is True
        assert dlg.btn_test.isEnabled() is True
        dlg.deleteLater()

    def test_toggle_back_disables_fields(self, qapp):
        dlg = _dialog(True)
        dlg.chk_enabled.setChecked(False)
        assert dlg.edit_server.isEnabled() is False
        assert dlg.btn_test.isEnabled() is False
        dlg.deleteLater()

    def test_current_config_reflects_edits(self, qapp):
        dlg = _dialog(True)
        dlg.edit_server.setText("vpn.fsyxrobot.com")
        dlg.spin_port.setValue(50443)
        dlg.edit_username.setText("liuwangcheng")
        dlg.edit_password.setText("p")
        dlg.edit_probe.setText("10.10.3.67:8080")
        cfg = dlg.current_config()
        assert cfg["enabled"] is True
        assert cfg["server"] == "vpn.fsyxrobot.com"
        assert cfg["port"] == 50443
        assert cfg["probe_host"] == "10.10.3.67"
        assert cfg["probe_port"] == 8080
        dlg.deleteLater()


class TestDisconnectDefault:
    def test_disconnect_checked_by_default(self, qapp):
        from gui.vpn_dialog import VpnDialog
        dlg = VpnDialog({}, default_probe="")
        assert dlg.chk_disconnect.isChecked() is True
        dlg.deleteLater()

    def test_explicit_false_preserved(self, qapp):
        from gui.vpn_dialog import VpnDialog
        dlg = VpnDialog({"disconnect_after": False}, default_probe="")
        assert dlg.chk_disconnect.isChecked() is False
        dlg.deleteLater()
