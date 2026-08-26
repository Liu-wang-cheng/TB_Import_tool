"""智能缺陷管理平台 GUI 入口"""

import logging
import logging.handlers
import os
import shutil
import sys
from datetime import datetime

# 确保项目根目录在 sys.path 中
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# PyInstaller excludes pandas 但传递依赖可能拉入残缺模块，
# 导致 sklearn is_pandas_df() 触发 AttributeError（只捕获 ImportError）。
if 'pandas' in sys.modules:
    try:
        import pandas
        pandas.DataFrame
    except (ImportError, AttributeError):
        del sys.modules['pandas']


def _is_frozen():
    """是否在 PyInstaller 打包环境中运行"""
    return getattr(sys, 'frozen', False)


def _get_app_dir():
    """获取应用程序运行目录（exe 所在目录或脚本根目录）"""
    if _is_frozen():
        return os.path.dirname(sys.executable)
    return _project_root


def _get_resource_dir():
    """获取内置资源目录（PyInstaller 解压的 _MEIPASS 或项目根目录）"""
    if _is_frozen():
        return sys._MEIPASS
    return _project_root


def _setup_file_logging():
    """打包后 stderr 被丢弃，必须落盘到 exe 同级 logs/ 才能排错。

    - 控制台 Handler：始终安装（开发/CLI 直接看；exe 无控制台时无害）
    - 文件 Handler：写入 <app_dir>/logs/gui_YYYYMMDD_HHMMSS.log
    - 同步保留最近 20 个日志文件，超出自动清理
    """
    log_dir = os.path.join(_get_app_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(log_format)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    log_path = os.path.join(
        log_dir, f"gui_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _prune_old_logs(log_dir, keep=20)
    logging.info("GUI 日志输出到: %s", log_path)


def _prune_old_logs(log_dir: str, keep: int = 20):
    """保留最近 keep 个 gui_*.log，其余删除"""
    try:
        files = sorted(
            (f for f in os.listdir(log_dir) if f.startswith("gui_")
             and f.endswith(".log")),
            reverse=True,
        )
        for stale in files[keep:]:
            try:
                os.remove(os.path.join(log_dir, stale))
            except OSError:
                pass
    except OSError:
        pass


def _ensure_external_configs():
    """确保 configs/ 目录存在于 exe 旁边（可读写）

    PyInstaller 打包后 configs/ 是内置只读资源。
    首次运行时自动释放到 exe 同级目录，供 GUI 修改保存。
    已存在时，只补充缺失的配置文件（不覆盖用户已编辑的文件）。
    """
    app_dir = _get_app_dir()
    ext_configs = os.path.join(app_dir, "configs")
    res_dir = _get_resource_dir()
    int_configs = os.path.join(res_dir, "configs")

    if not os.path.isdir(int_configs):
        return

    if not os.path.isdir(ext_configs):
        shutil.copytree(int_configs, ext_configs)
        logging.info("已释放配置文件到: %s", ext_configs)
        return

    # 已有外部 configs：只补充缺失的文件（不覆盖已有文件，保护用户编辑）
    for fname in os.listdir(int_configs):
        if not fname.endswith(('.yaml', '.yml')):
            continue
        int_path = os.path.join(int_configs, fname)
        ext_path = os.path.join(ext_configs, fname)
        if os.path.isfile(int_path) and not os.path.isfile(ext_path):
            shutil.copy2(int_path, ext_path)
            logging.info("已补充缺失配置: %s", fname)


def _ensure_external_qss():
    """确保 style.qss 在可访问路径"""
    app_dir = _get_app_dir()
    ext_qss = os.path.join(app_dir, "gui", "resources", "style.qss")
    if os.path.exists(ext_qss):
        return

    res_dir = _get_resource_dir()
    int_qss = os.path.join(res_dir, "gui", "resources", "style.qss")
    if os.path.exists(int_qss):
        qss_dir = os.path.dirname(ext_qss)
        os.makedirs(qss_dir, exist_ok=True)
        shutil.copy2(int_qss, ext_qss)
        logging.info("已释放样式文件到: %s", ext_qss)


def _ensure_external_data():
    """确保 data/ 目录（TF-IDF 训练模型）存在于 exe 同级目录

    PyInstaller 打包后 data/ 是内置只读资源。
    首次运行时自动释放到 exe 同级目录，供程序读写。
    已存在时跳过，避免覆盖用户增量学习后的模型。
    """
    app_dir = _get_app_dir()
    ext_data = os.path.join(app_dir, "data")

    if os.path.isdir(ext_data):
        return

    res_dir = _get_resource_dir()
    int_data = os.path.join(res_dir, "data")

    if os.path.isdir(int_data):
        shutil.copytree(int_data, ext_data)
        logging.info("已释放训练数据到: %s", ext_data)


def _install_excepthook():
    """未捕获异常落盘，避免 GUI 闪退后毫无线索"""
    def _hook(exc_type, exc_value, exc_tb):
        logging.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _hook


def _ask_retry_update() -> bool:
    """弹窗询问是否继续上次未完成的更新（Qt 未初始化，用 ctypes）"""
    try:
        import ctypes
        res = ctypes.windll.user32.MessageBoxW(
            0,
            "检测到上次自动更新未完成。\n"
            "是否立即继续更新到新版本？\n\n"
            "（选择“否”将取消本次更新并正常启动）",
            "继续更新",
            0x4 | 0x20)  # MB_YESNO | MB_ICONQUESTION
        return res == 6  # IDYES
    except Exception:
        return False


def _restart_to_update() -> bool:
    """用当前版本的新模板重新生成更新 bat 并退出（bat 等本进程退出后替换）"""
    app_dir = _get_app_dir()
    extract_dir = os.path.join(app_dir, "_update_extracted")
    try:
        from gui.workers import _generate_updater_bat
        bat_content = _generate_updater_bat(
            app_dir, extract_dir, os.getpid(), extract_dir=extract_dir)
        bat_path = os.path.join(app_dir, "_update_replace.bat")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)
        import subprocess
        subprocess.Popen(["cmd", "/c", bat_path],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        logging.info("已重新生成更新脚本，正在退出以继续更新")
        return True
    except Exception as e:
        logging.error("重新生成更新脚本失败: %s", e)
        return False


def _find_new_exe(extract_dir: str):
    """在解压目录中定位新版本 exe（递归）"""
    for root, _dirs, files in os.walk(extract_dir):
        for f in files:
            if f.endswith('.exe'):
                return os.path.join(root, f)
    return None


def _repair_update_state() -> bool:
    """更新自愈：启动时处理上次更新中断的状态。

    返回 True 表示已进入"继续更新"流程（调用方应退出，不再启动 GUI）。

    优先级：
    1. _internal 缺失 + _internal_old 存在 → 自动回滚（rename 恢复）
    2. _internal 与 _internal_old 都存在 → 新 _internal 已就位，清理 _internal_old
    3. _update_replace.bat 残留 + _update_extracted 有完整新 exe
       → 询问用户是否继续更新，确认后重新生成新模板 bat 并重启替换
    4. 其余临时文件（_update_extracted / _update_download.zip）清理
    """
    app_dir = _get_app_dir()
    internal = os.path.join(app_dir, "_internal")
    internal_old = os.path.join(app_dir, "_internal_old")
    bat_path = os.path.join(app_dir, "_update_replace.bat")
    extract_dir = os.path.join(app_dir, "_update_extracted")

    # 1. 回滚：_internal 缺失但 _internal_old 在（更新在 rename 后中断）
    if os.path.isdir(internal_old) and not os.path.isdir(internal):
        try:
            os.rename(internal_old, internal)
            logging.warning("检测到上次更新中断，已自动回滚 _internal_old → _internal")
        except OSError as e:
            logging.error("自动回滚 _internal 失败: %s", e)
    # 2. 新 _internal 已就位 → 清理旧 _internal_old（杀毒锁定则留待下次）
    elif os.path.isdir(internal_old) and os.path.isdir(internal):
        shutil.rmtree(internal_old, ignore_errors=True)
        logging.info("已清理旧 _internal_old")

    # 3. 失败重试：bat 残留且解压目录有完整新 exe 时询问用户；
    #    无可用新 exe（下载损坏）或用户拒绝时删除残留 bat，走正常启动
    if os.path.exists(bat_path):
        if _find_new_exe(extract_dir) and _ask_retry_update():
            if _restart_to_update():
                return True
        else:
            if not _find_new_exe(extract_dir):
                logging.warning("更新残留的解压目录无可用 exe，清理后正常启动")
            else:
                logging.info("用户取消继续更新，正常启动")
        try:
            os.remove(bat_path)
        except OSError:
            pass

    # 4. 清理其余临时文件（_internal_old 已由上面逻辑处理）
    for name in ("_update_extracted", "_update_download.zip"):
        path = os.path.join(app_dir, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.exists(path):
                    logging.info("已清理更新残留目录: %s", name)
            elif os.path.isfile(path):
                os.remove(path)
                logging.info("已清理更新残留文件: %s", name)
        except OSError as e:
            logging.warning("清理 %s 失败: %s", name, e)
    return False


def main():
    # 先装日志，确保后续异常 / 启动信息都能落盘
    _setup_file_logging()
    _install_excepthook()

    from gui.qt_compat import QApplication, exec_app
    from gui.main_window import MainWindow

    # PyInstaller 打包模式：释放配置和样式到 exe 同级目录
    if _is_frozen():
        _ensure_external_configs()
        _ensure_external_qss()
        _ensure_external_data()
        # 更新自愈：回滚/继续更新/清理残留
        if _repair_update_state():
            return

    app = QApplication(sys.argv)
    app.setApplicationName("智能缺陷管理平台")

    window = MainWindow()
    window.show()

    sys.exit(exec_app(app))


if __name__ == "__main__":
    main()
