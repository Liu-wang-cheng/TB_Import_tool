#!/usr/bin/env python3
"""
PDF 流程图视觉提取工具

将 PDF 产品文档中的流程图/状态图页面渲染为图片，
调用 GLM-4V 视觉分析提取结构化规则，补充到知识库中。

用法:
    python tools/extract_pdf_flowcharts.py
    # 自动扫描 data/ 目录下的 PDF，提取流程图知识

依赖:
    pip install pymupdf requests pyyaml
"""
import json
import logging
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.vision_analyzer import _encode_image_to_base64

logger = logging.getLogger(__name__)

# 配置
PDF_DIR = Path("data")
OUTPUT_YAML = Path("data/pdf_flowchart_knowledge.yaml")
API_KEY = ""
API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-4v-flash"

# 重点关注的 PDF 文档（产品逻辑相关）
TARGET_PDFS = [
    "回充集尘宝逻辑.pdf",
    "嵌入式整体设计文档.pdf",
    "工作状态切换.pdf",
    "断点续扫流程.pdf",
    "禁区启动逻辑.pdf",
    "全局扫逻辑-包括自动分区清扫.pdf",
    "自动分区全局扫逻辑.pdf",
    "自动分区实现逻辑文档.pdf",
    "建图与定位.pdf",
]

VISION_PROMPT = """你是一名扫地机器人系统设计专家。请仔细分析这张图片中的流程图或状态图，并将其转化为结构化的文字规则。

请按以下格式输出：

## 流程名称
（图片标题或主题）

## 状态/节点列表
- 状态A: 描述
- 状态B: 描述

## 转换规则
- 从[状态A]到[状态B]: 触发条件是什么
- 从[状态B]到[状态C]: 触发条件是什么

## 异常处理
- 在[状态X]时如果[条件Y]发生: 系统的预期行为是什么

## 关键阈值/参数
（如果有具体数值，如时间、电流、距离等）

注意：
1. 只描述图片中明确展示的内容，不要推测
2. 如果图片中包含流程图，请描述完整的分支逻辑
3. 如果图片是状态转换图，请列出所有状态和转换条件
4. 如果图片包含表格，请描述表格中的关键数值规则
"""


def render_pdf_page_to_bytes(pdf_path: Path, page_num: int = 0, dpi: int = 200) -> bytes:
    """将 PDF 指定页面渲染为 PNG 图片字节。"""
    try:
        import fitz
    except ImportError:
        logger.error("PyMuPDF (fitz) 未安装，请先执行: pip install pymupdf")
        raise

    doc = fitz.open(str(pdf_path))
    if page_num >= len(doc):
        doc.close()
        raise ValueError(f"PDF 只有 {len(doc)} 页，请求第 {page_num + 1} 页")

    page = doc[page_num]
    # 使用矩阵提高分辨率
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    doc.close()
    return img_bytes


def call_vision_api(image_bytes: bytes, prompt: str = VISION_PROMPT) -> str:
    """调用 GLM-4V 视觉 API。"""
    if not API_KEY:
        logger.error("未配置 API_KEY，请设置 API_KEY 环境变量或直接修改脚本")
        return ""

    base64_image = _encode_image_to_base64(image_bytes)
    # _encode_image_to_base64 返回 data:image/jpeg;base64,... 格式
    # 但我们是 PNG，需要修正前缀
    if base64_image.startswith("data:image/jpeg"):
        base64_image = base64_image.replace("data:image/jpeg", "data:image/png")

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": base64_image}},
                ],
            }
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        import requests
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            logger.warning("Vision API HTTP %d: %s", resp.status_code, resp.text[:200])
            return ""
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content
    except Exception as e:
        logger.warning("Vision API 调用失败: %s", e)
        return ""


def _is_flowchart_page(page, images: list, text_blocks: list) -> tuple:
    """优化后的流程图页面检测启发式。

    Returns:
        (is_flowchart: bool, reason: str)
    """
    # 1. 图片面积占比（流程图通常有大面积图片）
    img_area_ratio = 0.0
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height
    for img in images:
        # img 格式: (xref, smask, width, height, bpc, colorspace, alt_colorspace, name, filter)
        if len(img) >= 4:
            img_area = img[2] * img[3]
            img_area_ratio += img_area / page_area

    # 2. 文本块分析
    text_len = sum(len(b[4]) for b in text_blocks if len(b) >= 5)
    block_count = len(text_blocks)

    # 3. 流程图关键词检测
    all_text = " ".join(b[4] for b in text_blocks if len(b) >= 5)
    flowchart_keywords = ["开始", "结束", "判断", "流程", "状态", "转换",
                          "start", "end", "process", "decision", "state",
                          "transition", "flow", "chart", "diagram"]
    keyword_score = sum(1 for kw in flowchart_keywords if kw in all_text)

    # 4. 决策规则（多维度综合判断，降低误触发）
    reasons = []

    # 高置信度：大面积图片 + 流程图关键词
    if img_area_ratio > 0.3 and keyword_score >= 2:
        reasons.append(f"大面积图片({img_area_ratio:.0%})且含流程图关键词({keyword_score}个)")
        return True, "; ".join(reasons)

    # 中置信度：大面积图片 + 文字少
    if img_area_ratio > 0.5 and text_len < 200:
        reasons.append(f"大面积图片({img_area_ratio:.0%})且文字极少({text_len}字符)")
        return True, "; ".join(reasons)

    # 中置信度：有图片 + 明显的流程图关键词
    if len(images) >= 1 and keyword_score >= 3:
        reasons.append(f"含图片({len(images)}个)且流程图关键词丰富({keyword_score}个)")
        return True, "; ".join(reasons)

    # 低置信度排除：纯文字页、无图片且文字多
    if len(images) == 0 and text_len > 500:
        return False, "纯文字页，无流程图特征"

    # 低置信度：仅有少量图片但文字很多（可能是配图文档而非流程图）
    if len(images) >= 1 and text_len > 1000 and keyword_score == 0:
        return False, f"图片少且文字过多({text_len}字符)，无流程图关键词"

    # 默认：旧版启发式兜底
    if len(images) >= 1 or block_count < 10:
        reasons.append(f"基础启发式命中(图片{len(images)}个/文本块{block_count}个)")
        return True, "; ".join(reasons)

    return False, "无流程图特征"


