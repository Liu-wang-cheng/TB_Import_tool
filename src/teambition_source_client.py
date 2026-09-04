"""外部 Teambition 源客户端

通过账号密码/扫码登录获取 Cookie，调用 Teambition Web 私有 API
（www.teambition.com/api）拉取缺陷单、评论、附件。

注意：与 src/teambition_client.py（内部 TB 目标端，appToken 认证）不同，
本模块是"外部 TB 源端"，使用 Web Session Cookie 认证。
"""

import logging
import os
import re
import sys
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def _default_cookie_file() -> str:
    """Cookie 文件默认路径：打包后 exe 同级 tools/，源码环境项目根 tools/"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tools", ".tb_cookie.txt")

WEB_API = "https://www.teambition.com/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class TeambitionSourceClient:
    """外部 Teambition Web 源客户端（Cookie 认证）"""

    def __init__(self, base_url: str = "", account: str = "",
                 password: str = "", cookie_file: str = "",
                 project_id: str = ""):
        self.base_url = base_url or ""
        self.account = account
        self.password = password
        self.cookie_file = cookie_file or _default_cookie_file()
        self.project_id = project_id
        self._cookies: Dict[str, str] = {}
        self._http = requests.Session()
        self._user_cache: Dict[str, str] = {}
        self._sfconfig_cache: Dict[str, str] = {}  # scenariofieldconfigId → name
        self._cf_name_cache: Dict[str, str] = {}  # customfieldId → 字段名称
        self._media_driver = None  # 备注媒体签名抓取的 Edge 浏览器（惰性）

    # ── 认证 ──────────────────────────────────────────

    def authenticate(self) -> None:
        """确保 Cookie 有效；失效则尝试账号密码登录或扫码"""
        cookie_str = self._load_cookie()
        if cookie_str and self._cookie_valid(cookie_str):
            self._cookies = self._parse_cookie(cookie_str)
            logger.info("外部 TB 复用有效 Cookie")
            return

        logger.info("外部 TB Cookie 失效，尝试重新登录...")
        if self.account and self.password:
            cookie_str = self._login_via_selenium(self.account, self.password)
        else:
            # 无账号密码，打开浏览器让用户扫码
            cookie_str = self._login_via_selenium("", "")
        if cookie_str:
            self._cookies = self._parse_cookie(cookie_str)
            self._save_cookie(cookie_str)

    def _load_cookie(self) -> str:
        try:
            if os.path.exists(self.cookie_file):
                with open(self.cookie_file, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except Exception:
            pass
        return ""

    def _save_cookie(self, cookie_str: str) -> None:
        try:
            os.makedirs(os.path.dirname(self.cookie_file), exist_ok=True)
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                f.write(cookie_str)
        except Exception as e:
            logger.warning("保存 Cookie 失败: %s", e)

    @staticmethod
    def _parse_cookie(cookie_str: str) -> Dict[str, str]:
        cookies = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        return cookies

    def _cookie_valid(self, cookie_str: str) -> bool:
        cookies = self._parse_cookie(cookie_str)
        try:
            r = requests.get(
                f"{WEB_API}/users/me", cookies=cookies,
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=15,
            )
            return r.status_code == 200
        except Exception:
            return False

    def _login_via_selenium(self, account: str, password: str) -> str:
        """Selenium 自动登录 + 扫码兜底，返回 Cookie 字符串"""
        try:
            from tools.tb_source_debug import login_via_selenium
            # 有账号密码时后台自动登录（不显示浏览器），无账号密码时显示浏览器供扫码
            headless = bool(account and password)
            return login_via_selenium(account, password, headless=headless)
        except ImportError as e:
            logger.error("Selenium 登录不可用（缺少依赖或 tools 模块）: %s", e)
            return ""
        except Exception as e:
            logger.error("Selenium 登录失败: %s", e)
            return ""

    # ── Web API 封装 ──────────────────────────────────

    def _get(self, path: str, params: dict = None, timeout: int = 30) -> Optional[dict]:
        url = f"{WEB_API}{path}"
        try:
            r = self._http.get(url, params=params, cookies=self._cookies,
                               headers={"User-Agent": UA, "Accept": "application/json"},
                               timeout=timeout)
            if r.status_code == 200:
                return r.json()
            logger.debug("外部 TB API %s HTTP %d: %s", path, r.status_code, r.text[:200])
        except Exception as e:
            logger.debug("外部 TB API %s 失败: %s", path, e)
        return None

    # ── 数据拉取 ──────────────────────────────────────

    def extract_project_id(self, url: str) -> str:
        """从外部 TB 网址解析 project_id"""
        m = re.search(r'/project/([0-9a-f]{24})', url)
        return m.group(1) if m else ""

    def get_unique_id_prefix(self, project_id: str) -> str:
        """获取项目任务编号前缀（如 "323A"），用于拼接专属任务 ID"""
        data = self._get(f"/projects/{project_id}")
        if isinstance(data, dict):
            return data.get("uniqueIdPrefix", "") or ""
        return ""

    def get_taskflow_status_name(self, status_id: str) -> str:
        """任务流状态 ID → 状态名（如 "待处理"/"重新打开"/"关闭"，带缓存）"""
        if not status_id:
            return ""
        cache = getattr(self, "_status_name_cache", None)
        if cache is None:
            cache = {}
            self._status_name_cache = cache
        if status_id in cache:
            return cache[status_id]
        data = self._get(f"/taskflowstatus/{status_id}")
        name = data.get("name", "") if isinstance(data, dict) else ""
        cache[status_id] = name
        return name

    def find_bug_scenariofield_id(self, project_id: str) -> str:
        """找到 name='缺陷' 的 scenariofieldconfigId"""
        tasks = self._get(f"/projects/{project_id}/tasks")
        if not isinstance(tasks, list):
            return ""
        sfc_ids = {t.get("_scenariofieldconfigId") for t in tasks
                   if t.get("_scenariofieldconfigId")}
        for sid in sfc_ids:
            name = self._get_scenariofield_name(sid)
            if name == "缺陷":
                return sid
        return ""

    def _get_scenariofield_name(self, sfc_id: str) -> str:
        if sfc_id in self._sfconfig_cache:
            return self._sfconfig_cache[sfc_id]
        data = self._get(f"/scenariofieldconfigs/{sfc_id}")
        name = data.get("name", "") if isinstance(data, dict) else ""
        self._sfconfig_cache[sfc_id] = name
        return name

    def fetch_tasks(self, project_id: str, scenariofield_id: str = "",
                    is_done: bool = None) -> List[dict]:
        """拉取项目任务，可选按场景类型和完成状态筛选。

        is_done=None 拉全部（默认仅未完成）；True 拉已完成（状态"关闭"）；
        False 拉未完成。
        """
        params = {}
        if is_done is not None:
            params["isDone"] = "true" if is_done else "false"
        tasks = self._get(f"/projects/{project_id}/tasks", params=params)
        if not isinstance(tasks, list):
            return []
        if scenariofield_id:
            tasks = [t for t in tasks
                     if t.get("_scenariofieldconfigId") == scenariofield_id]
        return tasks

    def get_user_name(self, user_id: str) -> str:
        """用户 ID → 名字（带缓存）"""
        if not user_id:
            return ""
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        data = self._get(f"/users/{user_id}")
        name = data.get("name", "") if isinstance(data, dict) else ""
        self._user_cache[user_id] = name
        return name

    def fetch_customfield_name(self, cf_id: str) -> str:
        """按自定义字段 _customfieldId 查字段名称（带缓存）。

        仅成功结果缓存；瞬时 API 失败不缓存，避免当批所有名称匹配
        静默降级为值猜测。
        """
        if not cf_id:
            return ""
        if cf_id in self._cf_name_cache:
            return self._cf_name_cache[cf_id]
        data = self._get(f"/customfields/{cf_id}")
        if not isinstance(data, dict):
            return ""
        name = data.get("name", "")
        self._cf_name_cache[cf_id] = name
        return name

    # ── 备注媒体签名 URL（Selenium 抓取前端渲染结果）─────────────

    def _get_media_driver(self):
        """惰性创建 Edge 无头浏览器并注入 Cookie（复用登录的同一套驱动环境）"""
        if self._media_driver is None:
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options
            import time as _t
            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
            self._media_driver = webdriver.Edge(options=options)
            self._media_driver.get("https://www.teambition.com")
            _t.sleep(2)
            for k, v in self._cookies.items():
                try:
                    self._media_driver.add_cookie(
                        {"name": k, "value": v, "domain": ".teambition.com"})
                except Exception:
                    pass
        return self._media_driver

    def close_media_driver(self):
        """关闭备注媒体抓取浏览器（同步结束时调用）"""
        if self._media_driver is not None:
            try:
                self._media_driver.quit()
            except Exception:
                pass
            self._media_driver = None

    def fetch_signed_media_urls(self, task_id: str,
                                wait_seconds: int = 10) -> dict:
        """打开任务页，提取备注媒体渲染后的签名 URL。

        返回 {fileKey路径: 签名URL}：
        - 视频/文件：/api/awos/download/{fileKey}?tb-file-token=JWT
        - 图片：teambition-file.oss.../rich-text-*/{fileKey}?OSSAccessKeyId=...
        """
        import time as _t
        driver = self._get_media_driver()
        driver.get(f"https://www.teambition.com/task/{task_id}")
        _t.sleep(wait_seconds)
        html = driver.page_source
        result = {}
        # 视频 source src（awos/download + tb-file-token）
        for m in re.finditer(
                r'<source[^>]*src="([^"]*awos/download/[^"]+)"', html):
            url = m.group(1).replace("&amp;", "&")
            key = url.split("/api/awos/download/")[1].split("?")[0]
            result[key] = url
        # 图片 img src（OSS 签名直链）
        for m in re.finditer(
                r'<img[^>]*src="([^"]*teambition-file\.oss[^"]+)"', html):
            url = m.group(1).replace("&amp;", "&")
            if "aliyuncs.com/" in url:
                key = url.split("aliyuncs.com/")[1].split("?")[0]
                result[key] = url
        if result:
            logger.info("任务 %s 备注媒体签名 %d 个", task_id, len(result))
        else:
            logger.warning("任务 %s 未抓到备注媒体签名 URL", task_id)
        return result

    def fetch_task_comments(self, task_id: str) -> List[dict]:
        """拉取任务评论（activity.comment 和 activity.comment.attachments）

        返回 [{actor, date, action, comment, attachments}, ...]
        """
        acts = self._get(f"/tasks/{task_id}/activities")
        if not isinstance(acts, list):
            return []
        comments = []
        for a in acts:
            action = a.get("action", "")
            if action not in ("activity.comment", "activity.comment.attachments"):
                continue
            content = a.get("content", {})
            files = content.get("files", [])
            activity_id = a.get("_id", "")
            comments.append({
                "actor": content.get("creator", ""),
                "date": a.get("created", ""),
                "action": action,
                "comment": content.get("comment", "").strip(),
                "attachments": [
                    {
                        "id": f.get("_id", ""),
                        "name": self._build_file_name(f),
                        "ext": f.get("ext", ""),
                        "mimeType": f.get("mimeType", ""),
                        "size": f.get("size", 0),
                        "url": f.get("url", ""),
                        "thumbnailUrl": f.get("thumbnailUrl", ""),
                        # 下载签名 URL 需要：task_id + activity_id + file_id
                        "task_id": task_id,
                        "activity_id": activity_id,
                    }
                    for f in files
                ],
            })
        return comments

    @staticmethod
    def _build_file_name(f: dict) -> str:
        """构造完整文件名：name + ext"""
        name = f.get("name", "file")
        ext = f.get("ext", "")
        if ext and not name.lower().endswith(f".{ext.lower()}"):
            return f"{name}.{ext}"
        return name

    def fetch_task_detail(self, task_id: str) -> Optional[dict]:
        """拉取单条任务详情"""
        data = self._get(f"/tasks/{task_id}")
        return data if isinstance(data, dict) else None

    def download_file(self, url: str, filename: str = "",
                      timeout: int = 120) -> Optional[bytes]:
        """下载文件内容（url 为 https 或 oss:// 协议）

        优先用 thumbnailUrl（公开缩略图，可下载）；oss:// 转 https 尝试原图。
        原图私有需签名（open API needSign 仅付费企业），失败返回 None。
        """
        if not url:
            return None
        if url.startswith("oss://"):
            url = url.replace(
                "oss://",
                "https://teambition-file.oss-cn-zhangjiakou.aliyuncs.com/", 1)
        try:
            r = self._http.get(url, headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content
            logger.debug("下载文件失败 %s HTTP %d", filename, r.status_code)
        except Exception as e:
            logger.debug("下载文件异常 %s: %s", filename, e)
        return None

    def update_title(self, task_id: str, new_title: str) -> bool:
        """更新外部 TB 任务标题（PUT /tasks/{task_id}）

        无权限或接口不支持时返回 False，调用方回退到写评论。
        """
        if not task_id or not new_title:
            return False
        try:
            r = self._http.put(
                f"{WEB_API}/tasks/{task_id}",
                json={"content": new_title},
                cookies=self._cookies,
                headers={"User-Agent": UA, "Accept": "application/json",
                         "Content-Type": "application/json"},
                timeout=30,
            )
            if r.status_code == 200:
                logger.info("外部 TB 标题已更新: %s", new_title[:50])
                return True
            logger.info("外部 TB 写标题失败 HTTP %d（回退写评论）", r.status_code)
        except Exception as e:
            logger.warning("外部 TB 写标题异常: %s", e)
        return False

    def add_comment(self, task_id: str, content: str) -> bool:
        """写评论到外部 TB 任务（用于回写内部 TB 编号）

        POST /tasks/{task_id}/activities，body {'content': content}
        """
        if not task_id or not content:
            return False
        try:
            r = self._http.post(
                f"{WEB_API}/tasks/{task_id}/activities",
                json={"content": content},
                cookies=self._cookies,
                headers={"User-Agent": UA, "Accept": "application/json",
                         "Content-Type": "application/json"},
                timeout=30,
            )
            if r.status_code == 200:
                logger.info("外部 TB 评论已写入: %s", content[:50])
                return True
            logger.warning("外部 TB 写评论失败 HTTP %d: %s",
                           r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("外部 TB 写评论异常: %s", e)
        return False

    def download_comment_attachment(self, file_id: str, task_id: str,
                                    activity_id: str,
                                    timeout: int = 120) -> Optional[bytes]:
        """下载评论附件（通过文件详情接口拿签名下载 URL）

        流程：GET /files/{file_id}?scope=task&scopeId={task_id}
              &boundToObjectType=activity&boundToObjectId={activity_id}
              → 返回 url（带 token 的下载链接）→ 访问 url 下载
        """
        if not file_id:
            return None
        data = self._get(
            f"/files/{file_id}",
            params={
                "scope": "task",
                "scopeId": task_id,
                "boundToObjectType": "activity",
                "boundToObjectId": activity_id,
            },
        )
        if not isinstance(data, dict):
            return None
        url = data.get("url", "") or data.get("downloadUrl", "")
        if not url:
            return None
        try:
            r = self._http.get(url, headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content
            logger.debug("下载评论附件失败 %s HTTP %d", file_id, r.status_code)
        except Exception as e:
            logger.debug("下载评论附件异常 %s: %s", file_id, e)
        return None

    def close(self) -> None:
        self._http.close()
