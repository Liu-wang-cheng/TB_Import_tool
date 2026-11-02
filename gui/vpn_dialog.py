"""方案级 VPN 配置对话框（OpenConnect / Fortinet）。"""

from gui.qt_compat import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QVBoxLayout, QThread, pyqtSignal,
)
from src.vpn_client import VpnClient, parse_host_port, parse_certificate_pin


def vpn_config_from_dict(data: dict) -> dict:
    """规范化 VPN 配置（供 UI 与测试复用）"""
    data = data or {}
    return {
        "enabled": bool(data.get("enabled")),
        "server": str(data.get("server", "")).strip(),
        "port": int(data.get("port") or 0),
        "username": str(data.get("username", "")).strip(),
        "password": str(data.get("password", "")),
        "servercert": str(data.get("servercert", "")).strip(),
        "probe_host": str(data.get("probe_host", "")).strip(),
        "probe_port": int(data.get("probe_port") or 0),
        # 默认跟随方案服务器地址自动探测；已保存过探测地址的旧配置保持手动
        "probe_auto": bool(data.get("probe_auto",
                                    not (data.get("probe_host")
                                         and data.get("probe_port")))),
        "timeout": int(data.get("timeout") or 0) or 60,
        "auto_trust_cert": bool(data.get("auto_trust_cert", True)),
        # 默认导入完成后断开 VPN（已显式保存为 false 的方案保持 false）
        "disconnect_after": bool(data.get("disconnect_after", True)),
    }


class _VpnTestWorker(QThread):
    finished_ok = pyqtSignal(bool, str, str)  # ok, message, servercert

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config

    def run(self):
        import logging
        log = logging.getLogger(__name__)
        client = VpnClient(self._config)
        ok, msg, cert = False, "", ""
        try:
            ok, msg = client.connect()
            cert = client.servercert
        except Exception as e:  # noqa: BLE001
            log.exception("VPN 测试连接异常")
            ok, msg = False, f"连接异常: {e}"
        finally:
            # 测试完立即断开，避免占用用户网络
            try:
                client.disconnect()
            except Exception as e:
                log.warning("测试后断开 VPN 失败: %s", e)
        # 所有工作（含断开）完成后再通知 UI：若先发信号，用户可能立刻关闭
        # 弹窗，导致仍在运行的 QThread 被销毁 → Qt 直接崩溃
        if ok:
            log.info("VPN 测试连接成功: %s", msg)
        else:
            log.warning("VPN 测试连接失败: %s", msg)
        self.finished_ok.emit(ok, msg, cert)


