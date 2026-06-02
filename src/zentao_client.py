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
        self._cloud_session_auth = False  # 禅道云版：用 session 代替 token 认证
        self._branch_id = 0  # 分支ID，云版 URL 解析时设置，自建版忽略
        self._http = requests.Session()
        # 同一个 bug_id 在一次同步过程中多处需要详情（VLNS 检查、详细同步、评论），
        # 这里缓存原始 raw 数据避免重复 GET /api.php/v1/bugs/{id}。
        self._bug_raw_cache: dict = {}
        self._bug_raw_cache_lock = threading.Lock()
        # 模块列表按产品ID缓存，避免 resolve_module_ids_by_name / resolve_module_name
        # 在同一会话内重复 GET /api.php/v1/products/{id}/modules
        self._product_modules_cache: dict = {}
        self._product_modules_cache_lock = threading.Lock()
        # 云端浏览页数据缓存（含 modules），避免 get_bug_raw 已拉过一套数据又拉一次
        self._cloud_browse_cache: dict = {}
        self._cloud_browse_cache_lock = threading.Lock()
        # 云版用户名映射：中文名 → 英文账号（从 browse JSON users 字段构建）
        self._cloud_user_name_to_account: dict = {}
        self._cloud_user_cache_lock = threading.Lock()

    # ── 认证 ──────────────────────────────────────────

    def close(self):
        if self._http:
            self._http.close()

    def set_branch_id(self, branch_id: int):
        """设置分支ID（云版 URL 解析时使用）"""
        self._branch_id = branch_id

    def authenticate(self):
        """认证并获取 token（公共接口）

        云版不支持 Token 认证时自动切换到 Session 认证并验证登录结果。
        """
        self._ensure_token()
        # 云版在 _ensure_token 中已标记 _cloud_session_auth，
        # 此处需额外调用 _ensure_session 验证账号密码是否正确，
        # 避免 credential 错误时静默返回"认证成功"
        if self._cloud_session_auth:
            self._ensure_session()

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
            # 禅道云版返回 {"errcode":401,"errmsg":"缺少code参数"}，不支持 token 认证
            if "errcode" in data:
                logger.info("检测到禅道云版，将使用 Session 认证代替 Token")
                self._cloud_session_auth = True
                return
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
        # 云版登录失败时可能仍返回 HTTP 200，需检查响应体中的实际登录结果
        login_data = resp.json()
        if isinstance(login_data, dict):
            result = login_data.get("result")
            if result and str(result).lower() in ("fail", "failed"):
                reason = login_data.get("message", login_data.get("reason", str(login_data)))
                raise ZentaoAPIError(resp.status_code, f"登录失败: {reason}", "/user-login")
            # 某些版本用 status 字段
            status = login_data.get("status")
            if status and str(status).lower() in ("fail", "failed"):
                reason = login_data.get("message", login_data.get("reason", str(login_data)))
                raise ZentaoAPIError(resp.status_code, f"登录失败: {reason}", "/user-login")
            # 云版 errcode（如 {"errcode":401,"errmsg":"密码错误"}）
            if "errcode" in login_data and login_data["errcode"] != 0:
                errmsg = login_data.get("errmsg", "未知错误")
                raise ZentaoAPIError(resp.status_code, f"登录失败: [{login_data['errcode']}] {errmsg}", "/user-login")
        self._session_logged_in = True
        logger.info("禅道 Session 认证成功（用于文件下载）")

    # ── 云版 JSON 端点 ─────────────────────────────────

    def _cloud_json_get(self, path: str, params: dict = None) -> dict:
        """调用禅道云版 Web JSON 端点，返回内层 data dict。

        健壮性：服务器有时会在响应体中拼接多个 JSON（中间夹杂 user-deny 重定向），
        此时用 brace-count 截取第一个完整 JSON 对象解析，避免 _cloud_browse_cache
        被空 dict 污染后导致后续 fetch_bugs 拿到 0 条。
        """
        self._ensure_session()
        url = f"{self.base_url}/{path}"
        resp = self._http.get(url, params=params, timeout=30)
        if resp.status_code >= 400:
            raise ZentaoAPIError(resp.status_code, resp.text[:500], f"/{path}")
        if not resp.text:
            logger.error("云版响应为空: %s params=%s status=%s", path, params, resp.status_code)
            return {}
        time.sleep(self.api_delay)
        # 截取第一个完整 JSON 对象（应对响应体拼接多个 JSON 的情况）
        text = resp.text.strip()
        if text.startswith("{") and "}{" in text:
            # 用 brace 计数找第一个完整 JSON 对象的结束位置
            depth = 0
            for i, c in enumerate(text):
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        text = text[:i + 1]
                        break
        try:
            data = json.loads(text)
        except (ValueError, json.JSONDecodeError) as e:
            logger.error("云版JSON解析失败: %s, body前200字: %s", e, resp.text[:200])
            return {}
        inner = data.get("data", {})
        if isinstance(inner, str):
            inner = json.loads(inner)
        return inner

    def _cloud_get_browse(self, product_id: int, params: dict = None) -> dict:
        """获取云版 Bug 浏览页 JSON 数据（含 bugs、modules、users 等），结果缓存

        缓存键包含 pageID/recPerPage，避免分页请求命中第 1 页缓存导致重复数据。
        users 和 modules 在各页之间共享，每次拉取时合并更新。
        """
        _params = params or {}
        page = _params.get("pageID", 1)
        per_page = _params.get("recPerPage", 50)
        cache_key = f"{product_id}_{self._branch_id}_{page}_{per_page}"
        with self._cloud_browse_cache_lock:
            if cache_key in self._cloud_browse_cache:
                return self._cloud_browse_cache[cache_key]
        data = self._cloud_json_get(f"bug-browse-{product_id}-{self._branch_id}.json", params=params)
        with self._cloud_browse_cache_lock:
            self._cloud_browse_cache[cache_key] = data
        # 同时更新用户名映射缓存和模块名映射
        self._update_user_mapping(data)
        ZentaoClient._register_cloud_modules(data.get("modules", {}))
        return data

    def _update_user_mapping(self, browse_data: dict):
        """从云版浏览数据中提取用户名→中文名双向映射"""
        users = browse_data.get("users", {})
        if not users:
            return
        with self._cloud_user_cache_lock:
            for account, realname in users.items():
                if account and realname and realname != account:
                    self._cloud_user_name_to_account[realname] = account

    def _resolve_assigned_to_cloud(self, assigned_to: list) -> set:
        """将 assigned_to 列表中的中文名自动转换为英文账号名（云版）"""
        if not assigned_to or not self._cloud_session_auth:
            return set(assigned_to) if assigned_to else set()
        # 确保已加载用户映射（用任一已缓存的浏览数据，没有则尝试当前 branch_id）
        if not self._cloud_user_name_to_account:
            # 优先从已有缓存中提取用户
            with self._cloud_browse_cache_lock:
                for data in self._cloud_browse_cache.values():
                    if data.get("users"):
                        self._update_user_mapping(data)
                        break
        result = set()
        for name in assigned_to:
            result.add(name)
            if "-" in name:
                suffix = name.split("-", 1)[1]
                result.add(suffix)
                account = self._cloud_user_name_to_account.get(suffix)
                if account:
                    result.add(account)
            else:
                account = self._cloud_user_name_to_account.get(name)
                if account:
                    result.add(account)
        return result

    # ── 状态枚举 ──────────────────────────────────────

    def fetch_status_groups(self) -> dict:
        """动态获取当前禅道版本的开放/关闭状态码分组。

        返回 {"open": [<code>, ...], "closed": [<code>, ...]}。
        GUI 三个固定选项（激活/已关闭/激活+已关闭）按以下规则映射：
        - "激活" = open 组
        - "已关闭" = closed 组
        - "激活+已关闭" = open + closed

        - 自建版：从 /api.php/v1/bugStatuses 读取所有状态，按 name/code 归类
        - 云版：扫描浏览页提取实际出现的 status，按 code 关键字归类
        - 兜底：open=["active", "confirmed"]，closed=["resolved", "closed"]
        """
        if self._cloud_session_auth:
            return self._fetch_status_groups_cloud()
        return self._fetch_status_groups_self_hosted()

    def _fetch_status_groups_self_hosted(self) -> dict:
        """自建版：从 bugStatuses API 归类"""
        fallback = {"open": ["active", "confirmed"], "closed": ["resolved", "closed"]}
        try:
            data = self._request("GET", "/api.php/v1/bugStatuses")
            statuses = data.get("statuses", [])
            if not statuses:
                return fallback

            open_codes = []
            closed_codes = []
            for s in statuses:
                code = s.get("code", "")
                name = s.get("name", "")
                if not code:
                    continue
                # 归类规则：name/code 包含 "关闭/closed" → closed；"解决/resolved" → closed；
                # 包含 "激活/active/确认/confirmed/打开/opened" → open
                cn_lower = (name or "").lower()
                if "关闭" in name or "closed" in cn_lower or "解决" in name or "resolved" in cn_lower:
                    closed_codes.append(code)
                elif "激活" in name or "active" in cn_lower or "确认" in name or "confirmed" in cn_lower or "打开" in name or "opened" in cn_lower:
                    open_codes.append(code)
                else:
                    # 兜底：按 code 关键字
                    if code in ("active", "confirmed", "opened"):
                        open_codes.append(code)
                    elif code in ("resolved", "closed"):
                        closed_codes.append(code)

            # 去重保序
            seen = set()
            open_codes = [c for c in open_codes if not (c in seen or seen.add(c))]
            seen.clear()
            closed_codes = [c for c in closed_codes if not (c in seen or seen.add(c))]

            if not open_codes:
                open_codes = fallback["open"]
            if not closed_codes:
                closed_codes = fallback["closed"]
            return {"open": open_codes, "closed": closed_codes}
        except Exception as e:
            logger.debug("获取 bugStatuses 失败: %s", e)
            return fallback

    def _fetch_status_groups_cloud(self) -> dict:
        """云版：扫描浏览页，提取实际出现的 status"""
        fallback = {"open": ["active", "confirmed"], "closed": ["resolved", "closed"]}

        # 仅从已有缓存的浏览数据中提取 status，不主动请求新接口
        # （避免连接产品 0 触发权限错误、也避免不必要的网络请求）
        seen = set()
        with self._cloud_browse_cache_lock:
            for data in self._cloud_browse_cache.values():
                for b in data.get("bugs", []):
                    st = b.get("status", "")
                    if st:
                        seen.add(st)

        if not seen:
            logger.debug("云版浏览页缓存为空，使用兜底状态码分组")
            return fallback

        # 按 code 关键字归类
        open_codes = []
        closed_codes = []
        for st in sorted(seen):
            if st in ("active", "confirmed", "opened", "unclosed"):
                open_codes.append(st)
            elif st in ("resolved", "closed"):
                closed_codes.append(st)

        if not open_codes:
            open_codes = fallback["open"]
        if not closed_codes:
            closed_codes = fallback["closed"]
        return {"open": open_codes, "closed": closed_codes}

    # ── 通用请求 ──────────────────────────────────────

    def _request(self, method: str, path: str,
                 retry_on_401: bool = True, **kwargs) -> dict:
        if self._cloud_session_auth:
            self._ensure_session()
            headers = {}
        else:
            self._ensure_token()
            headers = {"Token": self._token}
        url = f"{self.base_url}{path}"

        for attempt in range(3):
            try:
                resp = self._http.request(method, url, headers=headers,
                                          timeout=30, **kwargs)
                if resp.status_code == 401 and retry_on_401:
                    if self._cloud_session_auth:
                        self._session_logged_in = False
                        self._ensure_session()
                    else:
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
        if not product_id and not project_id:
            raise ValueError("必须指定 product_id 或 project_id")

        if self._cloud_session_auth:
            return self._fetch_bugs_cloud(product_id, project_id, statuses,
                                          date_from, date_to, assigned_to,
                                          page, limit)

        if product_id:
            path = f"/api.php/v1/products/{product_id}/bugs"
        else:
            path = f"/api.php/v1/projects/{project_id}/bugs"

        params = {"page": page, "limit": limit}
        data = self._request("GET", path, params=params)

        bugs_data = data.get("bugs", [])
        total = data.get("total", 0)

        bugs = []
        for b in bugs_data:
            bug = self._parse_bug(b)
            if not self._passes_filters(bug, statuses, date_from, date_to, assigned_to):
                continue
            bugs.append(bug)

        return bugs, total

    def _fetch_bugs_cloud(self, product_id, project_id, statuses,
                          date_from, date_to, assigned_to, page, limit):
        """云版：通过 Web JSON 端点拉取 Bug 列表"""
        if not product_id:
            raise ValueError("云版必须指定 product_id，不支持按项目ID查询")
        data = self._cloud_get_browse(
            product_id,
            params={"recPerPage": limit, "pageID": page})
        bugs_data = data.get("bugs", [])
        pager = data.get("pager", {})
        total = pager.get("recTotal", len(bugs_data))

        # 浏览页数据已加载（含 users），现在可以解析中文名→英文账号
        if assigned_to and self._cloud_session_auth:
            resolved = self._resolve_assigned_to_cloud(assigned_to)
        else:
            resolved = set(assigned_to) if assigned_to else set()

        # 云版浏览页的 users 字典：英文账号 → 中文实名
        users_map = data.get("users", {})

        bugs = []
        for b in bugs_data:
            bug = self._parse_bug(b)
            # 用 users 映射将 assignedTo/openedBy 从英文账号替换为中文实名，
            # 确保后续 sync_engine._map_assignee 能匹配 TB 成员
            if users_map:
                if bug.assignedToAccount and bug.assignedToAccount in users_map:
                    bug.assignedTo = users_map[bug.assignedToAccount]
                if bug.openedBy and bug.openedBy in users_map:
                    bug.openedBy = users_map[bug.openedBy]
            if not self._passes_filters_with_assignees(
                    bug, statuses, date_from, date_to, resolved):
                continue
            bugs.append(bug)

        return bugs, total

    def _passes_filters(self, bug, statuses, date_from, date_to, assigned_to):
        """客户端筛选，自建版和云版共享"""
        if self._cloud_session_auth:
            return self._passes_filters_with_assignees(
                bug, statuses, date_from, date_to,
                set(assigned_to) if assigned_to else set())
        return self._passes_filters_with_assignees(
            bug, statuses, date_from, date_to, None,
            raw_assigned_to=assigned_to)

    def _passes_filters_with_assignees(self, bug, statuses, date_from, date_to,
                                        resolved_assignees, raw_assigned_to=None):
        """客户端筛选，自建版和云版共享"""
        if raw_assigned_to is not None and not self._cloud_session_auth:
            # 自建版路径：现在才把 assigned_to 转 resolved
            resolved_assignees = set()
            if raw_assigned_to:
                if isinstance(raw_assigned_to, str):
                    raw_assigned_to = [raw_assigned_to]
                for a in raw_assigned_to:
                    resolved_assignees.add(
                        self.account if str(a).lower() == "me" else a)

        if statuses and bug.status not in statuses:
            return False
        if date_from and str(date_from) > bug.openedDate[:10]:
            return False
        if date_to and str(date_to) < bug.openedDate[:10]:
            return False
        if resolved_assignees:
            if bug.assignedToAccount not in resolved_assignees \
                    and bug.assignedTo not in resolved_assignees:
                return False
        return True

    def _get_bug_raw(self, bug_id: int, retry_on_401: bool = True) -> Optional[dict]:
        """获取 Bug 的原始 JSON 数据并缓存，避免一次同步内重复 GET。"""
        with self._bug_raw_cache_lock:
            cached = self._bug_raw_cache.get(bug_id)
            if cached is not None:
                return cached

        if self._cloud_session_auth:
            data = self._cloud_json_get(f"bug-view-{bug_id}.json")
            bug_data = data.get("bug", data)
            # 云版 actions 是 dict，转为 list 以兼容自建版格式
            actions = data.get("actions", {})
            if isinstance(actions, dict):
                bug_data["actions"] = list(actions.values())
            elif isinstance(actions, list):
                bug_data["actions"] = actions
            # 注入 users map 供解析 assignedTo 等
            users_map = data.get("users", {})
            bug_data["_users"] = users_map
            # 将 assignedTo 从英文账号转为 {account, realname} 格式，
            # 确保 _parse_bug 输出的 assignedTo 为中文实名
            if users_map:
                raw_assigned = bug_data.get("assignedTo", "")
                if isinstance(raw_assigned, str) and raw_assigned in users_map:
                    bug_data["assignedTo"] = {
                        "account": raw_assigned,
                        "realname": users_map[raw_assigned],
                    }
                raw_opened = bug_data.get("openedBy", "")
                if isinstance(raw_opened, str) and raw_opened in users_map:
                    bug_data["openedBy"] = {
                        "account": raw_opened,
                        "realname": users_map[raw_opened],
                    }
            with self._bug_raw_cache_lock:
                self._bug_raw_cache[bug_id] = bug_data
            return bug_data

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
        """检查 Bug 的备注/历史记录中是否包含 VLNS 或 CPAX 文本"""
        try:
            bug_data = self._get_bug_raw(bug_id, retry_on_401=False)
            if not bug_data:
                return False
            actions = bug_data.get("actions", [])
            if not isinstance(actions, list):
                return False
            return bool(re.search(r'(?:VLNS|CPAX)-\d+', str(actions)))
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
        if self._cloud_session_auth:
            logger.warning("云版暂不支持修改 Bug 标题，请手动操作 (Bug#%d)", bug_id)
            return
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

        # 云版：扁平模块结构（无 parent），不需要 BFS 探测子模块
        if self._cloud_session_auth:
            logger.debug("云版模块为扁平结构，跳过BFS探测")
            with self._product_modules_cache_lock:
                self._product_modules_cache[product_id] = expanded
            return expanded

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
        # 云版：从浏览页 JSON 的 modules 字段获取
        if self._cloud_session_auth:
            try:
                data = self._cloud_get_browse(id_param)
                modules_dict = data.get("modules", {})
                if modules_dict:
                    return [{"id": k, "name": v, "parent": "0"}
                            for k, v in modules_dict.items() if k != "0"]
            except Exception as e:
                logger.debug("云版获取模块失败: %s", e)
            return []
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
                # 云版：扁平模块结构（无 parent），名称匹配即可，不用回退
                if self._cloud_session_auth:
                    logger.info("云版模块为扁平结构，按ID集合直接匹配 %d 个: %s",
                                len(matched), ",".join(sorted(matched))[:100])
                else:
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

        # 提取 SN 编码（从标题、步骤、附件文件名中搜索）
        files = ZentaoClient._normalize_files(data.get("files", []))
        file_names = " ".join(
            f.get("title", "") for f in files if isinstance(f, dict)
        )
        sn_text = data.get("steps", "") + " " + data.get("title", "") + " " + file_names
        sn_code = ZentaoClient._extract_sn(sn_text)

        # 模块名：自建版直接读 moduleTitle/moduleName；云版只有 module id，
        # 需要从浏览页缓存的 modules 字段（id→name）反查
        module_id = str(data.get("module", ""))
        module_name = data.get("moduleTitle", "") or data.get("moduleName", "")
        if not module_name and module_id and module_id != "0":
            module_name = ZentaoClient._lookup_cloud_module_name(module_id)

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
            module=module_id,
            moduleName=module_name,
            openedBuild=build_info,
            snCode=sn_code,
            files=ZentaoClient._normalize_files(data.get("files", [])),
        )

    # 全局缓存：云版模块名查询
    _cloud_module_id_to_name: dict = {}
    _cloud_module_id_to_name_lock = threading.Lock()

    @staticmethod
    def _lookup_cloud_module_name(module_id: str) -> str:
        """从最近一次云版浏览页缓存中查询模块名"""
        # 浏览页缓存是按 product_id 索引的，module_id 是全局唯一的
        with ZentaoClient._cloud_module_id_to_name_lock:
            return ZentaoClient._cloud_module_id_to_name.get(module_id, "")

    @classmethod
    def _register_cloud_modules(cls, modules_dict: dict):
        """注册云版浏览页中的 modules 映射（id→name）"""
        if not modules_dict:
            return
        with cls._cloud_module_id_to_name_lock:
            for mid, mname in modules_dict.items():
                if mid and mname:
                    cls._cloud_module_id_to_name[str(mid)] = str(mname)

    @staticmethod
    def _extract_sn(text: str) -> str:
        """从文本中提取 SN 编码，如 SN：0004、SN:12345

        匹配优先级：
        1. SN/SN码/设备SN 后跟冒号或空格
        2. HQ 开头 + 数字/字母（扫地机设备 SN 格式）
        找不到时返回 '/'。
        """
        if not text:
            return "/"
        # 1. 匹配 SN/SN码/设备SN 后跟冒号或空格
        match = re.search(r'(?:设备)?SN(?:码)?\s*[:：]\s*([A-Za-z0-9\-_]+)',
                          text, re.IGNORECASE)
        if match:
            return match.group(1)
        # 2. 匹配 HQ 开头的设备 SN（如 HQ5S00700002HC261300069）
        match = re.search(r'\b(HQ[0-9A-Z]{10,})\b', text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
        return "/"

    @staticmethod
    def _normalize_files(files):
        """v1 API 返回 dict 格式 {id: file_obj}，统一转为 list"""
        if isinstance(files, dict):
            return list(files.values())
        return files if isinstance(files, list) else []
