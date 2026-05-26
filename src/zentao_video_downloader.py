"""禅道附件下载器
从禅道 Bug 附件中下载视频/图片文件，作为 TB 附件下载的替代方案。
"""

import logging
import re
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _extract_zentao_bug_id(task_title: str) -> Optional[int]:
    """从 TB 任务标题中提取禅道 Bug ID。

    TB 同步时会在标题前加 `【禅道{id}】` 前缀。
    """
    m = re.search(r"【禅道(\d+)】", task_title)
    if m:
        return int(m.group(1))
    return None


VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")


def download_videos_from_zentao(
    zentao_client,
    bug_id: int,
    video_dir: Path,
) -> List[Path]:
    """从禅道 Bug 下载所有视频和图片附件。

    Args:
        zentao_client: ZentaoClient 实例
        bug_id: 禅道 Bug ID
        video_dir: 附件保存目录

    Returns:
        下载成功的本地路径列表
    """
    video_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    try:
        bug = zentao_client.get_bug(bug_id)
        if not bug:
            logger.warning("禅道 Bug #%d 不存在", bug_id)
            return downloaded

        files = getattr(bug, "files", []) or getattr(bug, "attachments", [])
        if not files:
            logger.info("禅道 Bug #%d 无附件", bug_id)
            return downloaded

        for f in files:
            fname = getattr(f, "title", None) or getattr(f, "filename", str(f))
            fid = getattr(f, "id", None)
            if not fid:
                continue
            name_lower = fname.lower()
            if not (name_lower.endswith(VIDEO_EXTS) or name_lower.endswith(IMAGE_EXTS)):
                continue

            local_path = video_dir / fname
            if local_path.exists() and local_path.stat().st_size > 0:
                logger.info("附件已缓存: %s", local_path)
                downloaded.append(local_path)
                continue

            try:
                att = zentao_client.download_attachment(fid, filename=fname)
                if att and att.data:
                    with open(local_path, "wb") as f_out:
                        f_out.write(att.data)
                    logger.info("从禅道下载成功: %s (%d bytes)", local_path, len(att.data))
                    downloaded.append(local_path)
            except Exception as e:
                logger.warning("禅道下载 %s 失败: %s", fname, e)

    except Exception as e:
        logger.error("禅道附件下载异常: %s", e)

    return downloaded