class VpnDialog(QDialog):
    """编辑当前方案的 VPN 配置"""

    def __init__(self, config: dict, default_probe: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("VPN 设置（OpenConnect / Fortinet）")
        self.resize(480, 420)
        cfg = vpn_config_from_dict(config)

        layout = QVBoxLayout(self)
        self.chk_enabled = QCheckBox("此方案需要先连接 VPN")
        self.chk_enabled.setChecked(cfg["enabled"])
        self.chk_enabled.toggled.connect(self._update_enabled)
        layout.addWidget(self.chk_enabled)

        form = QFormLayout()
        self.edit_server = QLineEdit(cfg["server"])
        self.edit_server.setPlaceholderText("vpn.example.com")
        form.addRow("服务器地址:", self.edit_server)

        self.spin_port = QSpinBox()
        self.spin_port.setRange(0, 65535)
        self.spin_port.setValue(cfg["port"] or 50443)
        form.addRow("端口:", self.spin_port)

        self.edit_username = QLineEdit(cfg["username"])
        form.addRow("登录账号:", self.edit_username)

        self.edit_password = QLineEdit(cfg["password"])
        self.edit_password.setEchoMode(QLineEdit.Password)
        form.addRow("初始密码:", self.edit_password)

        self._default_probe = default_probe
        self.edit_probe = QLineEdit(
            f"{cfg['probe_host']}:{cfg['probe_port']}"
            if cfg["probe_host"] and cfg["probe_port"] else default_probe)
        self.edit_probe.setPlaceholderText(
            "用于判断 VPN 是否生效，如 10.10.3.67:8080")
        form.addRow("内网探测地址:", self.edit_probe)

        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(10, 600)
        self.spin_timeout.setValue(cfg["timeout"] or 60)
        form.addRow("连接超时(秒):", self.spin_timeout)

        self.edit_cert = QLineEdit(cfg["servercert"])
        self.edit_cert.setPlaceholderText(
            "留空则首次连接自动记录（pin-sha256:...）")
        form.addRow("证书指纹:", self.edit_cert)
        layout.addLayout(form)

        self.chk_probe_auto = QCheckBox("自动使用当前方案的服务器地址探测")
        self.chk_probe_auto.setToolTip(
            "勾选后，内网探测地址随方案里的服务器地址自动变化；"
            "取消勾选可手动指定")
        self.chk_probe_auto.setChecked(cfg["probe_auto"])
        self.chk_probe_auto.toggled.connect(self._update_probe_state)
        layout.addWidget(self.chk_probe_auto)

        self.chk_auto_trust = QCheckBox("首次连接自动信任服务器证书")
        self.chk_auto_trust.setChecked(cfg["auto_trust_cert"])
        layout.addWidget(self.chk_auto_trust)

        self.chk_disconnect = QCheckBox("导入完成后断开 VPN")
        self.chk_disconnect.setChecked(cfg["disconnect_after"])
        layout.addWidget(self.chk_disconnect)

        self.lbl_hint = QLabel(
            "说明：使用 OpenConnect（Fortinet 协议）独立建隧道，"
            "不依赖 FortiClient；首次使用需管理员权限创建虚拟网卡。")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self.lbl_hint)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        btn_row = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        self.btn_test.clicked.connect(self._on_test)
        btn_row.addWidget(self.btn_test)
        btn_row.addStretch()
        self.btn_ok = QPushButton("确定")
        self.btn_cancel = QPushButton("取消")
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_ok)
        btn_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_row)

        self._test_worker = None
        self._testing = False
        self._update_enabled()
        self._update_probe_state()

    def _update_probe_state(self, _checked=None):
        """自动探测：跟随服务器地址并禁用输入；手动：可编辑"""
        auto = self.chk_probe_auto.isChecked()
        if auto:
            self.edit_probe.setText(self._default_probe)
        self.edit_probe.setEnabled(
            self.chk_enabled.isChecked() and not auto)

    def _update_enabled(self, _checked=None):
        on = self.chk_enabled.isChecked()
        for w in (self.edit_server, self.spin_port, self.edit_username,
                  self.edit_password, self.spin_timeout,
                  self.edit_cert, self.chk_probe_auto, self.chk_auto_trust,
                  self.chk_disconnect, self.btn_test):
            w.setEnabled(on)
        self._update_probe_state()

    def _on_test(self):
        cfg = self.current_config()
        self.lbl_status.setText("正在连接 VPN…")
        self._testing = True
        self.btn_test.setEnabled(False)
        self.btn_ok.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self._test_worker = _VpnTestWorker(cfg, self)
        self._test_worker.finished_ok.connect(self._on_test_done)
        self._test_worker.finished.connect(self._on_test_worker_finished)
        self._test_worker.start()

    def _on_test_worker_finished(self):
        self._test_worker = None

    def _on_test_done(self, ok, message, servercert):
        self._testing = False
        self.btn_test.setEnabled(self.chk_enabled.isChecked())
        self.btn_ok.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        self.lbl_status.setText(("✅ " if ok else "❌ ") + message)
        if servercert and not self.edit_cert.text().strip():
            self.edit_cert.setText(servercert)

    def _test_running(self) -> bool:
        worker = self._test_worker
        return self._testing or (worker is not None and worker.isRunning())

    def reject(self):
        # 测试线程未结束时禁止关闭：QThread 运行中被销毁会导致闪退
        if self._test_running():
            return
        super().reject()

    def accept(self):
        if self._test_running():
            return
        super().accept()

    def closeEvent(self, event):
        if self._test_running():
            event.ignore()
            return
        super().closeEvent(event)

    def current_config(self) -> dict:
        probe_text = (self._default_probe
                      if self.chk_probe_auto.isChecked()
                      else self.edit_probe.text())
        host, port = parse_host_port(probe_text)
        return vpn_config_from_dict({
            "enabled": self.chk_enabled.isChecked(),
            "server": self.edit_server.text(),
            "port": self.spin_port.value(),
            "username": self.edit_username.text(),
            "password": self.edit_password.text(),
            "servercert": self.edit_cert.text(),
            "probe_host": host,
            "probe_port": port,
            "probe_auto": self.chk_probe_auto.isChecked(),
            "timeout": self.spin_timeout.value(),
            "auto_trust_cert": self.chk_auto_trust.isChecked(),
            "disconnect_after": self.chk_disconnect.isChecked(),
        })
