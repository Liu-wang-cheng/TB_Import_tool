"""QThread 异步任务：所有耗时操作在后台线程执行，通过 signal 更新 UI"""

import copy
import logging
import os
import sys
import traceback

from gui.qt_compat import QThread, pyqtSignal

logger = logging.getLogger(__name__)


def _generate_updater_bat(current_dir: str, new_dir: str, pid: int,
                          extract_dir: str = "") -> str:
    """生成目录模式的更新 bat：等进程退出 → 复制新文件 → 重启"""
    exe_name = None
    for f in os.listdir(current_dir):
        if f.endswith('.exe'):
            exe_name = f
            break
    if not exe_name:
        exe_name = "智能缺陷管理平台.exe"

    cleanup_dir = extract_dir if extract_dir else new_dir
    return f"""@echo off
chcp 65001 >nul 2>&1
title 正在更新 智能缺陷管理平台

echo ============================================
echo   正在更新，请勿关闭此窗口
echo ============================================
echo.

REM 等待原进程退出（最多 30 秒）
set WAITED=0
:wait_loop
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if %errorlevel% equ 0 (
    set /a WAITED+=1
    if %WAITED% geq 30 (
        echo [ERROR] 原进程未能退出，更新取消
        goto :cleanup
    )
    timeout /t 1 /nobreak >nul
    goto wait_loop
)

echo [OK] 原进程已退出
echo.

REM 复制新文件覆盖旧文件（robocopy 支持重试）
echo 正在更新文件...
robocopy "{new_dir}" "{current_dir}" /e /xo /r:3 /w:1 /njh /njs /ndl /nc /ns >nul
if %errorlevel% geq 8 (
    echo [ERROR] 文件更新失败
    pause
    goto :cleanup
)

echo [OK] 文件更新成功
echo.

REM 启动新版本
echo 正在启动新版本...
start "" "{current_dir}\\{exe_name}"

:cleanup
REM 清理临时目录
if exist "{cleanup_dir}" rmdir /s /q "{cleanup_dir}"
(goto) 2>nul & del /f /q "%~f0"
"""


def _init_clients(config):
    """根据 config 创建源平台和 Teambition 客户端（供 worker 复用）"""
    from src.source_factory import create_source_client
    from src.teambition_client import TeambitionClient
    from src.utils import normalize_zentao_filters

    sync_cfg = config.get("sync", {})
    tb_cfg = config["teambition"]

    # 仅禅道平台需要 normalize filters
    platform = config.get("source", {}).get("platform", "zentao")
    if platform == "zentao":
        normalize_zentao_filters(config.setdefault("zentao", {}).setdefault("filters", {}))

    source = create_source_client(config)

    initial_project_id = tb_cfg.get("project_id", "")
    if not initial_project_id:
        project_cfg = tb_cfg.get("project", {})
        initial_project_id = project_cfg.get("id", "") or project_cfg.get("project_id", "")

    fallback_id = tb_cfg.get("creator_id") or tb_cfg.get("operator_id")
    teambition = TeambitionClient(
        app_id=tb_cfg["app_id"],
        app_secret=tb_cfg["app_secret"],
        org_id=tb_cfg["org_id"],
        project_id=initial_project_id,
        api_delay=sync_cfg.get("api_delay", 0.5),
        scenariofieldconfig_id=tb_cfg.get("scenariofieldconfig_id"),
        operator_id=fallback_id,
    )
    return source, teambition


class AuthTestWorker(QThread):
    """测试禅道和 Teambition 连接"""
    finished = pyqtSignal(bool, str)  # (success, message)
    progress = pyqtSignal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)

    def run(self):
        source = None
        teambition = None
        results = []

        try:
            # 源平台认证
            try:
                from src.source_factory import create_source_client
                source = create_source_client(self.config)
                source.authenticate()
                platform_name = "禅道" if source.source_type == "zentao" else "Jira"
                results.append((platform_name, True, "连接成功"))
                self.progress.emit(f"{platform_name}连接成功")
            except Exception as e:
                platform_name = "禅道" if self.config.get("source", {}).get("platform") == "zentao" else "Jira"
                results.append((platform_name, False, str(e)))
                self.progress.emit(f"{platform_name}连接失败: {e}")

            # Teambition 认证
            try:
                from src.teambition_client import TeambitionClient
                tb_cfg = self.config["teambition"]
                teambition = TeambitionClient(
                    app_id=tb_cfg["app_id"],
                    app_secret=tb_cfg["app_secret"],
                    org_id=tb_cfg["org_id"],
                    project_id="",
                )
                teambition.authenticate()
                results.append(("Teambition", True, "连接成功"))
                self.progress.emit("Teambition连接成功")
            except Exception as e:
                results.append(("Teambition", False, str(e)))
                self.progress.emit(f"Teambition连接失败: {e}")

            all_ok = all(ok for _, ok, _ in results)
            msg_parts = [f"{name}: {'✓' if ok else '✗ ' + detail}"
                         for name, ok, detail in results]
            self.finished.emit(all_ok, "\n".join(msg_parts))
        finally:
            if source:
                source.close()
            if teambition:
                teambition.close()


