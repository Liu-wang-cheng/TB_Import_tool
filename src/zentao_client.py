"""禅道 API 客户端"""

import json
import logging
import re
import threading
import time
from typing import List, Optional, Tuple

import requests

from src.models import AttachmentFile, ZentaoBug

logger = logging.getLogger(__name__)


class ZentaoAPIError(Exception):
    def __init__(self, status_code: int, message: str, path: str):
        self.status_code = status_code
        self.message = message
        self.path = path
        super().__init__(f"禅道API错误 [{status_code}] {path}: {message}")


class ZentaoClient:
    def __init__(self, base_url: str, account: str, password: str,
                 api_delay: float = 0.5):
        self.base_url = base_url.rstrip("/")
        self.account = account
        self.password = password
        self.api_delay = api_delay
        self._token: Optional[str] = None
        self._session_id: Optional[str] = None
        self._session_logged_in = False
        self._http = requests.Session()
        # 同一个 bug_id 在一次同步过程中多处需要详情（VLNS 检查、详细同步、评论），
        # 这里缓存原始 raw 数据避免重复 GET /api.php/v1/bugs/{id}。
        self._bug_raw_cache: dict = {}
        self._bug_raw_cache_lock = threading.Lock()
        # 模块列表按产品ID缓存，避免 resolve_module_ids_by_name / resolve_module_name
        # 在同一会话内重复 GET /api.php/v1/products/{id}/modules
        self._product_modules_cache: dict = {}
        self._product_modules_cache_lock = threading.Lock()

    # ── 认证 ──────────────────────────────────────────

    def close(self):
        if self._http:
            self._http.close()

    def authenticate(self):
        """认证并获取 token（公共接口）"""
        self._ensure_token()

    def _ensure_token(self):
        if self._token:
            return
        url = f"{self.base_url}/api.php/v1/tokens"
        resp = self._http.post(url, json={
            "account": self.account,
            "password": self.password,
        })
        if resp.status_code not in (200, 201):
            raise ZentaoAPIError(resp.status_code, resp.text, "/tokens")
        data = resp.json()
        self._token = data.get("token")
        if not self._token:
            raise ZentaoAPIError(resp.status_code, "未获取到token", "/tokens")
        logger.info("禅道 REST API 认证成功")

    def _ensure_session(self):
        if self._session_logged_in:
            return
        # 获取 session ID
        url = f"{self.base_url}/api-getsessionid.json"
        resp = self._http.get(url)
        if resp.status_code != 200:
            raise ZentaoAPIError(resp.status_code, "获取session失败", "/api-getsessionid")
        data = resp.json()
        # data["data"] 可能是 JSON 字符串或 dict
        inner = data.get("data", {})
        if isinstance(inner, str):
            import json as _json
            inner = _json.loads(inner)
        self._session_id = (
            inner.get("sessionID")
            or data.get("sessionID")
            or data.get("sessionid")
        )
        if not self._session_id:
            raise ZentaoAPIError(resp.status_code, f"未解析到sessionID: {resp.text[:200]}", "/api-getsessionid")

        # 用 session 登录（POST 避免密码暴露在 URL 参数中）
        url = f"{self.base_url}/user-login.json"
        resp = self._http.post(url, data={
            "account": self.account,
            "password": self.password,
            "zentaosid": self._session_id,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            raise ZentaoAPIError(resp.status_code, "session登录失败", "/user-login")
        self._session_logged_in = True
        logger.info("禅道 Session 认证成功（用于文件下载）")

    # ── 通用请求 ──────────────────────────────────────

    def _request(self, method: str, path: str,
                 retry_on_401: bool = True, **kwargs) -> dict:
        self._ensure_token()
        url = f"{self.base_url}{path}"
        headers = {"Token": self._token}

        for attempt in range(3):
            try:
                resp = self._http.request(method, url, headers=headers,
                                          timeout=30, **kwargs)
                if resp.status_code == 401 and retry_on_401:
                    self._token = None
                    self._ensure_token()
                    headers["Token"] = self._token
                    continue
                if resp.status_code >= 400:
                    raise ZentaoAPIError(resp.status_code, resp.text[:500], path)
                time.sleep(self.api_delay)
                try:
                    return resp.json()
                except (ValueError, json.JSONDecodeError):
                    logger.warning("禅道返回非JSON响应: %s", resp.text[:200])
                    return {}
            except ZentaoAPIError:
                raise
            except requests.exceptions.ConnectionError as e:
                if attempt == 2:
                    raise
                wait = 2 ** attempt
                logger.warning("请求失败，%d秒后重试: %s", wait, e)
                time.sleep(wait)
        return {}

    # ── Bug 操作 ──────────────────────────────────────

    def fetch_bugs(self, product_id: Optional[int] = None,
                   project_id: Optional[int] = None,
                   statuses: Optional[List[str]] = None,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None,
                   assigned_to: Optional[List[str]] = None,
                   page: int = 1, limit: int = 50) -> Tuple[List[ZentaoBug], int]:
        if product_id:
            path = f"/api.php/v1/products/{product_id}/bugs"
        elif project_id:
            path = f"/api.php/v1/projects/{project_id}/bugs"
        else:
            raise ValueError("必须指定 product_id 或 project_id")

        params = {"page": page, "limit": limit}
        data = self._request("GET", path, params=params)

        bugs_data = data.get("bugs", [])
        total = data.get("total", 0)

        # 解析 assigned_to: 统一为列表，"me" 替换为当前账号
        resolved_assignees = set()
        if assigned_to:
            if isinstance(assigned_to, str):
                assigned_to = [assigned_to]
            for a in assigned_to:
                if str(a).lower() == "me":
                    resolved_assignees.add(self.account)
                else:
                    resolved_assignees.add(a)

        bugs = []
        skipped_assignee = 0
        for b in bugs_data:
            bug = self._parse_bug(b)
            # 客户端筛选（注意：批量API不返回 moduleName，module_filter 在获取详情后处理）
            if statuses and bug.status not in statuses:
                continue
            if date_from and bug.openedDate[:10] < date_from:
                continue
            if date_to and bug.openedDate[:10] > date_to:
                continue
            if resolved_assignees and bug.assignedToAccount not in resolved_assignees \
                    and bug.assignedTo not in resolved_assignees:
                skipped_assignee += 1
                logger.debug("[过滤-指派人不匹配] Bug#%d assignedTo='%s' account='%s' 期望∈%s",
                             bug.id, bug.assignedTo, bug.assignedToAccount, resolved_assignees)
                continue
            bugs.append(bug)

        if resolved_assignees and skipped_assignee:
            logger.info("第 %d 页：%d/%d 条因指派人不匹配被过滤",
                        page, skipped_assignee, len(bugs_data))

        return bugs, total

    def _get_bug_raw(self, bug_id: int, retry_on_401: bool = True) -> Optional[dict]:
        """获取 Bug 的原始 JSON 数据并缓存，避免一次同步内重复 GET。"""
        with self._bug_raw_cache_lock:
            cached = self._bug_raw_cache.get(bug_id)
            if cached is not None:
                return cached
        path = f"/api.php/v1/bugs/{bug_id}"
        data = self._request("GET", path, retry_on_401=retry_on_401)
        bug_data = data.get("bug", data)
        with self._bug_raw_cache_lock:
            self._bug_raw_cache[bug_id] = bug_data
        return bug_data

    def fetch_bug_detail(self, bug_id: int) -> ZentaoBug:
        bug_data = self._get_bug_raw(bug_id)
        return self._parse_bug(bug_data or {})

    def check_bug_has_vlns(self, bug_id: int) -> bool:
        """检查 Bug 的备注/历史记录中是否包含 VLNS 文本"""
        try:
            bug_data = self._get_bug_raw(bug_id, retry_on_401=False)
            if not bug_data:
                return False
            actions = bug_data.get("actions", [])
            if not isinstance(actions, list):
                return False
            return bool(re.search(r'VLNS-\d+', str(actions)))
        except Exception as e:
            logger.debug("检查 Bug#%d 历史记录失败: %s", bug_id, e)
        return False

    def fetch_bug_comments(self, bug_id: int) -> List[dict]:
        """获取 Bug 的备注/评论列表

        禅道 actions 中 action 为 "commented" 的记录即为评论/备注。
        返回列表，每项包含 actor, date, comment 等字段。
        """
        try:
            bug_data = self._get_bug_raw(bug_id, retry_on_401=False)
            if not bug_data:
                return []
            actions = bug_data.get("actions", [])
            if not isinstance(actions, list):
                return []
            comments = []
            for act in actions:
                if not isinstance(act, dict):
                    continue
                action_type = act.get("action", "")
                comment_text = act.get("comment", "") or ""
                # 禅道中 "commented" 是纯评论，其他 action 也可能附带 comment
                if action_type == "commented" or comment_text.strip():
                    comments.append({
                        "actor": act.get("actor", ""),
                        "date": act.get("date", ""),
                        "action": action_type,
                        "comment": comment_text.strip(),
                    })
            return comments
        except Exception as e:
            logger.warning("获取 Bug#%d 评论失败: %s", bug_id, e)
            return []

    def update_bug_title(self, bug_id: int, new_title: str):
        path = f"/api.php/v1/bugs/{bug_id}"
        self._request("PUT", path, json={"title": new_title})
        logger.info("禅道 Bug#%d 标题已更新: %s", bug_id, new_title)

    def fetch_product_modules(self, product_id: int) -> List[dict]:
        """获取产品的完整模块列表（含子模块；按产品ID缓存）。

        部分禅道版本只返回根模块，不返回子模块。本函数策略:
          1) 顺序尝试多种端点拿到初始模块列表
          2) 探测一次：用首个根模块ID再请求 API
             - 若返回与原始相同的根集合 → API 不支持模块ID查询，跳过 BFS
             - 否则 → BFS 递归拉取所有子模块
        """
        with self._product_modules_cache_lock:
            cached = self._product_modules_cache.get(product_id)
            if cached is not None:
                return cached

        modules = self._fetch_modules_endpoint(product_id, "bug")
        if not modules:
            logger.warning("产品 %s 模块列表为空", product_id)
            with self._product_modules_cache_lock:
                self._product_modules_cache[product_id] = []
            return []

        initial_ids = {str(m.get("id")) for m in modules if m.get("id")}
        seen_ids = set(initial_ids)
        expanded = list(modules)

        # 探测：API 是否支持按 module_id 查询子树（避免无谓的 N 次 BFS）
        probe_mid = next(iter(initial_ids), None)
        if probe_mid:
            probe_children = self._fetch_modules_endpoint(int(probe_mid), "bug")
            probe_ch_ids = {
                str(c.get("id")) for c in probe_children if c.get("id")
            }
            if not probe_children or probe_ch_ids.issubset(initial_ids):
                logger.debug("模块API不支持按模块ID查询子树，跳过BFS")
                with self._product_modules_cache_lock:
                    self._product_modules_cache[product_id] = expanded
                return expanded

            # 探测成功：处理首次响应里的新模块，并继续 BFS 其他根
            queue = [m for m in initial_ids if m != probe_mid]
            self._merge_module_children(probe_children, seen_ids,
                                         expanded, queue)
            api_calls = 1
            max_calls = 50
            while queue and api_calls < max_calls:
                mid = queue.pop()
                children = self._fetch_modules_endpoint(int(mid), "bug")
                api_calls += 1
                if not children:
                    continue
                self._merge_module_children(children, seen_ids,
                                             expanded, queue)

            if len(expanded) > len(initial_ids):
                logger.info(
                    "模块树扩展: %d 个根模块 → %d 个总模块（%d 次API调用）",
                    len(initial_ids), len(expanded), api_calls)

        with self._product_modules_cache_lock:
            self._product_modules_cache[product_id] = expanded
        return expanded

    @staticmethod
    def _merge_module_children(children: List[dict], seen_ids: set,
                               expanded: list, queue: list):
        """将 children 中的新模块合并到 expanded/seen/queue（仅接受 parent 在 seen 内的）。"""
        for c in children:
            cid = str(c.get("id"))
            if not cid or cid in seen_ids:
                continue
            cparent = str(
                c.get("parent")
                or c.get("parentID")
                or c.get("pid")
                or "0"
            )
            if cparent in seen_ids:
                seen_ids.add(cid)
                expanded.append(c)
                queue.append(cid)

    def _fetch_modules_endpoint(self, id_param: int,
                                type_: str = "bug") -> List[dict]:
        """单次模块 API 调用，依次尝试多种端点格式。"""
        attempts = [
            ("/api.php/v1/modules",
             {"id": id_param, "type": type_, "limit": 200}),
            (f"/api.php/v1/products/{id_param}/modules", {"type": type_}),
            (f"/api.php/v1/products/{id_param}/modules", {}),
        ]
        for path, params in attempts:
            try:
                data = self._request("GET", path, params=params)
                cand = data.get("modules") or data.get("tree") or []
                if not cand and isinstance(data, list):
                    cand = data
                if cand and isinstance(cand, list):
                    return cand
            except Exception as e:
                logger.debug("模块API %s %s 失败: %s", path, params, e)
        return []

    def resolve_module_ids_by_name(self, product_id: int,
                                    name: str) -> Optional[set]:
        """根据模块名称（子串匹配）解析为模块 ID 集合。

        利用模块树构建完整路径（如 "HS341/子模块"），与当前
        ``module_filter in full_bug.moduleName`` 行为一致。
        - 返回 set（含空 set）：API 调用成功且模块树完整，使用此集合做过滤
          （空集合 = 没有模块匹配名称 → 过滤后 0 条）
        - 返回 None：API 不可用、或模块树明显不完整（如只拿到根模块），
          调用方应回退到逐条取详情比对 moduleName
        """
        try:
            modules = self.fetch_product_modules(product_id)
            if not modules:
                logger.warning("产品 %s 模块列表为空，无法预解析名称 '%s'",
                               product_id, name)
                return None

            id_map = {}   # id → name
            parent_map = {}  # id → parent_id
            for m in modules:
                mid = m.get("id")
                if mid is None:
                    continue
                mid = str(mid)
                id_map[mid] = m.get("name", "")
                # 兼容多种父节点字段名
                parent_map[mid] = str(
                    m.get("parent")
                    or m.get("parentID")
                    or m.get("pid")
                    or "0"
                )

            def _full_path(mid):
                parts = []
                cur = mid
                seen = set()
                while cur and cur != "0" and cur not in seen:
                    seen.add(cur)
                    parts.append(id_map.get(cur, ""))
                    cur = parent_map.get(cur, "0")
                return "/".join(reversed(parts))

            matched = set()
            for mid in id_map:
                fp = _full_path(mid)
                # 同时匹配完整路径和单独的模块名，覆盖 parent 字段缺失等异常
                if name in fp or name in id_map.get(mid, ""):
                    matched.add(mid)

            # 完整性检测：若模块列表中没有任何父子关系（所有 parent 都是 "0"
            # 且不在 id_map 中），说明禅道API没返回子模块、BFS 也未展开成功。
            # 此时按 ID 集合过滤会漏掉所有子模块下的 Bug，应回退到 slow-path。
            has_hierarchy = any(
                pm in id_map for pm in parent_map.values()
            )
            if matched and not has_hierarchy:
                logger.warning(
                    "模块API仅返回 %d 个根级模块、无父子层级，匹配 '%s' 的 "
                    "%d 个根模块下的子模块可能漏掉，回退到逐条比对 moduleName",
                    len(modules), name, len(matched))
                return None

            if matched:
                logger.info("模块名称 '%s' 解析为 %d 个ID: %s",
                            name, len(matched),
                            ",".join(sorted(matched))[:200])
            else:
                logger.warning("模块名称 '%s' 在 %d 个模块中没有匹配，"
                               "将返回 0 条 Bug",
                               name, len(modules))
            return matched
        except Exception as e:
            logger.warning("模块名称解析失败(将回退到逐条取详情): %s", e)
            return None

    def resolve_module_name(self, product_id: int, module_id: int) -> str:
        """通过模块API查找模块ID对应的模块名称首段

        返回模块路径的首段（如 "HS341"），找不到返回空字符串。
        """
        try:
            modules = self.fetch_product_modules(product_id)
            if not modules:
                return ""
            id_map = {}
            parent_map = {}
            for m in modules:
                mid = str(m.get("id", ""))
                if not mid:
                    continue
                id_map[mid] = m.get("name", "")
                parent_map[mid] = str(m.get("parent") or "0")
            # 构建完整路径取首段
            parts = []
            cur = str(module_id)
            seen = set()
            while cur and cur != "0" and cur not in seen:
                seen.add(cur)
                parts.append(id_map.get(cur, ""))
                cur = parent_map.get(cur, "0")
            if parts:
                first = parts[-1]  # 最顶层
                logger.info("模块ID %s 解析为: %s", module_id, first)
                return first
        except Exception as e:
            logger.warning("解析模块ID %s 失败: %s", module_id, e)
        return ""

    def fetch_all_bugs(self, product_id=None, project_id=None,
                       statuses=None, date_from=None, date_to=None,
                       assigned_to=None, page_size: int = 200) -> List[ZentaoBug]:
        """分页获取所有 Bug。

        page_size 默认 200（禅道通常上限），单次拉取更多 Bug 可减少
        分页次数与 api_delay 开销。比如 1000 条 Bug：
          - page_size=50  → 20 次请求 × 0.5s = 10s 延迟
          - page_size=200 →  5 次请求 × 0.5s = 2.5s 延迟
        """
        all_bugs = []
        page = 1
        raw_total = 0
        t0 = time.time()
        while True:
            bugs, total = self.fetch_bugs(
                product_id=product_id, project_id=project_id,
                statuses=statuses, date_from=date_from, date_to=date_to,
                assigned_to=assigned_to,
                page=page, limit=page_size,
            )
            all_bugs.extend(bugs)
            raw_total = total if total is not None else raw_total
            # 终止条件：API 返回的总数已被本次循环覆盖（按页码计算，避免被
            # 客户端筛选后的空页误判为没有更多数据）
            # 兜底：如果当前页返回0条且raw_total>0，也终止（避免无限循环）
            if raw_total <= 0 or page * page_size >= raw_total:
                break
            if not bugs:
                break
            page += 1
        logger.info(
            "从禅道获取到 %d 条Bug（API共 %d 条，筛选后 %d 条，耗时 %.1fs，%d 页 × %d）",
            len(all_bugs), raw_total, len(all_bugs),
            time.time() - t0, page, page_size)
        return all_bugs

    def search_product(self, name: str) -> Optional[int]:
        """根据产品名称搜索产品 ID"""
        if not name:
            return None
        try:
            data = self._request("GET", "/api.php/v1/products",
                                 params={"limit": 100})
            products = data.get("products", [])
            for p in products:
                if name == p.get("name", "") or name in p.get("name", ""):
                    pid = int(p.get("id", 0))
                    if pid:
                        logger.info("产品搜索匹配: '%s' → %d", name, pid)
                        return pid
        except Exception as e:
            logger.warning("搜索产品失败: %s - %s", name, e)
        return None

    def search_project(self, name: str) -> Optional[int]:
        """根据项目名称搜索项目 ID"""
        if not name:
            return None
        try:
            data = self._request("GET", "/api.php/v1/projects",
                                 params={"limit": 100})
            projects = data.get("projects", [])
            for p in projects:
                if name == p.get("name", "") or name in p.get("name", ""):
                    pid = int(p.get("id", 0))
                    if pid:
                        logger.info("项目搜索匹配: '%s' → %d", name, pid)
                        return pid
        except Exception as e:
            logger.warning("搜索项目失败: %s - %s", name, e)
        return None

    # ── 文件下载 ──────────────────────────────────────

    def download_attachment(self, file_id: int,
                            filename: str = "") -> AttachmentFile:
        self._ensure_session()
        url = f"{self.base_url}/file-download-{file_id}.html"
        resp = self._http.get(url, params={"zentaosid": self._session_id},
                              timeout=60)
        if resp.status_code != 200:
            raise ZentaoAPIError(resp.status_code, "下载附件失败",
                                 f"/file-download-{file_id}")

        content_type = resp.headers.get("Content-Type",
                                        "application/octet-stream")
        if not filename:
            cd = resp.headers.get("Content-Disposition", "")
            match = re.search(r'filename="?([^";\n]+)"?', cd)
            filename = match.group(1) if match else f"attachment_{file_id}"

        return AttachmentFile(
            filename=filename,
            content_type=content_type,
            data=resp.content,
            size=len(resp.content),
        )

    def download_image(self, file_id: int) -> AttachmentFile:
        self._ensure_session()
        url = f"{self.base_url}/file-read-{file_id}.html"
        resp = self._http.get(url, params={"zentaosid": self._session_id},
                              timeout=60)
        if resp.status_code != 200:
            raise ZentaoAPIError(resp.status_code, "下载图片失败",
                                 f"/file-read-{file_id}")
        return AttachmentFile(
            filename=f"image_{file_id}.png",
            content_type=resp.headers.get("Content-Type", "image/png"),
            data=resp.content,
            size=len(resp.content),
        )

    # ── 内部解析 ──────────────────────────────────────

    @staticmethod
    def _parse_bug(data: dict) -> ZentaoBug:
        if not isinstance(data, dict):
            data = {}
        status = data.get("status", "")
        if isinstance(status, dict):
            status = status.get("code", "")
        assigned_raw = data.get("assignedTo", "")
        assigned_account = ""
        assigned_realname = ""
        if isinstance(assigned_raw, dict):
            assigned_account = assigned_raw.get("account", "")
            assigned_realname = assigned_raw.get("realname", "")
        elif isinstance(assigned_raw, str):
            assigned_account = assigned_raw
        assigned = assigned_realname or assigned_account

        opened_raw = data.get("openedBy", "")
        opened_account = ""
        opened_realname = ""
        if isinstance(opened_raw, dict):
            opened_account = opened_raw.get("account", "")
            opened_realname = opened_raw.get("realname", "")
        elif isinstance(opened_raw, str):
            opened_account = opened_raw
        opened_by = opened_realname or opened_account

        # 解析版本信息（openedBuild 是 [{id, title}] 列表）
        build_info = ""
        opened_build = data.get("openedBuild", [])
        if isinstance(opened_build, list) and opened_build:
            build_info = opened_build[0].get("title", "")
        elif isinstance(opened_build, dict):
            build_info = opened_build.get("title", "")

        # 提取 SN 编码
        sn_code = ZentaoClient._extract_sn(
            data.get("steps", "") + " " + data.get("title", "")
        )

        return ZentaoBug(
            id=int(data.get("id") or 0),
            title=data.get("title", ""),
            severity=str(data.get("severity", "")),
            pri=str(data.get("pri", "")),
            type=data.get("type", ""),
            status=status,
            steps=data.get("steps", ""),
            assignedTo=assigned,
            assignedToAccount=assigned_account,
            openedBy=opened_by,
            openedByAccount=opened_account,
            openedDate=data.get("openedDate", ""),
            product=str(data.get("product", "")),
            productName=data.get("productName", ""),
            project=str(data.get("project", "")),
            projectName=data.get("projectName", ""),
            module=str(data.get("module", "")),
            moduleName=data.get("moduleTitle", "") or data.get("moduleName", ""),
            openedBuild=build_info,
            snCode=sn_code,
            files=ZentaoClient._normalize_files(data.get("files", [])),
        )

    @staticmethod
    def _extract_sn(text: str) -> str:
        """从文本中提取 SN 编码，如 SN：0004、SN:12345

        匹配格式：SN 后跟中文/英文冒号和可选空格，然后是字母数字组合。
        找不到时返回 '/'。
        """
        if not text:
            return "/"
        # 匹配 SN/SN码/设备SN 后跟冒号或空格
        match = re.search(r'(?:设备)?SN(?:码)?\s*[:：]\s*([A-Za-z0-9\-_]+)',
                          text, re.IGNORECASE)
        if match:
            return match.group(1)
        return "/"

    @staticmethod
    def _normalize_files(files):
        """v1 API 返回 dict 格式 {id: file_obj}，统一转为 list"""
        if isinstance(files, dict):
            return list(files.values())
        return files if isinstance(files, list) else []
