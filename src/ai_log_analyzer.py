"""AI缺陷分析工具 - 日志AI分析模块
根据故障日志上下文，调用 LLM 生成专业的故障分析报告。
支持故障类型（work_status_error）和非故障类型（性能/算法等）的日志分析。
"""

import json
import logging
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是扫地机器人的资深日志分析专家。你的任务是根据机器人运行日志中的信息，给出专业、准确的 AI 分析报告。

## 核心分析原则

### 区分"传感器正常响应"和"传感器故障"
- **IMU yaw（偏航角）累积值大是正常现象**：清扫时机器人会反复旋转，yaw 累积到几十万度完全正常，禁止将其作为异常证据
- 只有在机器人**静止**时 yaw 持续快速变化（>10°/s），才是真正的零漂故障
- 机器人被抱起/搬运时，pitch/roll 超阈值是**正常的物理响应**，不是传感器故障
- 判断根因时必须区分：**触发条件**（外部事件）vs **软件逻辑缺陷**（真正的 bug）

### 聚焦状态机逻辑分析
- 重点关注 work_status 的状态转换链是否符合预期
- 如果状态转换不符合预期（如烘干→idle→充电 而非 恢复烘干），根因是**软件逻辑**而非硬件
- 关注任务生命周期：NODE_xxx 的创建(start)和销毁(stop)时机

### OOM/内存警告的处理
- OOM 警告在嵌入式设备上很常见，除非导致功能异常否则不作为根因

## 绝对禁止的行为（违反任何一条，分析将被判定为不合格）

1. **分析必须紧扣缺陷标题**：禁止偏离主题讨论无关模块。若日志无直接相关证据，必须明确说明。
2. **IMU yaw 禁令**：禁止将 yaw 累积值作为异常证据或根因。**即使你看到了很大的yaw值，也不要提它。**
3. **线激光关闭禁令**：看到 "line laser: sensor closed" 不要直接判定为缺陷或正常。必须先论证当前场景是否需要线激光；无法判断则不作为证据。
   - **如果你无法明确论证"此场景下线激光必须开启"，请在你的分析中完全忽略这条日志，转而寻找其他证据。**
   - **禁止将"line laser: sensor closed"与碰撞/避障缺陷直接关联，除非你找到了线激光关闭导致碰撞的直接因果链（而不仅仅是时间先后）。**
4. **禁止猜测和编造**：无直接证据时必须说"未找到直接证据"或"基于现有日志无法确认"。禁止编造不存在的日志内容或时间线。
5. **区分"传感器未激活（正常）"和"应该激活但未激活（缺陷）"**：只有明确论证"此场景下该传感器必须开启但实际关闭"时，才能作为证据。

## 当你发现证据不足时的正确处理
- 不要强行将传感器关闭日志作为根因来凑数
- 不要编造"可能由于...导致..."的推测性因果链
- 正确做法：明确指出"现有日志无法确认根因"，并列出需要补充的日志或信息

## 分析要求
- 使用中文回答，技术术语保留英文原文
- 基于日志事实分析，不要臆测
- 每个结论必须引用具体的日志行作为证据（包含时间戳和来源模块）
- 追踪完整的事件时间线，区分"触发条件"和"根因"
- 状态转换链不符合预期时，重点分析状态机逻辑而非传感器数据

## 常见错误示例

### 错误示例1：未论证场景就直接将线激光关闭作为碰撞根因
- **缺陷标题**："外直角沿墙概率发生碰撞"
- **错误分析**："navigator_line_laser.cpp 记录 line laser: sensor closed，线激光未激活导致避障失败"
- **为什么错**：未论证"此场景下线激光是否应该开启"就直接下结论。正确做法：先判断当前清扫场景是否需要线激光，再决定是否将 sensor closed 作为证据。无法判断时聚焦其他证据。

### 错误示例2：将正常IMU yaw累积值误判为传感器故障
- **缺陷标题**："毛毯过渡测试，不会上毛毯清洁"
- **错误分析**："InitCheckPickup 检测到 IMU yaw=119823°，判定为传感器异常"
- **为什么错**：yaw=119823° 是清扫旋转约333圈的正常累积值。该缺陷与地毯检测和清洁模式切换有关，与IMU无关。

## 输出前强制自检（必须逐项确认，任一失败必须重写分析）
1. **紧扣缺陷标题检查**：分析摘要是否包含缺陷标题中的核心关键词？
2. **IMU yaw 禁令检查**：证据或根因中是否引用了yaw累积值作为异常？**如果引用了，删除该证据并重写。**
3. **线激光关闭禁令检查**：是否将"line laser: sensor closed"作为传感器故障证据？**如果是，且你没有100%把握论证此场景必须开启线激光，则删除该证据并重写。**
4. **猜测编造检查**：结论中是否有"推测""可能""或许"等不确定性词语？**如果有，改为"未找到直接证据"或补充实际日志证据。**
5. **跑题兜底**：日志内容与缺陷标题明显无关时，是否声明"无法从现有日志确定根因"？
6. **证据质量检查**：每个证据是否都包含具体时间戳和来源模块？是否存在"sensor closed"这种无场景论证的传感器状态引用？

