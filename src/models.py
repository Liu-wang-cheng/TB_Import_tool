"""数据模型定义"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncAction(Enum):
    CREATED = "created"
    REACTIVATED = "reactivated"
    SKIPPED_DEDUP = "skipped_dedup"
    SKIPPED_FILTERED = "skipped_filtered"
    ERROR = "error"


@dataclass
class ZentaoBug:
    id: int
    title: str
    severity: str = ""
    pri: str = ""
    type: str = ""
    status: str = ""
    steps: str = ""
    assignedTo: str = ""
    assignedToAccount: str = ""
    openedBy: str = ""
    openedByAccount: str = ""
    openedDate: str = ""
    product: str = ""
    productName: str = ""
    project: str = ""
    projectName: str = ""
    module: str = ""
    moduleName: str = ""
    openedBuild: str = ""
    snCode: str = ""
    frequency: str = ""
    files: list = field(default_factory=list)

    def get_base_title(self) -> str:
        title = self.title
        # 移除 【禅道xxx】 标注
        title = re.sub(r'【禅道\d+】', '', title)
        # 移除 【VLNS-xxxxx】 或旧格式 【TB-xxx】 标注
        title = re.sub(r'【[\w]+-\d+】', '', title)
        title = re.sub(r'【TB-[\w-]+】', '', title)
        return title.strip()

    def get_teambition_tag(self) -> str:
        return f"【禅道{self.id}】"


@dataclass
class TeambitionTask:
    id: str = ""
    taskId: str = ""
    taskIdentifier: str = ""
    content: str = ""
    note: str = ""
    priority: int = 0
    executorId: str = ""
    tagIds: list = field(default_factory=list)
    customfields: list = field(default_factory=list)
    status: str = ""
    created: str = ""
    updated: str = ""
    sfcId: str = ""
    taskflowId: str = ""
    severity: str = ""
    taskType: str = ""
    frequency: str = ""  # 复现概率 (1必现/2高概率/3中概率/4低概率)
    reproduction: str = ""
    defectCategory: str = ""
    project: str = ""
    isArchived: bool = False

    def get_base_title(self) -> str:
        title = self.content
        title = re.sub(r'【禅道\d+】', '', title)
        title = re.sub(r'【[\w]+-\d+】', '', title)
        title = re.sub(r'【TB-[\w-]+】', '', title)
        return title.strip()

    def get_zentao_bug_id(self) -> Optional[int]:
        match = re.search(r'【禅道(\d+)】', self.content)
        return int(match.group(1)) if match else None


@dataclass
class AttachmentFile:
    filename: str
    content_type: str = "application/octet-stream"
    data: bytes = b""
    size: int = 0


@dataclass
class SyncResult:
    zentao_bug_id: int
    action: SyncAction
    teambition_task_id: str = ""
    message: str = ""


@dataclass
class SyncStats:
    total: int = 0
    created: int = 0
    reactivated: int = 0
    closed_synced: int = 0
    skipped_dedup: int = 0
    skipped_filtered: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            f"同步完成: 共 {self.total} 条, "
            f"新建 {self.created} 条, "
            f"重新激活 {self.reactivated} 条, "
            f"同步关闭 {self.closed_synced} 条, "
            f"去重跳过 {self.skipped_dedup} 条, "
            f"筛选跳过 {self.skipped_filtered} 条, "
            f"错误 {self.errors} 条"
        )


# 禅道严重程度映射
SEVERITY_NAMES = {
    "1": "致命", "2": "严重", "3": "一般", "4": "轻微",
    "致命": "致命", "严重": "严重", "一般": "一般", "建议": "建议", "轻微": "轻微",
}

# 禅道严重程度中文名称（用于 CLI/钉钉显示）
# 默认映射，可在 teambition.yaml 中用 severity_labels 覆盖
SEVERITY_LABELS = {
    "1": "致命", "2": "严重", "3": "一般", "4": "建议",
    "致命": "致命", "严重": "严重", "一般": "一般", "建议": "建议", "轻微": "轻微",
}

# 禅道Bug类型映射（字符串名 + 数字ID 双映射）
BUG_TYPE_NAMES = {
    "codeerror": "代码错误", "1": "代码错误",
    "config": "配置相关", "2": "配置相关",
    "install": "安装部署", "3": "安装部署",
    "security": "安全相关", "4": "安全相关",
    "performance": "性能问题", "5": "性能问题",
    "standard": "标准规范", "6": "标准规范",
    "automation": "自动化测试", "7": "自动化测试",
    "designdefect": "设计缺陷", "8": "设计缺陷",
    "track": "跟踪", "9": "跟踪",
    "others": "其他", "10": "其他",
}

# Teambition 严重程度等级
TB_SEVERITY_LEVELS = ["S", "A", "B", "C"]

# Teambition 复现概率
TB_REPRODUCTION = ["必现", "高概率", "中概率", "低概率"]

# Teambition 任务状态
TB_STATUS_MAP = {
    "待处理": "pending",
    "修复中": "in_progress",
    "已解决": "resolved",
    "待回归": "waiting_verify",
    "关闭": "closed",
    "重新打开": "reopened",
}