class ListBugsWorker(QThread):
    """获取禅道 Bug 列表"""
    finished = pyqtSignal(list)  # list of ZentaoBug
    progress = pyqtSignal(str)
    error = pyqtSignal(str, str)  # (message, traceback_detail)

    def __init__(self, config, dingtalk_bot=None, parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)
        self.dingtalk_bot = dingtalk_bot

    def run(self):
        source = None
        try:
            from src.source_factory import create_source_client
            from src.utils import (
                apply_module_filter, normalize_zentao_filters, resolve_assigned_to,
            )

            sync_cfg = self.config.get("sync", {})
            source = create_source_client(self.config)
            source.authenticate()

            platform_name = "禅道" if source.source_type == "zentao" else "Jira"
            self.progress.emit(f"{platform_name}认证成功，正在获取Bug列表...")

            filters = self.config.get("zentao", {}).get("filters", {})
            if source.source_type == "zentao":
                normalize_zentao_filters(filters)
            assigned_to = resolve_assigned_to(filters, source.account)

            bugs = source.fetch_all_bugs(
                product_id=filters.get("product_id"),
                project_id=filters.get("project_id"),
                statuses=filters.get("statuses"),
                date_from=filters.get("date_from"),
                date_to=filters.get("date_to"),
                assigned_to=assigned_to,
            )
            self.progress.emit(f"获取到 {len(bugs)} 条Bug")

            module_filter = (filters.get("module_filter") or "").strip()
            if module_filter and bugs:
                import time
                t0 = time.time()
                # 名称过滤：优先用模块API一次性解析为ID集合，避免逐条取详情
                module_id_set = None
                product_id = filters.get("product_id")
                if not module_filter.isdigit() and product_id:
                    self.progress.emit(f"通过模块API解析名称 '{module_filter}'...")
                    module_id_set = source.resolve_module_ids_by_name(
                        int(product_id), module_filter)
                    # 区分"API成功(含空集合)"与"模块树不完整(回退)"
                    if module_id_set is not None:
                        self.progress.emit(
                            f"模块名称命中 {len(module_id_set)} 个ID，按ID快速过滤"
                            f"({len(bugs)} 条 Bug)..."
                        )
                    else:
                        # 并发详情拉取：5 worker 把 0.6s/条压到 ~0.12s/条
                        est = len(bugs) * 0.6 / 5
                        self.progress.emit(
                            f"模块树不完整（仅根模块），回退到逐条详情匹配 "
                            f"'{module_filter}' (5并发，预计 {est:.0f} 秒)..."
                        )

                def _on_progress(i, total):
                    if i % 10 == 0 or i == total:
                        self.progress.emit(f"模块过滤中 {i}/{total}...")

                bugs = apply_module_filter(
                    bugs, module_filter,
                    fetch_detail_fn=source.fetch_bug_detail,
                    progress_fn=_on_progress,
                    module_id_set=module_id_set,
                )
                elapsed = time.time() - t0
                self.progress.emit(
                    f"模块过滤后剩余 {len(bugs)} 条Bug (耗时 {elapsed:.1f}s)"
                )

            self.finished.emit(bugs)

            # 钉钉通知
            if self.dingtalk_bot:
                try:
                    from src.models import SEVERITY_DISPLAY_MAP
                    severity_map = SEVERITY_DISPLAY_MAP
                    lines = [f"共 {len(bugs)} 条 Bug:", "",
                             "| ID | 状态 | 严重程度 | 指派给 | 标题 |",
                             "| --- | --- | --- | --- | --- |"]
                    for bug in bugs[:20]:
                        sev = severity_map.get(str(bug.severity), bug.severity)
                        assignee = bug.assignedTo[:8] if bug.assignedTo else "-"
                        title = bug.title
                        lines.append(f"| {bug.id} | {bug.status} | {sev} | {assignee} | {title} |")
                    if len(bugs) > 20:
                        lines.append(f"\n> 仅显示前 20 条，共 {len(bugs)} 条")
                    self.dingtalk_bot.send_markdown("禅道 Bug 列表", "\n".join(lines))
                except Exception as e:
                    logger.warning("钉钉通知发送失败: %s", e)
        except Exception as e:
            logger.exception("列出Bug失败")
            self.error.emit(str(e), traceback.format_exc())
        finally:
            if source:
                source.close()