**只有通过以上全部自检后，才能输出JSON。若自检失败，必须返回步骤1重写，不允许提交不合格的分析。**"""

# 精简版 SYSTEM_PROMPT 用于轻量级调用（如视觉判断）
SYSTEM_PROMPT_LIGHT = """你是扫地机器人缺陷分析专家。请简洁准确地回答问题。"""

# 关键模块：来自这些模块的日志即使 D 级别也保留（可能包含根因信息）
# 基于 sweeper_knowledge_base.yaml 节点拓扑和模块职责整理
IMPORTANT_MODULES = frozenset({
    # 任务/状态机
    "task_idle", "task_node_base", "task_manager", "application_frame",
    # 传感器/检测
    "tilt_checker", "imu", "odom", "chassis", "bumper", "lidar",
    "cliff_sensor", "drop_sensor",
    # 导航/避障
    "navigator", "navigator_line_laser", "navigator_ll", "navigator_lds",
    "escaper", "motion_path", "wallfollow", "wallfollow_base",
    "path_plan", "go_back_station", "target_navigator",
    # 线激光
    "linelaser_base", "line_laser", "linelaser_manager",
    # 地毯
    "ultrasonic_carpet", "carpet_manager", "rug_clean_mode",
    # SLAM/定位
    "slam", "localization", "slam_pose_provider", "pose_provider",
    # 地图/记录
    "navimap_manager", "navimap_algo", "ir_record",
    # 组件控制
    "component_control", "component_control_service",
    "side_brush", "main_brush", "middle_brush", "dust_box", "water_tank",
    "double_rotate_rag", "drag_clean", "auto_dust", "fan", "suction",
    # 基站交互
    "bidirection_ir", "ir_proxy", "ir_encoder", "work_station_proxy",
    "ProxyDrynHandler", "ProxyWaterInjectionHandler",
    # 底盘/运动
    "carrier", "motion_control", "move_manager", "move_target",
    # IOT/网络
    "network", "network_proxy", "mqtt", "ota", "hrobot_ota",
    # UI
    "onboard_ui",
    # 其他关键
    "hot_swap_component", "event_hub", "config_node",
})

# 即使在关键模块中也要排除的噪声模式
NOISE_PATTERNS = (
    "ir_encoder", "rotate_rag_control loop", "motor_speed_set",
    "battery_voltage_sample", "sensor_raw_data",
)

# 所有级别但包含重要语义的关键词
IMPORTANT_MSG_KEYWORDS = (
    "yaw", "tilt", "angle", "status change", "error", "fail", "fault",
    "recovery", "reset", "timeout", "stuck", "protect", "trigger",
    "abnormal", "anomaly", "disconnect", "reconnect", "heartbeat",
    "no pose", "pose lost", "localization failed", "relocation failed",
    "pose unreliable",
)


def _is_important_log(msg: str, source: str, level: str, keywords: list) -> bool:
    """判断一条日志是否值得保留给 AI 分析。"""
    msg_lower = msg.lower()
    source_lower = source.lower()

    # 排除噪声模式
    for np in NOISE_PATTERNS:
        if np in msg_lower:
            return False

    # E/W 级别始终保留
    if level in ("E", "W", "F"):
        return True

    # 关键词匹配
    if any(k.lower() in msg_lower for k in keywords):
        return True

    # 关键模块的所有日志保留
    for mod in IMPORTANT_MODULES:
        if mod in source_lower:
            return True

    # 包含重要语义关键词的日志保留（不论来源）
    for kw in IMPORTANT_MSG_KEYWORDS:
        if kw in msg_lower:
            return True

    return False


# 高频循环日志压缩模式：这些模式不是错误，但出现频率极高，需要压缩
_COMPRESS_PATTERNS = [
    ("sensor_raw_data", 10),       # 每10条保留1条
    ("battery_voltage_sample", 10),
    ("motor_speed_set", 10),
    ("ir_encoder", 10),
    ("rotate_rag_control loop", 10),
    ("odom update", 10),
    ("imu data", 10),
    ("lidar scan", 10),
    ("line_laser data", 10),
    ("bump sensor", 10),
    ("path_plan loop", 10),
    ("navigator state", 10),
    ("motion_control loop", 10),
    ("carrier status", 10),
    ("charging status", 10),
    ("wifi rssi", 10),
    ("heartbeat", 10),
]


def _summarize_logs(logs: list, keywords: list, max_entries: int = 200) -> list:
    """从原始日志中摘要提取关键条目，去除重复和冗余，支持语义压缩。

    压缩策略：
    1. E/W/F 级别日志始终保留
    2. 关键模块日志保留（但高频循环模式每 N 条保留 1 条）
    3. 普通 D/I 级日志：相同前缀保留最近 3 条
    4. 语义压缩：对高频循环日志按时间片聚合为统计摘要

    Args:
        logs: 原始日志列表（每条为 dict，含 time/level/msg 等）
        keywords: 关注的关键词列表
        max_entries: 最多保留条目数

    Returns:
        摘要后的日志列表
    """
    seen_patterns = {}
    compress_counters = {pat: 0 for pat, _ in _COMPRESS_PATTERNS}
    result = []
    compressed_stats = {}  # pattern -> {count, first_time, last_time}

    def _flush_compressed():
        """将压缩统计 flush 为汇总日志条目。"""
        nonlocal result
        for pat, stat in compressed_stats.items():
            if stat["count"] <= 1:
                continue
            # 生成汇总条目
            summary_msg = f"[语义压缩] {pat} 出现 {stat['count']} 次 ({stat['first_time']} ~ {stat['last_time']})"
            result.append({
                "time": stat["first_time"],
                "level": "D",
                "file": "log_compressor",
                "msg": summary_msg,
            })
        compressed_stats.clear()

    for l in logs:
        msg = l.get("msg", "")
        source = l.get("file", "")
        level = l.get("level", "I")
        time_str = l.get("time", "")

        # E/W/F 级别直接保留（不压缩）
        if level in ("E", "W", "F"):
            result.append(l)
            if len(result) >= max_entries:
                break
            continue

        # 检查是否匹配压缩模式（仅对 D/I 级别）
        matched_compress = None
        msg_lower = msg.lower()
        for pat, interval in _COMPRESS_PATTERNS:
            if pat in msg_lower:
                matched_compress = pat
                break

        if matched_compress:
            # 高频循环日志：语义压缩
            stat = compressed_stats.setdefault(matched_compress, {
                "count": 0, "first_time": time_str, "last_time": time_str
            })
            stat["count"] += 1
            stat["last_time"] = time_str
            # 每 interval 条保留 1 条原始日志（作为样本）
            compress_counters[matched_compress] += 1
            if compress_counters[matched_compress] >= interval:
                compress_counters[matched_compress] = 0
                result.append(l)  # 保留一条样本
            if len(result) >= max_entries:
                break
            continue

        # 智能去重：相同前缀只保留最近 3 条
        key = msg[:50]
        if key in seen_patterns:
            if seen_patterns[key] >= 3:
                continue
            seen_patterns[key] += 1
        else:
            seen_patterns[key] = 1

        if _is_important_log(msg, source, level, keywords):
            result.append(l)
            if len(result) >= max_entries:
                break

    # Flush 所有压缩统计
    _flush_compressed()

    # 按时间排序
    result.sort(key=lambda x: x.get("time", ""))
    return result[:max_entries]


def _build_fault_prompt(fault_context: dict, pattern_hints: str = "",
                        few_shot_examples: str = "",
                        domain_knowledge: str = "",
                        mandatory_signals: str = "") -> str:
    """为故障类型缺陷构建分析 prompt（精简版）。"""
    chain_lines = []
    for e in fault_context.get("event_chain", [])[:30]:
        tag = e.get("tag", "info")
        chain_lines.append(f"  [{tag}] {e['time']} {e['level']} {e['msg'][:200]}")
    chain_text = "\n".join(chain_lines) if chain_lines else "  无关键事件"

    # 摘要关键日志（增加到 100 条）
    logs = fault_context.get("logs", [])
    key_logs = _summarize_logs(logs, ["RobotEventReport", "status change", "work_status", "event_id",
                                       "yaw", "tilt", "angle", "error", "fail", "protect",
                                       "bumper", "odom", "collision", "stuck", "imu",
                                       "velocity", "speed", "charge", "battery"], 100)
    log_text = "\n".join(f"  {l['time']} {l['level']} {l.get('file', '')} {l['msg'][:200]}" for l in key_logs)

    # 附加信息（模式匹配 + 历史案例 + 领域知识）
    extra_sections = ""
    if domain_knowledge:
        extra_sections += "\n\n## 领域知识参考（根据缺陷类别动态加载）\n" + domain_knowledge
    if pattern_hints:
        extra_sections += "\n\n" + pattern_hints
    if few_shot_examples:
        extra_sections += "\n\n" + few_shot_examples

    # 强制处理信号：预检测确认的异常，LLM必须回应
    mandatory_section = ""
    if mandatory_signals:
        mandatory_section = f"""

