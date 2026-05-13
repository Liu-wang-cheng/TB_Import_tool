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
    已存在时，补充缺失的配置文件并同步非敏感配置项。
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

    # 已有外部 configs：补充缺失文件 + 同步非敏感配置
    _SYNC_FILES = ("update.yaml", "source.yaml", "jira.yaml")
    for fname in _SYNC_FILES:
        int_path = os.path.join(int_configs, fname)
        ext_path = os.path.join(ext_configs, fname)
        if os.path.isfile(int_path):
            if not os.path.isfile(ext_path):
                shutil.copy2(int_path, ext_path)
                logging.info("已补充缺失配置: %s", fname)
            else:
                shutil.copy2(int_path, ext_path)
                logging.info("已同步配置: %s", fname)


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


def _cleanup_update_temp():
    """清理上次更新残留的临时目录和 bat 脚本"""
    app_dir = _get_app_dir()
    for name in ("_update_extracted", "_update_replace.bat", "_update_download.zip"):
        path = os.path.join(app_dir, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            logging.info("已清理更新残留目录: %s", name)
        elif os.path.isfile(path):
            os.remove(path)
            logging.info("已清理更新残留文件: %s", name)


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
        # 清理上次更新残留的临时目录
        _cleanup_update_temp()

    app = QApplication(sys.argv)
    app.setApplicationName("智能缺陷管理平台")

    window = MainWindow()
    window.show()

    sys.exit(exec_app(app))


if __name__ == "__main__":
    main()