class SyncWorker(QThread):
    """执行同步（试运行或正式）"""
    finished = pyqtSignal(str)  # stats summary
    progress = pyqtSignal(int, int, str)  # (current, total, message)
    error = pyqtSignal(str, str)  # (message, traceback_detail)

    def __init__(self, config, dry_run=False, dingtalk_bot=None, parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)
        self.dry_run = dry_run
        self.dingtalk_bot = dingtalk_bot

    def run(self):
        source = None
        teambition = None
        try:
            from src.config_resolver import ConfigResolver
            from src.sync_engine import SyncEngine

            source, teambition = _init_clients(self.config)

            platform_name = "禅道" if source.source_type == "zentao" else "Jira"
            self.progress.emit(0, 0, f"{platform_name}认证中...")
            source.authenticate()

            self.progress.emit(0, 0, "Teambition认证中...")
            teambition.authenticate()

            self.progress.emit(0, 0, "解析配置...")
            resolver = ConfigResolver(self.config, source, teambition)
            resolver.resolve()

            # 解析创建人（resolver 已解析 creator_name → creator_id，
            # 此处仅需将最终结果设为 operator_id）
            tb_cfg = self.config.get("teambition", {})
            creator_id = tb_cfg.get("creator_id", "")
            if creator_id:
                # 已经是 UUID 或被 resolver 解析为 UUID
                is_uuid = len(creator_id) == 24 and all(
                    c in '0123456789abcdef' for c in creator_id.lower())
                if is_uuid:
                    teambition.operator_id = creator_id
                else:
                    resolved = teambition.search_member(creator_id)
                    teambition.operator_id = resolved or None

            self.progress.emit(0, 0, "开始同步...")
            engine = SyncEngine(self.config, source, teambition,
                                dingtalk_bot=self.dingtalk_bot)
            stats = engine.run(
                dry_run=self.dry_run,
                progress_callback=lambda c, t, m: self.progress.emit(c, t, m),
            )
            self.finished.emit(str(stats))
        except Exception as e:
            logger.exception("同步失败")
            self.error.emit(str(e), traceback.format_exc())
        finally:
            if source:
                source.close()
            if teambition:
                teambition.close()


class ExportExcelWorker(QThread):
    """导出 Excel"""
    finished = pyqtSignal(str)  # output file path
    progress = pyqtSignal(str)
    error = pyqtSignal(str, str)  # (message, traceback_detail)

    def __init__(self, config, output_path="", parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)
        self.output_path = output_path

    def run(self):
        try:
            # 确保可以导入 tools 模块
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            from tools.export_bugs import export_bugs
            self.progress.emit("正在导出Excel...")
            path = export_bugs(self.config, self.output_path)
            self.finished.emit(path)
        except Exception as e:
            logger.exception("导出Excel失败")
            self.error.emit(str(e), traceback.format_exc())


class UpdateCheckWorker(QThread):
    """后台检查更新：并发测速镜像 → 获取版本 → 比对"""
    finished = pyqtSignal(dict)       # {has_update, info, mirrors, current, message}
    progress = pyqtSignal(str)        # 状态文本
    error = pyqtSignal(str, str)      # (message, traceback)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)

    def run(self):
        try:
            from gui.updater import (read_current_version, race_mirrors,
                                      fetch_version_info, compare_versions,
                                      DEFAULT_MIRRORS)
            update_cfg = self.config.get("update", {})
            if not update_cfg.get("enabled", True):
                self.finished.emit({"has_update": False, "message": "更新已禁用"})
                return

            repo = update_cfg.get("repository", "")
            mirrors = update_cfg.get("mirrors", DEFAULT_MIRRORS)
            version_file = update_cfg.get("version_file", "version.json")

            # 替换 {repo} 占位符，保留 download_prefix
            resolved = []
            for m in mirrors:
                resolved.append({
                    "name": m.get("name", ""),
                    "base_url": m.get("base_url", "").replace("{repo}", repo),
                    "download_prefix": m.get("download_prefix", ""),
                })
            if not resolved:
                resolved = [
                    {
                        "name": m.get("name", ""),
                        "base_url": m.get("base_url", "").replace("{repo}", repo),
                        "download_prefix": m.get("download_prefix", ""),
                    }
                    for m in DEFAULT_MIRRORS
                ]

            self.progress.emit("正在测试镜像速度...")
            results = race_mirrors(resolved, version_file)

            if not any(r.success for r in results):
                self.finished.emit({"has_update": False,
                                    "message": "所有镜像均不可达"})
                return

            fastest = next(r for r in results if r.success)
            self.progress.emit(f"最快镜像: {fastest.name} ({fastest.latency_ms:.0f}ms)")

            info_tuple = fetch_version_info(results, version_file)
            if info_tuple is None:
                self.finished.emit({"has_update": False,
                                    "message": "无法获取版本信息"})
                return

            info, best_mirror = info_tuple
            current = read_current_version()
            has_update = compare_versions(current, info.version) > 0

            self.finished.emit({
                "has_update": has_update,
                "info": info,
                "best_mirror": best_mirror,
                "sorted_mirrors": results,
                "current": current,
                "message": (
                    f"发现新版本 v{info.version}（当前 v{current}）"
                    if has_update else f"已是最新版本 v{current}"
                ),
            })
        except Exception as e:
            logger.exception("检查更新失败")
            self.error.emit(str(e), traceback.format_exc())