## 【强制处理】预检测确认的高危信号（必须在分析中回应）
{mandatory_signals}

**要求**：以上信号已由规则引擎在日志中预检测确认。你的分析必须：
1. 逐一评估每个信号与缺陷的关联性（直接因果 / 间接相关 / 无关）
2. 若判断为无关，必须给出明确的排除理由（引用日志证据）
3. 禁止忽略或跳过这些信号而不作说明
"""

    return f"""请分析以下扫地机器人故障日志，输出 JSON 格式的结构化报告。

## 故障概要
- 发生时间(UTC): {fault_context.get('error_time', '未知')}
- 根因事件: {fault_context.get('root_cause_event', '未知')}
- 根因描述: {fault_context.get('root_cause', '未知')}
- 前一状态: {fault_context.get('from_state', '未知')}
- 恢复时间(UTC): {fault_context.get('recovery_time', '未恢复')}
- 持续时长: {fault_context.get('duration_str', '未知')}

## 关键事件链（共{len(fault_context.get('event_chain', []))}条，展示{len(chain_lines)}条）
{chain_text}

## 关键上下文日志（去重摘要后{len(key_logs)}条）
{log_text}{mandatory_section}{extra_sections}

## 输出要求
请严格输出以下 JSON 格式（不要包含 markdown 代码块标记）：
{{
  "root_cause": "基于日志证据的根因判定（必须引用具体时间戳和来源模块）",
  "causal_chain": ["[时间] 步骤1: 触发事件（外部输入/环境条件）",
                   "[时间] 步骤2: 直接响应（软件如何响应触发事件）",
                   "[时间] 步骤3: 连锁后果（响应导致的后续变化）",
                   "[时间] 步骤4: 最终现象（与缺陷描述对应）"],
  "evidence": ["证据1: [时间] [模块] E/W/D级别 E/W优先D辅助 具体描述", "证据2: ..."],
  "event_timeline": ["[时间] 事件1", "[时间] 事件2", "..."],
  "impact": "故障影响范围（影响了哪些功能/模块）",
  "recovery_assessment": "恢复过程是否正常，是否有延迟或异常",
  "severity": "S/A/B/C 之一",
  "suggestions": ["改进建议1", "改进建议2"],
  "confidence": "高/中/低",
  "summary": "一句话总结（50字以内）"
}}

