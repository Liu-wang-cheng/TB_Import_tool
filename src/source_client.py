"""源平台客户端抽象层

定义 SourceClient Protocol，方法签名完全贴合 sync_engine 的已有调用方式，
使 ZentaoAdapter 可直接透传，JiraAdapter 内部做转换。
现有 ZentaoBug / AttachmentFile 模型复用，不引入新数据类。
"""

import logging
from typing import Dict, List, Optional, Protocol, Set, runtime_checkable

from src.models import AttachmentFile, ZentaoBug

logger = logging.getLogger(__name__)


@runtime_checkable
class SourceClient(Protocol):
    """源平台客户端协议，所有适配器必须实现此接口。

    方法签名与 sync_engine 中已有调用完全一致，
    ZentaoAdapter 可直接透传底层 ZentaoClient，无需字段重命名。
    """

    source_type: str  # "zentao" | "jira"
    account: str

    def authenticate(self) -> None:
        """认证并验证连接（对应 ZentaoClient._ensure_token）"""
        ...

    def fetch_all_bugs(self, product_id=None, project_id=None,
                       statuses=None, date_from=None, date_to=None,
                       assigned_to=None) -> List[ZentaoBug]:
        """根据筛选条件获取缺陷列表，返回统一 ZentaoBug"""
        ...

    def fetch_bug_detail(self, bug_id: int) -> ZentaoBug:
        """获取单条缺陷完整详情"""
        ...

    def check_bug_has_vlns(self, bug_id: int) -> bool:
        """检查缺陷备注/历史中是否包含 TB 标记（VLNS / CPAX 等）"""
        ...

    def extract_vlns_numbers(self, bug_id: int) -> List[str]:
        """从备注/历史中提取 VLNS/CPAX 编号列表（用于精确搜索 TB 任务）"""
        ...

    def fetch_bug_comments(self, bug_id: int) -> List[dict]:
        """获取评论/备注列表，返回 [{actor, date, action, comment}, ...]"""
        ...

    def update_bug_title(self, bug_id: int, new_title: str) -> None:
        """回写标题到源平台（双向标题同步）"""
        ...

    def download_attachment(self, file_id: int,
                            filename: str = "") -> AttachmentFile:
        """下载附件文件"""
        ...

    def download_image(self, file_id: int) -> AttachmentFile:
        """下载内联图片"""
        ...

    def resolve_module_ids_by_name(self, product_id: int,
                                    name: str) -> Optional[Set[int]]:
        """模块/组件名称解析为 ID 集合（禅道模块树 / Jira 组件）"""
        ...

    def search_product(self, name: str) -> Optional[int]:
        """根据名称搜索产品/项目 ID"""
        ...

    def search_project(self, name: str) -> Optional[int]:
        """根据名称搜索项目 ID"""
        ...

    def close(self) -> None:
        """释放资源（Session 关闭等）"""
        ...