class UpdateDownloadWorker(QThread):
    """后台下载更新：下载 → 校验 sha256 → 生成替换 bat"""
    finished = pyqtSignal(str)            # bat_path
    progress = pyqtSignal(int, int, str)  # (downloaded, total, speed_msg)
    error = pyqtSignal(str, str)          # (message, traceback)

    def __init__(self, config, version_info, best_mirror, sorted_mirrors,
                 parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)
        self.version_info = version_info
        self.best_mirror = best_mirror       # MirrorResult
        self.sorted_mirrors = sorted_mirrors  # List[MirrorResult]

    def run(self):
        try:
            from gui.updater import (download_exe, verify_sha256,
                                      build_download_url)
            import zipfile
            import shutil

            info = self.version_info

            if not getattr(sys, 'frozen', False):
                self.error.emit("仅在打包模式下支持自动更新", "")
                return

            exe_dir = os.path.dirname(sys.executable)
            is_zip = info.download_url.endswith('.zip')

            if is_zip:
                dest_path = os.path.join(exe_dir, "_update_download.zip")
            else:
                filename = info.download_url.rsplit("/", 1)[-1]
                dest_path = os.path.join(exe_dir, filename)

            # 按镜像速度顺序构建下载 URL 列表
            download_urls = []
            seen_prefixes = set()
            for mirror in [self.best_mirror] + [
                m for m in self.sorted_mirrors if m.success and m != self.best_mirror
            ]:
                url = build_download_url(info.download_url, mirror.download_prefix)
                if url not in seen_prefixes:
                    download_urls.append((mirror.name, url))
                    seen_prefixes.add(url)

            downloaded = False
            last_error = ""
            for mirror_name, dl_url in download_urls:
                self.progress.emit(0, 0, f"正在从 {mirror_name} 下载更新...")
                try:
                    download_exe(
                        dl_url, dest_path,
                        progress_callback=lambda d, t, s: self.progress.emit(d, t, s),
                    )
                    self.progress.emit(0, 0, "正在校验文件完整性...")
                    if verify_sha256(dest_path, info.sha256):
                        downloaded = True
                        break
                    logger.warning("SHA256 校验失败（镜像 %s），尝试下一个", mirror_name)
                    last_error = f"SHA256 mismatch from {mirror_name}"
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception as e:
                    last_error = str(e)
                    logger.warning("从 %s 下载失败: %s", mirror_name, e)

            if not downloaded:
                self.error.emit(f"下载失败: {last_error}", "")
                return

            if is_zip:
                # 解压 zip 到临时目录
                self.progress.emit(0, 0, "正在解压更新包...")
                extract_dir = os.path.join(exe_dir, "_update_extracted")
                if os.path.isdir(extract_dir):
                    shutil.rmtree(extract_dir)
                with zipfile.ZipFile(dest_path, 'r') as zf:
                    zf.extractall(extract_dir)

                # 找到解压后的 exe（可能在子目录中）
                new_exe = None
                for root, dirs, files in os.walk(extract_dir):
                    for f in files:
                        if f.endswith('.exe'):
                            new_exe = os.path.join(root, f)
                            break
                    if new_exe:
                        break

                if not new_exe:
                    self.error.emit("更新包中未找到 exe 文件", "")
                    return

                # 生成替换 bat：覆盖整个目录
                new_dir = os.path.dirname(new_exe)
                bat_content = _generate_updater_bat(exe_dir, new_dir, os.getpid(),
                                                    extract_dir=extract_dir)
                bat_path = os.path.join(exe_dir, "_update_replace.bat")
                with open(bat_path, "w", encoding="utf-8") as f:
                    f.write(bat_content)

                # 清理 zip
                os.remove(dest_path)
                self.finished.emit(bat_path)
            else:
                # 旧模式：单文件 exe
                from gui.updater import generate_updater_bat
                pid = os.getpid()
                bat_content = generate_updater_bat(sys.executable, dest_path, pid)
                bat_path = os.path.join(exe_dir, "_update_replace.bat")
                with open(bat_path, "w", encoding="utf-8") as f:
                    f.write(bat_content)
                self.finished.emit(bat_path)
        except Exception as e:
            logger.exception("下载更新失败")
            self.error.emit(str(e), traceback.format_exc())