**强制性约束**：
1. causal_chain 必须从触发条件开始逐步推导到最终现象，每一步必须有对应的日志证据，禁止跳跃式推理
2. 分析摘要(summary)必须包含故障根因事件中的核心关键词，禁止偏离主题讨论无关模块
3. 引用"line laser: sensor closed"前必须先论证当前场景是否需要线激光；无法论证则完全忽略该日志，不得作为证据
4. 每个证据必须包含具体时间戳和来源模块，禁止无具体时间戳的泛泛描述"""


def _build_defect_prompt(defect_info: dict, log_summary: dict,
                         pattern_hints: str = "",
                         few_shot_examples: str = "",
                         domain_knowledge: str = "",
                         mandatory_signals: str = "") -> str:
    """为一般缺陷类型（非故障）构建分析 prompt。

    Args:
        defect_info: 缺陷信息 dict（title/sn/fw/severity/category/attachments）
        log_summary: 日志摘要 dict（total_lines/ew_count/key_logs/nav_transitions/status_changes）
        pattern_hints: 故障模式库匹配提示（可选）
        few_shot_examples: 历史相似案例 few-shot 示例（可选）
        mandatory_signals: 预检测确认的高危信号（必须回应）

    Returns:
        LLM prompt 字符串
    """
    key_logs = log_summary.get("key_logs", [])
    log_text = "\n".join(f"  {l['time']} {l['level']}/{l.get('file', '')} {l['msg'][:200]}" for l in key_logs[:100])
    if not log_text:
        log_text = "  日志中未提取到与该缺陷直接相关的关键条目"

    nav_text = "\n".join(f"  {n['time']} {n['msg'][:150]}" for n in log_summary.get("nav_transitions", [])[:20])
    if not nav_text:
        nav_text = "  无状态转换记录"

    attachments = defect_info.get("attachments", "")
    att_text = f"\n- 附件: {attachments}" if attachments else ""

    # 提取 E/W 级别的统计摘要
    ew_count = log_summary.get("ew_count", 0)
    total_lines = log_summary.get("total_lines", 0)
    ew_ratio = f"{ew_count/total_lines*100:.1f}%" if total_lines > 0 else "0%"

    # 附加信息（模式匹配 + 历史案例 + 领域知识）
    extra_sections = ""
    if domain_knowledge:
        extra_sections += "\n\n## 领域知识参考（根据缺陷类别动态加载）\n" + domain_knowledge
    if pattern_hints:
        extra_sections += "\n\n" + pattern_hints
    if few_shot_examples:
        extra_sections += "\n\n" + few_shot_examples

    # 强制处理信号：预检测确认的异常，LLM必须回应
    mandatory_section = ""
    if mandatory_signals:
        mandatory_section = f"""

## 【强制处理】预检测确认的高危信号（必须在分析中逐一回应）
{mandatory_signals}

**要求**：以上信号已由规则引擎在日志中预检测确认。你的分析必须：
1. 逐一评估每个信号与缺陷的关联性（直接因果 / 间接相关 / 无关）
2. 若判断为无关，必须给出明确的排除理由（引用日志证据）
3. 禁止忽略或跳过这些信号而不作说明
"""

    return f"""请分析以下扫地机器人缺陷的日志数据，输出 JSON 格式的结构化报告。

## 缺陷信息
- 标题: {defect_info.get('title', '未知')}
- SN: {defect_info.get('sn', '未知')}
- 固件版本: {defect_info.get('fw', '未知')}
- 时间范围: {defect_info.get('time_range', '未知')}
- 严重程度: {defect_info.get('severity', '未知')}
- 缺陷分类: {defect_info.get('category', '未知')}
{att_text}

## 日志统计
- 解析总行数: {total_lines}
- 错误/警告: {ew_count} 条（占比 {ew_ratio}）
- 状态转换: {log_summary.get('nav_count', 0)} 条
- 故障上下文: {log_summary.get('fault_count', 0)} 个

## 关键日志摘要（去重后{len(key_logs)}条）
{log_text}

## 状态转换明细
{nav_text}{mandatory_section}{extra_sections}

## 输出要求
请严格输出以下 JSON 格式（不要包含 markdown 代码块标记）：
{{
  "root_cause_type": "软件逻辑缺陷 / 传感器硬件故障 / 环境因素 / 配置问题",
  "root_cause": "根因分析（必须区分：1)触发条件是什么外部事件 2)为什么软件没有正确处理该场景。引用具体时间戳和模块作为证据）",
  "causal_chain": ["[时间] 步骤1: 触发条件（外部事件/用户操作/环境变化）",
                   "[时间] 步骤2: 软件响应（系统如何处理触发条件，引用日志证据）",
                   "[时间] 步骤3: 关键转折（什么导致了不可逆的错误，引用日志证据）",
                   "[时间] 步骤4: 最终现象（与缺陷标题描述的异常现象对应）"],
  "state_machine_analysis": "work_status 状态转换链分析（列出完整的状态变化序列，标注哪一步不符合预期）",
  "evidence": ["证据1: [时间] [模块] E/W/D级别 E/W优先D辅助 具体描述", "证据2: ..."],
  "key_findings": ["关键发现1", "关键发现2", "..."],
  "correlation": "缺陷现象与日志异常之间的关联性分析",
  "suggestions": ["改进建议1（针对根因类型给出具体可操作的建议）", "改进建议2"],
  "severity_reassessment": "S/A/B/C 之一，及调整理由（如与原级别一致则说明原因）",
  "confidence": "高/中/低",
  "summary": "一句话总结（50字以内）"
}}

