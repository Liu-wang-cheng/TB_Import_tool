"""协同学习模块 — 通过 GitHub REST API 多用户共享知识库和分类器训练数据

无需安装 Git，使用 Personal Access Token 认证。
"""

import base64
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# GitHub 仓库
_REPO_OWNER = "Liu-wang-cheng"
_REPO_NAME = "TB_Import_tool"
_REPO_BRANCH = "main"
_API_BASE = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}/contents"

# 同步的白名单文件
_SYNC_FILES = {
    "knowledge_base.jsonl": "data/knowledge_base.jsonl",
    "knowledge_feedback.yaml": "data/knowledge_feedback.yaml",
    "classifier_model.pkl": "data/classifier_model.pkl",
}

# 文件大小上限
_MAX_SIZES = {
    "knowledge_base.jsonl": 10 * 1024 * 1024,    # 10 MB
    "knowledge_feedback.yaml": 1 * 1024 * 1024,   # 1 MB
    "classifier_model.pkl": 50 * 1024 * 1024,     # 50 MB
}


class CollaborativeLearning:
    """协同学习管理器。

    通过 GitHub REST API 拉取/推送共享的知识库和分类器数据。
    支持 JSONL 按 id 去重合并，不依赖本地 git 二进制。
    """

    def __init__(self, config: dict, data_dir: str = "data"):
        cl_cfg = config.get("collaborative_learning", {})
        self._enabled = cl_cfg.get("enabled", True)
        self._token = self._resolve_token(cl_cfg.get("github_token", ""))
        self._owner = cl_cfg.get("repo_owner", _REPO_OWNER)
        self._repo = cl_cfg.get("repo_name", _REPO_NAME)
        self._branch = cl_cfg.get("branch", _REPO_BRANCH)
        self._interval_hours = cl_cfg.get("sync_interval_hours", 168)
        self._auto_pull = cl_cfg.get("auto_pull", True)
        self._auto_push = cl_cfg.get("auto_push", True)
        self._data_dir = data_dir
        self._api_base = f"https://api.github.com/repos/{self._owner}/{self._repo}/contents"
        self._last_sync_time: float = 0

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._token)

    @property
    def token_configured(self) -> bool:
        return bool(self._token)

    @staticmethod
    def _resolve_token(config_token: str) -> str:
        """解析 Token：优先环境变量 > ~/.github_token 文件 > 配置文件。

        与 release.py 保持一致。
        """
        env_token = os.environ.get("GITHUB_TOKEN", "")
        if env_token:
            return env_token.strip()
        token_path = os.path.expanduser("~/.github_token")
        if os.path.exists(token_path):
            try:
                with open(token_path, "r") as f:
                    file_token = f.read().strip()
                if file_token:
                    return file_token
            except Exception:
                pass
        return config_token.strip()

    def _api_headers(self) -> dict:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "TB-Import-Tool-Collab",
        }

    def _api_url(self, remote_path: str) -> str:
        return f"{self._api_base}/{remote_path}?ref={self._branch}"

    def _check_connection(self) -> tuple[bool, str]:
        """检查 GitHub API 连接和 Token 权限。"""
        if not self._token:
            return False, "未配置 GitHub Token"
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{self._owner}/{self._repo}",
                headers=self._api_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                return True, f"已连接 {self._owner}/{self._repo}"
            elif resp.status_code == 401:
                return False, "Token 无效或已过期"
            elif resp.status_code == 403:
                return False, "Token 无权限访问该仓库（请确认已勾选 repo 权限）"
            elif resp.status_code == 404:
                return False, f"仓库 {self._owner}/{self._repo} 不存在"
            else:
                return False, f"API 返回 {resp.status_code}: {resp.text[:200]}"
        except requests.exceptions.Timeout:
            return False, "连接超时"
        except requests.exceptions.ConnectionError:
            return False, "网络连接失败"
        except Exception as e:
            return False, f"连接异常: {e}"

    def download_file(self, remote_path: str) -> tuple:
        """从 GitHub 下载单个文件。

        Returns:
            (content_bytes, sha, error_message)
            成功时 error_message 为 None
        """
        url = self._api_url(remote_path)
        try:
            resp = requests.get(url, headers=self._api_headers(), timeout=30)
            if resp.status_code == 404:
                return None, None, None  # 文件不存在，不是错误
            if resp.status_code != 200:
                return None, None, f"下载失败 HTTP {resp.status_code}"
            data = resp.json()
            raw = data.get("content", "")
            if not raw:
                return None, data.get("sha"), None
            content = base64.b64decode(raw)
            sha = data.get("sha", "")
            return content, sha, None
        except requests.exceptions.Timeout:
            return None, None, "下载超时"
        except Exception as e:
            return None, None, f"下载异常: {e}"

    def upload_file(self, remote_path: str, content: bytes, sha: str,
                    message: str = "") -> tuple:
        """上传单个文件到 GitHub。

        Args:
            sha: 当前文件的 blob sha（必须先下载获取）

        Returns:
            (success, message, new_sha)
        """
        if not message:
            message = f"sync: update {remote_path}"

        enc = base64.b64encode(content).decode("utf-8")
        body = {
            "message": message,
            "content": enc,
            "branch": self._branch,
        }
        if sha:
            body["sha"] = sha

        # PUT 只需要 raw path，ref 放在 body.branch 中
        url = f"{self._api_base}/{remote_path}"

        try:
            resp = requests.put(
                url,
                headers=self._api_headers(),
                json=body,
                timeout=60,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return True, "上传成功", data.get("content", {}).get("sha", "")
            elif resp.status_code == 401:
                return False, "Token 权限不足（请确认已勾选 repo 权限）", ""
            elif resp.status_code == 409:
                return False, "远程文件已更新，请先拉取再上传", ""
            elif resp.status_code == 422:
                return False, f"上传校验失败: {resp.json().get('message', '')}", ""
            else:
                return False, f"上传失败 HTTP {resp.status_code}: {resp.text[:200]}", ""
        except requests.exceptions.Timeout:
            return False, "上传超时", ""
        except Exception as e:
            return False, f"上传异常: {e}", ""

    # ── JSONL 合并 ────────────────────────────────────────

    def _merge_jsonl(self, local_content: bytes, remote_content: bytes) -> bytes:
        """按 id 字段去重合并两份 JSONL。"""
        seen_ids: set = set()
        lines: list[str] = []

        for content in (local_content, remote_content):
            text = content.decode("utf-8")
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rid = obj.get("id", "")
                    if rid and rid in seen_ids:
                        continue
                    if rid:
                        seen_ids.add(rid)
                    lines.append(json.dumps(obj, ensure_ascii=False))
                except json.JSONDecodeError:
                    lines.append(line)

        return "\n".join(lines).encode("utf-8")

    def _merge_yaml(self, local_content: bytes, remote_content: bytes) -> bytes:
        """深度合并两份 YAML 反馈文件。

        策略：合并 approved/rejected 列表，按 id 去重。
        """
        import yaml

        try:
            local_data = yaml.safe_load(local_content.decode("utf-8")) or {}
        except Exception:
            local_data = {}

        try:
            remote_data = yaml.safe_load(remote_content.decode("utf-8")) or {}
        except Exception:
            remote_data = {}

        merged = {}
        for key in ("approved", "rejected"):
            seen = set()
            merged_list = []
            for src in (local_data.get(key, []), remote_data.get(key, [])):
                if not isinstance(src, list):
                    continue
                for item in src:
                    if not isinstance(item, dict):
                        continue
                    rid = item.get("id", "")
                    if rid and rid in seen:
                        continue
                    if rid:
                        seen.add(rid)
                    merged_list.append(item)
            if merged_list:
                merged[key] = merged_list

        return yaml.dump(merged, allow_unicode=True, default_flow_style=False).encode("utf-8")

    def _validate_size(self, filename: str, content: bytes) -> bool:
        max_size = _MAX_SIZES.get(filename, 5 * 1024 * 1024)
        return len(content) <= max_size

    def _local_path(self, filename: str) -> str:
        return os.path.join(self._data_dir, filename)

    # ── 高层操作 ──────────────────────────────────────────

    def pull(self) -> tuple:
        """拉取远程数据到本地。

        Returns:
            (success, message, has_updates)
        """
        if not self.enabled:
            return False, "协同学习未启用或未配置 Token", False

        has_updates = False
        messages = []

        for local_name, remote_path in _SYNC_FILES.items():
            remote_content, sha, err = self.download_file(remote_path)
            if err:
                messages.append(f"{local_name}: {err}")
                continue
            if remote_content is None:
                continue
            if not self._validate_size(local_name, remote_content):
                messages.append(f"{local_name}: 远程文件过大，跳过")
                continue

            local_path = self._local_path(local_name)
            local_content = b""
            if os.path.exists(local_path):
                with open(local_path, "rb") as f:
                    local_content = f.read()

            # 合并策略
            if local_name == "knowledge_base.jsonl":
                merged = self._merge_jsonl(local_content, remote_content)
            elif local_name == "knowledge_feedback.yaml":
                merged = self._merge_yaml(local_content, remote_content)
            else:
                # classifier_model.pkl: 用远程替换本地
                merged = remote_content

            if merged != local_content:
                os.makedirs(self._data_dir, exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(merged)
                has_updates = True
                logger.info("协同学习: 已更新 %s", local_name)

        if not messages:
            if has_updates:
                return True, "已拉取最新数据并合并到本地", True
            return True, "数据和远程一致，无需更新", False
        return True, "; ".join(messages), has_updates

    def push(self) -> tuple:
        """推送本地数据到远程。

        Returns:
            (success, message)
        """
        if not self.enabled:
            return False, "协同学习未启用或未配置 Token"

        pushed_count = 0
        messages = []

        for local_name, remote_path in _SYNC_FILES.items():
            local_path = self._local_path(local_name)
            if not os.path.exists(local_path):
                continue

            local_content = self._read_local_file(local_path)
            if local_content is None:
                continue
            if not self._validate_size(local_name, local_content):
                messages.append(f"{local_name}: 文件过大，跳过")
                continue

            # 先获取远程 sha
            remote_content, sha, err = self.download_file(remote_path)
            if err:
                messages.append(f"{local_name}: 获取远程状态失败 - {err}")
                continue

            # 无变化时跳过
            if remote_content == local_content and remote_content is not None:
                continue

            success, msg, _ = self.upload_file(
                remote_path,
                local_content,
                sha or "",
                f"sync: update {local_name} ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
            )
            if success:
                pushed_count += 1
            else:
                messages.append(f"{local_name}: {msg}")

        if pushed_count > 0:
            self._last_sync_time = time.time()
            return True, f"已推送 {pushed_count} 个文件"
        if not messages:
            return True, "无变更需要推送"
        return False, "; ".join(messages)

    def sync(self) -> tuple:
        """完整同步流程：拉取 → 本地合并 → 推送。

        Returns:
            (success, message, has_local_updates)
        """
        if not self.enabled:
            return False, "协同学习未启用或未配置 Token", False

        # Step 1: 拉取远程最新数据并合并
        pull_ok, pull_msg, has_updates = self.pull()
        if not pull_ok:
            return False, f"拉取失败: {pull_msg}", False

        # Step 2: 推送本地数据
        push_ok, push_msg = self.push()

        if has_updates:
            return True, f"拉取: {pull_msg}; 推送: {push_msg}", True
        return push_ok, f"拉取: {pull_msg}; 推送: {push_msg}", False

    def should_sync(self) -> bool:
        """检查是否到了定时推送的时间。"""
        if not self.enabled or not self._auto_push:
            return False
        if self._last_sync_time <= 0:
            self._last_sync_time = time.time()
            return False
        elapsed_hours = (time.time() - self._last_sync_time) / 3600
        return elapsed_hours >= self._interval_hours

    def _read_local_file(self, path: str) -> Optional[bytes]:
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception as e:
            logger.warning("读取本地文件失败 %s: %s", path, e)
            return None


def rebuild_local_models(data_dir: str = "data", classifier: object = None):
    """在 pull 后重建本地模型。

    Args:
        data_dir: data/ 目录路径
        classifier: BugClassifier 实例（可选）
    """
    from src.knowledge_base import KnowledgeBase

    # 重建知识库模型
    kb_path = os.path.join(data_dir, "knowledge_base.jsonl")
    if os.path.exists(kb_path):
        logger.info("协同学习: 知识库已更新，触发模型重建")
        # KnowledgeBase 实例通常由调用者创建和管理，
        # 这里我们通过 KnowledgeBase 公开的方法来处理
        # 调用者应调用 kb.reload_data() + kb.rebuild_model()

    # 重载分类器模型
    if classifier is not None and hasattr(classifier, 'load_similarity_model'):
        logger.info("协同学习: 分类器模型已更新，触发重载")
        classifier.load_similarity_model()
