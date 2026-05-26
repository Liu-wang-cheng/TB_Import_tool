"""AI缺陷分析工具 - 视觉分析按需集成模块
从 TB 任务中提取视频附件、下载、并调用 GLM-4V 分析。
与日志分析独立，按需调用。
"""

import logging
from pathlib import Path
from typing import List, Optional

import requests

from src.vision_analyzer import VisionAnalyzer
from src.tb_web_downloader import TBWebDownloader
from src.zentao_video_downloader import (
    _extract_zentao_bug_id,
    download_videos_from_zentao,
)

logger = logging.getLogger(__name__)

API_BASE = "https://open.teambition.com/api"


VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")


def extract_visual_attachments(task_raw: dict) -> List[dict]:
    """从 TB 任务原始详情中提取视频和图片附件列表。

    Args:
        task_raw: /v3/task/query 返回的原始 dict

    Returns:
        [{id, name, type}, ...]  type: 'video' | 'image'
    """
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
                    name = item.get("title", "unknown")
                    name_lower = name.lower()
                    if name_lower.endswith(VIDEO_EXTENSIONS):
                        files.append({
                            "id": item["id"],
                            "name": name,
                            "type": "video",
                        })
                    elif name_lower.endswith(IMAGE_EXTENSIONS):
                        files.append({
                            "id": item["id"],
                            "name": name,
                            "type": "image",
                        })
    return files


def download_tb_video(
    file_id: str,
    file_name: str,
    video_dir: Path,
    http_session: requests.Session,
    headers: dict,
    timeout: int = 120,
) -> Optional[Path]:
    """通过 TB 开放平台 API 下载单个附件到本地。

    Returns:
        本地文件路径，下载失败返回 None
    """
    video_dir.mkdir(parents=True, exist_ok=True)
    local_path = video_dir / file_name
    if local_path.exists() and local_path.stat().st_size > 0:
        logger.info("附件已缓存: %s", local_path)
        return local_path

    try:
        resp = http_session.request(
            "GET", f"{API_BASE}/v3/file/{file_id}/download",
            headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            logger.warning("获取下载URL失败 HTTP %d: %s", resp.status_code, resp.text[:200])
            return None

        data = resp.json() or {}
        result = data.get("result") or {}
        url = result.get("downloadUrl", "")
        if not url:
            logger.warning("无 downloadUrl: %s", data)
            return None

        dresp = http_session.request("GET", url, timeout=timeout)
        if dresp.status_code == 200:
            with open(local_path, "wb") as f:
                f.write(dresp.content)
            logger.info("附件下载成功: %s (%d bytes)", local_path, len(dresp.content))
            return local_path
        else:
            logger.warning("附件下载失败 HTTP %d", dresp.status_code)
            return None
    except Exception as e:
        logger.error("下载附件异常: %s", e)
        return None


class VisionIntegration:
    """视觉分析集成器：按需下载并分析 TB 任务视频/图片附件。

    支持多源下载（优先级从高到低）：
    1. 禅道 Bug 附件（若 TB 任务是从禅道同步的）
    2. TB Web Cookie 下载（模拟浏览器）
    3. TB 开放平台 API 下载
    4. 本地 cache/videos/ 缓存
    """

    def __init__(
        self,
        vision_api_key: str,
        vision_base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        vision_model: str = "glm-4v-flash",
        vision_timeout: int = 60,
        cache_dir: str = "cache/videos",
        zentao_client=None,
        web_cookies: dict = None,
    ):
        self.analyzer = VisionAnalyzer(
            api_key=vision_api_key,
            base_url=vision_base_url,
            model=vision_model,
            timeout=vision_timeout,
        )
        self.cache_dir = Path(cache_dir)
        self.zentao_client = zentao_client
        self.web_downloader = TBWebDownloader(web_cookies, cache_dir) if web_cookies else None

    def analyze_task_videos(
        self,
        task_raw: dict,
        http_session: requests.Session = None,
        headers: dict = None,
        defect_title: str = "",
    ) -> dict:
        """分析任务中的所有视频和图片附件。

        依次尝试多种下载方式，直到获取到附件或所有方式都失败。

        Returns:
            {file_name: analysis_text_or_None}
        """
        attachments = extract_visual_attachments(task_raw)
        if not attachments:
            logger.info("任务中无视觉附件")
            return {}

        logger.info("发现 %d 个视觉附件", len(attachments))
        video_paths = []
        image_paths = []

        for att in attachments:
            path = self._try_download_video(
                att["id"], att["name"],
                task_raw, http_session, headers,
            )
            if path:
                if att["type"] == "video":
                    video_paths.append(path)
                else:
                    image_paths.append(path)

        results = {}
        if video_paths:
            results.update(self.analyzer.analyze_videos(video_paths, defect_title))
        if image_paths:
            results.update(self.analyzer.analyze_images(image_paths, defect_title))
        return results

    def _try_download_video(
        self,
        file_id: str,
        file_name: str,
        task_raw: dict,
        http_session: requests.Session,
        headers: dict,
    ) -> Optional[Path]:
        """尝试多种方式下载单个视觉附件，返回第一个成功的方式。"""
        local_path = self.cache_dir / file_name
        if local_path.exists() and local_path.stat().st_size > 0:
            logger.info("使用本地缓存: %s", local_path)
            return local_path

        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 方式1: 禅道下载（如果任务是从禅道同步的）
        task_title = task_raw.get("content", "")
        zentao_bug_id = _extract_zentao_bug_id(task_title)
        if zentao_bug_id and self.zentao_client:
            try:
                zentao_paths = download_videos_from_zentao(
                    self.zentao_client, zentao_bug_id, self.cache_dir,
                )
                for zp in zentao_paths:
                    if zp.name == file_name:
                        logger.info("从禅道下载成功: %s", zp)
                        return zp
            except Exception as e:
                logger.warning("禅道下载失败: %s", e)

        # 方式2: TB Web Cookie 下载
        if self.web_downloader:
            try:
                path = self.web_downloader.download_video(file_id, file_name)
                if path:
                    return path
            except Exception as e:
                logger.warning("Web Cookie 下载失败: %s", e)

        # 方式3: TB 开放平台 API 下载
        if http_session is not None and headers:
            try:
                path = download_tb_video(
                    file_id, file_name, self.cache_dir,
                    http_session, headers,
                )
                if path:
                    return path
            except Exception as e:
                logger.warning("开放平台 API 下载失败: %s", e)

        logger.warning(
            "附件 %s 所有下载方式均失败，请手动下载到 %s 后重试",
            file_name, self.cache_dir,
        )
        return None

    def close(self):
        self.analyzer.close()
        if self.web_downloader:
            self.web_downloader.close()
