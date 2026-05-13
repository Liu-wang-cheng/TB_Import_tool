"""日志桥接：将 Python logging 输出实时显示到 QTextEdit"""

import logging
from gui.qt_compat import QObject, pyqtSignal, QColor


class LogSignal(QObject):
    """信号对象，用于从 logging 线程安全地向 UI 发送日志消息"""
    log_message = pyqtSignal(str, str)  # (message, level)


class QtLogHandler(logging.Handler):
    """将 logging 输出桥接到 QTextEdit 的 handler"""

    LEVEL_COLORS = {
        "DEBUG": "#888888",
        "INFO": "#333333",
        "WARNING": "#CC8800",
        "ERROR": "#CC0000",
        "CRITICAL": "#CC0000",
    }

    def __init__(self, text_edit, parent=None):
        super().__init__()
        self.text_edit = text_edit
        self.log_signal = LogSignal(parent)
        self.log_signal.log_message.connect(self._append_log)

    def emit(self, record):
        msg = self.format(record)
        level = record.levelname
        self.log_signal.log_message.emit(msg, level)

    def _append_log(self, message, level):
        color = self.LEVEL_COLORS.get(level, "#333333")
        self.text_edit.setTextColor(QColor(color))
        self.text_edit.append(message)
        self.text_edit.setTextColor(QColor("#333333"))
        # 自动滚动到底部
        scrollbar = self.text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
