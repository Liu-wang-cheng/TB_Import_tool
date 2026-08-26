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
    """生成目录模式的更新 bat：等进程退出 → 只更新 _internal/ 和 exe → 重启

    只替换程序文件（exe + _internal/），不碰用户数据（configs/、data/、logs/）。
    用户数据通过 _ensure_external_*() 函数在启动时按需补充。

    注意：bat 不嵌入任何绝对/中文路径（UTF-8 写入的文件在 GBK 代码页系统
    会被 cmd 读乱码，导致 rename/robocopy 路径失效），全部用 %~dp0 和
    动态遍历解析。
    """
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
cd /d "%~dp0"

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
timeout /t 2 /nobreak >nul
echo.

REM 动态定位新版本 exe 与其所在目录（_update_extracted 内，名称不写死，
REM 避免中文/绝对路径在非 UTF-8 代码页系统上被 cmd 读乱码）
set "NEW_EXE="
for /r "_update_extracted" %%f in (*.exe) do if not defined NEW_EXE set "NEW_EXE=%%f"
if not defined NEW_EXE (
    echo [ERROR] 未找到新版本程序文件，更新取消
    pause
    goto :cleanup
)
for %%f in ("%NEW_EXE%") do set "NEW_DIR=%%~dpf"
for %%f in ("%~dp0*.exe") do set "EXE_NAME=%%~nxf"
if not defined EXE_NAME (
    echo [ERROR] 未找到当前程序文件，更新取消
    pause
    goto :cleanup
)

REM 清理可能的上次残留（如果上次更新中断留下的 _internal_old）
if exist "_internal_old" (
    rmdir /s /q "_internal_old" 2>nul
)

REM rename 策略：rename 是原子操作，即使文件被锁也能成功
echo 正在切换程序文件...
rename "_internal" "_internal_old"
if %errorlevel% neq 0 (
    echo [ERROR] 无法重命名 _internal 目录
    echo 可能原因：权限不足 / _internal 不存在 / 杀毒锁定
    echo.
    echo 请尝试：
    echo   1. 以管理员身份运行
    echo   2. 暂时禁用杀毒软件
    pause
    goto :cleanup
)
echo [OK] 旧 _internal 已重命名

REM robocopy 到空目录（无锁，必定成功）
robocopy "%NEW_DIR%_internal" "_internal" /e /r:3 /w:1 /njh /njs /ndl /nc /ns /np >nul
if %errorlevel% geq 8 (
    echo [ERROR] _internal 复制失败，回滚
    rename "_internal_old" "_internal"
    pause
    goto :cleanup
)
echo [OK] 新 _internal 已就位

REM 更新 exe（重命名 + 复制）
copy /y "%NEW_DIR%%EXE_NAME%" "%EXE_NAME%" >nul
if %errorlevel% neq 0 (
    echo [ERROR] exe 复制失败（新 _internal 已就位，可手动覆盖 exe）
    pause
    goto :cleanup
)
echo [OK] exe 已更新
echo.

REM 验证版本号
findstr /r "^[0-9]" "_internal\\VERSION" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] 更新完成
) else (
    echo [WARN] VERSION 文件异常，但更新已执行
)
echo.

REM 启动新版本
echo 正在启动新版本...
start "" "%EXE_NAME%"

REM 等待新版本启动，然后清理旧 _internal_old（多次重试，杀毒扫描可能锁定）
timeout /t 3 /nobreak >nul
set OLD_RETRY=0
:old_cleanup_loop
if not exist "_internal_old" goto :cleanup
rmdir /s /q "_internal_old" 2>nul
if exist "_internal_old" (
    set /a OLD_RETRY+=1
    if %OLD_RETRY% lss 5 (
        timeout /t 3 /nobreak >nul
        goto old_cleanup_loop
    )
    echo [WARN] _internal_old 残留（杀毒锁定？），将由新版本 GUI 启动时清理
)

