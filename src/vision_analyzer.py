"""AI缺陷分析工具 - 视觉分析模块
支持视频下载、关键帧提取、GLM-4V 多模态分析。
"""

import base64
import json
import logging
import os
import re
import time
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import requests

try:
    import cv2
except ImportError:
    cv2 = None

logger = logging.getLogger(__name__)

VISION_SYSTEM_PROMPT = (
    "你是一名扫地机器人测试视觉分析专家。"
    "请仔细观察图片，描述其中扫地机器人的行为、周围环境、障碍物情况。"
    "重点关注：机器人是否接触/推动障碍物、障碍物类型、机器人姿态、运动方向。"
    "使用中文回答，技术术语保留英文。"
)


def _encode_image_to_base64(image_bytes: bytes) -> str:
    """将图片字节编码为 base64 数据 URL。"""
    return f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode('utf-8')}"


def _extract_keyframes(video_path: Path, max_frames: int = 8) -> List[bytes]:
    """从视频中提取关键帧（均匀采样）。

    Returns:
        图片字节列表（JPEG 格式）
    """
    if cv2 is None:
        logger.error("OpenCV 未安装，无法提取视频帧")
        return []

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("无法打开视频: %s", video_path)
        return []

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    duration = total_frames / fps if fps > 0 else 0

    logger.info("视频 %s: %d 帧, %.1f fps, 时长 %.1fs", video_path.name, total_frames, fps, duration)

    # 均匀采样 max_frames 帧
    indices = [int(i * total_frames / max_frames) for i in range(max_frames)]
    frames = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret:
            # 压缩为 JPEG，限制大小
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            frames.append(buf.tobytes())

    cap.release()
    logger.info("提取 %d 个关键帧", len(frames))
    return frames


def _get_video_duration(video_path: Path) -> float:
    """获取视频时长（秒）。"""
    if cv2 is None:
        return 0.0
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return total / fps if fps > 0 else 0.0


class VisionAnalyzer:
    """视觉分析器：下载视频、提取关键帧、调用 GLM-4V 分析。"""

    def __init__(self, api_key: str, base_url: str = "https://open.bigmodel.cn/api/paas/v4",
                 model: str = "glm-4v", timeout: int = 60):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._http = requests.Session()

    def analyze_video(self, video_path: Path, defect_title: str = "") -> Optional[str]:
        """分析单个视频，返回文本描述。

        Args:
            video_path: 视频文件路径
            defect_title: 缺陷标题（用于上下文）

        Returns:
            视觉分析文本描述
        """
        frames = _extract_keyframes(video_path, max_frames=6)
        if not frames:
            return None

        # glm-4v 系列单次只支持1张图片，逐帧分析后汇总
        frame_analyses = []
        total_sec = _get_video_duration(video_path)
        for i, frame_bytes in enumerate(frames):
            timestamp = f"{int(total_sec * i / len(frames)) // 60}:{int(total_sec * i / len(frames)) % 60:02d}" if total_sec > 0 else f"帧{i+1}"
            prompt = f"""这是扫地机器人测试视频的关键帧 [{timestamp}]。
缺陷标题: {defect_title}
请描述：1.机器人行为(清扫/转圈/碰撞/停机) 2.周围地面类型(地板/毛毯/地毯) 3.机器人姿态和运动方向 4.任何异常现象"""

            contents = [
                {"type": "image_url", "image_url": {"url": _encode_image_to_base64(frame_bytes)}},
                {"type": "text", "text": prompt},
            ]
            messages = [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {"role": "user", "content": contents},
            ]
            payload = {
                "model": self._model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 500,
            }
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json; charset=utf-8",
            }
            url = f"{self._base_url}/chat/completions"
            try:
                resp = self._http.post(
                    url, headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self._timeout,
                )
                if resp.status_code == 200:
                    content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                    frame_analyses.append(f"[{timestamp}] {content}")
                else:
                    logger.warning("GLM-4V 帧%d HTTP %d: %s", i, resp.status_code, resp.text[:200])
            except Exception as e:
                logger.error("GLM-4V 帧%d 调用失败: %s", i, e)

        if not frame_analyses:
            return None

        return f"视频分析（共{len(frames)}帧，时长约{int(total_sec)}秒）:\n" + "\n".join(frame_analyses)

    def analyze_image(self, image_path: Path, defect_title: str = "") -> Optional[str]:
        """分析单张图片，返回文本描述。

        Args:
            image_path: 图片文件路径
            defect_title: 缺陷标题（用于上下文）

        Returns:
            视觉分析文本描述
        """
        try:
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            if not image_bytes:
                logger.warning("图片为空: %s", image_path)
                return None
        except Exception as e:
            logger.error("读取图片失败: %s - %s", image_path, e)
            return None

        b64 = _encode_image_to_base64(image_bytes)
        contents = [
            {"type": "image_url", "image_url": {"url": b64}},
        ]

        prompt = f"""请分析以下图片，该图片来自一个扫地机器人测试场景。

缺陷标题: {defect_title}

请描述：
1. 图片中展示了什么场景？
2. 扫地机器人的位置和姿态
3. 周围是否有障碍物？类型和距离
4. 是否有异常现象（碰撞痕迹、卡困、外观损伤等）
5. 任何有助于判断缺陷原因的细节"""

        contents.append({"type": "text", "text": prompt})

        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": contents},
        ]

        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 2000,
        }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

        url = f"{self._base_url}/chat/completions"
        logger.info("调用 GLM-4V 分析图片 %s", image_path.name)

        try:
            resp = self._http.post(
                url, headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                logger.warning("GLM-4V HTTP %d: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            logger.info("GLM-4V 完成: prompt=%s, completion=%s",
                        usage.get("prompt_tokens"), usage.get("completion_tokens"))
            return content
        except Exception as e:
            logger.error("GLM-4V 调用失败: %s", e)
            return None

    def analyze_images(self, image_paths: List[Path], defect_title: str = "") -> dict:
        """批量分析多张图片。

        Returns:
            {image_name: analysis_text}
        """
        results = {}
        for ip in image_paths:
            analysis = self.analyze_image(ip, defect_title)
            results[ip.name] = analysis
        return results

    def analyze_videos(self, video_paths: List[Path], defect_title: str = "") -> dict:
        """批量分析多个视频。

        Returns:
            {video_name: analysis_text}
        """
        results = {}
        for vp in video_paths:
            analysis = self.analyze_video(vp, defect_title)
            results[vp.name] = analysis
        return results

    def close(self):
        self._http.close()
