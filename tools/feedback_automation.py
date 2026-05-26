#!/usr/bin/env python3
"""
知识库反馈闭环自动化工具

扫描 knowledge_feedback.yaml 和批量分析结果，自动提取高频 rejected 原因，
生成 sweeper_knowledge_base.yaml 的补丁建议。

用法:
    python tools/feedback_automation.py
    # 自动读取 data/knowledge_feedback.yaml
    # 扫描 data/batch_analysis_results.json（如存在）
    # 生成 data/knowledge_base_patch.yaml

依赖:
    pip install pyyaml
"""
import json
import logging
import re
from collections import Counter
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

FEEDBACK_FILE = Path("data/knowledge_feedback.yaml")
SOURCE_KB = Path("data/sweeper_knowledge_base.yaml")
PATCH_FILE = Path("data/knowledge_base_patch.yaml")

# 批量分析结果文件路径（支持多个候选）
BATCH_RESULT_CANDIDATES = [
    Path("data/batch_analysis_results.json"),
    Path("batch_analysis_results.json"),
    Path("data/batch_analysis_latest.json"),
]

# 违规模式检测规则：(名称, 检测正则或函数, 类别标签)
VIOLATION_RULES = [
    (
        "line_laser_misuse",
        re.compile(
            r"线激光.*?(?:未激活|关闭|sensor\s*closed).*?(?:碰撞|避障|沿墙|根因)|"
            r"(?:碰撞|避障|沿墙).*?线激光.*?(?:未激活|关闭|sensor\s*closed)|"
            r"line\s*laser.*?(?:closed|not\s*activated).*?(?:collision|avoid|obstacle|wall)|"
            r"root_cause.*line\s*laser.*closed",
            re.IGNORECASE,
        ),
        "算法-避障",
    ),
    (
        "imu_yaw_misuse",
        re.compile(
            r"IMU\s+yaw\s*=\s*\d+.*?(?:异常|故障|根因|error)|"
            r"yaw\s*=\s*\d+.*?(?:零漂|漂移|abnormal)",
            re.IGNORECASE,
        ),
        "算法-避障",
    ),
    (
        "speculation_chain",
        re.compile(
            r"可能由于|推测|或许|大概|猜测|估计.*导致|"
            r"可能原因.*未找到直接证据",
            re.IGNORECASE,
        ),
        "通用",
    ),
]


def load_feedback() -> dict:
    """加载反馈文件。"""
    if not FEEDBACK_FILE.exists():
        logger.warning("反馈文件不存在: %s", FEEDBACK_FILE)
        return {}
    with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def find_batch_result_file() -> Path:
    """查找批量分析结果文件。"""
    for p in BATCH_RESULT_CANDIDATES:
        if p.exists():
            return p
    return None