:cleanup
if exist "_update_extracted" rmdir /s /q "_update_extracted"
if exist "_update_download.zip" del /f /q "_update_download.zip"
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
                platform_name = {"zentao": "禅道", "jira": "Jira",
                                  "teambition": "外部TB"}.get(source.source_type, "禅道")
                results.append((platform_name, True, "连接成功"))
                self.progress.emit(f"{platform_name}连接成功")
            except Exception as e:
                platform_name = {"zentao": "禅道", "jira": "Jira",
                                  "teambition": "外部TB"}.get(
                    self.config.get("source", {}).get("platform"), "禅道")
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
    severity_labels: dict = {}  # 禅道页面翻译的严重程度

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)

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

            # 用户主动刷新操作，清空云版禅道浏览页缓存，
            # 确保拿到最新数据（避免单例 client 缓存导致显示陈旧 bug 列表）
            invalidate = getattr(source, "invalidate_cloud_browse_cache", None)
            if callable(invalidate):
                invalidate()

            platform_name = {"zentao": "禅道", "jira": "Jira",
                              "teambition": "外部TB"}.get(source.source_type, "禅道")
            self.progress.emit(f"{platform_name}认证成功，正在获取缺陷列表...")

            # 指派人公用（assignee.yaml），外部 TB 和禅道都从这读
            assignee_filters = self.config.get("assignee", {})
            list_assigned_to = resolve_assigned_to(assignee_filters, source.account)

            # 外部 TB：列出时不筛状态（列出所有）
            if source.source_type == "teambition":
                filters = self.config.get("teambition_source", {}).get("filters", {})
                list_statuses = None
            else:
                filters = self.config.get("zentao", {}).get("filters", {})
                if source.source_type == "zentao":
                    normalize_zentao_filters(filters)
                list_statuses = filters.get("statuses")

            # 获取严重程度翻译（仅禅道，多产品合并）
            if source.source_type == "zentao":
                self.severity_labels = {}
                for pid in (filters.get("product_ids") or
                            ([int(filters["product_id"])]
                             if filters.get("product_id") else [])):
                    labels = source.fetch_severity_labels(pid)
                    if labels:
                        self.severity_labels.update(labels)

            # 多产品/多项目：循环拉取后保序去重
            if source.source_type == "teambition":
                from src.utils import _as_str_list
                product_ids = []
                project_ids = _as_str_list(
                    filters.get("project_ids") or filters.get("project_id"))
            else:
                product_ids = filters.get("product_ids") or (
                    [int(filters["product_id"])]
                    if filters.get("product_id") else [])
                project_ids = filters.get("project_ids") or (
                    [int(filters["project_id"])]
                    if filters.get("project_id") else [])
            bugs = []
            for pid in product_ids or [None]:
                for jid in project_ids or [None]:
                    bugs.extend(source.fetch_all_bugs(
                        product_id=pid,
                        project_id=jid,
                        statuses=list_statuses,
                        date_from=filters.get("date_from"),
                        date_to=filters.get("date_to"),
                        assigned_to=list_assigned_to,
                    ))
            seen = set()
            dedup = []
            for b in bugs:
                if b.id not in seen:
                    seen.add(b.id)
                    dedup.append(b)
            bugs = dedup
            self.progress.emit(f"获取到 {len(bugs)} 条缺陷")

            module_filter = (filters.get("module_filter") or "").strip()
            if module_filter and bugs:
                import time
                t0 = time.time()
                # 优先用模块API一次性解析为ID集合，避免逐条取详情
                module_id_set = None
                product_ids = filters.get("product_ids") or (
                    [int(filters["product_id"])]
                    if filters.get("product_id") else [])
                if product_ids:
                    api_ok = False
                    if module_filter.isdigit():
                        # 数字ID：递归包含子模块（与禅道网页 byModule 一致），
                        # 多产品合并集合（模块ID全局唯一，安全）
                        resolve_desc = getattr(source, "resolve_module_descendant_ids", None)
                        if resolve_desc:
                            self.progress.emit(
                                f"解析模块 {module_filter} 及其子模块...")
                            module_id_set = set()
                            for pid in product_ids:
                                sub = resolve_desc(int(pid), module_filter)
                                if sub is None:
                                    continue
                                api_ok = True
                                module_id_set |= sub
                    else:
                        self.progress.emit(f"通过模块API解析名称 '{module_filter}'...")
                        module_id_set = set()
                        for pid in product_ids:
                            sub = source.resolve_module_ids_by_name(
                                int(pid), module_filter)
                            if sub is None:
                                continue
                            api_ok = True
                            module_id_set |= sub
                    if not api_ok:
                        module_id_set = None
                    # 区分"API成功(含空集合)"与"模块树不完整(回退)"
                    if module_id_set is not None:
                        self.progress.emit(
                            f"模块命中 {len(module_id_set)} 个ID，按ID快速过滤"
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
        except Exception as e:
            logger.exception("列出缺陷失败")
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

            platform_name = {"zentao": "禅道", "jira": "Jira",
                              "teambition": "外部TB"}.get(source.source_type, "禅道")
            self.progress.emit(0, 0, f"{platform_name}认证中...")
            source.authenticate()

            # 用户主动同步操作，清空云版禅道浏览页缓存，确保拿到最新 bug 列表
            invalidate = getattr(source, "invalidate_cloud_browse_cache", None)
            if callable(invalidate):
                invalidate()

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
            seen_urls = set()
            for mirror in [self.best_mirror] + [
                m for m in self.sorted_mirrors if m.success and m != self.best_mirror
            ]:
                url = build_download_url(info.download_url, mirror.download_prefix)
                if url not in seen_urls:
                    download_urls.append((mirror.name, url))
                    seen_urls.add(url)
            # 始终追加 GitHub 直连作为最终兜底（镜像可能不代理 Release 大文件）
            if info.download_url not in seen_urls:
                download_urls.append(("GitHub直连", info.download_url))
                seen_urls.add(info.download_url)

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