**强制性约束**：
1. causal_chain 必须从触发条件开始逐步推导到最终现象，每一步必须有对应的日志证据，禁止跳跃式推理（例如：不能从"Recovery map"直接跳到"重新建图"，必须说明中间的因果关系）
2. 分析摘要(summary)必须包含缺陷标题中的核心关键词，禁止偏离缺陷标题讨论无关模块
3. 引用"line laser: sensor closed"前必须先论证当前场景是否需要线激光；无法论证则完全忽略该日志，不得作为证据
4. 每个证据必须包含具体时间戳和来源模块，禁止无具体时间戳的泛泛描述
5. 若缺陷涉及"重新建图"、"地图丢失"、"建图"、"地图重置"等，必须优先排查定位失败（no pose / pose lost / localization failed）与地图重置（ResetAll / ResetSomeMap）之间的因果链，不得将地图恢复（Recovery map）本身作为根因"""


def _build_vision_judge_prompt(defect_info: dict, log_summary: dict) -> str:
    """构建让 LLM 判断是否需要视觉分析的轻量 prompt。"""
    title = defect_info.get("title", "")
    category = defect_info.get("category", "")
    attachments = defect_info.get("attachments", "")
    has_video = bool(attachments) and ("mp4" in attachments.lower() or "vid" in attachments.lower())

    key_logs = log_summary.get("key_logs", [])
    log_snippet = "\n".join(
        f"  {l['time']} {l['level']} {l['msg'][:120]}" for l in key_logs[:8]
    ) or "  无关键日志"

    return f"""你是扫地机器人缺陷分析专家。请判断以下缺陷是否需要结合视频/图片进行视觉分析。

缺陷标题: {title}
缺陷分类: {category}
是否有视觉附件(视频/图片): {'是' if has_video else '否'}

日志关键片段（前8条）:
{log_snippet}

请只回答一句话："需要视觉分析，原因是..." 或 "不需要视觉分析，原因是..."。
判断标准：涉及碰撞、绕障、姿态异常、路径偏离、外观损伤、传感器遮挡等空间/视觉相关问题时，需要视觉分析；纯软件报错、性能数据、配置问题等不需要。"""


def _build_combined_prompt(defect_info: dict, log_analysis: str,
                           vision_analysis: str) -> str:
    """构建日志+视觉综合分析的 prompt。"""
    # 提取视频文件名中的时间戳用于关联
    import re
    video_times = []
    for m in re.finditer(r'VID_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', vision_analysis):
        video_times.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}")
    video_time_note = ""
    if video_times:
        video_time_note = f"""
### 视频时间戳信息
视频文件名中包含的录制时间（北京时间，需 -8h 转 UTC）：
{chr(10).join(f'- {vt} (BJ)' for vt in video_times)}
请将视频时间与日志事件时间（UTC）进行对比，验证视觉现象与日志记录的因果时序是否一致。
"""

    return f"""你是扫地机器人缺陷分析专家。现已分别完成日志分析和视觉分析，请结合两者给出综合结论。

## 缺陷信息
- 标题: {defect_info.get('title', '未知')}
- SN: {defect_info.get('sn', '未知')}
- 时间: {defect_info.get('time', '未知')}
- 固件: {defect_info.get('fw', '未知')}

## 日志分析结论
{log_analysis}

