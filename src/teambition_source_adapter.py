"""外部 Teambition 源适配器：将 TeambitionSourceClient 包装为 SourceClient 接口

把外部 TB 的缺陷任务（task）映射为统一的 ZentaoBug 模型，
使 sync_engine 无需改动即可把外部 TB 缺陷导入到内部 TB。

关键适配：
- bug.id ← 外部任务 uniqueId（int 编号，用于去重标签）
- file_id 用全局 int 索引（外部文件 _id 是 hex 字符串，sync_engine 假设 int）
"""

import logging
import re
from typing import Dict, List, Optional, Set

from src.models import AttachmentFile, ZentaoBug
from src.teambition_source_client import TeambitionSourceClient

logger = logging.getLogger(__name__)


class TeambitionSourceAdapter:
    """外部 Teambition → SourceClient 适配器"""

    source_type = "teambition"

    def __init__(self, client: TeambitionSourceClient, project_id: str = "",
                 field_ids: dict = None):
        self._client = client
        self.project_id = project_id or client.project_id
        # 外部TB项目自定义字段 id → 语义 精确映射（如 {"sn_code": "6306e...4c"}）。
        # 外部TB与内部TB同项目时字段 id 一致，可直接导入 SN/产生时间等
        self._field_ids = field_ids or {}
        self._bug_scenariofield_id = ""
        self._unique_id_prefix = ""  # 项目任务编号前缀（如 "323A"）
        # uniqueId(int) → task 原始 dict，供 fetch_bug_detail 回查
        self._task_cache: Dict[int, dict] = {}
        # 全局文件索引(int) → {"task_id": str, "file": dict}
        self._file_registry: Dict[int, dict] = {}
        self._file_counter = 0

    @property
    def account(self) -> str:
        return self._client.account

    # ── 认证 ──────────────────────────────────────────

    def authenticate(self) -> None:
        self._client.authenticate()

    # ── 核心：拉取缺陷 ────────────────────────────────

    def _ensure_bug_type(self) -> str:
        if not self._bug_scenariofield_id and self.project_id:
            self._bug_scenariofield_id = self._client.find_bug_scenariofield_id(
                self.project_id)
        return self._bug_scenariofield_id

    def _task_to_bug(self, task: dict) -> ZentaoBug:
        """外部 TB task → ZentaoBug"""
        unique_id = task.get("uniqueId") or 0
        bug_id = int(unique_id) if str(unique_id).isdigit() else 0
        if bug_id == 0:
            # 无编号时用 _id（24位 hex）转数值兜底（避免 id=0 冲突）。
            # 不能用 abs(hash())：str hash 跨进程随机（PYTHONHASHSEED），
            # 去重标签会跨运行不一致导致重复建任务。
            _tid = str(task.get("_id", ""))
            try:
                bug_id = int(_tid, 16)
            except ValueError:
                bug_id = abs(hash(_tid)) % (10 ** 9)

        # 专属任务 ID：uniqueIdPrefix-uniqueId（如 "323A-24"），用于去重标签
        task_id = ""
        if self._unique_id_prefix and str(unique_id).isdigit():
            task_id = f"{self._unique_id_prefix}-{unique_id}"
        elif str(unique_id).isdigit():
            task_id = str(unique_id)

        # 用户名字（带缓存）
        executor_id = task.get("_executorId", "")
        creator_id = task.get("_creatorId", "")
        executor_name = self._client.get_user_name(executor_id)
        creator_name = self._client.get_user_name(creator_id)

        # 自定义字段提取
        cfs = self._extract_customfields(task)

        # 附件（注册到 file_registry，返回 int 索引）
        files = self._register_files(task)

        return ZentaoBug(
            id=bug_id,
            title=task.get("content", ""),
            status=self._client.get_taskflow_status_name(
                task.get("_taskflowstatusId", "")),
            steps=task.get("note", ""),
            severity=cfs.get("severity", ""),
            type=cfs.get("category", ""),
            frequency=cfs.get("frequency", ""),
            assignedTo=executor_name,
            assignedToAccount=executor_id,
            openedBy=creator_name,
            openedByAccount=creator_id,
            # 缺陷产生时间：优先外部TB自定义字段的时间字段，其次 startDate，最后创建时间。
            # 归一化为 YYYY-MM-DD HH:MM（点分日期如 "2026.8.21——15:50" 直接比较会
            # 因 '.' > '-' 导致 _filter_bugs 的日期筛选系统性错误；
            # M/D 格式如 "8/26 15：25" 用任务创建时间补全年份）
            openedDate=self._normalize_datetime(
                cfs.get("found_time") or task.get("startDate") or task.get("created", ""),
                reference=task.get("created") or task.get("startDate")),
            project=self.project_id,
            projectName=task.get("content", "")[:50],
            snCode=cfs.get("sn_code", ""),
            openedBuild=cfs.get("version", ""),
            files=files,
            task_id=task_id,
        )

    @staticmethod
    def _normalize_datetime(value, reference: str = "") -> str:
        """把外部TB时间字符串归一化为 YYYY-MM-DD HH:MM（失败原样返回）。

        reference: 参考时间字符串（如任务创建时间），用于 M/D 格式补全年份。
        """
        if not value or not isinstance(value, str):
            return value or ""
        try:
            from src.extractor import extract_datetime
            from datetime import datetime
            ref = None
            v = value.strip()
            for cand in (v, (reference or "").strip()):
                try:
                    ref = datetime.fromisoformat(cand)
                    break
                except (ValueError, TypeError):
                    continue
            result = extract_datetime(v, ref)
            return result if result else value
        except Exception:
            return value

    def _extract_customfields(self, task: dict) -> dict:
        """从 task.customfields 提取 severity/category/frequency/sn_code/version

        优先按 _customfieldId 精确映射（field_ids 配置，外部TB与内部TB
        同项目时字段 id 一致，可"直接导入"）；未配置/未命中时按值特征猜测：
        - severity：值含 致命/严重/一般/建议/S/A/B/C
        - category：commongroup 类型的值（如"固件缺陷"）
        - frequency：值含 概率 或 %（如"中(≤30%)"、"必现(=100%)"）
        - version：值形如 X.Y.Z（如 "1.0.12"、"V1.0.38"）
        - sn_code：值形如 HQ... 或 长字母数字（严格；混合大小写 SN
          如 Philips... 需通过 field_ids 精确映射）
        """
        result = {"severity": "", "category": "", "frequency": "",
                  "sn_code": "", "version": "", "found_time": ""}
        # 按 _customfieldId 精确映射（如 {"sn_code": "6306e205c09533eb452f004c"}）
        field_ids = getattr(self, "_field_ids", {}) or {}
        id_to_key = {str(v): k for k, v in field_ids.items() if v}
        for cf in task.get("customfields", []):
            cf_id = str(cf.get("_customfieldId") or "")
            key = id_to_key.get(cf_id)
            if not key:
                continue
            value = cf.get("value", [])
            title = ""
            if isinstance(value, list) and value:
                first = value[0]
                title = first.get("title", "") if isinstance(first, dict) else str(first)
            elif isinstance(value, str):
                title = value
            if title and key in result and not result[key]:
                result[key] = title
        for cf in task.get("customfields", []):
            value = cf.get("value", [])
            title = ""
            if isinstance(value, list) and value:
                first = value[0]
                title = first.get("title", "") if isinstance(first, dict) else str(first)
            elif isinstance(value, str):
                title = value
            if not title:
                continue
            cf_type = cf.get("type", "")
            # 缺陷分类：commongroup 类型
            if cf_type == "commongroup" and not result["category"]:
                result["category"] = title
            # 严重程度：值含严重程度关键字（排除 commongroup 分类值，
            # 如"一般性建议类问题"会同时命中"一般/建议"）
            if cf_type != "commongroup" \
                    and re.search(r'致命|严重|一般|建议|^[SABC]$', title) \
                    and not result["severity"]:
                result["severity"] = title
            # 复现概率：值含"概率"或"%"（如"中(≤30%)"、"高概率"）
            if ("概率" in title or "%" in title) and not result["frequency"]:
                result["frequency"] = title
            # SN 编码：值形如 HQ... 或 长字母数字（严格，防误判；
            # 混合大小写如 Philips... 由 field_ids 精确映射处理）
            if re.match(r'^[A-Z]{2,}[0-9A-Z]{6,}$', title) and not result["sn_code"]:
                result["sn_code"] = title
            # 版本：值形如 X.Y.Z 或 vX.Y.Z（去掉 V 前缀）
            if re.match(r'^[vV]?\d+\.\d+(\.\d+)?$', title) and not result["version"]:
                result["version"] = re.sub(r'^[vV]', '', title)
            # 缺陷产生时间：值形如 2026.8.21——15:50 / 2026-08-21 15:50
            # 或 M/D 格式 8/26 15：25（无年份，_normalize_datetime 用参考日期补全）
            if re.search(r'\d{1,2}[/.-]\d{1,2}[^0-9]*\d{1,2}[：:]\d{1,2}', title) \
                    and not result["found_time"]:
                result["found_time"] = title
        return result

    def _register_files(self, task: dict) -> list:
        """注册任务附件到 file_registry，返回 [{id:int, title, size}]

        外部 TB 的附件在评论里（activity.comment.attachments），不在 customfields。
        因此这里从评论附件收集（fetch_bug_detail 阶段才拉评论）。
        """
        return []

    def _collect_comment_attachments(self, task: dict) -> list:
        """拉取任务的评论附件，注册到 file_registry，返回 [{id:int, title, size}]"""
        files = []
        comments = self._client.fetch_task_comments(task.get("_id", ""))
        for c in comments:
            for att in c.get("attachments", []):
                self._file_counter += 1
                idx = self._file_counter
                self._file_registry[idx] = {"task_id": task.get("_id", ""), "file": att}
                files.append({
                    "id": idx,
                    "title": att.get("name", ""),
                    "size": int(att.get("size", 0) or 0),
                })
        return files

    # ── SourceClient 接口实现 ─────────────────────────

    def fetch_all_bugs(self, product_id=None, project_id=None,
                       statuses=None, date_from=None, date_to=None,
                       assigned_to=None,
                       server_status: str = "") -> List[ZentaoBug]:
        pid = project_id or self.project_id
        if not pid:
            logger.warning("外部 TB 未配置 project_id，无法拉取缺陷")
            return []
        # 获取项目任务编号前缀（如 "323A"），拼接专属任务 ID
        if not self._unique_id_prefix:
            self._unique_id_prefix = self._client.get_unique_id_prefix(pid)
        sfc_id = self._ensure_bug_type()
        # server_status="all" 时拉已完成缺陷（isDone=True，状态"关闭"），
        # 用于关闭同步；默认拉未完成缺陷
        is_done = True if server_status == "all" else None
        tasks = self._client.fetch_tasks(pid, sfc_id, is_done=is_done)

        # 清空缓存，重建
        self._task_cache = {}
        self._file_registry = {}
        self._file_counter = 0

        bugs = []
        for task in tasks:
            bug = self._task_to_bug(task)
            self._task_cache[bug.id] = task
            bugs.append(bug)

        # 客户端筛选（复用 sync_engine 的过滤约定）
        bugs = self._filter_bugs(bugs, statuses, date_from, date_to, assigned_to)
        return bugs

    def _filter_bugs(self, bugs, statuses, date_from, date_to, assigned_to):
        """按状态/日期/指派人客户端筛选"""
        result = []
        for bug in bugs:
            if statuses and bug.status not in statuses:
                continue
            if date_from or date_to:
                if not bug.openedDate or len(bug.openedDate) < 10:
                    if date_from or date_to:
                        continue
                else:
                    bug_date = bug.openedDate[:10]
                    if date_from and str(date_from) > bug_date:
                        continue
                    if date_to and str(date_to) < bug_date:
                        continue
            if assigned_to:
                names = set(assigned_to) if not isinstance(assigned_to, str) else {assigned_to}
                # 名字匹配（剥离部门前缀，像禅道那样）
                clean = self._strip_dept_prefix(bug.assignedTo or "")
                if clean not in names and bug.assignedTo not in names and bug.assignedToAccount not in names:
                    continue
            result.append(bug)
        return result

    @staticmethod
    def _strip_dept_prefix(name: str) -> str:
        if not name or not isinstance(name, str):
            return ""
        from src.utils import extract_department_prefix
        if extract_department_prefix(name):
            return name.split("-", 1)[1].strip()
        return name.strip()

    def fetch_bug_detail(self, bug_id: int) -> ZentaoBug:
        """返回完整详情（从缓存 task 重新映射 + 收集评论附件）"""
        task = self._task_cache.get(bug_id)
        if task is None:
            # 缓存未命中，返回最小 bug（id 保真）
            logger.debug("外部 TB 任务 %d 缓存未命中", bug_id)
            return ZentaoBug(id=bug_id, title=f"任务 {bug_id}")
        bug = self._task_to_bug(task)
        # 收集评论附件到 files（附件在评论里，需单独拉 activities）
        bug.files = self._collect_comment_attachments(task)
        return bug

    def check_bug_has_vlns(self, bug_id: int) -> bool:
        return len(self.extract_vlns_numbers(bug_id)) > 0

    def extract_vlns_numbers(self, bug_id: int) -> List[str]:
        """从评论中提取 VLNS/CPAX 编号

        注意：必须读原始评论（含【内部TB同步】回写评论），
        不能走 fetch_bug_comments，因为后者会把回写评论过滤掉，
        导致回写里的 VLNS 编号提取不到，去重失效。
        """
        task = self._task_cache.get(bug_id)
        if task is None:
            return []
        comments = self._client.fetch_task_comments(task.get("_id", ""))
        text = " ".join(c.get("comment", "") for c in comments)
        return list(dict.fromkeys(re.findall(r'(?:VLNS|CPAX)-(\d+)', text)))

    def fetch_bug_comments(self, bug_id: int) -> List[dict]:
        """拉取评论，返回 [{actor, date, action, comment, attachments}]

        过滤掉回写标记评论（【内部TB同步】开头），避免同步回内部 TB。
        """
        task = self._task_cache.get(bug_id)
        if task is None:
            return []
        comments = self._client.fetch_task_comments(task.get("_id", ""))
        return [c for c in comments
                if not c.get("comment", "").startswith("【内部TB同步】")]

    def update_bug_title(self, bug_id: int, new_title: str) -> None:
        """回写内部 TB 编号：先试写标题，失败则写评论（只写编号）"""
        task = self._task_cache.get(bug_id)
        if task is None:
            logger.warning("外部 TB 任务 %d 缓存未命中，无法回写", bug_id)
            return
        task_id = task.get("_id", "")
        # 先尝试写标题（无权限则回退写评论）
        if self._client.update_title(task_id, new_title):
            return
        # 从 new_title 提取编号（如【VLNS-72536】→ VLNS-72536），评论只写编号
        m = re.search(r'【([^】]+)】', new_title)
        display_id = m.group(1) if m else new_title
        self._client.add_comment(
            task_id, f"【内部TB同步】内部TB任务编号: {display_id}")

    def add_comment(self, bug_id: int, content: str) -> bool:
        """写评论到外部 TB 缺陷（回写内部 TB 编号）"""
        task = self._task_cache.get(bug_id)
        if task is None:
            logger.warning("外部 TB 任务 %d 缓存未命中，无法写评论", bug_id)
            return False
        return self._client.add_comment(task.get("_id", ""), content)

    def download_attachment(self, file_id: int,
                            filename: str = "") -> AttachmentFile:
        """下载附件（file_id 是全局索引，映射回真实文件）"""
        entry = self._file_registry.get(file_id)
        if not entry:
            logger.warning("外部 TB 附件 %d 未在 registry 中", file_id)
            return AttachmentFile(filename=filename or f"file_{file_id}")
        f = entry["file"]
        name = filename or f.get("name", "")
        mime = f.get("mimeType", "application/octet-stream")

        # 通过文件详情接口拿签名下载 URL（需要 task_id + activity_id + file_id）
        data = self._client.download_comment_attachment(
            f.get("id", ""),
            f.get("task_id", "") or entry.get("task_id", ""),
            f.get("activity_id", ""),
        )
        if data is None:
            logger.warning("外部 TB 附件下载失败: %s", name)
            data = b""
        return AttachmentFile(
            filename=name,
            content_type=mime,
            data=data,
            size=len(data),
        )

    def download_image(self, file_id: int) -> AttachmentFile:
        return self.download_attachment(file_id)

    def resolve_module_ids_by_name(self, product_id: int,
                                    name: str) -> Optional[Set[int]]:
        """外部 TB 无模块概念，返回 None（触发 sync_engine 回退）"""
        return None

    def resolve_module_descendant_ids(self, product_id: int,
                                      module_id) -> Optional[Set[int]]:
        """外部 TB 无模块概念，返回 None"""
        return None

    def search_product(self, name: str) -> Optional[int]:
        return None

    def search_project(self, name: str) -> Optional[int]:
        return None

    def close(self) -> None:
        self._client.close()

    def fetch_severity_labels(self, product_id: int = None) -> Dict[str, str]:
        """外部 TB 无严重程度翻译，返回空"""
        return {}

    def invalidate_cloud_browse_cache(self) -> None:
        """外部 TB 无浏览页缓存，空实现"""
        pass