def extract_pdf_knowledge(pdf_path: Path, dry_run: bool = False) -> list:
    """从单个 PDF 提取流程图知识。

    Args:
        pdf_path: PDF 文件路径
        dry_run: 如果为True，只检测哪些页面会被分析，不调用API
    """
    import fitz
    doc = fitz.open(str(pdf_path))
    results = []

    for i in range(len(doc)):
        page = doc[i]
        images = page.get_images()
        text_blocks = page.get_text("blocks")

        has_diagram, reason = _is_flowchart_page(page, images, text_blocks)

        if not has_diagram:
            continue

        if dry_run:
            logger.info("[DRY-RUN] %s 第 %d 页会被分析: %s", pdf_path.name, i + 1, reason)
            results.append({
                "pdf": pdf_path.name,
                "page": i + 1,
                "analysis": f"[DRY-RUN] 触发原因: {reason}",
            })
            continue

        logger.info("分析 %s 第 %d 页...", pdf_path.name, i + 1)
        try:
            img_bytes = render_pdf_page_to_bytes(pdf_path, i)
            analysis = call_vision_api(img_bytes)
            if analysis:
                results.append({
                    "pdf": pdf_path.name,
                    "page": i + 1,
                    "analysis": analysis,
                })
        except Exception as e:
            logger.warning("分析 %s 第 %d 页失败: %s", pdf_path.name, i + 1, e)

    doc.close()
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PDF 流程图视觉提取工具")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅检测哪些页面会被分析，不调用视觉API（零成本测试）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # 优先从项目配置读取兜底LLM的API Key（与视觉分析共用智谱AI平台）
    global API_KEY
    if not API_KEY:
        try:
            from src.config_loader import load_configs
            cfg = load_configs("configs")
            classifier_cfg = cfg.get("classifier", {})
            if "llm" not in classifier_cfg and "classifier" in classifier_cfg:
                classifier_cfg = classifier_cfg["classifier"]
            llm_cfg = classifier_cfg.get("llm", {})
            fb_cfg = llm_cfg.get("fallback", {})
            API_KEY = fb_cfg.get("api_key", "")
            if API_KEY:
                logger.info("已从 configs/classifier.yaml 加载 fallback API Key")
        except Exception as e:
            logger.debug("从配置加载 API Key 失败: %s", e)

    # 其次从环境变量读取
    if not API_KEY:
        API_KEY = os.environ.get("VISION_API_KEY", "")

    if not args.dry_run and not API_KEY:
        logger.error("未配置 API_KEY。请检查以下任一方式：")
        logger.error("  1. configs/classifier.yaml 中 classifier.llm.fallback.api_key")
        logger.error("  2. 环境变量 VISION_API_KEY")
        logger.info("可使用 --dry-run 参数进行零成本测试，查看哪些页面会被分析。")
        return

    all_results = []
    for name in TARGET_PDFS:
        path = PDF_DIR / name
        if not path.exists():
            logger.warning("PDF 不存在，跳过: %s", path)
            continue
        results = extract_pdf_knowledge(path, dry_run=args.dry_run)
        all_results.extend(results)
        logger.info("%s: %s %d 页流程图知识",
                    name,
                    "[DRY-RUN] 会分析" if args.dry_run else "提取",
                    len(results))

    if not all_results:
        logger.info("未提取到任何流程图知识")
        return

    # 保存为 YAML
    import yaml
    OUTPUT_YAML.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_YAML, "w", encoding="utf-8") as f:
        yaml.dump(
            {"metadata": {"source": "PDF流程图视觉提取", "count": len(all_results),
                          "dry_run": args.dry_run},
             "flowcharts": all_results},
            f,
            allow_unicode=True,
            sort_keys=False,
        )

    if args.dry_run:
        logger.info("[DRY-RUN] 流程图知识预览已保存到 %s，共 %d 条", OUTPUT_YAML, len(all_results))
    else:
        logger.info("流程图知识已保存到 %s，共 %d 条", OUTPUT_YAML, len(all_results))


if __name__ == "__main__":
    main()