def scan_batch_results_for_violations(batch_path: Path) -> dict:
    """扫描批量分析结果，自动检测常见违规模式。

    Returns:
        {
            "line_laser_misuse": [(defect_id, title, snippet), ...],
            "imu_yaw_misuse": [...],
            ...
        }
    """
    try:
        with open(batch_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("读取批量分析结果失败: %s", e)
        return {}

    results = data if isinstance(data, list) else data.get("results", [])
    violations = {name: [] for name, _, _ in VIOLATION_RULES}

    for item in results:
        if not isinstance(item, dict):
            continue
        defect_id = item.get("defect_id", "?")
        title = item.get("title", "")
        analysis = item.get("analysis", "")
        summary = item.get("summary", "")
        text_to_scan = f"{title}\n{analysis}\n{summary}"

        for rule_name, pattern, cat_label in VIOLATION_RULES:
            if pattern.search(text_to_scan):
                snippet = analysis[:300].replace("\n", " ") if analysis else ""
                violations[rule_name].append((defect_id, title, snippet))

    return violations


def analyze_rejected(feedback: dict) -> dict:
    """分析 rejected 条目，提取高频错误模式。"""
    rejected = feedback.get("rejected", [])
    if not rejected:
        return {}

    # 统计 error_pattern
    patterns = Counter()
    categories = Counter()
    lessons = []

    for item in rejected:
        ep = item.get("error_pattern", "")
        cat = item.get("category", "未知")
        lesson = item.get("lesson", "")

        if ep:
            # 简化 error_pattern，去掉具体细节
            simplified = re.sub(r"[：:].*", "", ep).strip()
            patterns[simplified] += 1
        if cat:
            categories[cat] += 1
        if lesson:
            lessons.append(lesson)

    return {
        "total_rejected": len(rejected),
        "top_error_patterns": patterns.most_common(10),
        "top_categories": categories.most_common(10),
        "lessons": lessons,
    }


def generate_patch(analysis: dict, source_kb: dict,
                   batch_violations: dict = None) -> dict:
    """根据分析结果生成知识库补丁。"""
    patch = {
        "metadata": {
            "generated_by": "feedback_automation",
            "rejected_count": analysis.get("total_rejected", 0),
        },
        "additions": {},
        "modifications": {},
    }

    # 1. 从高频错误模式生成 common_mistakes 补丁
    new_mistakes = []
    for pattern, count in analysis.get("top_error_patterns", []):
        if count >= 2:  # 出现 2 次以上的错误模式才加入
            # 查找对应的 lesson
            lesson = ""
            for l in analysis.get("lessons", []):
                if pattern in l or l in pattern:
                    lesson = l
                    break
            new_mistakes.append({
                "mistake": f"[自动化] {pattern}",
                "why_wrong": f"基于 {count} 次反馈确认的常见错误模式",
                "correct_approach": lesson or "请严格遵循分析原则，禁止偏离缺陷标题",
                "auto_generated": True,
            })

    # 从批量分析结果中的违规模式补充 common_mistakes
    if batch_violations:
        for rule_name, items in batch_violations.items():
            if not items:
                continue
            if rule_name == "line_laser_misuse":
                new_mistakes.append({
                    "mistake": "[自动化-批量扫描] 未论证场景就将'line laser: sensor closed'作为碰撞/避障根因",
                    "why_wrong": f"基于批量分析结果扫描，发现 {len(items)} 条分析存在此违规",
                    "correct_approach": "必须先论证当前场景是否需要线激光开启，无法论证时完全忽略该日志",
                    "auto_generated": True,
                    "sample_defects": [f"{did}:{t}" for did, t, _ in items[:3]],
                })
            elif rule_name == "imu_yaw_misuse":
                new_mistakes.append({
                    "mistake": "[自动化-批量扫描] 将IMU yaw累积值误判为传感器异常",
                    "why_wrong": f"基于批量分析结果扫描，发现 {len(items)} 条分析存在此违规",
                    "correct_approach": "yaw累积值是清扫旋转的正常现象，静止时快速变化才是零漂",
                    "auto_generated": True,
                    "sample_defects": [f"{did}:{t}" for did, t, _ in items[:3]],
                })
            elif rule_name == "speculation_chain":
                new_mistakes.append({
                    "mistake": "[自动化-批量扫描] 分析中存在无证据的推测性因果链",
                    "why_wrong": f"基于批量分析结果扫描，发现 {len(items)} 条分析存在此违规",
                    "correct_approach": "无直接证据时必须声明'未找到直接证据'，禁止编造推测链",
                    "auto_generated": True,
                })

    if new_mistakes:
        patch["additions"]["common_mistakes"] = new_mistakes

    # 2. 从高频错误类别生成 banned_evidence 补丁
    banned_map = {
        "偏离主题": ["分析必须与缺陷标题一致，禁止偏离主题讨论无关模块"],
        "误读传感器状态": ["禁止将'sensor closed'直接作为缺陷证据，必须先论证场景需求"],
        "过度关联": ["禁止将两个可能无关的日志异常强行关联到缺陷现象"],
        "证据不足": ["每个结论必须有直接日志证据支撑，证据不足时应明确说明"],
        "IMU": ["禁止将IMU yaw累积值作为异常证据"],
        "线激光": ["禁止未论证场景就将'line laser: sensor closed'作为缺陷证据"],
    }

    for pattern, count in analysis.get("top_error_patterns", []):
        for key, bans in banned_map.items():
            if key in pattern:
                patch["modifications"][f"banned_evidence_{key}"] = {
                    "action": "add",
                    "values": bans,
                    "confidence": f"基于 {count} 次反馈",
                }

    # 批量扫描的 banned_evidence（更具体）
    if batch_violations and batch_violations.get("line_laser_misuse"):
        count = len(batch_violations["line_laser_misuse"])
        patch["modifications"]["banned_evidence_line_laser_auto"] = {
            "action": "add",
            "values": [
                "禁止未论证场景就将'line laser: sensor closed'作为避障/碰撞缺陷的证据",
                "禁止将时间跨度超过1分钟的'sensor closed'与碰撞事件强行关联",
            ],
            "confidence": f"基于批量扫描 {count} 条分析结果自动提取",
            "target_categories": ["算法", "算法-避障"],
        }

    # 3. 分析关键词映射优化建议
    cat_keywords = {}
    for cat, count in analysis.get("top_categories", []):
        if count >= 2:
            cat_keywords[cat] = {
                "note": f"该类别有 {count} 次被拒绝的分析，建议加强关键词匹配和 banned_evidence",
            }
    # 补充批量扫描的类别建议
    if batch_violations and batch_violations.get("line_laser_misuse"):
        cat_keywords.setdefault("算法-避障", {})
        cat_keywords["算法-避障"]["line_laser_violations"] = len(batch_violations["line_laser_misuse"])
    if cat_keywords:
        patch["modifications"]["category_warnings"] = cat_keywords

    return patch


def apply_patch_to_kb(source_kb: dict, patch: dict) -> dict:
    """将补丁应用到知识库（返回更新后的知识库副本）。"""
    kb = dict(source_kb)

    # 添加新的 common_mistakes
    additions = patch.get("additions", {})
    if "common_mistakes" in additions:
        existing = kb.setdefault("common_mistakes", [])
        existing_mistakes = {m.get("mistake", "") for m in existing}
        for new_m in additions["common_mistakes"]:
            if new_m["mistake"] not in existing_mistakes:
                existing.append(new_m)
                logger.info("新增 common_mistakes: %s", new_m["mistake"])

    # 修改 analysis_keywords 中的 banned_evidence
    modifications = patch.get("modifications", {})
    keywords = kb.get("analysis_keywords", {})
    for key, mod in modifications.items():
        if key.startswith("banned_evidence_"):
            target_cats = mod.get("target_categories")
            # 如果有指定目标类别，只给目标类别添加；否则全部添加
            if target_cats:
                for cat_name, cat_data in keywords.items():
                    if any(cat_name.startswith(t) or t.startswith(cat_name) for t in target_cats):
                        be = cat_data.setdefault("banned_evidence", [])
                        for val in mod.get("values", []):
                            if val not in be:
                                be.append(val)
                logger.info("新增 banned_evidence (%s) 到目标类别 %s: %s",
                            key, target_cats, mod.get("values", []))
            else:
                # 为所有 analysis_keywords 添加 banned_evidence
                for cat_data in keywords.values():
                    be = cat_data.setdefault("banned_evidence", [])
                    for val in mod.get("values", []):
                        if val not in be:
                            be.append(val)
                logger.info("新增 banned_evidence (%s): %s", key, mod.get("values", []))

    return kb


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    feedback = load_feedback()
    analysis = analyze_rejected(feedback) if feedback else {}

    # 扫描批量分析结果
    batch_path = find_batch_result_file()
    batch_violations = {}
    if batch_path:
        logger.info("扫描批量分析结果: %s", batch_path)
        batch_violations = scan_batch_results_for_violations(batch_path)
        for rule_name, items in batch_violations.items():
            if items:
                logger.info("  发现 %s: %d 条", rule_name, len(items))
    else:
        logger.info("未找到批量分析结果文件，跳过自动扫描")

    if not analysis and not batch_violations:
        logger.info("无反馈数据且无批量扫描结果，退出")
        return

    logger.info("分析结果: %d 条 rejected, 前3错误模式: %s",
                analysis.get("total_rejected", 0),
                ", ".join(f"{p}({c})" for p, c in analysis.get("top_error_patterns", [])[:3]))

    # 加载源知识库
    source_kb = {}
    if SOURCE_KB.exists():
        with open(SOURCE_KB, "r", encoding="utf-8") as f:
            source_kb = yaml.safe_load(f) or {}

    patch = generate_patch(analysis, source_kb, batch_violations=batch_violations)

    # 保存补丁文件
    with open(PATCH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(patch, f, allow_unicode=True, sort_keys=False)
    logger.info("补丁已保存到 %s", PATCH_FILE)

    # 询问是否直接应用补丁
    updated_kb = apply_patch_to_kb(source_kb, patch)
    with open(SOURCE_KB, "w", encoding="utf-8") as f:
        yaml.dump(updated_kb, f, allow_unicode=True, sort_keys=False)
    logger.info("补丁已自动应用到 %s", SOURCE_KB)


if __name__ == "__main__":
    main()
