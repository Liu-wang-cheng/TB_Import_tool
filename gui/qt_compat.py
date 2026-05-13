"""PyQt5/PyQt6 兼容层 — 自动选择可用的绑定并统一 API"""

import sys

QT_VERSION = 5

try:
    from PyQt6.QtCore import pyqtSignal, QThread, QObject, QDate, Qt, QTimer
    from PyQt6.QtGui import QAction, QColor, QFont
    from PyQt6.QtWidgets import (  # noqa: F401
        QApplication, QAbstractItemView, QCheckBox, QComboBox,
        QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
        QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
        QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow,
        QMessageBox, QProgressBar, QPushButton, QSpinBox, QStackedWidget,
        QSplitter, QStatusBar, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
    )
    QT_VERSION = 6

    # PyQt6 枚举统一为旧式写法（便于代码兼容）
    if not hasattr(Qt, 'Checked'):
        Qt.Checked = Qt.CheckState.Checked
        Qt.Unchecked = Qt.CheckState.Unchecked
        Qt.PartiallyChecked = Qt.CheckState.PartiallyChecked
    if not hasattr(Qt, 'AlignLeft'):
        Qt.AlignLeft = Qt.AlignmentFlag.AlignLeft
        Qt.AlignRight = Qt.AlignmentFlag.AlignRight
        Qt.AlignCenter = Qt.AlignmentFlag.AlignCenter
        Qt.AlignTop = Qt.AlignmentFlag.AlignTop
        Qt.AlignBottom = Qt.AlignmentFlag.AlignBottom
    if not hasattr(Qt, 'UserRole'):
        Qt.UserRole = Qt.ItemDataRole.UserRole
        Qt.DisplayRole = Qt.ItemDataRole.DisplayRole
    if not hasattr(Qt, 'Window'):
        Qt.Window = Qt.WindowType.Window
        Qt.Dialog = Qt.WindowType.Dialog
    if not hasattr(Qt, 'WaitCursor'):
        Qt.WaitCursor = Qt.CursorShape.WaitCursor
        Qt.ArrowCursor = Qt.CursorShape.ArrowCursor
    if not hasattr(Qt, 'TextSelectableByMouse'):
        Qt.TextSelectableByMouse = Qt.TextInteractionFlag.TextSelectableByMouse
    if not hasattr(Qt, 'ScrollBarAlwaysOff'):
        Qt.ScrollBarAlwaysOff = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        Qt.ScrollBarAlwaysOn = Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        Qt.ScrollBarAsNeeded = Qt.ScrollBarPolicy.ScrollBarAsNeeded
    if not hasattr(Qt, 'ItemIsUserCheckable'):
        Qt.ItemIsUserCheckable = Qt.ItemFlag.ItemIsUserCheckable
        Qt.ItemIsEnabled = Qt.ItemFlag.ItemIsEnabled
        Qt.ItemIsSelectable = Qt.ItemFlag.ItemIsSelectable
        Qt.ItemIsEditable = Qt.ItemFlag.ItemIsEditable

    # QAbstractItemView.EditTrigger
    if not hasattr(QAbstractItemView, 'DoubleClicked'):
        QAbstractItemView.DoubleClicked = QAbstractItemView.EditTrigger.DoubleClicked
        QAbstractItemView.EditKeyPressed = QAbstractItemView.EditTrigger.EditKeyPressed
        QAbstractItemView.NoEditTriggers = QAbstractItemView.EditTrigger.NoEditTriggers
        QAbstractItemView.SelectedClicked = QAbstractItemView.EditTrigger.SelectedClicked
    if not hasattr(Qt, 'ApplicationModal'):
        Qt.ApplicationModal = Qt.WindowModality.ApplicationModal
        Qt.WindowModal = Qt.WindowModality.WindowModal
        Qt.NonModal = Qt.WindowModality.NonModal

    # QLineEdit.EchoMode
    if not hasattr(QLineEdit, 'Password'):
        QLineEdit.Password = QLineEdit.EchoMode.Password
        QLineEdit.Normal = QLineEdit.EchoMode.Normal

    # QMessageBox.StandardButton
    if not hasattr(QMessageBox, 'Yes'):
        QMessageBox.Yes = QMessageBox.StandardButton.Yes
        QMessageBox.No = QMessageBox.StandardButton.No
        QMessageBox.Ok = QMessageBox.StandardButton.Ok
        QMessageBox.Cancel = QMessageBox.StandardButton.Cancel

    # QDialog.DialogCode
    if not hasattr(QDialog, 'Accepted'):
        QDialog.Accepted = QDialog.DialogCode.Accepted
        QDialog.Rejected = QDialog.DialogCode.Rejected

    # QDialogButtonBox.StandardButton
    if not hasattr(QDialogButtonBox, 'Save'):
        QDialogButtonBox.Save = QDialogButtonBox.StandardButton.Save
        QDialogButtonBox.Cancel = QDialogButtonBox.StandardButton.Cancel
        QDialogButtonBox.Ok = QDialogButtonBox.StandardButton.Ok

    # QListView.Flow
    if not hasattr(QListView, 'LeftToRight'):
        QListView.LeftToRight = QListView.Flow.LeftToRight
        QListView.TopToBottom = QListView.Flow.TopToBottom
        QListView.IconMode = QListView.ViewMode.IconMode
        QListView.ListMode = QListView.ViewMode.ListMode
        QListView.Adjust = QListView.ResizeMode.Adjust
        QListView.Fixed = QListView.ResizeMode.Fixed

except ImportError:
    from PyQt5.QtCore import pyqtSignal, QThread, QObject, QDate, Qt, QTimer  # noqa: F401
    from PyQt5.QtGui import QAction, QColor, QFont  # noqa: F401
    from PyQt5.QtWidgets import (  # noqa: F401
        QApplication, QAbstractItemView, QCheckBox, QComboBox,
        QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
        QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
        QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow,
        QMessageBox, QProgressBar, QPushButton, QSpinBox, QStackedWidget,
        QSplitter, QStatusBar, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
    )


def exec_dialog(dialog):
    if hasattr(dialog, 'exec'):
        return dialog.exec()
    return dialog.exec_()


def exec_app(app):
    if hasattr(app, 'exec'):
        return app.exec()
    return app.exec_()
