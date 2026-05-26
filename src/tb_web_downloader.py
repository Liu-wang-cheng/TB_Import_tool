"""TB Web 前端模拟下载器
通过浏览器 Cookie 访问 TB Web 私有 API 下载附件。
因为 TB 开放平台未开放文件 API，这是替代方案。
"""

import json
import logging
import re
from pathlib import Path
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

TB_WEB_BASE = "https://www.teambition.com/api"

VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")


def _extract_file_ids_from_task(task_raw: dict) -> List[dict]:
    """从任务原始详情中提取文件 ID 列表。"""
    files = []
    cfs = task_raw.get("customfields", [])
    for cf in cfs:
        val = cf.get("value", [])
        if isinstance(val, list):
            for item in val:
                if (
                    isinstance(item, dict)
                    and item.get("metaString", "").startswith('{"boundToObjectType":"file"')
                ):
                    files.append({
                        "id": item["id"],
                        "name": item.get("title", "unknown"),
                        "resource_id": item.get("metaString", ""),
                    })
    return files


class TBWebDownloader:
    """使用 TB Web Cookie 模拟浏览器下载附件。"""

    def __init__(self, cookies: dict, video_dir: str = "cache/videos"):
        """
        Args:
            cookies: 从浏览器复制的 TB Cookie 字典，至少包含 TEAMBITION_SESSIONID
            video_dir: 附件保存目录
        """
        self._cookies = cookies
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.video_dir = Path(video_dir)

    def _web_get(self, path: str, params: dict = None) -> Optional[dict]:
        """调用 TB Web API。"""
        url = f"{TB_WEB_BASE}{path}"
        try:
            resp = self._http.get(url, params=params, cookies=self._cookies, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Web API %s HTTP %d: %s", path, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("Web API %s 失败: %s", path, e)
        return None

    def _get_work_info(self, work_id: str) -> Optional[dict]:
        """通过 Web API 查询 work（文件）元信息。"""
        # TB Web 前端通常用 /works/{id} 或 /works?ids=...
        data = self._web_get(f"/works/{work_id}")
        if data:
            return data
        # 尝试批量查询
        data = self._web_get("/works", {"_id": work_id})
        if data and isinstance(data, list) and data:
            return data[0]
        return None

    def _get_download_url(self, work_id: str) -> Optional[str]:
        """获取文件下载 URL。"""
        # TB Web 通常通过 works API 返回 fileKey 或 downloadUrl
        info = self._get_work_info(work_id)
        if not info:
            return None
        # 可能的字段名
        for key in ("downloadUrl", "fileUrl", "url", "fileKey"):
            url = info.get(key)
            if url and isinstance(url, str) and url.startswith("http"):
                return url
        # 有些返回的是 OSS key，需要拼接
        file_key = info.get("fileKey") or info.get("key")
        if file_key:
            # 尝试构造 OSS URL（可能不准确，仅作兜底）
            logger.info("Work %s 返回 fileKey: %s", work_id, file_key)
        return None

    def download_video(self, file_id: str, file_name: str) -> Optional[Path]:
        """下载单个附件文件。"""
        self.video_dir.mkdir(parents=True, exist_ok=True)
        local_path = self.video_dir / file_name
        if local_path.exists() and local_path.stat().st_size > 0:
            logger.info("附件已缓存: %s", local_path)
            return local_path

        url = self._get_download_url(file_id)
        if not url:
            logger.warning("无法获取 %s 的下载链接", file_name)
            return None

        try:
            resp = self._http.get(url, cookies=self._cookies, timeout=120)
            if resp.status_code == 200:
                with open(local_path, "wb") as f:
                    f.write(resp.content)
                logger.info("下载成功: %s (%d bytes)", local_path, len(resp.content))
                return local_path
            else:
                logger.warning("下载失败 HTTP %d", resp.status_code)
        except Exception as e:
            logger.error("下载异常: %s", e)
        return None

    def download_task_videos(self, task_raw: dict) -> dict:
        """下载任务中的所有视频和图片附件。

        Returns:
            {file_name: local_path_or_None}
        """
        files = _extract_file_ids_from_task(task_raw)
        visual = [f for f in files
                  if f["name"].lower().endswith(VIDEO_EXTS + IMAGE_EXTS)]
        if not visual:
            logger.info("任务中无视觉附件")
            return {}

        results = {}
        for vf in visual:
            path = self.download_video(vf["id"], vf["name"])
            results[vf["name"]] = path
        return results

    def close(self):
        self._http.close()


def test_with_cookie(cookies: dict, task_raw: dict):
    """快速测试 Cookie 是否可用。"""
    dl = TBWebDownloader(cookies)
    files = _extract_file_ids_from_task(task_raw)
    if not files:
        print("任务中无文件附件")
        dl.close()
        return

    print(f"发现 {len(files)} 个文件附件")
    for f in files:
        print(f"\n测试: {f['name']} (id={f['id']})")
        info = dl._get_work_info(f["id"])
        print(f"  work info: {json.dumps(info, ensure_ascii=False)[:500] if info else 'None'}")
        url = dl._get_download_url(f["id"])
        print(f"  download url: {url[:80] if url else 'None'}")

    dl.close()