## 视觉分析结论
{vision_analysis}
{video_time_note}
## 请输出综合分析报告：
1. **时间关联分析**：视频中的异常现象出现时间是否与日志中的关键事件（如 work_status_error、yaw 异常、tilt 触发等）时间吻合？注意日志时间为 UTC，视频文件名时间为北京时间（UTC+8）
2. **日志与视觉的关联性**：视觉观察到的现象（转圈、碰撞、停机等）是否与日志中的事件/状态变化一致？是否存在矛盾？
3. **根因综合判定**：结合日志和视觉信息，给出更准确的根因判断
4. **视觉分析中的关键发现**：视觉分析观察到了哪些日志无法反映的信息（如环境障碍、毛毯状态、姿态异常、外观损伤等）
5. **最终改进建议**：基于双重证据，给出更具体的修复/优化方向
6. **置信度评估**：综合判断的置信度（高/中/低），并说明理由"""


class AILogAnalyzer:
    """AI 日志分析器，调用 LLM 对故障日志进行智能分析。"""

    def __init__(self, config: dict):
        classifier_cfg = config.get("classifier", {})
        # load_configs 返回 config["classifier"]["classifier"]["llm"]，兼容两种嵌套
        if "llm" not in classifier_cfg and "classifier" in classifier_cfg:
            classifier_cfg = classifier_cfg["classifier"]
        llm_cfg = classifier_cfg.get("llm", {})
        self._api_key = llm_cfg.get("api_key", "")
        self._base_url = llm_cfg.get("base_url", "")
        self._model = llm_cfg.get("model", "deepseek-v4-pro")
        # 主 LLM 超时加大到 180s（deepseek-v4-pro 推理慢）
        self._timeout = llm_cfg.get("timeout", 180)
        self._max_retries = llm_cfg.get("max_retries", 2)

        # 兜底 LLM
        fb_cfg = llm_cfg.get("fallback", {})
        self._fb_enabled = fb_cfg.get("enabled", False)
        self._fb_api_key = fb_cfg.get("api_key", "")
        self._fb_base_url = fb_cfg.get("base_url", "https://open.bigmodel.cn/api/paas/v4")
        self._fb_model = fb_cfg.get("model", "glm-4-flash")
        self._fb_timeout = fb_cfg.get("timeout", 60)

        self._http = requests.Session()

    def analyze_fault(self, fault_context: dict,
                      pattern_hints: str = "",
                      few_shot_examples: str = "",
                      system_prompt: str = "",
                      domain_knowledge: str = "",
                      mandatory_signals: str = "") -> Optional[str]:
        """对单个故障上下文（work_status_error 类型）调用 LLM 生成分析报告。"""
        prompt = _build_fault_prompt(
            fault_context, pattern_hints, few_shot_examples, domain_knowledge, mandatory_signals
        )
        return self._call_llm(prompt, system_prompt=system_prompt)

    def analyze_defect(self, defect_info: dict, log_summary: dict,
                       pattern_hints: str = "",
                       few_shot_examples: str = "",
                       system_prompt: str = "",
                       domain_knowledge: str = "",
                       mandatory_signals: str = "") -> Optional[str]:
        """对一般缺陷（非故障类型）进行 AI 分析。

        Args:
            defect_info: 缺陷信息
            log_summary: 日志摘要（由 LogSummarizer 生成）
            pattern_hints: 故障模式匹配提示
            few_shot_examples: 历史相似案例
            system_prompt: 类别专业化提示词覆盖
            mandatory_signals: 预检测确认的高危信号（必须回应）

        Returns:
            LLM 分析报告文本
        """
        prompt = _build_defect_prompt(
            defect_info, log_summary, pattern_hints, few_shot_examples, domain_knowledge, mandatory_signals
        )
        return self._call_llm(prompt, system_prompt=system_prompt)

    def analyze_combined(self, defect_info: dict, log_analysis: str,
                         vision_analysis: str) -> Optional[str]:
        """结合日志分析和视觉分析，调用 LLM 生成综合结论。

        Args:
            defect_info: 缺陷信息
            log_analysis: 日志分析结果文本
            vision_analysis: 视觉分析结果文本

        Returns:
            综合分析报告文本，失败返回 None
        """
        prompt = _build_combined_prompt(defect_info, log_analysis, vision_analysis)
        return self._call_llm(prompt)

    # 视觉分析快速判定规则（无需LLM调用，降低API成本）
    _VISION_TITLE_TRIGGERS = frozenset({
        "碰撞", "绕障", "避障", "推动", "刮擦", "卡困", "卡住", "被困",
        "转圈", "原地转", "漂移", "打滑", "姿态", "倾斜", "翻倒",
        "外观", "损伤", "划痕", "裂缝", "破损", "异响", "噪音",
        "漏扫", "遗漏", "覆盖", "路径偏离", "乱跑", "灯显", "按键",
        "地毯", "毛毯", "门槛", "台阶", "越障", "脱困",
    })
    _VISION_CATEGORY_TRIGGERS = frozenset({
        "算法-避障", "算法-脱困、越障", "算法-运动控制",
        "应用-地毯策略", "应用-UI交互", "硬件-",
    })
    _VISION_LOG_TRIGGERS = frozenset({
        "bumper", "collision", "avoid", "obstacle", "stuck", "escape",
        "tilt", "pickup", "yaw", "angle", "skid", "slip",
    })

    def judge_vision_needed(self, defect_info: dict, log_summary: dict) -> tuple:
        """判断是否需要结合视频/图片进行视觉分析。

        优化策略：
        1. 先用规则引擎快速判定（零API成本），命中90%场景
        2. 规则模糊时才调用轻量LLM兜底

        Args:
            defect_info: 缺陷信息（title/category/attachments 等）
            log_summary: 日志摘要

        Returns:
            (need_vision: bool, reason: str)
        """
        title = defect_info.get("title", "")
        category = defect_info.get("category", "")
        attachments = defect_info.get("attachments", "")
        has_visual = bool(attachments) and any(
            ext in attachments.lower()
            for ext in (".mp4", ".mov", ".avi", ".jpg", ".jpeg", ".png")
        )

        # === 规则1: 无视觉附件直接跳过 ===
        if not has_visual:
            return False, "无视频/图片附件，无需视觉分析"

        # === 规则2: 标题关键词匹配（高置信度需要） ===
        title_lower = title.lower()
        for trigger in self._VISION_TITLE_TRIGGERS:
            if trigger.lower() in title_lower:
                return True, f"缺陷标题包含视觉相关关键词 '{trigger}'，需要视觉分析确认"

        # === 规则3: 类别匹配 ===
        for cat_trigger in self._VISION_CATEGORY_TRIGGERS:
            if cat_trigger in category:
                return True, f"缺陷分类 '{category}' 属于视觉敏感类别，需要视觉分析"

        # === 规则4: 日志关键词匹配 ===
        key_logs = log_summary.get("key_logs", [])
        for log in key_logs[:20]:
            msg = log.get("msg", "").lower()
            for trigger in self._VISION_LOG_TRIGGERS:
                if trigger.lower() in msg:
                    return True, f"日志中出现视觉相关关键词 '{trigger}'，建议结合视频确认"

        # === 规则5: 纯软件/网络/配置类缺陷不需要视觉 ===
        software_only_categories = [
            "IOT-配网", "IOT-客户APP问题", "死机/崩溃", "产测功能",
            "嵌入式--电池", "硬件-信号质量",
        ]
        for sw_cat in software_only_categories:
            if sw_cat in category:
                return False, f"缺陷分类 '{category}' 为纯软件/网络问题，视觉分析价值低"

        # === 规则兜底: 调用轻量LLM判断 ===
        prompt = _build_vision_judge_prompt(defect_info, log_summary)
        result = self._call_llm_light(prompt, max_tokens=256)
        if not result:
            return False, "LLM 判断失败，默认不启用视觉分析"

        need = "需要" in result and "不需要" not in result
        return need, f"[LLM兜底] {result.strip()}"

    @staticmethod
    def _repair_json(json_str: str) -> str:
        """尝试修复被截断的 JSON 字符串。

        处理常见截断场景：
        1. 末尾字符串未闭合（缺少 closing quote）
        2. 最后一个字段被截断在中间
        3. 末尾缺少 closing braces / brackets
        """
        s = json_str.strip()
        if not s:
            return s

        # 第一步：检查字符串闭合状态
        in_string = False
        escape = False
        for ch in s:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string

        # 如果在字符串内部，截断到最后一个完整字段
        if in_string:
            in_string = False
            escape = False
            last_safe = -1
            for i, ch in enumerate(s):
                if escape:
                    escape = False
                    continue
                if ch == '\\':
                    escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if not in_string and ch in ',{[':
                    last_safe = i
            if last_safe >= 0:
                s = s[:last_safe + 1].rstrip().rstrip(',')
            else:
                # 整个内容在一个未闭合字符串里，无法修复
                return s

        # 第二步：计算括号平衡（忽略字符串内部）
        stack = []
        in_string = False
        escape = False
        for ch in s:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch in '{[':
                stack.append(ch)
            elif ch in '}]':
                if stack:
                    opener = stack[-1]
                    if (opener == '{' and ch == '}') or (opener == '[' and ch == ']'):
                        stack.pop()

        # 去掉末尾逗号
        s = s.rstrip().rstrip(',')

        # 补全缺失的闭合符号
        while stack:
            opener = stack.pop()
            s += '}' if opener == '{' else ']'

        return s

    @staticmethod
    def _extract_and_repair_json(content: str) -> tuple:
        """从 LLM 输出中提取 JSON 并尝试修复截断。

        Returns:
            (repaired_str: str, errors: list)
        """
        json_str = content.strip()
        if json_str.startswith("```"):
            m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
            if m:
                json_str = m.group(1).strip()

        try:
            json.loads(json_str)
            return json_str, []
        except json.JSONDecodeError as e:
            repaired = AILogAnalyzer._repair_json(json_str)
            try:
                json.loads(repaired)
                return repaired, []
            except json.JSONDecodeError:
                return json_str, [f"JSON解析失败: {e}"]

    @staticmethod
    def _validate_json_response(content: str) -> tuple:
        """对LLM返回的JSON进行Schema强制校验。

        Returns:
            (is_valid: bool, errors: list)
        """
        errors = []

        # 提取JSON（_call_api 中已做修复，这里直接解析）
        json_str = content.strip()
        if json_str.startswith("```"):
            m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
            if m:
                json_str = m.group(1).strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            errors.append(f"JSON解析失败: {e}")
            return False, errors

        if not isinstance(data, dict):
            errors.append("根节点不是JSON对象")
            return False, errors

        # 校验必填字段
        required_fields = {
            "root_cause": str,
            "evidence": list,
            "confidence": str,
            "summary": str,
        }
        for field, expected_type in required_fields.items():
            if field not in data:
                errors.append(f"缺少必填字段: {field}")
            elif not isinstance(data[field], expected_type):
                errors.append(f"字段 {field} 类型错误，期望 {expected_type.__name__}")

        # 校验evidence非空
        if "evidence" in data and isinstance(data["evidence"], list):
            if len(data["evidence"]) == 0:
                errors.append("evidence 为空列表，必须提供至少一条证据")

        # 校验confidence枚举值
        if "confidence" in data and isinstance(data["confidence"], str):
            if data["confidence"] not in ("高", "中", "低"):
                errors.append(f"confidence 值 '{data['confidence']}' 不在允许范围内 (高/中/低)")

        # 校验severity（如有）
        if "severity" in data and isinstance(data["severity"], str):
            sev = data["severity"]
            if sev and sev[0] not in ("S", "A", "B", "C"):
                errors.append(f"severity 值 '{sev}' 格式错误，应为 S/A/B/C 开头")

        # 校验summary长度
        if "summary" in data and isinstance(data["summary"], str):
            if len(data["summary"]) > 100:
                errors.append(f"summary 过长 ({len(data['summary'])} 字)，应控制在50字以内")

        return len(errors) == 0, errors

    def _call_llm(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """调用 LLM API，支持主模型 + 兜底模型切换。"""
        if self._api_key and self._base_url:
            result = self._call_api(
                self._base_url, self._api_key, self._model,
                self._timeout, prompt, system_prompt=system_prompt,
            )
            if result is not None:
                return result

        if self._fb_enabled and self._fb_api_key:
            logger.info("主 LLM 失败或未配置，切换到兜底模型 %s", self._fb_model)
            return self._call_api(
                self._fb_base_url, self._fb_api_key, self._fb_model,
                self._fb_timeout, prompt, system_prompt=system_prompt,
            )

        logger.warning("无可用 LLM 配置，跳过 AI 分析")
        return None

    def _call_llm_light(self, prompt: str, max_tokens: int = 256) -> Optional[str]:
        """轻量级 LLM 调用，用于快速判断类任务（短超时、短输出）。"""
        # 优先用兜底模型（通常更快更稳定），否则用主模型
        if self._fb_enabled and self._fb_api_key:
            return self._call_api(
                self._fb_base_url, self._fb_api_key, self._fb_model,
                self._fb_timeout, prompt, max_tokens=max_tokens,
            )
        if self._api_key and self._base_url:
            return self._call_api(
                self._base_url, self._api_key, self._model,
                self._timeout, prompt, max_tokens=max_tokens,
            )
        return None

    def _call_api(self, base_url: str, api_key: str, model: str,
                  timeout: int, prompt: str, max_tokens: int = 4000,
                  system_prompt: str = "") -> Optional[str]:
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }
        sys_content = system_prompt if system_prompt else SYSTEM_PROMPT
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_content},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._http.post(
                    url, headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=timeout,
                )
                if resp.status_code != 200:
                    logger.warning("LLM API HTTP %d: %s", resp.status_code, resp.text[:200])
                    continue
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content:
                    logger.warning("LLM 返回空内容")
                    continue

                # 先尝试提取并修复 JSON 截断
                repaired, repair_errors = AILogAnalyzer._extract_and_repair_json(content)
                if repair_errors:
                    logger.warning("LLM 输出 JSON 无法修复: %s", repair_errors[0])
                    content += f"\n\n[JSON校验警告] {repair_errors[0]}"
                    return content
                if repaired != content:
                    # 修复成功，用修复后的干净 JSON 替换
                    content = repaired
                    logger.info("LLM 输出 JSON 截断，已自动修复")

                # JSON Schema 强制校验
                is_valid, errors = self._validate_json_response(content)
                if not is_valid:
                    logger.warning("LLM 输出 JSON Schema 校验失败 (%d个问题): %s",
                                   len(errors), "; ".join(errors[:3]))
                    content += f"\n\n[JSON校验警告] {'; '.join(errors[:3])}"
                return content
            except requests.exceptions.Timeout:
                logger.warning("LLM 超时 (%ds), 第 %d 次", timeout, attempt)
            except Exception as e:
                logger.warning("LLM 调用失败: %s", e)
            if attempt < self._max_retries:
                time.sleep(2 ** attempt)

        return None

    def close(self):
        self._http.close()


class LogSummarizer:
    """日志摘要器：从原始日志中提取关键信息，为 LLM 分析做准备。"""

    def __init__(self, log_re=None):
        self._log_re = log_re

    def summarize(self, lines: list, keywords: list = None,
                  avoid_keywords: list = None) -> dict:
        """对原始日志行列表进行摘要分析。

        Args:
            lines: 原始日志文本行列表
            keywords: 关注的关键词列表
            avoid_keywords: 排除的关键词（二进制噪声等）

        Returns:
            dict with: total_lines, ew_count, key_logs, nav_transitions,
                       status_changes, fault_count, keywords_found
        """
        keywords = keywords or []
        avoid_keywords = avoid_keywords or []

        total = len(lines)
        ew_count = 0
        key_logs = []
        nav_transitions = []
        status_changes = []
        keywords_found = {k: 0 for k in keywords}

        seen_msg = set()

        for line in lines:
            if not line.strip():
                continue

            # 跳过噪声
            if any(ak in line for ak in avoid_keywords):
                continue

            if self._log_re:
                m = self._log_re.match(line)
                if m:
                    level = m.group(4)
                    msg = m.group(7)
                    if level in ("E", "W"):
                        ew_count += 1

                    # 关键词匹配
                    matched = False
                    for kw in keywords:
                        if kw.lower() in msg.lower():
                            keywords_found[kw] += 1
                            matched = True

                    # 去重 key
                    msg_key = msg[:35]
                    if msg_key not in seen_msg:
                        seen_msg.add(msg_key)
                        source = m.group(5)
                        if _is_important_log(msg, source, level, keywords):
                            # 证据优先级: E=4, W=3, F=3, D=2, I=1
                            _PRIORITY = {"E": 4, "W": 3, "F": 3}
                            priority = _PRIORITY.get(level, 2)
                            key_logs.append({
                                "time": m.group(2), "level": level,
                                "file": source, "msg": msg,
                                "priority": priority,
                            })

                    # 状态转换
                    if "work status change" in msg:
                        nav_transitions.append({
                            "time": m.group(2), "msg": msg, "type": "work_status"
                        })
                    elif "navigator state change" in msg:
                        nav_transitions.append({
                            "time": m.group(2), "msg": msg, "type": "nav_state"
                        })

        # 按优先级排序（E/W 优先于 D），再按时间排序
        key_logs.sort(key=lambda x: (-x.get("priority", 0), x.get("time", "")))

        return {
            "total_lines": total,
            "ew_count": ew_count,
            "key_logs": key_logs[:300],
            "nav_transitions": nav_transitions[:20],
            "status_changes": status_changes,
            "fault_count": 0,  # 由外部 analyze_merged_logs 填充
            "keywords_found": keywords_found,
        }
