"""定时导入方案选择对话框：勾选方案并调整执行顺序。"""

from gui.qt_compat import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, Qt,
)


class ScheduleProfilesDialog(QDialog):
    """选择定时任务要执行的方案（可多选，按列表顺序执行）。"""

    def __init__(self, all_names: list, selected_names: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("定时导入方案")
        self.resize(420, 380)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "勾选要定时导入的方案；到点后按列表顺序逐个执行。\n"
            "未勾选任何方案时，定时任务按当前界面配置导入。"))

        ordered = ([n for n in selected_names if n in all_names]
                   + [n for n in all_names if n not in selected_names])
        self.list = QListWidget()
        for name in ordered:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if name in selected_names else Qt.Unchecked)
            self.list.addItem(item)
        layout.addWidget(self.list)

        btn_row = QHBoxLayout()
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        btn_row.addWidget(self.btn_up)
        btn_row.addWidget(self.btn_down)
        btn_row.addStretch()

        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def _move(self, delta: int):
        row = self.list.currentRow()
        new_row = row + delta
        if row < 0 or not (0 <= new_row < self.list.count()):
            return
        item = self.list.takeItem(row)
        self.list.insertItem(new_row, item)
        self.list.setCurrentRow(new_row)

    def selected_profiles(self) -> list:
        """按列表顺序返回勾选的方案名"""
        return [self.list.item(i).text()
                for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]
