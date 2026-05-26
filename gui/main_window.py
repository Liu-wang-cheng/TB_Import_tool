"""主窗口：筛选面板、操作按钮、日志区、进度条、状态栏"""

import logging
import os
import sys

import yaml
from gui.qt_compat import (  # noqa: F401
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QStatusBar, QTextEdit, QVBoxLayout, QWidget, QDialog,
    pyqtSignal, QThread, QDate, Qt, QColor, QFont,
    exec_dialog, QT_VERSION, QIcon,
)

from src.config_loader import load_configs
from gui.yaml_utils import update_yaml_values
from src.utils import parse_zentao_url
from dingtalk.bot import DingTalkBot
from gui.config_dialog import ConfigDialog
from gui.log_handler import QtLogHandler
from gui.workers import (
    AuthTestWorker, ListBugsWorker, SyncWorker,
    UpdateCheckWorker, UpdateDownloadWorker,
)

logger = logging.getLogger(__name__)



class _ModuleResolveThread(QThread):
    """后台线程：通过禅道API将模块ID解析为模块名称"""
    finished_signal = pyqtSignal(str, int)  # (module_name, module_id)

    def __init__(self, base_url, product_id, module_id, zt_cfg,
                 account, password, parent=None):
        super().__init__(parent)
        self._base_url = base_url
        self._product_id = product_id
        self._module_id = module_id
        self._zt_cfg = zt_cfg
        self._account = account
        self._password = password

    def run(self):
        zt = None
        try:
            from src.zentao_client import ZentaoClient
            url = self._base_url or self._zt_cfg.get("base_url", "")
            account = self._account or self._zt_cfg.get("account", "")
            password = self._password or self._zt_cfg.get("password", "")
            if not all([url, account, password]):
                self.finished_signal.emit("", self._module_id)
                return
            zt = ZentaoClient(base_url=url, account=account, password=password)
            zt._ensure_token()
            name = zt.resolve_module_name(self._product_id, self._module_id)
            self.finished_signal.emit(name, self._module_id)
        except Exception as e:
            logger.warning("模块名称解析失败: %s", e)
            self.finished_signal.emit("", self._module_id)
        finally:
            if zt:
                zt.close()


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        # 从 VERSION 文件读取版本号（兼容 PyInstaller 打包）
        try:
            import sys
            if getattr(sys, 'frozen', False):
                _base = sys._MEIPASS
            else:
                _base = os.path.dirname(os.path.dirname(__file__))
            _vpath = os.path.join(_base, "VERSION")
            with open(_vpath, "r") as _vf:
                _ver = _vf.read().strip()
        except Exception:
            _ver = "?"
        self.setWindowTitle(f"智能缺陷管理平台 v{_ver}")
        self.setMinimumSize(780, 550)

        # 设置窗口图标
        _icon_path = os.path.join(_base, "gui", "resources", "icon.ico")
        if os.path.exists(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))
        self.resize(880, 680)

        # 窗口居中
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            self.move((sg.width() - self.width()) // 2,
                      (sg.height() - self.height()) // 2)

        self.config = {}
        self._worker = None
        self._log_handler = None
        self._module_thread = None
        self._update_check_worker = None
        self._update_download_worker = None
        self._update_bat_path = None
        self._manual_check_pending = False
        # 必须在 _build_ui 之前初始化，_on_platform_changed 会读取此值
        self._last_platform = None

        self._build_ui()
        self._load_style()
        self._load_config()

        # 启动后延迟检查更新（非阻塞）
        from gui.qt_compat import QTimer
        QTimer.singleShot(2000, self._check_for_updates)

    def closeEvent(self, event):
        """窗口关闭时清理后台线程"""
        if self._module_thread and self._module_thread.isRunning():
            self._module_thread.wait(2000)
        if self._update_download_worker and self._update_download_worker.isRunning():
            self._update_download_worker.wait(3000)
        if self._update_check_worker and self._update_check_worker.isRunning():
            self._update_check_worker.wait(3000)
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)
        event.accept()

    # ── UI 构建 ───────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)

        # 筛选面板
        layout.addWidget(self._build_filter_panel())

        # 操作按钮
        layout.addLayout(self._build_action_buttons())

        # 日志区
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setPlaceholderText("操作日志将显示在此处...")
        layout.addWidget(self.log_text, stretch=1)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("就绪")
        self.status_bar.addPermanentWidget(self.status_label)

    def _build_filter_panel(self):
        group = QGroupBox("筛选条件")
        grid = QVBoxLayout(group)
        grid.setSpacing(6)

        # 第0行：源平台选择器
        platform_row = QHBoxLayout()
        platform_row.addWidget(QLabel("源平台:"))
        self.filter_platform = QComboBox()
        self.filter_platform.addItems(["禅道", "Jira"])
        self.filter_platform.setMaximumWidth(120)
        self.filter_platform.currentIndexChanged.connect(self._on_platform_changed)
        platform_row.addWidget(self.filter_platform)

        self.platform_arrow = QLabel("→ Teambition")
        self.platform_arrow.setStyleSheet("color: #666; font-weight: bold;")
        platform_row.addWidget(self.platform_arrow)
        platform_row.addStretch()
        grid.addLayout(platform_row)

        # 第0行：URL 自动解析
        row0 = QHBoxLayout()
        self.lbl_url = QLabel("禅道Bug页面地址:")
        row0.addWidget(self.lbl_url)
        self.filter_url = QLineEdit()
        self.filter_url.setPlaceholderText(
            "粘贴禅道Bug列表页地址，自动解析产品ID和模块，"
            "如 https://zentao.xxx.com/zentao/bug-browse-11--byModule-122.html"
        )
        self.filter_url.returnPressed.connect(self._on_parse_url)
        row0.addWidget(self.filter_url, stretch=1)

        self.btn_parse_url = QPushButton("解析")
        self.btn_parse_url.setFixedSize(48, 28)
        self.btn_parse_url.setStyleSheet(
            "QPushButton { padding: 2px; font-size: 12px; border-radius: 3px; }"
        )
        self.btn_parse_url.setToolTip("解析URL中的产品ID、项目ID和模块")
        self.btn_parse_url.clicked.connect(self._on_parse_url)
        row0.addWidget(self.btn_parse_url)
        grid.addLayout(row0)

        # 第一行：产品ID、项目ID、模块
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("产品ID:"))
        self.filter_product = QLineEdit()
        self.filter_product.setPlaceholderText("数字ID")
        self.filter_product.setMaximumWidth(100)
        row1.addWidget(self.filter_product)

        row1.addWidget(QLabel("项目ID:"))
        self.filter_project = QLineEdit()
        self.filter_project.setPlaceholderText("数字ID")
        self.filter_project.setMaximumWidth(100)
        row1.addWidget(self.filter_project)

        self.lbl_module = QLabel("禅道项目名称:")
        row1.addWidget(self.lbl_module)
        self.filter_module = QLineEdit()
        self.filter_module.setPlaceholderText("如 HS341")
        self.filter_module.setMaximumWidth(80)
        row1.addWidget(self.filter_module)

        # 状态：中文显示，英文保存
        self._status_map = {
            "active,confirmed": "活跃+已确认",
            "active": "活跃",
            "confirmed": "已确认",
            "resolved": "已解决",
            "closed": "已关闭",
            "": "全部",
        }
        self._status_reverse = {v: k for k, v in self._status_map.items()}

        row1.addWidget(QLabel("状态:"))
        self.filter_status = QComboBox()
        self.filter_status.setEditable(True)
        self.filter_status.addItems(list(self._status_reverse.keys()))
        self.filter_status.setCurrentIndex(0)
        self.filter_status.setMaximumWidth(160)
        row1.addWidget(self.filter_status)
        row1.addStretch()
        grid.addLayout(row1)

        # 第二行：指派人列表（可添加/删除/双击编辑）
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("指派人:"))
        self.filter_assigned = QListWidget()
        self.filter_assigned.setMaximumHeight(72)
        self.filter_assigned.setFlow(QListView.LeftToRight)
        self.filter_assigned.setWrapping(True)
        self.filter_assigned.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.filter_assigned.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        self.filter_assigned.itemChanged.connect(self._on_assigned_item_changed)
        row2.addWidget(self.filter_assigned, stretch=1)

        # 指派人操作按钮（竖向）
        assigned_btns = QVBoxLayout()
        assigned_btns.setSpacing(2)
        self.btn_add_assigned = QPushButton("添加")
        self.btn_add_assigned.setFixedSize(40, 26)
        self.btn_add_assigned.setStyleSheet(
            "QPushButton { padding: 1px; font-size: 11px; border-radius: 3px; }"
        )
        self.btn_add_assigned.setToolTip("添加指派人")
        self.btn_add_assigned.clicked.connect(self._on_add_assigned)
        assigned_btns.addWidget(self.btn_add_assigned)

        self.btn_del_assigned = QPushButton("删除")
        self.btn_del_assigned.setFixedSize(40, 26)
        self.btn_del_assigned.setStyleSheet(
            "QPushButton { padding: 1px; font-size: 11px; border-radius: 3px; }"
        )
        self.btn_del_assigned.setToolTip("删除选中项")
        self.btn_del_assigned.clicked.connect(self._on_del_assigned)
        assigned_btns.addWidget(self.btn_del_assigned)

        self.btn_toggle_assigned = QPushButton("全选")
        self.btn_toggle_assigned.setFixedSize(40, 26)
        self.btn_toggle_assigned.setStyleSheet(
            "QPushButton { padding: 1px; font-size: 11px; border-radius: 3px; }"
        )
        self.btn_toggle_assigned.setToolTip("全选 / 反选指派人")
        self.btn_toggle_assigned.clicked.connect(self._on_toggle_assigned)
        assigned_btns.addWidget(self.btn_toggle_assigned)
        row2.addLayout(assigned_btns)
        grid.addLayout(row2)

        # 第三行：源平台配置
        self.source_group = QGroupBox("禅道配置")
        zt_form = QHBoxLayout()
        zt_form.addWidget(QLabel("服务器地址:"))
        self.edit_zentao_base_url = QLineEdit()
        self.edit_zentao_base_url.setPlaceholderText("https://zentao.xxx.com/zentao")
        zt_form.addWidget(self.edit_zentao_base_url, stretch=1)

        zt_form.addWidget(QLabel("账号:"))
        self.edit_zentao_account = QLineEdit()
        self.edit_zentao_account.setPlaceholderText("禅道登录账号")
        zt_form.addWidget(self.edit_zentao_account)

        zt_form.addWidget(QLabel("密码:"))
        self.edit_zentao_password = QLineEdit()
        self.edit_zentao_password.setEchoMode(QLineEdit.Password)
        self.edit_zentao_password.setPlaceholderText("密码")
        zt_form.addWidget(self.edit_zentao_password)
        self.source_group.setLayout(zt_form)
        grid.addWidget(self.source_group)

        # 第四行：Teambition 配置
        tb_group = QGroupBox("Teambition 配置")
        tb_form = QHBoxLayout()
        tb_form.setSpacing(2)
        tb_form.setContentsMargins(4, 4, 4, 4)

        tb_form.addWidget(QLabel("创建人:"))
        self.edit_tb_creator = QLineEdit()
        self.edit_tb_creator.setPlaceholderText("中文名")
        self.edit_tb_creator.setMaximumWidth(120)
        tb_form.addWidget(self.edit_tb_creator)

        tb_form.addSpacing(16)

        tb_form.addWidget(QLabel("备用创建人:"))
        self.edit_tb_creator_id = QLineEdit()
        self.edit_tb_creator_id.setPlaceholderText("中文名或UUID")
        self.edit_tb_creator_id.setMaximumWidth(120)
        tb_form.addWidget(self.edit_tb_creator_id)

        tb_form.addSpacing(16)

        tb_form.addWidget(QLabel("所属项目:"))
        self.edit_tb_belong_project = QLineEdit()
        self.edit_tb_belong_project.setPlaceholderText("项目名")
        self.edit_tb_belong_project.setMaximumWidth(120)
        tb_form.addWidget(self.edit_tb_belong_project)

        tb_form.addStretch()
        tb_group.setLayout(tb_form)
        grid.addWidget(tb_group)

        # 第四行：日期范围
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("日期筛选:"))
        self.filter_date_mode = QComboBox()
        self.filter_date_mode.addItems(["全部", "指定时间段"])
        self.filter_date_mode.currentIndexChanged.connect(self._on_date_mode_changed)
        row4.addWidget(self.filter_date_mode)

        self.filter_date_from = QDateEdit()
        self.filter_date_from.setCalendarPopup(True)
        self.filter_date_from.setDisplayFormat("yyyy-MM-dd")
        self.filter_date_from.setMinimumDate(QDate(2000, 1, 1))
        self.filter_date_from.setSpecialValueText("不限")
        self.filter_date_from.setDate(QDate.currentDate())
        self.filter_date_from.setMaximumWidth(130)
        self.filter_date_from_label = QLabel("起始:")
        row4.addWidget(self.filter_date_from_label)
        row4.addWidget(self.filter_date_from)

        self.filter_date_to = QDateEdit()
        self.filter_date_to.setCalendarPopup(True)
        self.filter_date_to.setDisplayFormat("yyyy-MM-dd")
        self.filter_date_to.setMinimumDate(QDate(2000, 1, 1))
        self.filter_date_to.setSpecialValueText("不限")
        self.filter_date_to.setDate(QDate.currentDate())
        self.filter_date_to.setMaximumWidth(130)
        self.filter_date_to_label = QLabel("截止:")
        row4.addWidget(self.filter_date_to_label)
        row4.addWidget(self.filter_date_to)

        # 默认隐藏日期选择器
        self._on_date_mode_changed(0)

        row4.addStretch()
        grid.addLayout(row4)

        return group

    def _build_action_buttons(self):
        layout = QVBoxLayout()
        layout.setSpacing(4)

        # 第一行：操作按钮
        btn_row = QHBoxLayout()

        self.btn_list = QPushButton("列出Bug")
        self.btn_list.setToolTip("获取禅道Bug列表（不需要Teambition认证）")
        self.btn_list.clicked.connect(self._on_list_bugs)

        self.btn_dryrun = QPushButton("试运行")
        self.btn_dryrun.setToolTip("预览同步结果，不实际创建任务")
        self.btn_dryrun.clicked.connect(self._on_dry_run)

        self.btn_sync = QPushButton("正式同步")
        self.btn_sync.setObjectName("btnSync")
        self.btn_sync.setToolTip("正式同步Bug到Teambition")
        self.btn_sync.clicked.connect(self._on_full_sync)

        self.btn_test = QPushButton("连接测试")
        self.btn_test.setObjectName("btnTest")
        self.btn_test.setToolTip("测试禅道和Teambition连接是否正常")
        self.btn_test.clicked.connect(self._on_test_auth)

        self.btn_config = QPushButton("配置")
        self.btn_config.setObjectName("btnConfig")
        self.btn_config.setToolTip("编辑高级配置")
        self.btn_config.clicked.connect(self._on_config)

        self.btn_update = QPushButton("检查更新")
        self.btn_update.setObjectName("btnUpdate")
        self.btn_update.setToolTip("检查是否有新版本")
        self.btn_update.clicked.connect(self._on_manual_check_update)

        for btn in [self.btn_list, self.btn_dryrun, self.btn_sync,
                     self.btn_test, self.btn_config, self.btn_update]:
            btn_row.addWidget(btn)

        layout.addLayout(btn_row)

        # 第二行：同步选项开关
        sync_row = QHBoxLayout()
        sync_row.setSpacing(12)

        self.chk_reopen = QCheckBox("重新打开任务")
        self.chk_reopen.setToolTip("同步时若TB任务已关闭，自动重新打开并同步最新评论和附件")
        self.chk_reopen.stateChanged.connect(self._save_reopen_switch)

        sync_row.addWidget(self.chk_reopen)
        sync_row.addStretch()
        layout.addLayout(sync_row)

        # 第三行：AI 分析开关
        ai_row = QHBoxLayout()
        ai_row.setSpacing(12)

        self.chk_ai_analysis = QCheckBox("开启AI分析日志")
        self.chk_ai_analysis.setToolTip("同步后自动下载DRC日志并调用LLM分析，结果写入TB评论")
        self.chk_ai_analysis.stateChanged.connect(self._on_ai_switch_changed)

        self.chk_fault_pattern = QCheckBox("故障模式库")
        self.chk_fault_pattern.setToolTip("匹配已知故障模式（倾斜误触发、抱起未恢复等），提供根因提示")
        self.chk_fault_pattern.stateChanged.connect(lambda: self._save_ai_switches())

        self.chk_specialized_prompt = QCheckBox("模块化提示词")
        self.chk_specialized_prompt.setToolTip("根据缺陷类别（算法/嵌入式/IOT/应用等）使用专业化分析提示词")
        self.chk_specialized_prompt.stateChanged.connect(lambda: self._save_ai_switches())

        self.chk_knowledge_base = QCheckBox("RAG知识库")
        self.chk_knowledge_base.setToolTip("从历史分析中检索相似案例作为参考，越用越准")
        self.chk_knowledge_base.stateChanged.connect(lambda: self._save_ai_switches())

        for chk in [self.chk_ai_analysis, self.chk_fault_pattern,
                     self.chk_specialized_prompt, self.chk_knowledge_base]:
            ai_row.addWidget(chk)

        ai_row.addStretch()
        layout.addLayout(ai_row)

        return layout

    # ── 样式 ──────────────────────────────────────────

    def _load_style(self):
        # 优先从 exe 同级目录加载（打包模式），其次从包内路径加载
        candidates = [
            os.path.join(os.path.dirname(sys.executable),
                         "gui", "resources", "style.qss"),
            os.path.join(
                os.path.dirname(__file__), "resources", "style.qss"
            ),
        ]
        for qss_path in candidates:
            if os.path.exists(qss_path):
                with open(qss_path, "r", encoding="utf-8") as f:
                    self.setStyleSheet(f.read())
                return

    # ── 配置加载 ──────────────────────────────────────

    def _load_config(self):
        try:
            self.config = load_configs("configs")
            self._populate_filters()
            self._init_dingtalk()
            self._check_classifier_config()
            self._check_ai_analysis_config()
            self._load_ai_switches()
            # 协同学习启动自动拉取
            self._start_collab_auto_pull()
            # 状态栏显示当前平台链路
            source_cfg = self.config.get("source", {})
            platform = source_cfg.get("platform", "zentao")
            platform_name = "Jira" if platform == "jira" else "禅道"
            self.status_label.setText(f"源: {platform_name} → 目标: Teambition")
        except FileNotFoundError as e:
            self.status_label.setText(f"配置未找到: {e}")
            self._log(f"配置加载失败: {e}", "ERROR")

    def _check_classifier_config(self):
        """检查分类器 LLM 配置是否完整，缺失则提示并打开配置界面"""
        cls_cfg = self.config.get("classifier", {})
        if "classifier" in cls_cfg and "llm" not in cls_cfg:
            cls_cfg = cls_cfg["classifier"]

        if not cls_cfg.get("enabled", True):
            return

        llm_cfg = cls_cfg.get("llm", {})
        if not llm_cfg.get("enabled", False):
            return

        missing = []
        if not (llm_cfg.get("base_url") or "").strip():
            missing.append("API地址")
        if not (llm_cfg.get("api_key") or "").strip():
            missing.append("API Key")

        if not missing:
            return

        items = "、".join(missing)
        reply = QMessageBox.warning(
            self, "分类器配置不完整",
            f"LLM 大模型分类已启用，但以下配置未填写：\n\n"
            f"  {items}\n\n"
            f"请先在「分类器」页面填写 LLM 配置信息。\n"
            f"是否现在打开配置界面？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._on_config()

    def _check_ai_analysis_config(self):
        """检查 AI 分析配置状态，在状态栏显示"""
        ai_cfg = self.config.get("ai_analysis", {})
        if ai_cfg.get("enabled"):
            cls_cfg = self.config.get("classifier", {})
            if "classifier" in cls_cfg and "llm" not in cls_cfg:
                cls_cfg = cls_cfg["classifier"]
            llm_cfg = cls_cfg.get("llm", {})
            if not (llm_cfg.get("api_key") or "").strip():
                reply = QMessageBox.warning(
                    self, "AI分析配置不完整",
                    "AI 日志分析已启用，但分类器 LLM 的 API Key 未配置。\n"
                    "AI 分析需要 LLM 支持，请先在「分类器」页面配置 LLM。\n\n"
                    "是否现在打开配置界面？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if reply == QMessageBox.Yes:
                    self._on_config()

    def _load_ai_switches(self):
        """从配置文件加载开关状态，全部默认关闭"""
        # 重新打开任务开关（默认关闭）
        sync_cfg = self.config.get("sync", {})
        self.chk_reopen.blockSignals(True)
        self.chk_reopen.setChecked(bool(sync_cfg.get("reactivate_closed", False)))
        self.chk_reopen.blockSignals(False)

        # AI 日志分析开关（默认关闭）
        ai_cfg = self.config.get("ai_analysis", {})
        self.chk_ai_analysis.blockSignals(True)
        self.chk_ai_analysis.setChecked(bool(ai_cfg.get("enabled", False)))
        self.chk_ai_analysis.blockSignals(False)

        import yaml
        for chk, yaml_path, key in [
            (self.chk_fault_pattern, "configs/fault_patterns.yaml", "enabled"),
            (self.chk_specialized_prompt, "configs/prompts.yaml", "enabled"),
        ]:
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                chk.blockSignals(True)
                chk.setChecked(bool(cfg.get(key, False)))
                chk.blockSignals(False)
            except Exception:
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)

        kb_cfg = ai_cfg.get("knowledge_base", {})
        self.chk_knowledge_base.blockSignals(True)
        self.chk_knowledge_base.setChecked(bool(kb_cfg.get("enabled", False)))
        self.chk_knowledge_base.blockSignals(False)

        # AI 关闭时禁用子开关
        self._update_ai_sub_switches()

    def _save_reopen_switch(self):
        """保存重新打开任务开关到 sync.yaml"""
        import yaml
        from gui.yaml_utils import update_yaml_values
        sync_path = os.path.join("configs", "sync.yaml")
        enabled = self.chk_reopen.isChecked()
        try:
            update_yaml_values(sync_path, {"reactivate_closed": enabled})
        except Exception as e:
            logger.warning("保存重新打开任务开关失败: %s", e)

    def _on_ai_switch_changed(self, state):
        """AI 分析总开关变化时更新子开关可用性"""
        self._update_ai_sub_switches()
        self._save_ai_switches()

    def _update_ai_sub_switches(self):
        """AI 总开关关闭时禁用子开关"""
        enabled = self.chk_ai_analysis.isChecked()
        for chk in [self.chk_fault_pattern, self.chk_specialized_prompt,
                     self.chk_knowledge_base]:
            chk.setEnabled(enabled)

    def _save_ai_switches(self):
        """将 AI 开关状态保存到配置文件"""
        from gui.yaml_utils import update_yaml_values

        # AI 分析总开关 → ai_analysis.yaml
        ai_enabled = self.chk_ai_analysis.isChecked()
        ai_path = os.path.join("configs", "ai_analysis.yaml")
        if os.path.exists(ai_path):
            update_yaml_values(ai_path, {"enabled": ai_enabled})
        self.config.setdefault("ai_analysis", {})["enabled"] = ai_enabled

        # 故障模式库 → fault_patterns.yaml
        fp_path = os.path.join("configs", "fault_patterns.yaml")
        if os.path.exists(fp_path):
            update_yaml_values(fp_path, {"enabled": self.chk_fault_pattern.isChecked()})

        # 模块化提示词 → prompts.yaml
        prompt_path = os.path.join("configs", "prompts.yaml")
        if os.path.exists(prompt_path):
            update_yaml_values(prompt_path, {"enabled": self.chk_specialized_prompt.isChecked()})

        # RAG 知识库 → ai_analysis.yaml
        kb_enabled = self.chk_knowledge_base.isChecked() and ai_enabled
        if os.path.exists(ai_path):
            update_yaml_values(ai_path, {"knowledge_base.enabled": kb_enabled})
        self.config.setdefault("ai_analysis", {}).setdefault("knowledge_base", {})["enabled"] = kb_enabled

        # 更新状态栏
        parts = []
        if ai_enabled:
            parts.append("AI分析:开")
            if self.chk_fault_pattern.isChecked():
                parts.append("模式库")
            if self.chk_specialized_prompt.isChecked():
                parts.append("专业提示词")
            if self.chk_knowledge_base.isChecked():
                parts.append("知识库")
        else:
            parts.append("AI分析:关")
        self.status_label.setText(" | ".join(parts))

    def _init_dingtalk(self):
        """根据配置初始化钉钉机器人"""
        self._dingtalk_bot = None
        dt_cfg = self.config.get("dingtalk", {})
        if dt_cfg.get("enabled") and dt_cfg.get("webhook_url"):
            try:
                self._dingtalk_bot = DingTalkBot(
                    webhook_url=dt_cfg["webhook_url"],
                    secret=dt_cfg.get("secret", ""),
                )
                logger.info("钉钉通知已启用")
            except Exception as e:
                logger.warning("钉钉机器人初始化失败: %s", e)

    def _start_collab_auto_pull(self):
        """启动协同学习自动拉取（延迟执行，等待组件初始化完成）。"""
        cl_cfg = self.config.get("ai_analysis", {}).get("collaborative_learning", {})
        if not cl_cfg.get("enabled", True) or not cl_cfg.get("auto_pull", True):
            return
        if not cl_cfg.get("github_token", ""):
            return

        from gui.qt_compat import QTimer
        QTimer.singleShot(5000, self._do_collab_auto_pull)

        # 定时推送：每小时检查一次
        self._collab_sync_interval_hours = cl_cfg.get("sync_interval_hours", 168)
        self._collab_sync_timer = QTimer()
        self._collab_sync_timer.timeout.connect(self._do_collab_periodic_sync)
        self._collab_sync_timer.start(3600 * 1000)  # 每小时检查

    def _do_collab_auto_pull(self):
        """后台自动拉取共享数据。"""
        from src.collaborative_learning import CollaborativeLearning
        cl = CollaborativeLearning(self.config.get("ai_analysis", {}))
        try:
            success, msg, has_updates = cl.pull()
            if has_updates:
                self._log(f"[协同学习] 自动拉取: {msg}")
                # 尝试重建模型
                self._rebuild_models_after_pull()
            elif success:
                logger.info("协同学习自动拉取: %s", msg)
        except Exception as e:
            logger.warning("协同学习自动拉取失败: %s", e)

    def _do_collab_periodic_sync(self):
        """定时检查是否需要推送。"""
        from src.collaborative_learning import CollaborativeLearning
        cl = CollaborativeLearning(self.config.get("ai_analysis", {}))
        if not cl.enabled or not cl.should_sync():
            return
        try:
            success, msg = cl.push()
            if success:
                self._log(f"[协同学习] 定时推送: {msg}")
        except Exception as e:
            logger.warning("协同学习定时推送失败: %s", e)

    def _rebuild_models_after_pull(self):
        """在协同学习拉取新数据后重建本地模型。"""
        try:
            from src.knowledge_base import KnowledgeBase
            kb = KnowledgeBase(self.config.get("ai_analysis", {}))
            if kb.enabled:
                kb.reload_data()
                kb.rebuild_model()
                self._log("[协同学习] 知识库模型已重建")
        except Exception as e:
            logger.warning("协同学习模型重建失败: %s", e)

    def _on_platform_changed(self, index: int):
        """源平台切换时更新界面标签、提示文字、字段值"""
        is_zentao = (index == 0)
        platform = "禅道" if is_zentao else "Jira"

        self.source_group.setTitle(f"{platform}配置")
        self.lbl_url.setText("禅道Bug页面地址:" if is_zentao else "Jira筛选器URL:")
        self.filter_url.setPlaceholderText(
            "粘贴禅道Bug列表页地址，自动解析产品ID和模块，"
            "如 https://zentao.xxx.com/zentao/bug-browse-11--byModule-122.html"
            if is_zentao else
            "粘贴Jira筛选器URL，如 https://jira.xxx.com/issues/?filter=12345"
        )
        self.btn_parse_url.setToolTip(
            "解析URL中的产品ID、项目ID和模块" if is_zentao else "解析Jira筛选器参数"
        )
        self.btn_list.setText("列出Bug" if is_zentao else "列出Issue")
        self.btn_list.setToolTip(
            f"获取{platform}Bug列表（不需要Teambition认证）" if is_zentao
            else f"获取{platform}Issue列表（暂未适配）"
        )
        self.lbl_module.setText("禅道项目名称:" if is_zentao else "Jira组件:")
        self.filter_module.setPlaceholderText("如 HS341" if is_zentao else "如 Backend")
        self.edit_zentao_base_url.setPlaceholderText(
            "https://zentao.xxx.com/zentao" if is_zentao else "https://jira.xxx.com"
        )
        self.edit_zentao_account.setPlaceholderText(
            "禅道登录账号" if is_zentao else "Jira用户名"
        )
        self.edit_zentao_password.setPlaceholderText(
            "密码" if is_zentao else "API Token"
        )

        # ── 保存当前平台字段值到 config ──
        old_platform = self._last_platform
        if old_platform == "zentao":
            zt_cfg = self.config.setdefault("zentao", {})
            zt_cfg["base_url"] = self.edit_zentao_base_url.text().strip()
            zt_cfg["account"] = self.edit_zentao_account.text().strip()
            zt_cfg["password"] = self.edit_zentao_password.text().strip()
            filters = zt_cfg.setdefault("filters", {})
            filters["product"] = self.filter_product.text().strip() or None
            filters["project"] = self.filter_project.text().strip() or None
            filters["module_filter"] = self.filter_module.text().strip() or None
        elif old_platform == "jira":
            jira_cfg = self.config.setdefault("jira", {})
            jira_cfg["base_url"] = self.edit_zentao_base_url.text().strip()
            jira_cfg["username"] = self.edit_zentao_account.text().strip()
            jira_cfg["api_token"] = self.edit_zentao_password.text().strip()
            jira_cfg["project_key"] = self.filter_product.text().strip() or None
            jira_cfg["jql"] = self.filter_module.text().strip() or None
        self._last_platform = "zentao" if is_zentao else "jira"

        # ── 加载目标平台字段值 ──
        if is_zentao:
            zt_cfg = self.config.get("zentao", {})
            filters = zt_cfg.get("filters", {})
            self.edit_zentao_base_url.setText(zt_cfg.get("base_url", ""))
            self.edit_zentao_account.setText(zt_cfg.get("account", ""))
            self.edit_zentao_password.setText(zt_cfg.get("password", ""))
            self.filter_product.setText(str(filters.get("product", "") or ""))
            self.filter_project.setText(str(filters.get("project", "") or ""))
            self.filter_module.setText(filters.get("module_filter", "") or "")
            self.filter_url.setText("")

            # 加载状态
            statuses = filters.get("statuses", []) or []
            if statuses:
                key = ",".join(statuses)
                self.filter_status.setCurrentText(self._status_map.get(key, key))
            else:
                self.filter_status.setCurrentIndex(0)

            # 加载指派人列表
            assigned_checked = filters.get("assigned_to", []) or []
            if isinstance(assigned_checked, str):
                assigned_checked = [assigned_checked]
            assigned_known = filters.get("assigned_to_known") or assigned_checked
            if isinstance(assigned_known, str):
                assigned_known = [assigned_known]
            merged = list(assigned_known)
            for name in assigned_checked:
                if name not in merged:
                    merged.append(name)
            checked_set = set(assigned_checked)

            self.filter_assigned.blockSignals(True)
            self.filter_assigned.clear()
            for name in merged:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
                item.setCheckState(Qt.Checked if name in checked_set else Qt.Unchecked)
                self.filter_assigned.addItem(item)
            self.filter_assigned.blockSignals(False)
            self._update_toggle_btn_text()
        else:
            jira_cfg = self.config.get("jira", {})
            self.edit_zentao_base_url.setText(jira_cfg.get("base_url", ""))
            self.edit_zentao_account.setText(jira_cfg.get("username", ""))
            self.edit_zentao_password.setText(jira_cfg.get("api_token", ""))
            self.filter_product.setText(jira_cfg.get("project_key", ""))
            self.filter_project.clear()
            self.filter_module.setText(jira_cfg.get("jql", ""))
            self.filter_url.clear()
            self.filter_assigned.clear()

    def _populate_filters(self):
        """从配置文件填充筛选面板"""
        # 源平台
        source_cfg = self.config.get("source", {})
        platform = source_cfg.get("platform", "zentao")
        platform_idx = 1 if platform == "jira" else 0
        self.filter_platform.blockSignals(True)
        self.filter_platform.setCurrentIndex(platform_idx)
        self.filter_platform.blockSignals(False)
        # _on_platform_changed 会根据平台加载对应的凭证和筛选条件
        self._on_platform_changed(platform_idx)

        # Teambition 配置（与源平台无关）
        tb_cfg = self.config.get("teambition", {})
        self.edit_tb_creator.setText(tb_cfg.get("creator_name", ""))
        self.edit_tb_creator_id.setText(tb_cfg.get("creator_id", ""))
        # 优先从 project.name 读取（目标项目），其次 belong_project_value，最后 project_name
        project_cfg = tb_cfg.get("project", {})
        project_val = (
            project_cfg.get("name", "")
            or tb_cfg.get("belong_project_value", "")
            or tb_cfg.get("project_name", "")
        )
        self.edit_tb_belong_project.setText(project_val)

    def _apply_filters_to_config(self):
        """将筛选面板的值写回 config dict"""
        platform_idx = self.filter_platform.currentIndex()
        is_zentao = (platform_idx == 0)

        if is_zentao:
            filters = self.config.setdefault("zentao", {}).setdefault("filters", {})

            product = self.filter_product.text().strip()
            filters["product"] = int(product) if product.isdigit() else (product or None)
            # 清空产品时同步清理残留的 product_id，避免后续用到旧值
            if not product:
                filters.pop("product_id", None)

            project = self.filter_project.text().strip()
            filters["project"] = int(project) if project.isdigit() else (project or None)
            if not project:
                filters.pop("project_id", None)

            module = self.filter_module.text().strip()
            filters["module_filter"] = module or None

            status_text = self.filter_status.currentText().strip()
            if status_text and status_text != "全部":
                # 中文 → 英文
                val = self._status_reverse.get(status_text, status_text)
                filters["statuses"] = [s.strip() for s in val.split(",") if s.strip()]
            else:
                filters["statuses"] = None

            checked_assigned = []
            all_assigned = []
            for i in range(self.filter_assigned.count()):
                item = self.filter_assigned.item(i)
                all_assigned.append(item.text())
                if item.checkState() == Qt.Checked:
                    checked_assigned.append(item.text())
            filters["assigned_to"] = checked_assigned if checked_assigned else None
            # 完整列表（含未勾选项），用于 resolve_assigned_to 做后缀歧义检测
            filters["assigned_to_known"] = all_assigned if all_assigned else None

            if self.filter_date_mode.currentIndex() == 1:  # 指定时间段
                date_from = self.filter_date_from.date()
                if date_from.year() > 2000:
                    filters["date_from"] = date_from.toString("yyyy-MM-dd")
                else:
                    filters["date_from"] = None
                date_to = self.filter_date_to.date()
                if date_to.year() > 2000:
                    filters["date_to"] = date_to.toString("yyyy-MM-dd")
                else:
                    filters["date_to"] = None
            else:
                filters["date_from"] = None
                filters["date_to"] = None

            # 保存禅道凭证
            zt_cfg = self.config.setdefault("zentao", {})
            zt_cfg["base_url"] = self.edit_zentao_base_url.text().strip()
            zt_cfg["account"] = self.edit_zentao_account.text().strip()
            zt_cfg["password"] = self.edit_zentao_password.text().strip()
        else:
            # 保存Jira筛选条件和凭证
            jira_cfg = self.config.setdefault("jira", {})
            jira_cfg["project_key"] = self.filter_product.text().strip() or None
            jira_cfg["jql"] = self.filter_module.text().strip() or None
            jira_cfg["base_url"] = self.edit_zentao_base_url.text().strip()
            jira_cfg["username"] = self.edit_zentao_account.text().strip()
            jira_cfg["api_token"] = self.edit_zentao_password.text().strip()

        # 源平台写回 config（只存平台类型）
        source_cfg = self.config.setdefault("source", {})
        source_cfg["platform"] = "jira" if platform_idx == 1 else "zentao"

        # TB 配置（与源平台无关）
        tb_cfg = self.config.setdefault("teambition", {})
        tb_cfg["creator_name"] = self.edit_tb_creator.text().strip()
        tb_cfg["creator_id"] = self.edit_tb_creator_id.text().strip()
        tb_cfg["belong_project_value"] = self.edit_tb_belong_project.text().strip()
        # 同步更新目标项目名称
        project_name = self.edit_tb_belong_project.text().strip()
        if project_name:
            project_cfg = tb_cfg.setdefault("project", {})
            project_cfg["name"] = project_name

    # ── 配置持久化 ────────────────────────────────────

    def _save_config_to_yaml(self):
        """将当前 config dict 中的 GUI 可编辑字段保存回 YAML 文件（保留注释）"""
        self._apply_filters_to_config()
        zt_cfg = self.config.get("zentao", {})
        zt_filters = zt_cfg.get("filters", {})
        tb_cfg = self.config.get("teambition", {})
        source_cfg = self.config.get("source", {})

        # 保存 source.yaml —— 只存平台类型
        source_path = os.path.join("configs", "source.yaml")
        if os.path.exists(source_path):
            update_yaml_values(source_path, {
                "platform": source_cfg.get("platform", "zentao"),
            })

        # 保存禅道配置（独立文件）
        zt_path = os.path.join("configs", "zentao.yaml")
        if os.path.exists(zt_path):
            update_yaml_values(zt_path, {
                "base_url": zt_cfg.get("base_url"),
                "account": zt_cfg.get("account"),
                "password": zt_cfg.get("password"),
                "filters.product": zt_filters.get("product"),
                "filters.project": zt_filters.get("project"),
                "filters.module_filter": zt_filters.get("module_filter"),
                "filters.statuses": zt_filters.get("statuses"),
                "filters.assigned_to": zt_filters.get("assigned_to"),
                "filters.assigned_to_known": zt_filters.get("assigned_to_known"),
                "filters.date_from": zt_filters.get("date_from"),
                "filters.date_to": zt_filters.get("date_to"),
            })

        # 保存 Jira 配置（独立文件）
        jira_cfg = self.config.get("jira", {})
        jira_path = os.path.join("configs", "jira.yaml")
        if os.path.exists(jira_path):
            update_yaml_values(jira_path, {
                "base_url": jira_cfg.get("base_url"),
                "username": jira_cfg.get("username"),
                "api_token": jira_cfg.get("api_token"),
                "project_key": jira_cfg.get("project_key"),
                "jql": jira_cfg.get("jql"),
            })

        # 保存 Teambition 配置
        tb_path = os.path.join("configs", "teambition.yaml")
        if os.path.exists(tb_path):
            update_yaml_values(tb_path, {
                "creator_name": tb_cfg.get("creator_name"),
                "creator_id": tb_cfg.get("creator_id"),
                "belong_project_value": tb_cfg.get("belong_project_value"),
                "project.name": tb_cfg.get("project", {}).get("name"),
            })

        logger.info("配置已保存到文件")

    # ── 指派人增删 ────────────────────────────────────

    def _on_add_assigned(self):
        """添加指派人条目"""
        from gui.qt_compat import QInputDialog
        name, ok = QInputDialog.getText(
            self, "添加指派人", "输入指派人（如 IOT-陈斌、应用-罗林旺）:"
        )
        if ok and name.strip():
            self.filter_assigned.blockSignals(True)
            item = QListWidgetItem(name.strip())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            item.setCheckState(Qt.Checked)
            self.filter_assigned.addItem(item)
            self.filter_assigned.blockSignals(False)
            self._save_config_to_yaml()

    def _on_del_assigned(self):
        """删除选中的指派人条目"""
        row = self.filter_assigned.currentRow()
        if row >= 0:
            self.filter_assigned.takeItem(row)
            self._save_config_to_yaml()

    def _on_toggle_assigned(self):
        """全选 / 反选：全部已选时反选，否则全选"""
        count = self.filter_assigned.count()
        if count == 0:
            return
        all_checked = all(
            self.filter_assigned.item(i).checkState() == Qt.Checked
            for i in range(count)
        )
        self.filter_assigned.blockSignals(True)
        new_state = Qt.Unchecked if all_checked else Qt.Checked
        for i in range(count):
            self.filter_assigned.item(i).setCheckState(new_state)
        self.filter_assigned.blockSignals(False)
        self._update_toggle_btn_text()
        self._save_config_to_yaml()

    def _update_toggle_btn_text(self):
        """根据当前勾选状态更新按钮文字"""
        count = self.filter_assigned.count()
        if count == 0:
            self.btn_toggle_assigned.setText("全选")
            return
        all_checked = all(
            self.filter_assigned.item(i).checkState() == Qt.Checked
            for i in range(count)
        )
        self.btn_toggle_assigned.setText("反选" if all_checked else "全选")

    def _on_assigned_item_changed(self, item):
        """指派人项变更（勾选/编辑文本）后自动保存配置"""
        self._save_config_to_yaml()

    # ── 日志 ──────────────────────────────────────────

    def _setup_logging(self):
        """安装 QtLogHandler，将所有日志重定向到日志区"""
        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)

        self._log_handler = QtLogHandler(self.log_text, self)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                              datefmt="%H:%M:%S")
        )
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.INFO)

    def _log(self, message, level="INFO"):
        color_map = {"DEBUG": "#888", "INFO": "#333", "WARNING": "#CC8800",
                     "ERROR": "#CC0000", "CRITICAL": "#CC0000"}
        self.log_text.setTextColor(QColor(color_map.get(level, "#333")))
        self.log_text.append(message)
        self.log_text.setTextColor(QColor("#333"))

    # ── Worker 管理 ───────────────────────────────────

    def _set_busy(self, busy):
        """操作进行中禁用所有按钮"""
        for btn in [self.btn_list, self.btn_dryrun, self.btn_sync,
                     self.btn_test, self.btn_config, self.btn_update]:
            btn.setEnabled(not busy)

    def _start_worker(self, worker):
        self._worker = worker
        self._setup_logging()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_busy(True)

    def _worker_finished(self, save_config=False):
        self._worker = None
        self._set_busy(False)
        if save_config:
            try:
                self._save_config_to_yaml()
            except Exception as e:
                logger.warning("保存配置失败: %s", e)

    def _on_worker_error(self, msg, detail=""):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical if hasattr(QMessageBox, "Critical")
                    else QMessageBox.Icon.Critical)
        box.setWindowTitle("操作失败")
        box.setText(msg)
        if detail:
            box.setDetailedText(detail)
        exec_dialog(box)
        self._worker = None
        self._set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")

    # ── 按钮事件 ──────────────────────────────────────

    def _on_date_mode_changed(self, index):
        show = index == 1  # "指定时间段"
        self.filter_date_from_label.setVisible(show)
        self.filter_date_from.setVisible(show)
        self.filter_date_to_label.setVisible(show)
        self.filter_date_to.setVisible(show)

    def _on_parse_url(self):
        """解析禅道 Bug 页面 URL，自动填充产品ID、项目ID、模块名称"""
        url = self.filter_url.text().strip()
        if not url:
            return

        parsed = parse_zentao_url(url)
        filled = []

        if parsed["product_id"]:
            self.filter_product.setText(str(parsed["product_id"]))
            filled.append(f"产品ID={parsed['product_id']}")
        if parsed["project_id"]:
            self.filter_project.setText(str(parsed["project_id"]))
            filled.append(f"项目ID={parsed['project_id']}")
        if parsed["base_url"]:
            self.edit_zentao_base_url.setText(parsed["base_url"])
            filled.append(f"禅道地址={parsed['base_url']}")

        if filled:
            self.status_label.setText("已解析: " + ", ".join(filled))
        else:
            self.status_label.setText("未能从URL中解析出有效信息")
            QMessageBox.information(
                self, "解析结果",
                "未能识别此URL格式。\n\n"
                "支持的格式示例：\n"
                "  bug-browse-11--byModule-136.html\n"
                "  bug-browse-11.html\n"
                "  project-bug-5.html\n"
                "  index.php?m=bug&f=browse&productID=324"
            )
            return

        # 如果 URL 中有模块ID，异步解析模块名称（避免阻塞 UI）
        if parsed["module_id"] and parsed["product_id"]:
            self.btn_parse_url.setEnabled(False)
            self.status_label.setText("正在解析模块名称...")
            self._module_thread = _ModuleResolveThread(
                base_url=parsed["base_url"],
                product_id=parsed["product_id"],
                module_id=parsed["module_id"],
                zt_cfg=self.config.get("zentao", {}),
                account=self.edit_zentao_account.text().strip(),
                password=self.edit_zentao_password.text().strip(),
                parent=self,
            )
            self._module_thread.finished_signal.connect(self._on_module_resolved)
            self._module_thread.start()

    def _on_module_resolved(self, module_name, module_id):
        """模块名称异步解析完成回调"""
        self.btn_parse_url.setEnabled(True)
        if module_name:
            self.filter_module.setText(module_name)
            self.status_label.setText(f"已解析: 模块ID={module_id} → {module_name}")
        else:
            self.status_label.setText(f"模块ID={module_id} 未找到对应名称，可手动填写")

    def _on_test_auth(self):
        self._apply_filters_to_config()
        self.log_text.clear()
        self.status_label.setText("正在测试连接...")
        worker = AuthTestWorker(self.config, self)
        worker.progress.connect(self._on_progress_message)
        worker.finished.connect(self._on_auth_result)
        self._start_worker(worker)
        worker.start()

    def _on_auth_result(self, success, message):
        self.progress_bar.setValue(100)
        self._worker_finished(save_config=success)
        self.status_label.setText(message.replace("\n", " | "))
        if not success:
            QMessageBox.warning(self, "连接测试", message)
        else:
            QMessageBox.information(self, "连接测试", message)

    def _on_list_bugs(self):
        self._apply_filters_to_config()
        self.log_text.clear()
        self.status_label.setText("正在获取Bug列表...")
        worker = ListBugsWorker(self.config, dingtalk_bot=self._dingtalk_bot, parent=self)
        worker.progress.connect(self._on_progress_message)
        worker.finished.connect(self._on_list_result)
        worker.error.connect(self._on_worker_error)
        self._start_worker(worker)
        worker.start()

    def _on_list_result(self, bugs):
        self.progress_bar.setValue(100)
        self._worker_finished(save_config=True)
        severity_map = {"1": "致命", "2": "严重", "3": "一般", "4": "轻微"}
        self._log(f"\n共 {len(bugs)} 条 Bug:\n")
        for bug in bugs[:100]:
            sev = severity_map.get(str(bug.severity), bug.severity)
            assignee = (bug.assignedTo or "-")
            title = bug.title or ""
            self._log(f"#{bug.id}  [{bug.status}]  [{sev}]  @{assignee}\n    {title}")
        if len(bugs) > 100:
            self._log(f"\n仅显示前100条，共 {len(bugs)} 条")
        self.status_label.setText(f"共 {len(bugs)} 条Bug")

    def _on_dry_run(self):
        self._apply_filters_to_config()
        self.log_text.clear()
        self.status_label.setText("试运行中...")
        worker = SyncWorker(self.config, dry_run=True,
                            dingtalk_bot=self._dingtalk_bot, parent=self)
        worker.progress.connect(self._on_sync_progress)
        worker.finished.connect(self._on_sync_result)
        worker.error.connect(self._on_worker_error)
        self._start_worker(worker)
        worker.start()

    def _on_full_sync(self):
        self._apply_filters_to_config()
        reply = QMessageBox.question(
            self, "确认同步",
            "确定要正式同步吗？\n此操作将在 Teambition 中创建任务。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.log_text.clear()
        self.status_label.setText("同步中...")
        worker = SyncWorker(self.config, dry_run=False,
                            dingtalk_bot=self._dingtalk_bot, parent=self)
        worker.progress.connect(self._on_sync_progress)
        worker.finished.connect(self._on_sync_result)
        worker.error.connect(self._on_worker_error)
        self._start_worker(worker)
        worker.start()

    def _on_sync_progress(self, current, total, message):
        if total > 0:
            # 确定模式：显示百分比
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            pct = int(current / total * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{message} %p%")
        else:
            # 不确定模式：动画滚动条，提示当前阶段
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(message)
        self.status_label.setText(message)

    def _on_sync_result(self, stats_text):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("%p%")
        self._worker_finished(save_config=True)
        self.status_label.setText(stats_text)
        QMessageBox.information(self, "同步完成", stats_text)

    def _on_config(self):
        try:
            dlg = ConfigDialog("configs", parent=self)
            result = exec_dialog(dlg)
            if result == QDialog.Accepted:
                self._load_config()
        except Exception as e:
            logger.exception("打开配置对话框失败")
            QMessageBox.critical(self, "错误", f"无法打开配置对话框:\n{e}")

    # ── 通用回调 ──────────────────────────────────────

    def _on_progress_message(self, msg):
        self.status_label.setText(msg)

    # ── 自动更新 ──────────────────────────────────────

    def _check_for_updates(self, manual=False):
        """后台检查更新（非阻塞）"""
        if self._worker and self._worker.isRunning():
            if manual:
                QMessageBox.information(self, "提示", "有任务正在执行，请稍后再检查更新")
            return
        if self._update_download_worker and self._update_download_worker.isRunning():
            if manual:
                QMessageBox.information(self, "提示", "正在下载更新，请等待完成")
            return
        if self._update_check_worker and self._update_check_worker.isRunning():
            if manual:
                self._manual_check_pending = True
                self.status_label.setText("正在检查更新，请稍候...")
            return
        update_cfg = self.config.get("update", {})
        if not update_cfg.get("enabled", True):
            if manual:
                QMessageBox.information(self, "提示", "更新功能已禁用")
            return
        repo = update_cfg.get("repository", "")
        if not repo:
            if manual:
                QMessageBox.information(self, "提示", "未配置更新仓库地址")
            return

        self._manual_check_pending = manual
        self._update_check_worker = UpdateCheckWorker(self.config, parent=self)
        self._update_check_worker.progress.connect(self._on_update_check_progress)
        self._update_check_worker.finished.connect(self._on_update_check_result)
        self._update_check_worker.error.connect(self._on_update_check_error)
        self._update_check_worker.start()

    def _on_update_check_progress(self, msg):
        self.status_label.setText(msg)

    def _on_update_check_error(self, msg, detail):
        logger.warning("更新检查失败: %s\n%s", msg, detail)
        self.status_label.setText(f"更新检查失败: {msg}")
        if self._manual_check_pending:
            self._manual_check_pending = False
            QMessageBox.warning(self, "更新检查失败", f"检查更新时出错:\n{msg}")
        self._update_check_worker = None

    def _on_update_check_result(self, result):
        """更新检查完成"""
        self._update_check_worker = None
        msg = result.get("message", "")
        if not result.get("has_update"):
            if self._manual_check_pending:
                self._manual_check_pending = False
                QMessageBox.information(self, "检查更新", msg or "未发现新版本")
            elif "已是最新" in msg:
                self.status_label.setText(msg)
            return
        self._manual_check_pending = False

        info = result.get("info")
        if not info:
            return

        # 检查最低版本
        from gui.updater import compare_versions
        current = result.get("current", "")
        if info.min_version and compare_versions(current, info.min_version) > 0:
            QMessageBox.warning(
                self, "版本过旧",
                f"当前版本 v{current} 低于最低可更新版本 v{info.min_version}，\n"
                f"请手动下载最新版本 v{info.version}。\n\n"
                f"更新说明:\n{info.release_notes}"
            )
            return

        # 弹窗提示更新
        reply = QMessageBox.information(
            self, f"发现新版本 v{info.version}",
            f"当前版本: v{current}\n"
            f"最新版本: v{info.version} ({info.release_date})\n\n"
            f"更新说明:\n{info.release_notes}\n\n"
            f"是否立即更新？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._start_download_update(info, result.get("best_mirror"),
                                         result.get("sorted_mirrors", []))

    def _start_download_update(self, version_info, best_mirror, sorted_mirrors):
        """开始下载更新"""
        self._set_busy(True)
        self.status_label.setText("正在下载更新...")
        self.progress_bar.setRange(0, 0)

        self._update_download_worker = UpdateDownloadWorker(
            self.config, version_info, best_mirror, sorted_mirrors, parent=self
        )
        self._update_download_worker.progress.connect(self._on_update_download_progress)
        self._update_download_worker.finished.connect(self._on_update_download_result)
        self._update_download_worker.error.connect(self._on_update_download_error)
        self._update_download_worker.start()

    def _on_update_download_progress(self, downloaded, total, speed_msg):
        if total > 0:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            pct = int(downloaded / total * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{speed_msg} %p%")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(speed_msg)
        self.status_label.setText(speed_msg)

    def _on_update_download_result(self, bat_path):
        """下载完成，提示重启"""
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("%p%")
        self._update_download_worker = None
        self._set_busy(False)
        self._update_bat_path = bat_path

        reply = QMessageBox.information(
            self, "下载完成",
            "新版本已下载并校验通过。\n"
            "点击「确定」将重启应用以完成更新。",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Ok,
        )
        if reply == QMessageBox.Ok:
            self._apply_update_and_restart()

    def _on_update_download_error(self, msg, detail):
        self._update_download_worker = None
        self._set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        QMessageBox.critical(self, "更新失败", f"下载更新失败:\n{msg}")

    def _apply_update_and_restart(self):
        """执行 bat 替换脚本并退出"""
        import subprocess
        bat_path = self._update_bat_path
        if not bat_path or not os.path.exists(bat_path):
            QMessageBox.warning(self, "更新失败", "替换脚本不存在")
            return

        subprocess.Popen(
            ['cmd', '/c', bat_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=os.path.dirname(bat_path),
        )
        QApplication.quit()

    def _on_manual_check_update(self):
        """手动检查更新"""
        self._check_for_updates(manual=True)
