"""Teambition API 客户端"""

import logging
import time
import urllib.parse
from typing import Dict, List, Optional

import jwt
import requests

from src.models import AttachmentFile, TeambitionTask

logger = logging.getLogger(__name__)

API_BASE = "https://open.teambition.com/api"


class TeambitionAPIError(Exception):
    def __init__(self, code: int, message: str, path: str):
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"Teambition API错误 [{code}] {path}: {message}")


class TeambitionClient:
    def __init__(self, app_id: str, app_secret: str, org_id: str,
                 project_id: str, api_delay: float = 0.5,
                 scenariofieldconfig_id: Optional[str] = None,
                 operator_id: Optional[str] = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.org_id = org_id
        self.project_id = project_id
        self.api_delay = api_delay
        self.scenariofieldconfig_id = scenariofieldconfig_id
        self.operator_id = operator_id
        self._user_token: Optional[str] = None
        self._app_token: Optional[str] = None
        self._app_token_mode: bool = False
        self._unique_id_prefix: Optional[str] = None
        self._member_index: Optional[Dict[str, str]] = None  # name → userId 缓存
        self._taskflow_status_map: Optional[Dict[str, str]] = None  # statusId → statusName 缓存
        self._http = requests.Session()

    # ── 认证 ──────────────────────────────────────────

    def _generate_app_token(self) -> str:
        now = int(time.time())
        payload = {
            "_appId": self.app_id,
            "iat": now,
            "exp": now + 3600,
        }
        return jwt.encode(payload, self.app_secret, algorithm="HS256")

    def _get_app_token(self) -> str:
        """获取缓存的 appToken（仅 OAuth 模式使用）"""
        if not self._app_token:
            self._app_token = self._generate_app_token()
        return self._app_token

    def _get_unique_id_prefix(self) -> str:
        """获取项目任务编号前缀（如 VLNS）"""
        if self._unique_id_prefix:
            return self._unique_id_prefix
        try:
            data = self._request("GET", "/project/info",
                                 params={"projectId": self.project_id})
            result = data.get("result", data)
            if isinstance(result, list) and result:
                result = result[0]
            self._unique_id_prefix = result.get("uniqueIdPrefix", "")
            if self._unique_id_prefix:
                logger.info("项目编号前缀: %s", self._unique_id_prefix)
        except Exception as e:
            logger.warning("获取项目编号前缀失败: %s", e)
        return self._unique_id_prefix or ""

    def build_task_display_id(self, unique_id) -> str:
        """拼接平台显示编号，如 VLNS-62819"""
        prefix = self._get_unique_id_prefix()
        return f"{prefix}-{unique_id}" if prefix else str(unique_id)

    def authenticate(self):
        """使用 appToken 认证"""
        self._app_token = self._generate_app_token()
        self._user_token = self._app_token
        self._app_token_mode = True
        if self.operator_id:
            logger.info("Teambition 使用 appToken + X-Operator-Id 模式")
        else:
            logger.info("Teambition 使用 appToken 直接访问")

    def _ensure_token(self):
        if self._app_token_mode:
            # appToken 是 JWT，有效期短，每次调用前重新生成
            self._app_token = self._generate_app_token()
            self._user_token = self._app_token
        elif not self._user_token:
            self.authenticate()

    def _get_headers(self) -> dict:
        self._ensure_token()
        h = {
            "Authorization": f"Bearer {self._user_token}",
            "X-Tenant-Id": self.org_id,
            "X-Tenant-Type": "organization",
            "Content-Type": "application/json",
        }
        if self.operator_id:
            # HTTP header 不允许非ASCII字符，仅UUID格式(24位hex)才设置
            oid = str(self.operator_id)
            if all(c in '0123456789abcdef' for c in oid.lower()) and len(oid) == 24:
                h["X-Operator-Id"] = oid
        return h

    # ── 通用请求 ──────────────────────────────────────

    def close(self):
        if self._http:
            self._http.close()

    def _request(self, method: str, path: str,
                 retry_on_401: bool = True, **kwargs) -> dict:
        self._ensure_token()
        url = f"{API_BASE}{path}"

        last_error = None
        for attempt in range(3):
            try:
                resp = self._http.request(
                    method, url, headers=self._get_headers(),
                    timeout=30, **kwargs,
                )
                if resp.status_code == 401 and retry_on_401:
                    if self._app_token_mode:
                        self._app_token = self._generate_app_token()
                        self._user_token = self._app_token
                    else:
                        self._user_token = None
                        self._ensure_token()
                    continue
                # 服务端 500/502/503 超时，自动重试
                if resp.status_code in (500, 502, 503) and attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning("TB 服务端 %d 错误，%d秒后重试 (%d/3)",
                                   resp.status_code, wait, attempt + 1)
                    time.sleep(wait)
                    continue
                try:
                    body = resp.json() if resp.text else {}
                except (ValueError, Exception):
                    logger.warning("TB 返回非JSON响应: %s", resp.text[:200])
                    body = {}
                code = body.get("code", resp.status_code)
                err_code = body.get("errorCode", "")
                err_msg = body.get("errorMessage", "")
                if resp.status_code >= 400 or (isinstance(code, int) and code >= 400):
                    raise TeambitionAPIError(
                        resp.status_code,
                        err_msg or resp.text[:500],
                        path,
                    )
                # Teambition 常返回 HTTP 200 + code:0 但 errorCode 非空表示业务错误
                if err_code and str(err_code) != "0":
                    raise TeambitionAPIError(
                        resp.status_code,
                        f"{err_msg} (errorCode={err_code})",
                        path,
                    )
                time.sleep(self.api_delay)
                return body
            except TeambitionAPIError:
                raise
            except requests.exceptions.ConnectionError as e:
                last_error = e
                if attempt == 2:
                    raise
                wait = 2 ** attempt
                logger.warning("请求失败，%d秒后重试: %s", wait, e)
                time.sleep(wait)
        raise TeambitionAPIError(0, "请求失败：多次重试后仍无法完成请求", path)

    # ── 任务操作 ──────────────────────────────────────

    def create_task(self, content: str, note: str = "",
                    executor_id: Optional[str] = None,
                    priority: int = 0,
                    tasklist_id: Optional[str] = None,
                    stage_id: Optional[str] = None,
                    customfields: Optional[list] = None,
                    involve_member_ids: Optional[list] = None) -> tuple:
        body = {
            "projectId": self.project_id,
            "content": content,
            "note": note,
            "priority": priority,
        }
        if executor_id:
            body["executorId"] = executor_id
        if tasklist_id:
            body["tasklistId"] = tasklist_id
        if stage_id:
            body["stageId"] = stage_id
        if self.scenariofieldconfig_id:
            body["scenariofieldconfigId"] = self.scenariofieldconfig_id
        if customfields:
            body["customfields"] = customfields
        if involve_member_ids:
            body["involveMembers"] = involve_member_ids

        data = self._request("POST", "/v3/task/create", json=body)
        result = data.get("result", data)
        task_id = result.get("taskId") or result.get("id", "")

        # 创建后查询 uniqueId 并拼接平台编号（如 VLNS-62819）
        task_display_id = ""
        if task_id:
            try:
                task = self.get_task(task_id)
                if task and task.taskIdentifier:
                    task_display_id = self.build_task_display_id(task.taskIdentifier)
            except Exception as e:
                logger.warning("获取任务编号失败: %s", e)

        logger.info("创建 Teambition 任务: %s (ID: %s, 编号: %s)",
                     content[:50], task_id, task_display_id or "未知")
        return task_id, task_display_id

    def update_task_note(self, task_id: str, note: str):
        self._request("PUT", f"/v3/task/{task_id}/note",
                      json={"note": note})

    def add_task_comment(self, task_id: str, content: str):
        """向 Teambition 任务追加评论"""
        self._request("POST", f"/v3/task/{task_id}/comment",
                      json={"content": content})
        logger.info("添加评论到 TB 任务 %s", task_id)

    def get_task(self, task_id: str) -> Optional[TeambitionTask]:
        data = self._request("GET", "/v3/task/query",
                             params={"taskId": task_id})
        result = data.get("result", [])
        if isinstance(result, list) and result:
            return self._parse_task(result[0])
        elif isinstance(result, dict) and result:
            return self._parse_task(result)
        return None

    def get_task_by_identifier(self, identifier: str) -> Optional[TeambitionTask]:
        """按任务显示 ID（如 VLNS-66259）精确查找任务"""
        pid = self.project_id
        # TB API 的 uniqueId 参数只接受纯数字，去掉前缀
        num = identifier.replace("VLNS-", "").replace("CPAX-", "")
        data = self._request(
            "GET", f"/v3/project/{pid}/task/query",
            params={"uniqueId": num, "pageSize": 5},
        )
        result = data.get("result", [])
        if isinstance(result, list):
            for item in result:
                task = self._parse_task(item)
                # TB 存储的是纯数字，需精确比对
                if task and task.taskIdentifier == num:
                    return task
        elif isinstance(result, dict) and result:
            task = self._parse_task(result)
            if task and task.taskIdentifier == num:
                return task
        return None

    def search_tasks(self, keyword: str,
                     project_id: Optional[str] = None) -> List[TeambitionTask]:
        pid = project_id or self.project_id
        tql = f'text ~ "{keyword}"'
        data = self._request(
            "GET", "/all-task/search",
            params={"tql": tql, "projectId": pid, "pageSize": 50},
        )
        task_ids = data.get("result", [])
        if not task_ids:
            return []

        tasks = []
        for tid in task_ids[:20]:
            try:
                task = self.get_task(tid)
                if task:
                    tasks.append(task)
            except Exception as e:
                logger.warning("查询任务 %s 失败: %s", tid, e)
        return tasks

    def query_project_tasks(self, project_id: Optional[str] = None,
                            page_size: int = 50,
                            page_token: str = "",
                            sfc_id: Optional[str] = None,
                            order_by: str = "created",
                            order: str = "desc") -> tuple:
        """查询项目任务列表，返回 (tasks, next_page_token)

        sfc_id: 传入 scenariofieldconfigId 只查询特定类型（如缺陷）
        order_by/order: 排序字段和方向，默认按创建时间降序（最新在前）
        """
        pid = project_id or self.project_id
        params: dict = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        if sfc_id:
            params["scenariofieldconfigId"] = sfc_id
        if order_by:
            params["orderBy"] = order_by
            params["order"] = order
        data = self._request(
            "GET", f"/v3/project/{pid}/task/query",
            params=params,
        )
        results = data.get("result", [])
        next_token = data.get("nextPageToken", "")
        tasks = [self._parse_task(t) for t in results if t]
        return tasks, next_token

    def get_defect_scenariofieldconfig_id(self) -> Optional[str]:
        if self.scenariofieldconfig_id:
            return self.scenariofieldconfig_id
        try:
            data = self._request(
                "GET",
                f"/v3/project/{self.project_id}/scenariofieldconfig/search",
            )
            configs = data.get("result", [])
            for cfg in configs:
                name = cfg.get("name", "")
                if "缺陷" in name:
                    self.scenariofieldconfig_id = cfg.get("id", "")
                    logger.info("自动检测到缺陷类型ID: %s",
                                self.scenariofieldconfig_id)
                    return self.scenariofieldconfig_id
        except Exception as e:
            logger.warning("自动检测缺陷类型失败: %s", e)
        return None

    # ── 文件上传 ──────────────────────────────────────

    def upload_attachment(self, task_id: str,
                          file: AttachmentFile) -> Optional[tuple]:
        """上传附件到 Teambition 任务。

        Returns:
            (work_id, download_url) 元组，失败返回 None。
            download_url 为签名 OSS 链接，用于内联图片显示。
        """
        try:
            size_mb = file.size / 1024 / 1024
            logger.info("开始上传附件: %s (%.1f MB)", file.filename, size_mb)

            # Step 1: 获取上传凭证
            data = self._request("POST", "/v3/awos/upload-token",
                                 json={"category": "attachment",
                                       "fileName": file.filename,
                                       "fileType": file.content_type,
                                       "fileSize": file.size,
                                       "scope": "task",
                                       "scopeId": task_id})
            result = data.get("result")
            if not isinstance(result, dict):
                logger.warning("upload-token 响应格式异常: %s", data)
                return None

            download_url = result.get("downloadUrl", "")
            sdk = result.get("sdk", {})
            credentials = sdk.get("credentials", {})
            upload_info = result.get("upload", {})
            file_token = result.get("token", "")

            bucket = upload_info.get("Bucket", "")
            object_key = upload_info.get("Key", "")

            if not (credentials and bucket and object_key):
                logger.warning("upload-token 缺少必要的上传参数")
                return None

            # Step 2: 上传文件到 Aliyun OSS（大文件设置超时）
            import oss2
            auth = oss2.StsAuth(
                credentials["accessKeyId"],
                credentials["secretAccessKey"],
                credentials["sessionToken"],
            )
            endpoint = "oss-cn-zhangjiakou.aliyuncs.com"
            # oss2 的 connect_timeout 参数实际控制整个 HTTP 请求超时（连接+传输）
            # 按文件大小计算超时（保守按 100KB/s），最少 120s，最多 900s
            transfer_timeout = max(120, min(900, int(size_mb * 10)))
            bucket_obj = oss2.Bucket(
                auth, f"https://{endpoint}", bucket,
                connect_timeout=transfer_timeout,
            )
            logger.info("OSS 上传中: %s (%.1f MB, 超时 %ds)...",
                        file.filename, size_mb, transfer_timeout)
            oss_retries = 3
            for oss_attempt in range(1, oss_retries + 1):
                try:
                    bucket_obj.put_object(object_key, file.data)
                    break
                except Exception as oss_e:
                    if oss_attempt < oss_retries:
                        wait = 2 ** oss_attempt
                        logger.warning("OSS 上传失败，%ds 后重试 (%d/%d): %s",
                                       wait, oss_attempt, oss_retries, oss_e)
                        time.sleep(wait)
                    else:
                        raise
            logger.debug("OSS 上传成功: %s/%s", bucket, object_key)

            # Step 3: 通过 /v3/work/create 创建文件记录
            work_data = self._request("POST", "/v3/work/create", json={
                "projectId": self.project_id,
                "taskId": task_id,
                "fileTokens": [file_token],
            })
            # 获取创建的文件 ID
            work_result = work_data.get("result", [])
            work_id = ""
            if isinstance(work_result, list) and work_result:
                work_id = work_result[0].get("id", "")
            elif isinstance(work_result, dict):
                work_id = work_result.get("id", "")

            logger.info("附件上传成功: %s → 任务 %s (workId=%s)",
                        file.filename, task_id, work_id)
            wid = work_id or object_key
            return (wid, download_url)
        except Exception as e:
            logger.warning("附件上传失败: %s - %s", file.filename, e)
            return None

    # ── 文件/Work 查询 ────────────────────────────────

    def list_task_works(self, task_id: str) -> List[dict]:
        """查询任务已上传的所有文件(works)列表。

        Returns:
            [{"id": work_id, "fileName": "xxx", ...}, ...]
        """
        try:
            data = self._request("GET", f"/v3/work/list",
                                 params={"projectId": self.project_id,
                                        "taskId": task_id, "limit": 200})
            result = data.get("result", [])
            if isinstance(result, list):
                return result
            return []
        except Exception as e:
            logger.warning("查询任务 works 列表失败: %s - %s", task_id, e)
            return []

    def get_work_info(self, work_id: str) -> Optional[dict]:
        """根据 work_id 查询文件记录（元数据：fileName, fileSize 等）。

        Teambition 富文本中内联图片的 src 直接使用 work_id（UUID），
        前端识别 work_id 并自动渲染图片，无需拼接下载 URL。
        """
        if not work_id:
            return None
        try:
            data = self._request("GET", "/v3/work/query",
                                 params={"workIds": work_id})
            result = data.get("result", [])
            if isinstance(result, list) and result:
                return result[0]
            return None
        except Exception as e:
            logger.warning("查询 work 失败: %s - %s", work_id, e)
            return None

    # ── 名称搜索解析 ──────────────────────────────────

    def search_project(self, name: str) -> Optional[str]:
        """根据项目名称搜索项目 ID，返回匹配的 project_id"""
        if not name:
            return None
        try:
            # 尝试多个 API 路径（不同权限模式可用路径不同）
            candidates = [
                ("POST", "/v3/project/search",
                 {"pageSize": 50, "keyword": name}),
                ("POST", "/v3/org/project/list",
                 {"orgId": self.org_id, "pageSize": 50}),
            ]
            for method, path, body in candidates:
                try:
                    if method == "POST":
                        data = self._request("POST", path, json=body)
                    else:
                        data = self._request("GET", path, params=body)
                    result = data.get("result", {})
                    projects = []
                    if isinstance(result, list):
                        projects = result
                    elif isinstance(result, dict):
                        projects = result.get("projects",
                                              result.get("list", []))
                    for p in projects:
                        pname = p.get("name", "")
                        pid = p.get("id") or p.get("projectId", "")
                        if name == pname or name in pname:
                            logger.info("项目搜索匹配: '%s' → %s", name, pid)
                            return pid
                except Exception:
                    continue
        except Exception as e:
            logger.warning("搜索项目失败: %s - %s", name, e)
        return None

    def search_scenariofieldconfig(self, name: str) -> Optional[str]:
        """根据场景类型名称（如'缺陷'）搜索 scenariofieldconfig_id"""
        if not name:
            return None
        try:
            data = self._request(
                "GET",
                f"/v3/project/{self.project_id}/scenariofieldconfig/search",
            )
            configs = data.get("result", [])
            for cfg in configs:
                cfg_name = cfg.get("name", "")
                if name in cfg_name:
                    cid = cfg.get("id", "")
                    logger.info("场景类型搜索匹配: '%s' → %s", name, cid)
                    return cid
        except Exception as e:
            logger.warning("搜索场景类型失败: %s - %s", name, e)
        return None

    def search_customfield(self, name: str) -> Optional[str]:
        """根据自定义字段名称搜索 customfield_id"""
        if not name:
            return None
        try:
            all_fields = []
            page_token = ""
            while True:
                params = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                data = self._request(
                    "GET",
                    f"/v3/project/{self.project_id}/customfield/search",
                    params=params,
                )
                fields = data.get("result", [])
                all_fields.extend(fields)
                page_token = data.get("nextPageToken", "")
                if not page_token or not fields:
                    break
            for f in all_fields:
                fname = f.get("name", "")
                fid = f.get("id", "")
                if name in fname:
                    logger.info("自定义字段搜索匹配: '%s' → %s", name, fid)
                    return fid
        except Exception as e:
            logger.warning("搜索自定义字段失败: %s - %s", name, e)
        return None

    def preload_members(self) -> int:
        """一次性加载组织全部成员到内存索引，后续 search_member 直接走 O(1) 查询。

        多次调用幂等。返回索引中收录的可搜索名称数量。
        """
        if self._member_index is not None:
            return len(self._member_index)
        index: Dict[str, str] = {}
        member_count = 0
        try:
            page_token = ""
            while True:
                params = {"orgId": self.org_id, "pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                data = self._request("GET", "/org/member/list", params=params)
                members = data.get("result", data.get("data", []))
                if isinstance(members, dict):
                    members = members.get("list", members.get("data", []))
                if not isinstance(members, list):
                    members = [members] if members else []
                for m in members:
                    uid = m.get("userId") or m.get("memberId") or m.get("id")
                    if not uid:
                        continue
                    member_count += 1
                    # 同一用户的多种可搜索名都映射到同一 userId
                    candidates = [
                        m.get("account"), m.get("realName"),
                        m.get("name"), m.get("nickname"),
                    ]
                    email = m.get("email", "")
                    if "@" in email:
                        candidates.append(email.split("@")[0])
                    for key in candidates:
                        if key:
                            index.setdefault(str(key).strip(), uid)
                page_token = data.get("nextPageToken", "")
                if not page_token or not members:
                    break
        except Exception as e:
            logger.warning("预加载 Teambition 成员失败: %s", e)
        self._member_index = index
        logger.info("已加载 %d 个 Teambition 成员（索引 %d 项）",
                    member_count, len(index))
        return len(index)

    def search_member(self, name: str) -> Optional[str]:
        """根据用户名查找 Teambition 用户 ID（命中即返回，未命中返回 None）

        首次调用会一次性拉取全部成员构建索引（自动通过 preload_members）。
        """
        if not name:
            return None
        try:
            self.preload_members()
            uid = (self._member_index or {}).get(str(name).strip())
            if uid:
                logger.debug("用户映射命中: %s → %s", name, uid)
                return uid
        except Exception as e:
            logger.warning("查找 Teambition 用户失败: %s - %s", name, e)
        return None

    @staticmethod
    def _parse_task(data: dict) -> TeambitionTask:
        return TeambitionTask(
            id=data.get("id") or "",
            taskId=data.get("taskId") or data.get("id") or "",
            taskIdentifier=str(data.get("uniqueId") or ""),
            content=data.get("content") or "",
            note=data.get("note") or "",
            priority=int(data.get("priority") or 0),
            executorId=data.get("executorId") or "",
            tagIds=data.get("tagIds") or [],
            customfields=data.get("customfields") or [],
            status=(data.get("taskflowstatusId")
                    or data.get("tfsId") or ""),
            created=data.get("created") or "",
            updated=data.get("updated") or "",
            sfcId=data.get("sfcId")
                     or data.get("scenariofieldconfigId") or "",
            taskflowId=data.get("taskflowId") or "",
            isArchived=bool(data.get("isArchived")),
        )

    # ── 任务流状态 ─────────────────────────────────────

    def get_taskflow_status_map(self) -> Dict[str, str]:
        """获取项目任务流状态映射 {statusId: statusName}，带缓存"""
        if self._taskflow_status_map is not None:
            return self._taskflow_status_map
        status_map: Dict[str, str] = {}
        try:
            endpoints = [
                f"/v3/project/{self.project_id}/taskflowstatus/search",
                f"/v3/project/{self.project_id}/taskflow",
            ]
            for endpoint in endpoints:
                try:
                    data = self._request("GET", endpoint)
                    result = data.get("result", [])
                    if isinstance(result, dict):
                        result = result.get("statuses",
                                            result.get("list", []))
                    if not isinstance(result, list) or not result:
                        continue
                    for s in result:
                        sid = s.get("id", "")
                        sname = s.get("name", "")
                        if sid and sname:
                            status_map[sid] = sname
                    if status_map:
                        break
                except Exception:
                    continue
            self._taskflow_status_map = status_map
            if status_map:
                logger.info("加载 %d 个任务流状态: %s",
                            len(status_map),
                            ", ".join(status_map.values()))
            else:
                logger.warning("未能获取任务流状态，状态同步功能不可用")
        except Exception as e:
            logger.warning("获取任务流状态失败: %s", e)
            self._taskflow_status_map = {}
        return self._taskflow_status_map or {}

    def get_taskflow_statuses(self, taskflow_id: str) -> Dict[str, str]:
        """获取指定任务流的状态列表 {statusId: statusName}"""
        status_map: Dict[str, str] = {}
        try:
            data = self._request("GET",
                                 f"/v3/taskflow/{taskflow_id}/status/search")
            result = data.get("result", [])
            if isinstance(result, list):
                for s in result:
                    sid = s.get("id", "")
                    sname = s.get("name", "")
                    if sid and sname:
                        status_map[sid] = sname
        except Exception as e:
            logger.debug("查询 taskflow %s 状态失败: %s", taskflow_id, e)
        return status_map

    def update_task_status(self, task_id: str, taskflowstatus_id: str):
        """更新任务工作流状态

        使用 Teambition v3 端点：
        PUT /v3/task/{taskId}/taskflowstatus
        """
        endpoint = f"/v3/task/{task_id}/taskflowstatus"
        self._request("PUT", endpoint,
                      json={"taskflowstatusId": taskflowstatus_id})
        logger.info("任务 %s 状态已更新 → %s", task_id, taskflowstatus_id[:8])

    def update_task_executor(self, task_id: str, executor_id: str):
        """更新任务执行人

        使用 Teambition v3 端点：
        PUT /v3/task/{taskId}/executor
        """
        self._request("PUT", f"/v3/task/{task_id}/executor",
                      json={"executorId": executor_id})
        logger.info("任务 %s 执行人已更新 → %s", task_id, executor_id[:8])
