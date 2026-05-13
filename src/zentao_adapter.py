"""禅道适配器：将 ZentaoClient 包装为 SourceClient 接口

直接透传所有方法调用，零转换开销。
sync_engine 通过 SourceClient Protocol 调用时，
ZentaoAdapter 等价于原始 ZentaoClient 的薄包装。
"""

import logging
from typing import Dict, List, Optional, Set

from src.models import AttachmentFile, ZentaoBug
from src.zentao_client import ZentaoClient

logger = logging.getLogger(__name__)


class ZentaoAdapter:
    """透传型适配器，对外提供 SourceClient 协议接口"""

    source_type = "zentao"

    def __init__(self, client: ZentaoClient):
        self._client = client

    @property
    def account(self) -> str:
        return self._client.account

    def authenticate(self) -> None:
        self._client._ensure_token()

    def fetch_all_bugs(self, product_id=None, project_id=None,
                       statuses=None, date_from=None, date_to=None,
                       assigned_to=None) -> List[ZentaoBug]:
        return self._client.fetch_all_bugs(
            product_id=product_id,
            project_id=project_id,
            statuses=statuses,
            date_from=date_from,
            date_to=date_to,
            assigned_to=assigned_to,
        )

    def fetch_bug_detail(self, bug_id: int) -> ZentaoBug:
        return self._client.fetch_bug_detail(bug_id)

    def check_bug_has_vlns(self, bug_id: int) -> bool:
        return self._client.check_bug_has_vlns(bug_id)

    def fetch_bug_comments(self, bug_id: int) -> List[dict]:
        return self._client.fetch_bug_comments(bug_id)

    def update_bug_title(self, bug_id: int, new_title: str) -> None:
        self._client.update_bug_title(bug_id, new_title)

    def download_attachment(self, file_id: int,
                            filename: str = "") -> AttachmentFile:
        return self._client.download_attachment(file_id, filename)

    def download_image(self, file_id: int) -> AttachmentFile:
        return self._client.download_image(file_id)

    def resolve_module_ids_by_name(self, product_id: int,
                                    name: str) -> Optional[Set[int]]:
        return self._client.resolve_module_ids_by_name(product_id, name)

    def search_product(self, name: str) -> Optional[int]:
        return self._client.search_product(name)

    def search_project(self, name: str) -> Optional[int]:
        return self._client.search_project(name)

    def close(self) -> None:
        self._client.close()
