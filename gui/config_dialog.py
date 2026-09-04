"""配置编辑对话框：分 tab 编辑各模块配置"""

import logging
import os
import re

import yaml
from gui.qt_compat import Qt
from gui.qt_compat import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QStackedWidget, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from gui.yaml_utils import update_yaml_values
from gui.qt_compat import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class CollabSyncWorker(QThread):
    """协同学习同步后台线程"""
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, config: dict, data_dir: str = "data"):
        super().__init__()
        self._config = config
        self._data_dir = data_dir

    def run(self):
        try:
            from src.collaborative_learning import CollaborativeLearning
            cl = CollaborativeLearning(self._config, self._data_dir)
            success, msg, _ = cl.sync()
            self.finished_signal.emit(success, msg)
        except Exception as e:
            self.finished_signal.emit(False, f"同步异常: {e}")


class ConfigDialog(QDialog):
    """配置编辑对话框，支持编辑禅道、Teambition、同步、钉钉配置"""

    def __init__(self, config_dir="configs", parent=None):
        super().__init__(parent)
        self.config_dir = config_dir
        self.setWindowTitle("配置管理")
        self.setMinimumSize(600, 500)
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_source_tab(), "源平台")
        self.tabs.addTab(self._build_teambition_tab(), "Teambition")
        self.tabs.addTab(self._build_sync_tab(), "同步")
        self.tabs.addTab(self._build_classifier_tab(), "分类器")
        self.tabs.addTab(self._build_ai_analysis_tab(), "AI分析")
        self.tabs.addTab(self._build_dingtalk_tab(), "钉钉")
        layout.addWidget(self.tabs)

        # 保存/取消按钮（独立 QPushButton，避免 PyQt6 QDialogButtonBox 行为差异）
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    # ── 源平台 Tab ──────────────────────────────────────

    def _build_source_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)

        # 平台类型选择器
        platform_row = QHBoxLayout()
        platform_row.addWidget(QLabel("平台类型:"))
        self.source_platform = QComboBox()
        self.source_platform.addItems(["禅道", "外部TB"])
        self.source_platform.currentIndexChanged.connect(self._on_platform_changed)
        platform_row.addWidget(self.source_platform)
        platform_row.addStretch()
        layout.addLayout(platform_row)

        # StackedWidget：根据平台切换配置页面
        self.source_stack = QStackedWidget()
        self.source_stack.addWidget(self._build_zentao_page())
        self.source_stack.addWidget(self._build_teambition_page())
        layout.addWidget(self.source_stack)

        return w

    def _on_platform_changed(self, index: int):
        """切换源平台时更新 StackedWidget"""
        self.source_stack.setCurrentIndex(index)

    def _build_zentao_page(self):
        """禅道配置页面（完整保留原有字段）"""
        w = QWidget()
        form = QFormLayout(w)

        self.zt_base_url = QLineEdit()
        self.zt_base_url.setPlaceholderText("https://zentao.example.com/zentao")
        form.addRow("服务器地址:", self.zt_base_url)

        self.zt_account = QLineEdit()
        form.addRow("登录账号:", self.zt_account)

        self.zt_password = QLineEdit()
        self.zt_password.setEchoMode(QLineEdit.Password)
        form.addRow("登录密码:", self.zt_password)

        self.zt_product = QLineEdit()
        self.zt_product.setPlaceholderText("产品ID或名称")
        form.addRow("产品ID:", self.zt_product)

        self.zt_module_filter = QLineEdit()
        self.zt_module_filter.setPlaceholderText("如 HS341")
        form.addRow("模块过滤:", self.zt_module_filter)

        self.zt_assigned_to = QTextEdit()
        self.zt_assigned_to.setMaximumHeight(80)
        self.zt_assigned_to.setPlaceholderText("每行一个，如:\n胡继珍\n乐动开发-343")
        form.addRow("指派人筛选:", self.zt_assigned_to)

        return w

    def _build_teambition_page(self):
        """外部 TB 源配置页面（缺陷列表网址 + 账号密码）"""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)

        form = QFormLayout()

        self.tb_src_url = QLineEdit()
        self.tb_src_url.setPlaceholderText(
            "https://www.teambition.com/project/xxx/bug/section/all")
        form.addRow("缺陷列表网址:", self.tb_src_url)

        self.tb_src_account = QLineEdit()
        self.tb_src_account.setPlaceholderText("手机号/邮箱")
        form.addRow("登录账号:", self.tb_src_account)

        self.tb_src_password = QLineEdit()
        self.tb_src_password.setEchoMode(QLineEdit.Password)
        self.tb_src_password.setPlaceholderText("留空则扫码登录")
        form.addRow("登录密码:", self.tb_src_password)

        layout.addLayout(form)
        layout.addStretch()
        return w

    # ── Teambition Tab ────────────────────────────────

    def _build_teambition_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.tb_app_id = QLineEdit()
        form.addRow("App ID:", self.tb_app_id)

        self.tb_app_secret = QLineEdit()
        self.tb_app_secret.setEchoMode(QLineEdit.Password)
        form.addRow("App Secret:", self.tb_app_secret)

        self.tb_org_id = QLineEdit()
        form.addRow("组织ID:", self.tb_org_id)

        self.tb_project_id = QLineEdit()
        form.addRow("项目ID:", self.tb_project_id)

        self.tb_project_name = QLineEdit()
        form.addRow("项目名称:", self.tb_project_name)

        self.tb_creator_name = QLineEdit()
        form.addRow("创建人姓名:", self.tb_creator_name)

        self.tb_creator_id = QLineEdit()
        form.addRow("创建人ID(备用):", self.tb_creator_id)

        return w

    # ── 同步 Tab ──────────────────────────────────────

    def _build_sync_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.sync_attachments = QCheckBox("同步附件和内联图片")
        form.addRow("", self.sync_attachments)

        self.sync_max_size = QSpinBox()
        self.sync_max_size.setRange(1, 200)
        self.sync_max_size.setSuffix(" MB")
        form.addRow("附件最大大小:", self.sync_max_size)

        self.sync_dedup = QDoubleSpinBox()
        self.sync_dedup.setRange(0.1, 1.0)
        self.sync_dedup.setSingleStep(0.05)
        form.addRow("去重阈值:", self.sync_dedup)

        self.sync_delay = QDoubleSpinBox()
        self.sync_delay.setRange(0.0, 5.0)
        self.sync_delay.setSingleStep(0.1)
        self.sync_delay.setSuffix(" 秒")
        form.addRow("API间隔:", self.sync_delay)

        self.sync_retries = QSpinBox()
        self.sync_retries.setRange(0, 10)
        form.addRow("附件重试次数:", self.sync_retries)

        return w

    # ── 分类器 Tab ──────────────────────────────────────

    def _build_classifier_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        # TF-IDF 相似度
        tfidf_group = QGroupBox("TF-IDF 相似度分类（本地学习）")
        tfidf_form = QFormLayout(tfidf_group)

        self.cls_sim_enabled = QCheckBox("启用（从 TB 已分类缺陷中自动学习）")
        self.cls_sim_enabled.setChecked(True)
        tfidf_form.addRow("", self.cls_sim_enabled)

        self.cls_threshold = QDoubleSpinBox()
        self.cls_threshold.setRange(0.1, 0.9)
        self.cls_threshold.setSingleStep(0.05)
        self.cls_threshold.setToolTip("相似度低于此值视为无把握，交给 LLM 或兜底")
        tfidf_form.addRow("相似度阈值:", self.cls_threshold)

        self.cls_max_fetch = QSpinBox()
        self.cls_max_fetch.setRange(500, 20000)
        self.cls_max_fetch.setSingleStep(500)
        self.cls_max_fetch.setToolTip("首次训练时从 TB 拉取的最新缺陷数量，越大越准但越慢")
        tfidf_form.addRow("训练缺陷数量:", self.cls_max_fetch)

        self.cls_inc_days = QSpinBox()
        self.cls_inc_days.setRange(0, 90)
        self.cls_inc_days.setSpecialValueText("每次都学习")
        self.cls_inc_days.setToolTip(
            "两次增量学习的间隔天数。0 表示每次同步都增量学习，"
            "大于 0 则每隔指定天数自动学习最新缺陷优化模型")
        tfidf_form.addRow("增量学习间隔(天):", self.cls_inc_days)

        form.addRow(tfidf_group)

        # LLM 大模型
        llm_group = QGroupBox("LLM 大模型分类（API 调用）")
        llm_form = QFormLayout(llm_group)

        self.cls_llm_enabled = QCheckBox("启用")
        llm_form.addRow("", self.cls_llm_enabled)

        self.cls_llm_base_url = QLineEdit()
        self.cls_llm_base_url.setPlaceholderText("http://192.168.160.145:8081/v1")
        llm_form.addRow("API地址:", self.cls_llm_base_url)

        self.cls_llm_api_key = QLineEdit()
        self.cls_llm_api_key.setEchoMode(QLineEdit.Password)
        llm_form.addRow("API Key:", self.cls_llm_api_key)

        self.cls_llm_model = QLineEdit()
        self.cls_llm_model.setPlaceholderText("MiniMax-M2.7")
        llm_form.addRow("模型名称:", self.cls_llm_model)

        self.cls_llm_timeout = QSpinBox()
        self.cls_llm_timeout.setRange(5, 300)
        self.cls_llm_timeout.setSuffix(" 秒")
        llm_form.addRow("超时时间:", self.cls_llm_timeout)

        self.cls_llm_batch_size = QSpinBox()
        self.cls_llm_batch_size.setRange(1, 50)
        llm_form.addRow("批量大小:", self.cls_llm_batch_size)

        form.addRow(llm_group)

        return w

    # ── AI分析 Tab ──────────────────────────────────────

    def _build_ai_analysis_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.ai_enabled = QCheckBox("启用 AI 日志分析（同步后自动分析并写入评论）")
        form.addRow("", self.ai_enabled)

        # DRC 服务器
        drc_group = QGroupBox("DRC 日志服务器")
        drc_form = QFormLayout(drc_group)
        self.ai_drc_server = QLineEdit()
        self.ai_drc_server.setPlaceholderText("http://61.141.202.107:8008")
        drc_form.addRow("服务器地址:", self.ai_drc_server)
        self.ai_drc_username = QLineEdit()
        self.ai_drc_username.setPlaceholderText("ldrobot-team")
        drc_form.addRow("用户名:", self.ai_drc_username)
        self.ai_drc_password = QLineEdit()
        self.ai_drc_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_drc_password.setPlaceholderText("密码")
        drc_form.addRow("密码:", self.ai_drc_password)
        self.ai_project_name = QLineEdit()
        self.ai_project_name.setPlaceholderText("与 Teambition 配置自动同步")
        self.ai_project_name.setReadOnly(True)
        self.ai_project_name.setStyleSheet("color:#6b7280;")
        drc_form.addRow("所属项目:", self.ai_project_name)
        form.addRow(drc_group)

        form.addRow(QLabel(
            "说明：AI 分析依赖 LLM（配置在「分类器」Tab 中）。\n"
            "DRC 服务器配置留空则使用默认值。"
        ))

        # 协同学习
        collab_group = QGroupBox("协同学习（多用户知识共享）")
        collab_layout = QVBoxLayout(collab_group)
        collab_layout.setSpacing(8)

        self.collab_enabled = QCheckBox("启用协同学习（自动同步知识库和分类器训练数据到 GitHub）")
        self.collab_enabled.setChecked(True)
        collab_layout.addWidget(self.collab_enabled)

        token_row = QHBoxLayout()
        token_row.addWidget(QLabel("GitHub Token:"))
        self.collab_token = QLineEdit()
        self.collab_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.collab_token.setPlaceholderText("ghp_xxxxxxxxxxxxxxxxxxxx")
        token_row.addWidget(self.collab_token)
        # 获取 Token 帮助链接
        token_help = QLabel(
            "<a href='https://github.com/settings/tokens' style='color:#2563eb;text-decoration:none;'>获取Token</a> (需勾选 repo 权限)"
        )
        token_help.setOpenExternalLinks(True)
        token_help.setStyleSheet("font-size:12px;")
        token_row.addWidget(token_help)
        collab_layout.addLayout(token_row)

        # 仓库地址（只读）
        repo_row = QHBoxLayout()
        repo_row.addWidget(QLabel("共享仓库:"))
        self.collab_repo = QLineEdit("Liu-wang-cheng/TB_Import_tool")
        self.collab_repo.setReadOnly(True)
        self.collab_repo.setStyleSheet("color:#6b7280;")
        repo_row.addWidget(self.collab_repo)
        collab_layout.addLayout(repo_row)

        # 同步间隔
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("自动同步间隔:"))
        self.collab_interval = QComboBox()
        self.collab_interval.addItems(["每天", "每周", "每月"])
        self.collab_interval.setCurrentIndex(1)  # 默认每周
        interval_row.addWidget(self.collab_interval)
        interval_row.addStretch()
        collab_layout.addLayout(interval_row)

        # 立即同步按钮 + 状态
        sync_row = QHBoxLayout()
        self.collab_sync_btn = QPushButton("立即同步")
        self.collab_sync_btn.setToolTip("立即从 GitHub 拉取最新数据并推送本地数据")
        self.collab_sync_btn.clicked.connect(self._on_collab_sync)
        sync_row.addWidget(self.collab_sync_btn)
        self.collab_status = QLabel("")
        self.collab_status.setStyleSheet("color:#6b7280; font-size:12px;")
        sync_row.addWidget(self.collab_status)
        sync_row.addStretch()
        collab_layout.addLayout(sync_row)

        # 说明
        note = QLabel(
            "说明：开启后，知识库和分类器训练数据会通过 GitHub 仓库在团队成员间自动同步。\n"
            "首次使用需先到 GitHub Settings 创建 Personal Access Token 并勾选 repo 权限。"
        )
        note.setStyleSheet("color:#6b7280; font-size:12px;")
        collab_layout.addWidget(note)

        form.addRow(collab_group)

        return w

    # ── 钉钉 Tab ──────────────────────────────────────

    def _build_dingtalk_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.dt_enabled = QCheckBox("启用钉钉通知")
        self.dt_enabled.setChecked(True)
        self.dt_enabled.setEnabled(False)  # 置灰：钉钉通知强制开启，不允许关闭
        form.addRow("", self.dt_enabled)

        self.dt_webhook = QLineEdit()
        form.addRow("Webhook地址:", self.dt_webhook)

        self.dt_secret = QLineEdit()
        self.dt_secret.setEchoMode(QLineEdit.Password)
        form.addRow("加签密钥:", self.dt_secret)

        self.dt_at_all = QCheckBox("@所有人")
        form.addRow("", self.dt_at_all)

        return w

    # ── 加载/保存 ─────────────────────────────────────

    def _load_yaml(self, filename):
        path = os.path.join(self.config_dir, filename)
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_values(self):
        # source.yaml 只存平台类型
        source = self._load_yaml("source.yaml")
        platform = source.get("platform", "zentao")
        self.source_platform.setCurrentIndex(1 if platform == "teambition" else 0)

        # 禅道配置：独立从 zentao.yaml 加载
        zt = self._load_yaml("zentao.yaml")
        self.zt_base_url.setText(zt.get("base_url", ""))
        self.zt_account.setText(zt.get("account", ""))
        self.zt_password.setText(zt.get("password", ""))
        filters = zt.get("filters", {})
        product = filters.get("product", "")
        # 列表值转逗号串（与主窗口格式一致），并清洗历史污染的 []'"} 字符
        if isinstance(product, (list, tuple)):
            product = ",".join(str(x) for x in product)
        product = re.sub(r"[\[\]'\"]", "", str(product)).strip() if product else ""
        self.zt_product.setText(product)
        self.zt_module_filter.setText(str(filters.get("module_filter", "") or ""))
        # 指派人：从公用 assignee.yaml 读（外部 TB 和禅道共用）
        assignee = self._load_yaml("assignee.yaml")
        assigned_to = assignee.get("assigned_to", []) or []
        if isinstance(assigned_to, str):
            assigned_to = [assigned_to]
        self.zt_assigned_to.setPlainText("\n".join(str(a) for a in assigned_to))

        # 外部 TB 源配置：独立从 teambition_source.yaml 加载
        tb_src = self._load_yaml("teambition_source.yaml")
        self.tb_src_url.setText(str(tb_src.get("url", "") or ""))
        self.tb_src_account.setText(str(tb_src.get("account", "") or ""))
        self.tb_src_password.setText(str(tb_src.get("password", "") or ""))

        # Teambition
        tb = self._load_yaml("teambition.yaml")
        self.tb_app_id.setText(tb.get("app_id", ""))
        self.tb_app_secret.setText(tb.get("app_secret", ""))
        self.tb_org_id.setText(tb.get("org_id", ""))
        project_cfg = tb.get("project", {})
        self.tb_project_id.setText(project_cfg.get("id", ""))
        self.tb_project_name.setText(project_cfg.get("name", ""))
        self.tb_creator_name.setText(tb.get("creator_name", ""))
        self.tb_creator_id.setText(tb.get("creator_id", ""))

        # 同步
        sync = self._load_yaml("sync.yaml")
        self.sync_attachments.setChecked(sync.get("sync_attachments", True))
        self.sync_max_size.setValue(sync.get("max_attachment_size_mb", 50))
        self.sync_dedup.setValue(sync.get("dedup_threshold", 0.8))
        self.sync_delay.setValue(sync.get("api_delay", 0.5))
        self.sync_retries.setValue(sync.get("attachment_retries", 3))

        # 钉钉
        dt = self._load_yaml("dingtalk.yaml")
        self.dt_enabled.setChecked(dt.get("enabled", True))
        self.dt_webhook.setText(dt.get("webhook_url", ""))
        self.dt_secret.setText(dt.get("secret", ""))
        self.dt_at_all.setChecked(dt.get("at_all", False))

        # 分类器
        cls = self._load_yaml("classifier.yaml").get("classifier", {})
        sim = cls.get("similarity", {})
        self.cls_sim_enabled.setChecked(sim.get("enabled", True))
        self.cls_threshold.setValue(sim.get("threshold", 0.35))
        self.cls_max_fetch.setValue(sim.get("max_fetch", 5000))
        self.cls_inc_days.setValue(sim.get("incremental_days", 7))
        llm = cls.get("llm", {})
        self.cls_llm_enabled.setChecked(llm.get("enabled", False))
        self.cls_llm_base_url.setText(llm.get("base_url", ""))
        self.cls_llm_api_key.setText(llm.get("api_key", ""))
        self.cls_llm_model.setText(llm.get("model", ""))
        self.cls_llm_timeout.setValue(llm.get("timeout", 180))
        self.cls_llm_batch_size.setValue(llm.get("batch_size", 10))

        # AI分析
        ai = self._load_yaml("ai_analysis.yaml")
        self.ai_enabled.setChecked(ai.get("enabled", False))
        self.ai_drc_server.setText(ai.get("drc_server", "") or "http://61.141.202.107:8008")
        self.ai_drc_username.setText(ai.get("drc_username", "") or "ldrobot-team")
        self.ai_drc_password.setText(ai.get("drc_password", "") or "ldrobotlog4110")
        # 所属项目：自动从 TB 配置同步
        self.ai_project_name.setText(project_cfg.get("name", ""))

        # 协同学习
        cl = ai.get("collaborative_learning", {})
        self.collab_enabled.setChecked(cl.get("enabled", True))
        token = cl.get("github_token", "")
        # 如果 config 没配，尝试从环境变量或 ~/.github_token 文件中读取
        if not token:
            token = self._resolve_github_token()
        self.collab_token.setText(token)
        interval_hours = cl.get("sync_interval_hours", 168)
        if interval_hours <= 24:
            self.collab_interval.setCurrentIndex(0)    # 每天
        elif interval_hours <= 168:
            self.collab_interval.setCurrentIndex(1)    # 每周
        else:
            self.collab_interval.setCurrentIndex(2)    # 每月

    def _save(self):
        try:
            self._save_source()
            self._save_teambition()
            self._save_sync()
            self._save_classifier()
            self._save_ai_analysis()
            self._save_dingtalk()
            QMessageBox.information(self, "保存成功", "配置已保存")
            self.accept()
        except Exception as e:
            logger.exception("保存配置失败")
            QMessageBox.critical(self, "保存失败", str(e))

    def _save_source(self):
        """保存源平台配置：source.yaml 只存平台类型，各平台配置存到独立文件。"""
        import yaml

        platform_idx = self.source_platform.currentIndex()
        platform = "teambition" if platform_idx == 1 else "zentao"

        # 1. source.yaml —— 只存平台类型
        source_path = os.path.join(self.config_dir, "source.yaml")
        with open(source_path, "w", encoding="utf-8") as f:
            f.write("# 源平台配置\n")
            yaml.safe_dump({"platform": platform}, f, allow_unicode=True)

        # 2. zentao.yaml —— 禅道专属配置
        # 清洗 []'"} 污染字符；支持逗号分隔多值 → 存列表（与主窗口一致）
        product = re.sub(r"[\[\]'\"]", "", self.zt_product.text()).strip()
        if product:
            items = [x.strip() for x in product.replace("，", ",").split(",")
                     if x.strip()]
            product_val = (
                [int(x) for x in items if x.isdigit()]
                if all(x.isdigit() for x in items) else items)
        else:
            product_val = None

        zt_path = os.path.join(self.config_dir, "zentao.yaml")
        update_yaml_values(zt_path, {
            "base_url": self.zt_base_url.text().strip(),
            "account": self.zt_account.text().strip(),
            "password": self.zt_password.text().strip(),
            "filters.product": product_val,
            "filters.module_filter": self.zt_module_filter.text().strip() or None,
        })

        # 指派人：保存到公用 assignee.yaml（外部 TB 和禅道共用）
        assigned_text = self.zt_assigned_to.toPlainText().strip()
        assigned_to = [line.strip() for line in assigned_text.splitlines()
                       if line.strip()] if assigned_text else None
        assignee_path = os.path.join(self.config_dir, "assignee.yaml")
        if os.path.exists(assignee_path):
            update_yaml_values(assignee_path, {
                "assigned_to": assigned_to,
            })

        # 3. teambition_source.yaml —— 外部 TB 源专属配置
        tb_src_path = os.path.join(self.config_dir, "teambition_source.yaml")
        tb_src_data = {
            "url": self.tb_src_url.text().strip(),
            "account": self.tb_src_account.text().strip(),
            "password": self.tb_src_password.text().strip(),
        }
        if not os.path.exists(tb_src_path):
            with open(tb_src_path, "w", encoding="utf-8") as f:
                f.write("# 外部 Teambition 源配置\n")
                yaml.safe_dump(tb_src_data, f, allow_unicode=True, sort_keys=False)
        else:
            update_yaml_values(tb_src_path, tb_src_data)

    def _save_teambition(self):
        path = os.path.join(self.config_dir, "teambition.yaml")
        update_yaml_values(path, {
            "app_id": self.tb_app_id.text().strip(),
            "app_secret": self.tb_app_secret.text().strip(),
            "org_id": self.tb_org_id.text().strip(),
            "project.id": self.tb_project_id.text().strip(),
            "project.name": self.tb_project_name.text().strip(),
            "creator_name": self.tb_creator_name.text().strip(),
            "creator_id": self.tb_creator_id.text().strip(),
        })

    def _save_sync(self):
        path = os.path.join(self.config_dir, "sync.yaml")
        update_yaml_values(path, {
            "sync_attachments": self.sync_attachments.isChecked(),
            "max_attachment_size_mb": self.sync_max_size.value(),
            "dedup_threshold": self.sync_dedup.value(),
            "api_delay": self.sync_delay.value(),
            "attachment_retries": self.sync_retries.value(),
        })

    def _save_classifier(self):
        path = os.path.join(self.config_dir, "classifier.yaml")
        update_yaml_values(path, {
            "classifier.similarity.enabled": self.cls_sim_enabled.isChecked(),
            "classifier.similarity.threshold": self.cls_threshold.value(),
            "classifier.similarity.max_fetch": self.cls_max_fetch.value(),
            "classifier.similarity.incremental_days": self.cls_inc_days.value(),
            "classifier.llm.enabled": self.cls_llm_enabled.isChecked(),
            "classifier.llm.base_url": self.cls_llm_base_url.text().strip(),
            "classifier.llm.api_key": self.cls_llm_api_key.text().strip(),
            "classifier.llm.model": self.cls_llm_model.text().strip(),
            "classifier.llm.timeout": self.cls_llm_timeout.value(),
            "classifier.llm.batch_size": self.cls_llm_batch_size.value(),
        })

    def _save_ai_analysis(self):
        values = {
            "enabled": self.ai_enabled.isChecked(),
        }
        if self.ai_drc_server.text().strip():
            values["drc_server"] = self.ai_drc_server.text().strip()
        if self.ai_drc_username.text().strip():
            values["drc_username"] = self.ai_drc_username.text().strip()
        if self.ai_drc_password.text().strip():
            values["drc_password"] = self.ai_drc_password.text().strip()
        # 所属项目：只读，自动与 TB 配置同步，无需保存

        # 协同学习
        interval_map = {0: 24, 1: 168, 2: 720}
        values["collaborative_learning.enabled"] = self.collab_enabled.isChecked()
        values["collaborative_learning.github_token"] = self.collab_token.text().strip()
        values["collaborative_learning.sync_interval_hours"] = interval_map.get(
            self.collab_interval.currentIndex(), 168
        )
        values["collaborative_learning.repo_owner"] = "Liu-wang-cheng"
        values["collaborative_learning.repo_name"] = "TB_Import_tool"
        values["collaborative_learning.branch"] = "main"

        path = os.path.join(self.config_dir, "ai_analysis.yaml")
        update_yaml_values(path, values)

    def _save_dingtalk(self):
        path = os.path.join(self.config_dir, "dingtalk.yaml")
        update_yaml_values(path, {
            "enabled": True,  # 强制开启（开关置灰不允许关闭）
            "webhook_url": self.dt_webhook.text().strip(),
            "secret": self.dt_secret.text().strip(),
            "at_all": self.dt_at_all.isChecked(),
        })

    @staticmethod
    def _resolve_github_token() -> str:
        """从环境变量或文件读取 GitHub token（与 release.py 一致）"""
        env_token = os.environ.get("GITHUB_TOKEN", "")
        if env_token:
            return env_token.strip()
        token_path = os.path.expanduser("~/.github_token")
        if os.path.exists(token_path):
            try:
                with open(token_path, "r") as f:
                    return f.read().strip()
            except Exception:
                pass
        return ""

    def _set_collab_busy(self, busy: bool):
        """协同学习同步时禁用/启用所有交互元素"""
        enabled = not busy
        self.tabs.setEnabled(enabled)
        # 找到保存/取消按钮并禁用（它们是 QPushButton 没有直接引用）
        for child in self.findChildren(QPushButton):
            if child.text() in ("保存", "取消"):
                child.setEnabled(enabled)
        self.collab_sync_btn.setEnabled(enabled)
        # collab_status label 不受影响

    def _on_collab_sync(self):
        """触发协同学习手动同步。"""
        token = self.collab_token.text().strip()
        if not token:
            QMessageBox.warning(self, "未配置 Token",
                "请先在 GitHub Settings 创建 Personal Access Token\n"
                "并填入 GitHub Token 输入框（需勾选 repo 权限）。\n\n"
                "获取地址: https://github.com/settings/tokens")
            return

        # 防止重复点击
        if hasattr(self, '_collab_worker') and self._collab_worker is not None:
            if self._collab_worker.isRunning():
                return

        self._set_collab_busy(True)
        self.collab_status.setText("正在同步...")
        self.collab_status.setStyleSheet("color:#d97706; font-size:12px;")

        config = {
            "collaborative_learning": {
                "enabled": self.collab_enabled.isChecked(),
                "github_token": token,
                "repo_owner": "Liu-wang-cheng",
                "repo_name": "TB_Import_tool",
                "branch": "main",
            }
        }

        self._collab_worker = CollabSyncWorker(config)
        self._collab_worker.finished_signal.connect(self._on_collab_sync_finished)
        self._collab_worker.start()

    def _on_collab_sync_finished(self, success: bool, message: str):
        self._set_collab_busy(False)
        if success:
            self.collab_status.setText(message)
            self.collab_status.setStyleSheet("color:#059669; font-size:12px;")
        else:
            self.collab_status.setText(f"同步失败: {message}")
            self.collab_status.setStyleSheet("color:#dc2626; font-size:12px;")

    def done(self, result):
        """对话框关闭前等待协同同步线程结束，避免 QThread 运行中被销毁崩溃"""
        worker = getattr(self, "_collab_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(3000)
        super().done(result)
