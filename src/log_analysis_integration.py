"""AI缺陷分析工具 - TB缺陷导入后自动日志分析集成模块
缺陷导入 Teambition 后，自动：
1. 从缺陷信息提取 SN、精确时间
2. 下载对应时段前后10分钟 DRC 日志
3. AI 分析日志
4. 按需调用视觉分析（视频/图片）
5. 将分析结果写入 TB 缺陷评论
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.ai_log_analyzer import AILogAnalyzer, LogSummarizer
from src.config_loader import load_configs
from src.vision_integration import VisionIntegration
from src.fault_pattern_library import FaultPatternLibrary
from src.prompt_builder import PromptBuilder
from src.knowledge_base import KnowledgeBase
from src.knowledge_rag import KnowledgeRAG
from src.html_report_generator import generate_html_bytes
from src.models import AttachmentFile

logger = logging.getLogger(__name__)

# DRC 日志服务器配置（默认，可覆盖）
_DEFAULT_DRC_SERVER = "http://61.141.202.107:8008"
_DEFAULT_DRC_USER = "ldrobot-team"
_DEFAULT_DRC_PASS = "ldrobotlog4110"
_DEFAULT_DRC_MODEL = "CLA_HS4"
_DEFAULT_FW = "AR-0.7.277.4377-2.1.41-23662-HQ5S00700002HC261300022-7caade1501fd"

# 日志分析时间窗口：缺陷发生时间前后分钟数
LOG_WINDOW_MINUTES = 10


def _extract_sn_from_task(task: "TeambitionTask") -> Optional[str]:
    """从 TB 任务自定义字段中提取设备 SN。"""
    for cf in task.customfields:
        val = cf.get("value", "")
        if isinstance(val, list):
            val = ", ".join(v.get("title", str(v)) for v in val)
        if not val:
            continue
        s = str(val).strip()
        # HQ 格式（如 HQ5S00700002HC261300069）
        if len(s) >= 10 and (s.upper().startswith("HQ") or s[0].isdigit()):
            return s
    # 尝试从标题中提取
    m = re.search(r"HQ\S{10,}", task.content)
    if m:
        return m.group(0)
    # 尝试提取非 HQ 格式 SN（如 48HCNFBN0049X）
    m = re.search(r"\b([0-9]{2,}[A-Z]{2,}[0-9A-Z]{4,})\b", task.content, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _extract_time_from_task(task: "TeambitionTask") -> Optional[datetime]:
    """从 TB 任务中提取精确的缺陷发生时间（UTC）。

    提取策略（按优先级）：
    1. customfields 中明确标记为"缺陷时间/发生时间"的字段
    2. 标题/内容/描述（以及 customfields 文本值）中手写的时间戳
    3. 回退到任务创建时间（作为日期基准，时间用 00:00）

    注意：用户在禅道中填写的时间均为北京时间（UTC+8），
    DRC 文件名中的时间戳为 UTC，因此需要统一转换为 UTC。

    Returns:
        UTC datetime 或 None
    """
    # 收集所有待搜索文本（标题 + customfields 的 str value）
    search_parts = [task.content]
    for cf in task.customfields:
        val = cf.get("value", "")
        if isinstance(val, str):
            search_parts.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    search_parts.append(item.get("title", ""))
                    search_parts.append(item.get("metaString", ""))
                else:
                    search_parts.append(str(item))
    search_text = " ".join(search_parts)
    # 统一全角标点
    search_text = search_text.replace("：", ":").replace("／", "/").replace("，", ",").replace("-", "-")

    # 0. 从 customfield 中提取明确标记为"缺陷时间"的字段
    _TIME_FIELD_KEYWORDS = ["时间", "发生", "缺陷", "故障", "异常"]
    _EXCLUDE_TIME_KEYWORDS = ["截止", "计划", "完成", "截止日", "到期", "截至", "期限"]

    def _is_time_field(cf_title: str) -> bool:
        t = cf_title.lower()
        if any(k in t for k in _EXCLUDE_TIME_KEYWORDS):
            return False
        return any(k in t for k in _TIME_FIELD_KEYWORDS)

    for cf in task.customfields:
        if cf.get("type") != "text":
            continue
        cf_title = cf.get("title", "")
        if not _is_time_field(cf_title):
            continue
        val = cf.get("value", "")
        candidates = []
        if isinstance(val, str):
            candidates.append(val.strip())
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    candidates.append(item.get("title", "").strip())
                else:
                    candidates.append(str(item).strip())
        for t in candidates:
            m = re.match(r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)$', t)
            if m:
                try:
                    fmt = "%Y-%m-%d %H:%M:%S" if ':' in m.group(1) and m.group(1).count(':') == 2 else "%Y-%m-%d %H:%M"
                    dt = datetime.strptime(m.group(1), fmt)
                    utc_dt = (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
                    logger.info("从自定义字段[%s]提取缺陷时间: %s 北京时间 -> %s UTC", cf_title, t, utc_dt.strftime("%Y-%m-%d %H:%M"))
                    return utc_dt
                except ValueError:
                    pass

    # 1. ISO 格式（如 2026-05-24T14:08:00Z）——带 Z 或时区标记的视为 UTC
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)", search_text)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    # 以下时间均为用户手动填写的北京时间，需 -8h 转 UTC

    # 2. 标准格式 YYYY-MM-DD HH:MM:SS 或 YYYY/MM/DD HH:MM:SS
    m = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2}\s+\d{2}:\d{2}:\d{2})", search_text)
    if m:
        try:
            ts_str = m.group(1)
            sep = "/" if "/" in ts_str else "-"
            dt = datetime.strptime(ts_str, f"%Y{sep}%m{sep}%d %H:%M:%S")
            utc_dt = (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
            logger.info("从标准格式提取时间: %s 北京时间 → %s UTC", dt.strftime("%Y-%m-%d %H:%M:%S"), utc_dt.strftime("%Y-%m-%d %H:%M:%S"))
            return utc_dt
        except ValueError:
            pass

    # 3. 标准格式 YYYY-MM-DD HH:MM（无秒）
    m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", search_text)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            utc_dt = (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
            logger.info("从标准格式(无秒)提取时间: %s 北京时间 → %s UTC", dt.strftime("%Y-%m-%d %H:%M"), utc_dt.strftime("%Y-%m-%d %H:%M"))
            return utc_dt
        except ValueError:
            pass

    # 4. 简写格式 M/D-HH:MM 或 M-D HH:MM（如 5/24-14:08、5-24 14:08）
    # 也支持 M月D日 HH:MM
    patterns_short = [
        r"(\d{1,2})[/-](\d{1,2})[-\s]+(\d{2}):(\d{2})",  # 5/24-14:08  5-24 14:08
        r"(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2})",      # 5月24日 14:08
    ]
    for pat in patterns_short:
        m = re.search(pat, search_text)
        if m:
            try:
                month, day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                now = datetime.now(timezone.utc)
                dt = datetime(now.year, month, day, hour, minute, 0)
                utc_dt = (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
                logger.info("从简写格式提取时间: %s 北京时间 → %s UTC", dt.strftime("%m-%d %H:%M"), utc_dt.strftime("%m-%d %H:%M"))
                return utc_dt
            except ValueError:
                pass

    # 5. MM-DD HH:MM:SS（无年份，如 05-24 14:08:30）
    m = re.search(r"(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", search_text)
    if m:
        try:
            now = datetime.now(timezone.utc)
            dt = datetime.strptime(f"{now.year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S")
            utc_dt = (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
            logger.info("从 MM-DD 格式提取时间: %s 北京时间 → %s UTC", dt.strftime("%m-%d %H:%M:%S"), utc_dt.strftime("%m-%d %H:%M:%S"))
            return utc_dt
        except ValueError:
            pass

    # 6. 回退到创建时间（TB 创建时间是 UTC，无需转换）
    # 若创建时间存在，取其日期部分作为缺陷日期，时间设为 00:00
    if task.created:
        try:
            dt = datetime.fromisoformat(task.created.replace("Z", "+00:00"))
            created_dt = dt.astimezone(timezone.utc)
            fallback_dt = created_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            logger.info("无明确缺陷时间，回退到创建时间日期: %s", fallback_dt.strftime("%Y-%m-%d %H:%M UTC"))
            return fallback_dt
        except ValueError:
            pass

    return None


def _extract_fw_from_task(task: "TeambitionTask") -> str:
    """从 TB 任务自定义字段或标题中提取固件版本。

    返回完整的固件目录名（如 AR-0.7.281.4381-2.1.42-...），
    用于拼接 DRC 服务器路径。若无法提取则返回空字符串，
    由 _fetch_and_analyze_logs 自动探测。
    """
    short_ver = ""
    # 1. 优先从 DRC 文件名中提取完整固件版本
    for cf in task.customfields:
        val = cf.get("value", "")
        cf_title = cf.get("title", "")
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    item_title = item.get("title", "")
                    if "record_" in item_title and ".drc" in item_title:
                        m = re.search(r"AR-[\d.]+-[\d.]+-\d+-\w+-[\da-f]+", item_title)
                        if m:
                            return m.group(0)
                    # 收集简短版本号（如 2.1.42）用于后续匹配
                    if re.match(r"\d+\.\d+\.\d+", item_title):
                        short_ver = item_title
        if isinstance(val, str) and "record_" in val:
            m = re.search(r"AR-[\d.]+-[\d.]+-\d+-\w+-[\da-f]+", val)
            if m:
                return m.group(0)
        # 从自定义字段 title/value 中提取简短版本号
        if re.match(r"\d+\.\d+\.\d+", str(val)) and len(str(val)) < 20:
            short_ver = str(val)
        if "版本" in cf_title or "固件" in cf_title:
            if isinstance(val, str) and re.match(r"\d+\.\d+\.\d+", val):
                short_ver = val
            elif isinstance(val, list) and val:
                v = val[0].get("title", "") if isinstance(val[0], dict) else str(val[0])
                if re.match(r"\d+\.\d+\.\d+", v):
                    short_ver = v
        # title 为空时，也从 value 的 item title 中提取版本号
        if not short_ver and isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    t = item.get("title", "")
                    if re.match(r"\d+\.\d+\.\d+", t) and len(t) < 20:
                        short_ver = t
                        break

    # 2. 从任务备注中提取版本号（如 "版本: 2.1.42"）
    if not short_ver and task.note:
        m = re.search(r'版本[:\s]*(\d+\.\d+\.\d+)', task.note)
        if m:
            short_ver = m.group(1)
            logger.info("从任务备注提取版本: %s", short_ver)

    # 3. 尝试从标题推断完整固件版本
    m = re.search(r"AR-[\d.]+-[\d.]+-\d+-\w+-[\da-f]+", task.content)
    if m:
        return m.group(0)

    # 3. 尝试从标题推断完整固件版本
    m = re.search(r"AR-[\d.]+-[\d.]+-\d+-\w+-[\da-f]+", task.content)
    if m:
        return m.group(0)

    # 4. 尝试从标题中提取简短版本号（如 2.1.42, V2.1.42）
    if not short_ver:
        m = re.search(r'(?:V|v|版本|固件)[:\s]*(\d+\.\d+\.\d+)', task.content)
        if m:
            short_ver = m.group(1)
        else:
            # 排除 IP 地址（4段），只匹配 3 段的版本号
            for m2 in re.finditer(r'(?<!\d)(\d+\.\d+\.\d+)(?!\.\d)', task.content):
                short_ver = m2.group(1)
                break

    # 5. 返回简短版本号，_fetch_and_analyze_logs 会用它匹配服务器上的 FW 目录
    return short_ver


def _infer_category_from_title(title: str) -> str:
    """从缺陷标题推断分类（当customfields中无分类时使用）。

    返回最匹配的类别前缀，供PromptBuilder做前缀匹配。
    匹配顺序：越具体的分类越优先。
    与 classifier.yaml 分类体系对齐。
    """
    t = title.lower()

    # 1. APP端（最具体，直接涉及APP界面/弹框/页面）
    if any(k in t for k in ["app", "弹框", "弹窗", "页面", "返回", "ui", "界面", "显示", "提示", "闪退"]):
        return "IOT-客户APP问题"

    # 2. IOT-配网/连接/离线/OTA/语音包
    if any(k in t for k in ["配网", "离线", "断连", "语音包", "连接失败", "reconnect", "网络连接"]):
        return "IOT-配网、连接、离线、OTA、语音包"

    # 3. IOT-OTA升级（单独优先级）
    if any(k in t for k in ["ota", "升级失败", "firmware", "固件升级", "u盘升级"]):
        return "IOT-配网、连接、离线、OTA、语音包"

    # 4. IOT-WiFi/蓝牙（网络层）
    if any(k in t for k in ["wifi", "蓝牙", "ble", "mqtt", "信号弱", "信号差"]):
        return "IOT-配网、连接、离线、OTA、语音包"

    # 5. IOT-组件与状态控制交互/预约清扫
    if any(k in t for k in ["预约清扫", "定时清扫", "清扫模式", "组件启停"]):
        return "IOT-组件与状态控制交互，预约清扫"

    # 6. IOT-地图/禁区/区域/划区
    if any(k in t for k in ["app地图", "虚拟墙", "划区", "app端地图", "地图显示异常"]):
        return "IOT-地图/禁区/区域/划区/机器&回"

    # 7. 应用-地毯策略（地毯相关，非常具体）
    if any(k in t for k in ["地毯", "毛毯", "carpet", "rug", "避毯", "上地毯", "下地毯"]):
        return "应用-地毯策略"

    # 8. 嵌入式-地检相关（非常具体）
    if any(k in t for k in ["地检", "跌落", "悬崖", "悬空", "cliff", "drop", "防跌落"]):
        return "嵌入式--地检相关"

    # 9. 嵌入式-电池/电量及充电相关
    if any(k in t for k in ["电量跳变", "续航", "电池", "电量异常", "充电管理", "battery"]):
        return "嵌入式--电池/电量及充电相关"

    # 10. 嵌入式-传感器硬件（IMU/编码器/雷达/线激光）
    if any(k in t for k in ["imu", "tilt", "倾斜", "编码器", "encoder", "陀螺仪",
                             "雷达", "lidar", "线激光", "line laser", "传感器异常"]):
        return "嵌入式"

    # 11. 嵌入式-电机/硬件（堵转/过流/电机停机）
    if any(k in t for k in ["电机", "堵转", "过流", "轮子", "轮组", "wheel",
                             "motor", "硬件故障", "电机停机"]):
        return "嵌入式"

    # 12. 算法-避障/沿墙/碰撞（空间感知相关）
    if any(k in t for k in ["避障", "绕障", "碰撞", "沿墙", "wallfollow",
                             "推动障碍物", "obstacle", "bump", "直角", "拐角"]):
        return "算法-避障"

    # 13. 算法-回充&基站（回充路径/对桩/搜桩）
    if any(k in t for k in ["回充失败", "对桩失败", "充电失败", "dock fail",
                             "找不到基站", "回充超时", "搜桩"]):
        return "算法-回充&基站"

    # 14. 算法-地图/建图/定位（SLAM相关）
    if any(k in t for k in ["错图", "slam", "localization", "重定位", "定位丢失",
                             "建图失败", "地图叠加", "漏建", "墙体识别"]):
        return "算法-地图/建图/定位"

    # 15. 算法-导航、规划、路径（路径规划相关）
    if any(k in t for k in ["导航", "路径", "规划", "迷路", "重复覆盖", "遗漏区域",
                             "path", "navigation", "replan"]):
        return "算法-导航、规划、禁区、区域、分区"

    # 16. 算法-脱困、越障（卡困相关）
    if any(k in t for k in ["被困", "卡困", "脱困", "卡住", "stuck", "trapped",
                             "escape", "越障", "卡死"]):
        return "算法-脱困、越障"

    # 17. 算法-运动控制（速度/转圈/运动异常）
    if any(k in t for k in ["原地转圈", "速度异常", "运动异常", "漂移", "打滑", "skid"]):
        return "算法-运动控制"

    # 18. 应用-状态机（任务中断/恢复/地图操作冲突）
    if any(k in t for k in ["状态转换", "idle", "暂停", "恢复", "中断", "任务丢失", "状态机"]):
        return "应用-状态机"

    # 19. 应用-区域、路径、禁区、地图操作
    if any(k in t for k in ["删除地图", "地图删除", "explore_map", "禁区", "区域", "分区", "虚拟墙"]):
        return "应用-区域、路径、禁区、地图操作"

    # 20. 应用-基站交互（烘干/洗拖布/集尘/风干/自清洁）
    if any(k in t for k in ["烘干", "洗拖布", "集尘", "风干", "自清洁",
                             "基站交互", "基站维护", "维护"]):
        return "应用-基站交互"

    # 21. 应用-回充集尘任务逻辑（回充流程/集尘流程）
    if any(k in t for k in ["回充流程", "集尘流程", "充电流程", "回充逻辑"]):
        return "应用-回充集尘任务逻辑"

    # 22. 应用-组件控制逻辑（风机/边刷/滚刷/拖布控制）
    if any(k in t for k in ["风机", "吸力", "边刷", "滚刷", "中扫", "拖布",
                             "组件控制", "地毯增压", "组件状态", "清扫组件"]):
        return "应用-组件控制逻辑"

    # 23. 应用-UI交互
    if any(k in t for k in ["按键", "灯显", "语音", "ui交互", "界面卡顿"]):
        return "应用-UI交互（UI指：按键响应、灯显控"

    # 24. 死机/崩溃/内存问题
    if any(k in t for k in ["死机", "崩溃", "重启", "内存泄漏", "oom", "panic"]):
        return "死机/崩溃/内存问题"

    # 25. 硬件-信号质量
    if any(k in t for k in ["rssi", "信号质量", "信号强度", "信号干扰"]):
        return "硬件-信号质量"

    # 26. 硬件-硬件设计问题
    if any(k in t for k in ["结构干涉", "电气参数", "硬件设计", "认证"]):
        return "硬件-硬件设计问题"

    # 27. 产测功能
    if any(k in t for k in ["产测", "工厂测试", "test_mode", "calibration"]):
        return "产测功能"

    # 28. 通用回充/充电（未明确失败的回充问题）
    if any(k in t for k in ["回充", "充电", "基站", "对桩", "dock", "charge"]):
        return "算法-回充&基站"

    # 29. 通用兜底
    if any(k in t for k in ["任务", "建图", "component", "传感器", "状态"]):
        return "应用-状态机"

    return "未分类缺陷"


def _build_defect_info(task: "TeambitionTask", sn: str, fw: str,
                       target_dt: datetime) -> dict:
    """构建缺陷信息字典。"""
    severity = ""
    category = ""
    attachments = ""
    for cf in task.customfields:
        title = cf.get("title", "")
        val = cf.get("value", "")
        if isinstance(val, list):
            val = ", ".join(v.get("title", str(v)) for v in val)
        if "严重" in title or "级别" in title:
            severity = val
        if "分类" in title or "模块" in title:
            category = val
        if "附件" in title or "视频" in title:
            attachments = val

    # 如果customfields中没有分类，从标题推断
    if not category:
        category = _infer_category_from_title(task.content)

    # 报告展示用北京时间（UTC+8）
    beijing_dt = target_dt + timedelta(hours=8)
    return {
        "title": task.content,
        "sn": sn,
        "fw": fw,
        "time": beijing_dt.strftime("%Y-%m-%d %H:%M:%S") + " 北京时间",
        "severity": severity,
        "category": category,
        "attachments": attachments,
        "task_id": task.id,
    }


class LogAnalysisIntegration:
    """日志分析集成器：将 AI 日志分析集成到 TB 缺陷导入流程中。"""

    def __init__(self, tb_client=None, drc_server: str = None,
                 drc_username: str = None, drc_password: str = None,
                 drc_model: str = None,
                 zentao_client=None, web_cookies: dict = None):
        """
        Args:
            tb_client: TeambitionClient 实例
            drc_server: DRC 日志服务器地址
            drc_username: DRC 服务器用户名
            drc_password: DRC 服务器密码
            drc_model: 设备型号（如 CLA_HS4）
            zentao_client: ZentaoClient 实例（用于从禅道下载视觉附件）
            web_cookies: TB Web Cookie 字典（用于模拟浏览器下载附件）
        """
        self.tb_client = tb_client
        self.drc_server = drc_server or _DEFAULT_DRC_SERVER
        self.drc_username = drc_username or _DEFAULT_DRC_USER
        self.drc_password = drc_password or _DEFAULT_DRC_PASS
        self.drc_model = drc_model or _DEFAULT_DRC_MODEL

        # 加载 LLM 配置
        try:
            config = load_configs("configs")
            self.analyzer = AILogAnalyzer(config)
            # DRC 日志格式正则：5-20 7:50:0.134/SW D/file.cpp:148 msg
            import re
            drc_log_re = re.compile(
                r'^(\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{1,2}:\d{1,2}\.\d{3})'
                r'/([A-Z]{2,})\s+([DIWEF])'
                r'/([^:]+):(\d+)\s+(.*)$'
            )
            self.summarizer = LogSummarizer(log_re=drc_log_re)
        except Exception as e:
            logger.warning("LLM 配置加载失败: %s", e)
            self.analyzer = None
            self.summarizer = None

        # 初始化视觉分析集成（按需）
        self.vision = None
        try:
            classifier_cfg = config.get("classifier", {})
            if "llm" not in classifier_cfg and "classifier" in classifier_cfg:
                classifier_cfg = classifier_cfg["classifier"]
            llm_cfg = classifier_cfg.get("llm", {})
            fb_cfg = llm_cfg.get("fallback", {})
            # 视觉分析API Key优先使用fallback，其次使用主LLM（智谱AI平台通用）
            vision_key = fb_cfg.get("api_key", "") or llm_cfg.get("api_key", "")
            if vision_key:
                self.vision = VisionIntegration(
                    vision_api_key=vision_key,
                    zentao_client=zentao_client,
                    web_cookies=web_cookies,
                )
                logger.info("视觉分析模块已初始化")
            else:
                logger.info("未配置视觉分析 API Key，跳过")
        except Exception as e:
            logger.warning("视觉分析初始化失败: %s", e)
            self.vision = None

        # ── 三层分析增强初始化 ──
        # 故障模式库
        self.pattern_lib = FaultPatternLibrary()
        if self.pattern_lib.enabled:
            logger.info("故障模式库已加载 (%d 个模式)", self.pattern_lib.pattern_count)

        # 模块化提示词构建器
        self.prompt_builder = PromptBuilder()

        # RAG 知识库（历史案例检索）
        self.knowledge_base = KnowledgeBase(config)
        if self.knowledge_base.enabled:
            logger.info("RAG 知识库已初始化 (%d/%d 已批准)",
                        self.knowledge_base.approved_count,
                        self.knowledge_base.record_count)

        # 领域知识 RAG（按缺陷类别动态加载知识片段）
        self.knowledge_rag = KnowledgeRAG()
        logger.info("领域知识 RAG 已初始化")

    def analyze_and_comment(self, task: "TeambitionTask",
                            task_raw: dict = None,
                            fw_hint: str = "") -> bool:
        """对单个 TB 缺陷任务进行日志分析并写入评论。

        Args:
            task: TeambitionTask 对象
            task_raw: /v3/task/query 返回的原始 dict（用于提取视觉附件）
            fw_hint: 调用方已知的固件版本号（如 2.1.42），优先使用

        Returns:
            是否成功
        """
        sn = _extract_sn_from_task(task)
        if not sn:
            logger.info("任务 %s 未提取到 SN，跳过日志分析", task.id)
            return False

        target_dt = _extract_time_from_task(task)
        if not target_dt:
            logger.info("任务 %s 未提取到时间信息，跳过日志分析", task.id)
            return False

        fw = fw_hint or _extract_fw_from_task(task)
        beijing_dt = target_dt + timedelta(hours=8)
        time_range_str = (
            f"{beijing_dt.strftime('%Y-%m-%d %H:%M:%S')} 北京时间 "
            f"(±{LOG_WINDOW_MINUTES}min)"
        )
        logger.info("任务 %s: SN=%s, 时间=%s, FW=%s", task.id, sn, time_range_str, fw)

        # 下载并分析日志（前后10分钟）
        log_summary = self._fetch_and_analyze_logs(sn, target_dt, fw, title=task.content)
        if not log_summary and task.created:
            # 缺陷时间与创建时间可能不同（如用户事后补录），尝试创建时间
            try:
                created_dt = datetime.fromisoformat(task.created.replace("Z", "+00:00"))
                created_dt = created_dt.astimezone(timezone.utc)
                if abs((created_dt - target_dt).total_seconds()) > 3600:
                    logger.info("缺陷时间 %s 无日志，尝试创建时间 %s",
                                target_dt.strftime("%Y-%m-%d %H:%M"),
                                created_dt.strftime("%Y-%m-%d %H:%M"))
                    log_summary = self._fetch_and_analyze_logs(sn, created_dt, fw, title=task.content)
                    if log_summary:
                        target_dt = created_dt
            except Exception:
                pass
        if not log_summary:
            logger.warning("任务 %s: 未获取到日志数据", task.id)
            return False

        if not self.analyzer:
            logger.warning("LLM 未配置，跳过 AI 分析")
            return False

        # 构建缺陷信息
        # 如果时间自动探测调整了窗口，用新时间更新 target_dt
        if log_summary.get("time_auto_detected"):
            try:
                new_start = datetime.fromisoformat(log_summary["window_start"])
                new_target = new_start + timedelta(minutes=LOG_WINDOW_MINUTES)
                logger.info("时间自动探测已调整: %s BJT → %s BJT",
                            (target_dt + timedelta(hours=8)).strftime("%H:%M"),
                            (new_target + timedelta(hours=8)).strftime("%H:%M"))
                target_dt = new_target
            except Exception:
                pass
        defect_info = _build_defect_info(task, sn, fw, target_dt)

        # ── Layer 3: 故障模式库匹配 ──
        pattern_matches = []
        pattern_hints = ""
        key_logs = log_summary.get("key_logs", [])
        fault_contexts = log_summary.get("fault_contexts", [])
        if self.pattern_lib.enabled:
            pattern_matches = self.pattern_lib.match(
                log_summary, key_logs, fault_contexts,
                category=defect_info.get("category", "")
            )
            if pattern_matches:
                pattern_hints = self.pattern_lib.format_pattern_hints(pattern_matches)
                logger.info("命中 %d 个故障模式: %s",
                            len(pattern_matches),
                            ", ".join(m.pattern_name for m in pattern_matches))

        # ── Layer 1: RAG 知识库检索相似案例 ──
        few_shot_examples = ""
        if self.knowledge_base.enabled:
            similar = self.knowledge_base.retrieve_similar(defect_info, log_summary, status_filter="approved")
            if similar:
                few_shot_examples = self.knowledge_base.format_few_shot_examples(similar)
                logger.info("检索到 %d 个相似案例", len(similar))
            # 同时检索反面教材（被拒绝的案例）
            rejected = self.knowledge_base.retrieve_similar(defect_info, log_summary, status_filter="rejected")
            if rejected:
                rejected_text = self.knowledge_base.format_rejected_examples(rejected)
                few_shot_examples += "\n\n" + rejected_text
                logger.info("检索到 %d 个反面教材", len(rejected))

        # ── Layer 2: 模块化提示词 ──
        category = defect_info.get("category", "")
        system_prompt = ""
        if self.prompt_builder.enabled and category:
            from src.ai_log_analyzer import SYSTEM_PROMPT
            system_prompt = self.prompt_builder.get_specialized_system_prompt(
                category, SYSTEM_PROMPT
            )
            if system_prompt != SYSTEM_PROMPT:
                logger.info("使用 %s 类别专业化提示词", category)

        # 动态加载领域知识（RAG）
        domain_knowledge = self.knowledge_rag.retrieve(category)
        if domain_knowledge:
            logger.info("已加载 %s 类别的领域知识片段", category)

        # ── 日志异常预检测层：在LLM前快速识别异常模式 ──
        mandatory_signals = self._pre_detect_anomalies(log_summary, defect_info)
        if mandatory_signals:
            logger.info("日志异常预检测发现: %s", mandatory_signals[:100])

        # 判断是故障类型还是一般缺陷
        if fault_contexts:
            analysis = self.analyzer.analyze_fault(
                fault_contexts[0],
                pattern_hints=pattern_hints,
                few_shot_examples=few_shot_examples,
                system_prompt=system_prompt,
                domain_knowledge=domain_knowledge,
                mandatory_signals=mandatory_signals,
            )
        else:
            analysis = self.analyzer.analyze_defect(
                defect_info, log_summary,
                pattern_hints=pattern_hints,
                few_shot_examples=few_shot_examples,
                system_prompt=system_prompt,
                domain_knowledge=domain_knowledge,
                mandatory_signals=mandatory_signals,
            )

        if not analysis:
            logger.warning("任务 %s: AI 日志分析失败", task.id)
            return False

        # ── 后处理校验层：自动检测常见分析错误 ──
        analysis, validation_warnings, rewrite_reason = self._validate_analysis(analysis, defect_info)
        if validation_warnings:
            logger.warning("任务 %s: 分析校验发现 %d 个问题: %s",
                           task.id, len(validation_warnings), "; ".join(validation_warnings))
        if rewrite_reason:
            logger.warning("任务 %s: 分析触发强制重写: %s", task.id, rewrite_reason)
            rewritten = self._rewrite_analysis(
                analysis, defect_info, log_summary, rewrite_reason,
                pattern_hints=pattern_hints, system_prompt=system_prompt,
                domain_knowledge=domain_knowledge, mandatory_signals=anomaly_hints,
            )
            if rewritten:
                analysis = rewritten
                analysis, validation_warnings, rewrite_reason = self._validate_analysis(analysis, defect_info)
                if validation_warnings:
                    logger.warning("任务 %s: 重写后校验发现 %d 个问题: %s",
                                   task.id, len(validation_warnings), "; ".join(validation_warnings))

        # ── 按需视觉分析 ──
        vision_raw = ""
        if self.vision and task_raw:
            need_vision, reason = self.analyzer.judge_vision_needed(defect_info, log_summary)
            logger.info("视觉分析判断: %s, 理由: %s", "需要" if need_vision else "不需要", reason)
            if need_vision:
                vision_results = self.vision.analyze_task_videos(
                    task_raw,
                    self.tb_client._http if self.tb_client else None,
                    self.tb_client._get_headers() if self.tb_client else {},
                    defect_title=defect_info.get("title", ""),
                )
                if vision_results:
                    vision_parts = []
                    for vname, vtext in vision_results.items():
                        if vtext:
                            vision_parts.append(f"**{vname}**\n{vtext}")
                    if vision_parts:
                        vision_raw = "\n\n".join(vision_parts)
                else:
                    logger.info("判断需要视觉分析，但任务中无可用视频/图片附件，回退到纯日志分析")

        # ── 二次综合：日志 + 视觉 ──
        if vision_raw:
            combined = self.analyzer.analyze_combined(defect_info, analysis, vision_raw)
            if combined:
                analysis = (
                    "## 日志与视觉综合分析\n\n" + combined +
                    "\n\n---\n\n## 原始日志分析\n\n" + analysis
                )
            else:
                vision_analysis = "\n\n## 视觉分析\n\n" + vision_raw
        else:
            vision_analysis = ""

        # ── 低置信度自动重试：扩大时间窗口重新分析 ──
        _RETRY_WINDOWS = [30, 60]
        _original_target_dt = target_dt  # 每次重试以用户原始报告时间为锚点
        for retry_idx, retry_window in enumerate(_RETRY_WINDOWS):
            conf = self._extract_confidence(analysis)
            if conf != "低":
                break
            logger.warning("任务 %s: 分析置信度为低，扩大窗口至±%dmin 重试 (%d/%d)",
                           task.id, retry_window, retry_idx + 1, len(_RETRY_WINDOWS))
            retry_summary = self._fetch_and_analyze_logs(
                sn, _original_target_dt, fw, title=task.content,
                window_minutes=retry_window,
            )
            if not retry_summary:
                logger.warning("扩大窗口重试未获取到更多日志，保持当前分析")
                continue

            log_summary = retry_summary
            if log_summary.get("time_auto_detected"):
                try:
                    new_start = datetime.fromisoformat(log_summary["window_start"])
                    target_dt = new_start + timedelta(minutes=retry_window)
                except Exception:
                    pass
            defect_info = _build_defect_info(task, sn, fw, target_dt)

            # 重新运行预检测
            mandatory_signals = self._pre_detect_anomalies(log_summary, defect_info)
            # 重新匹配故障模式
            key_logs = log_summary.get("key_logs", [])
            fault_contexts = log_summary.get("fault_contexts", [])
            if self.pattern_lib.enabled:
                pattern_matches = self.pattern_lib.match(
                    log_summary, key_logs, fault_contexts,
                    category=defect_info.get("category", "")
                )
                if pattern_matches:
                    pattern_hints = self.pattern_lib.format_pattern_hints(pattern_matches)

            # 重新分析
            if fault_contexts:
                analysis = self.analyzer.analyze_fault(
                    fault_contexts[0],
                    pattern_hints=pattern_hints,
                    few_shot_examples=few_shot_examples,
                    system_prompt=system_prompt,
                    domain_knowledge=domain_knowledge,
                    mandatory_signals=mandatory_signals,
                )
            else:
                analysis = self.analyzer.analyze_defect(
                    defect_info, log_summary,
                    pattern_hints=pattern_hints,
                    few_shot_examples=few_shot_examples,
                    system_prompt=system_prompt,
                    domain_knowledge=domain_knowledge,
                    mandatory_signals=mandatory_signals,
                )

            if not analysis:
                logger.warning("任务 %s: 重试分析失败", task.id)
                continue

            analysis, validation_warnings, rewrite_reason = self._validate_analysis(analysis, defect_info)
            if rewrite_reason:
                rewritten = self._rewrite_analysis(
                    analysis, defect_info, log_summary, rewrite_reason,
                    pattern_hints=pattern_hints, system_prompt=system_prompt,
                    domain_knowledge=domain_knowledge, mandatory_signals=mandatory_signals,
                )
                if rewritten:
                    analysis = rewritten
                    analysis, validation_warnings, _ = self._validate_analysis(analysis, defect_info)

            new_conf = self._extract_confidence(analysis)
            logger.info("任务 %s: 重试后置信度: %s", task.id, new_conf)

        # ── Layer 1: 存储分析结果到知识库 ──
        if self.knowledge_base.enabled and analysis:
            try:
                analysis_json = self._parse_analysis_json(analysis)
                self.knowledge_base.store_analysis(
                    defect_info, analysis_json, log_summary,
                    pattern_matches=pattern_matches,
                )
            except Exception as e:
                logger.warning("存储分析结果到知识库失败: %s", e)

        # 生成 HTML 报告并上传为附件
        html_attachment_id = ""
        filename = ""
        upload_result = None
        if self.tb_client:
            try:
                html_bytes = generate_html_bytes(
                    defect_info=defect_info,
                    analysis=analysis,
                    log_summary=log_summary,
                    pattern_matches=pattern_matches,
                    pre_detect_signals=mandatory_signals,
                    vision_analysis=vision_raw,
                )
                safe_title = re.sub(r'[\\/:*?"<>|]', "_", defect_info.get("title", "report"))[:40]
                filename = f"AI_Report_{safe_title}_{task.id}.html"
                att = AttachmentFile(
                    filename=filename,
                    content_type="text/html",
                    data=html_bytes,
                    size=len(html_bytes),
                )
                upload_result = self.tb_client.upload_attachment(task.id, att)
                if upload_result:
                    html_attachment_id = upload_result[0]
                    logger.info("任务 %s: HTML 报告已上传为附件 %s", task.id, filename)
                else:
                    logger.warning("任务 %s: HTML 报告上传失败", task.id)
            except Exception as e:
                logger.warning("任务 %s: 生成或上传 HTML 报告失败: %s", task.id, e)

        # 写入 TB 评论（Markdown 摘要 + 附件提示）
        html_download_url = ""
        if self.tb_client:
            if upload_result and isinstance(upload_result, (list, tuple)) and len(upload_result) > 1:
                html_download_url = upload_result[1] or ""
            comment = self._format_comment(
                defect_info, analysis, vision_analysis,
                pattern_matches=pattern_matches,
                html_attachment_id=html_attachment_id,
                html_filename=filename if html_attachment_id else "",
                html_download_url=html_download_url,
            )
            try:
                self.tb_client.add_task_comment(task.id, comment)
                logger.info("任务 %s: AI 分析已写入评论", task.id)
                return True
            except Exception as e:
                logger.error("任务 %s: 写入评论失败: %s", task.id, e)
                return False
        else:
            logger.info("任务 %s: AI 分析完成（未配置 TB 客户端，未写入评论）", task.id)
            return True

    @staticmethod
    def _keywords_from_title(title: str) -> list:
        """根据缺陷标题推断应重点关注的关键词列表。

        注意：标题中常包含固定前缀（如【禅道xxx】【HSxxx】【主机:Vx.x.x 基站:X.X.X】），
        这些前缀不应影响关键词推断。先清理前缀，再从实际缺陷描述中提取关键词。

        基于 sweeper_knowledge_base.yaml 中的 analysis_keywords 映射优化。
        """
        import re as _re
        # 移除标题中的【...】前缀和版本号信息
        cleaned = title
        cleaned = _re.sub(r'【[^】]+】', '', cleaned)
        cleaned = _re.sub(r'主机[:\s]*V?\d+\.\d+\.\d+\s*基站[:\s]*\d+\.\d+\.\d+', '', cleaned)
        cleaned = _re.sub(r'主机[:\s]*V?\d+\.\d+\.\d+', '', cleaned)
        cleaned = _re.sub(r'基站[:\s]*\d+\.\d+\.\d+', '', cleaned)
        cleaned = _re.sub(r'[A-Z]+\d+[A-Z]*\d*', '', cleaned)
        cleaned = _re.sub(r'\b(PVT\d|T\d|EB\d|Build\d+\.?\d*)\b', '', cleaned, flags=_re.IGNORECASE)
        cleaned = cleaned.replace('样机', '').replace('阶段试产', '')
        t = cleaned.lower()
        base = ["error", "fail", "status change", "EID_E", "work_status"]

        # ===== 决策树匹配（按优先级排序，越具体的越先匹配） =====

        # 1. APP端（最具体，直接涉及APP界面）
        if any(k in t for k in ["app", "弹框", "页面", "返回", "ui", "界面", "显示", "弹窗"]):
            return base + ["app", "mqtt", "network_proxy", "operating_state",
                           "EID_I_APP_SET_CMD", "EID_I_APP_START_CLEAN"]

        # 2. IOT（网络/升级类）
        if any(k in t for k in ["ota", "wifi", "蓝牙", "离线", "配网", "mqtt", "网络", "断连"]):
            return base + ["ota", "wifi", "mqtt", "network", "network_proxy",
                           "EID_I_NET_CONN", "EID_I_NET_DISCONN", "EID_E_WIFI_BREAKDOWN"]

        # 3. 地毯/毛毯相关（非常具体）
        if any(k in t for k in ["地毯", "毛毯", "carpet", "rug", "避毯", "上地毯", "下地毯"]):
            return base + ["carpet", "rug", "ultrasonic_carpet", "rug_clean_mode",
                           "carpet_avoid", "carpet_manager", "label_carpet",
                           "AUTO_RUG_CLEAN_MODE", "deal_skid",
                           "EID_I_WALK_IN_RUG", "EID_I_WALK_OUT_RUG",
                           "EID_E_START_MOP_ON_RUG"]

        # 4. 传感器异常（IMU/编码器/雷达等）
        if any(k in t for k in ["imu", "tilt", "倾斜", "编码器", "encoder", "陀螺仪",
                                 "雷达", "lidar", "线激光", "line laser", "传感器"]):
            return base + ["imu", "yaw", "pitch", "roll", "tilt", "InitCheckPickup",
                           "encoder", "lidar", "line_laser", "sensor",
                           "EID_E_IMU_DATA", "EID_E_IMU_DATA_NO_CHANGE",
                           "EID_E_LIDAR_SPEED_ERROR", "EID_E_LIDAR_POINT_ERR",
                           "EID_E_KIT_LINE_LASER_ERROR"]

        # 5. 避障/绕障/碰撞/沿墙
        if any(k in t for k in ["避障", "绕障", "碰撞", "沿墙", "推动障碍物",
                                 "obstacle", "bump", "wallfollow", "直角", "拐角"]):
            return base + ["avoid", "obstacle", "collision", "bump", "wallfollow",
                           "line_laser", "navigator", "escaper", "motion_path",
                           "angle_range_ok", "CUSTOM_SLOW_AVOID", "GAT_EMERGENCY_STOP",
                           "EID_E_COLLISION", "EID_E_FRONT_WALL_DIRTY", "EID_E_PSD_DIRTY"]

        # 5.5 地检/防跌落/悬崖/悬空
        if any(k in t for k in ["地检", "跌落", "悬崖", "悬空", "cliff", "drop",
                                 "防跌落", "台阶"]):
            return base + ["cliff_sensor", "drop", "carrier", "InitCheckPickup",
                           "EID_E_DROP", "EID_E_CLIFF"]

        # 6. 回充失败/对桩失败（比通用回充更具体）
        if any(k in t for k in ["回充失败", "对桩失败", "充电失败", "dock fail", "找不到基站"]):
            return base + ["back_charge", "charging", "dock", "go_back_station",
                           "base_station", "bidirection_ir", "ir_proxy",
                           "EID_I_FIND_CHARGER_TASK_FAILED", "EID_E_FIND_CHARGER_ON_CHARGE",
                           "target_navigator", "path_follower", "output_state"]

        # 7. 建图/定位/错图（比通用地图更具体）
        if any(k in t for k in ["错图", "slam", "localization", "重定位", "定位丢失",
                                 "建图失败", "地图叠加", "漏建"]):
            return base + ["map", "slam", "explore_map", "reset_map",
                           "localization", "pathinfo", "transform_map",
                           "EID_I_SLAM_LOCATION_SUCCESS", "EID_E_SPP_LOST_POS",
                           "EID_E_CLEAN_LOST_POSE", "EID_E_FIND_CHGER_LOST_POSE"]

        # 8. 地图/区域/禁区/分区/导航规划
        if any(k in t for k in ["删除地图", "地图删除", "explore_map", "分区", "区域",
                                 "禁区", "虚拟墙", "自动分区", "划区", "存盘区域", "临时区域",
                                 "导航", "路径", "规划", "迷路", "重复覆盖", "遗漏区域",
                                 "path", "navigation", "replan", "coverage"]):
            return base + ["map", "slam", "explore_map", "reset_map", "delete_map",
                           "localization", "pathinfo", "navigator", "path_plan",
                           "transform_map", "forbid_area", "virtual_wall",
                           "EID_I_MAP_AND_REGION_CHANGED", "EID_I_MAP_AND_REGION_DELETED",
                           "EID_N_CLEAN_DONE_NORMAL", "EID_N_TARGET_FAILED"]

        # 9. 状态机中断/恢复/抱起/断点续扫
        if any(k in t for k in ["抱起", "中断", "恢复", "pickup", "tilt", "pause",
                                 "idle", "状态转换", "任务丢失", "断点续扫", "续扫",
                                 "低电暂停", "充电恢复"]):
            return base + ["pickup", "tilt", "InitCheckPickup", "node_ctrl_pause",
                           "task_idle", "task_node_base", "RobotEventReport",
                           "work_status", "resume", "recover",
                           "EID_E_PICK_UP", "EID_E_PICK_UP_DO_TASK",
                           "EID_E_TILE_DO_TASK", "EID_I_CHARGE_FULL_RESUME_TASK"]

        # 10. 烘干/洗拖布/集尘/风干
        if any(k in t for k in ["烘干", "洗拖布", "集尘", "dry", "wash", "dust",
                                 "风干", "自清洁"]):
            return base + ["dry_mop", "wash_mop", "collect_dust", "proxy",
                           "ir_proxy", "bidirection_ir", "ProxyDrynHandler",
                           "ProxyWaterInjectionHandler", "auto_dust",
                           "EID_I_COLLECT_DUST_START", "EID_I_START_DRY",
                           "EID_E_DRYING_LOST_CHARGER", "EID_E_SELF_CLEAN_LOST_CHARGER"]

        # 11. 通用回充/充电/基站
        if any(k in t for k in ["回充", "充电", "基站", "对桩", "dock", "charge",
                                 "back_charge", "full_charging", "充电桩"]):
            return base + ["back_charge", "charging", "dock", "go_back_station",
                           "base_station", "bidirection_ir", "ir_proxy",
                           "EID_I_FIND_CHARGER_TASK_START", "EID_I_CHARGE_FULL"]

        # 12. 风机/吸力/组件控制
        if any(k in t for k in ["风机", "吸力", "suction", "fan", "边刷", "滚刷",
                                 "中扫", "拖布", "组件"]):
            return base + ["fan", "suction", "component_control", "component_control_service",
                           "clean_type", "sweep_mode", "side_brush", "main_brush",
                           "EID_E_FAN_SPEED", "EID_E_SIDE_BRUSH", "EID_E_MIDDLE_BRUSH",
                           "EID_I_OPEN_CLEAN_COMPONENT", "EID_I_CLOSE_CLEAN_COMPONENT"]

        # 13. 被困/卡困/脱困
        if any(k in t for k in ["被困", "卡困", "脱困", "卡住", "stuck", "trapped", "escape"]):
            return base + ["trapped", "stuck", "escape", "rescue", "bumper",
                           "EID_E_PHYSICAL_TRAPPED", "EID_E_PLAN_TRAPPED",
                           "EID_E_VIRTUAL_TRAPPED", "EID_N_ESCAPE_DONE"]

        # 14. 电机异常/堵转/过流
        if any(k in t for k in ["电机", "堵转", "过流", "轮子", "轮组", "wheel", "motor"]):
            return base + ["wheel", "stuck", "over_current", "encoder", "motor",
                           "EID_E_WHEEL", "EID_E_LEFT_WHEEL", "EID_E_RIGHT_WHEEL",
                           "EID_E_SIDE_BRUSH", "EID_E_MIDDLE_BRUSH", "EID_E_FAN_SPEED"]

        # 15. 默认兜底
        return base + ["avoid", "obstacle", "碰撞", "bump", "RobotEventReport", "work_status"]

    def _fetch_and_analyze_logs(self, sn: str, target_dt: datetime,
                                 fw: str, title: str = "",
                                 window_minutes: int = None) -> Optional[dict]:
        """下载 DRC 日志并进行摘要分析。

        Args:
            window_minutes: 时间窗口半宽（分钟），默认使用 LOG_WINDOW_MINUTES
        """
        win = window_minutes if window_minutes is not None else LOG_WINDOW_MINUTES
        year = target_dt.strftime("%Y")
        month = target_dt.strftime("%m")
        day = target_dt.strftime("%d")

        start_dt = target_dt - timedelta(minutes=win)
        end_dt = target_dt + timedelta(minutes=win)

        # 复用 batch_analyze 模块
        try:
            import batch_analyze as ba
            ba.SERVER_URL = self.drc_server
            ba.USERNAME = self.drc_username
            ba.PASSWORD = self.drc_password
            ba.MODEL = self.drc_model
            ba.SN = sn
            ba.YEAR, ba.MONTH, ba.DAY = year, month, day
            ba.CACHE_DIR = Path(f"cache/{sn}_{year}{month}{day}")
            ba.MERGED_LOG = ba.CACHE_DIR / "merged_logs.txt"
            ba.CHECKPOINT = ba.CACHE_DIR / "checkpoint.json"

            os.makedirs(ba.CACHE_DIR, exist_ok=True)

            # ── FW 自动探测（搜索所有版本） ──
            fw_dirs = self._detect_fw_on_server(sn, year, month, day, fw_hint=fw)
            if not fw_dirs:
                logger.warning("无法在服务器 %s/%s/%s 探测到 FW", year, month, day)
                return None
            logger.info("探测到 %d 个 FW 版本: %s", len(fw_dirs), ", ".join(fw_dirs))

            # ── 智能缓存：检查是否已有覆盖所需时间窗口的缓存日志 ──
            coverage = LogAnalysisIntegration._load_cache_coverage(ba.CACHE_DIR)
            cache_hit = (
                ba.MERGED_LOG.exists() and
                LogAnalysisIntegration._is_window_covered(coverage, start_dt, end_dt, fw_dirs)
            )

            if cache_hit:
                logger.info("缓存命中: %s 已覆盖 %s~%s UTC，跳过下载",
                            sn, start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M"))
            else:
                # 遍历所有 FW 目录，收集匹配时间窗口的 DRC 文件
                drc_filtered = []
                for fw_dir in fw_dirs:
                    ba.FW = fw_dir
                    drc_files = ba.list_drc_files()
                    matched = ba.filter_by_utc_hour(
                        drc_files,
                        start_dt.hour, end_dt.hour,
                        start_dt.minute, end_dt.minute,
                    )
                    if matched:
                        logger.info("FW %s: 找到 %d 个匹配文件", fw_dir, len(matched))
                        drc_filtered.extend(matched)

                if not drc_filtered:
                    # ── 精确窗口未命中，回退到全天扫描 ──
                    logger.warning("精确窗口 (±%dmin) 未命中 DRC，回退到全天扫描", win)
                    logger.info("全天扫描 %s: 下载全部 FW 版本日志...", sn)
                    all_drc = []
                    for fw_dir in fw_dirs:
                        ba.FW = fw_dir
                        all_drc.extend(ba.list_drc_files())
                    if not all_drc:
                        logger.warning("未找到 %s %s 的 DRC 日志（已搜索全部 %d 个 FW 版本）",
                                       sn, target_dt.strftime("%Y-%m-%d %H:%M"), len(fw_dirs))
                        return None
                    logger.info("全天扫描: 下载 %d 个 DRC 文件...", len(all_drc))
                    ba.download_and_merge_drc(all_drc)
                else:
                    logger.info("开始下载 %d 个 DRC 文件（精确窗口）...", len(drc_filtered))
                    ba.download_and_merge_drc(drc_filtered)

                # 更新缓存覆盖索引
                LogAnalysisIntegration._update_cache_coverage(
                    ba.CACHE_DIR, start_dt, end_dt, fw_dirs
                )

            if not ba.MERGED_LOG.exists():
                logger.warning("合并日志为空")
                return None

            # 分析日志
            result, total = ba.analyze_merged_logs()
            if not result:
                logger.warning("日志分析结果为空")
                return None

            # 关键词提取（根据缺陷标题动态推断）
            keywords = self._keywords_from_title(title)
            logger.info("根据标题推断关键词: %s", ", ".join(keywords))

            # 读取合并日志做摘要
            with open(ba.MERGED_LOG, "r", encoding="utf-8") as f:
                lines = [l.rstrip() for l in f if l.strip()]

            # ── 时间自动探测：以用户报告时间为锚点，寻找事件密度最高的窗口 ──
            best_start, best_end = self._find_best_time_window(
                lines, year, target_dt, window_minutes=win
            )

            # 过滤到最佳时间窗口
            window_lines = []
            for line in lines:
                m = re.match(r"(\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2}:\d{2}\.\d{3})", line)
                if m:
                    try:
                        log_dt = datetime.strptime(
                            f"{year}-{m.group(1)} {m.group(2)}",
                            "%Y-%m-%d %H:%M:%S.%f"
                        ).replace(tzinfo=timezone.utc)
                        if best_start <= log_dt <= best_end:
                            window_lines.append(line)
                    except ValueError:
                        pass
                else:
                    window_lines.append(line)

            if best_start != start_dt:
                bj_best = best_start + timedelta(hours=8)
                logger.info("时间自动探测: 用户报告 %s BJT, 最佳窗口调整为 %s BJT (±%dmin)",
                            (target_dt + timedelta(hours=8)).strftime("%H:%M"),
                            bj_best.strftime("%H:%M"),
                            win)

            log_summary = self.summarizer.summarize(window_lines, keywords=keywords)
            log_summary["fault_contexts"] = result.get("fault_contexts", [])
            log_summary["fault_count"] = len(result.get("fault_contexts", []))
            log_summary["total_lines"] = total
            log_summary["window_start"] = best_start.isoformat()
            log_summary["window_end"] = best_end.isoformat()
            log_summary["time_auto_detected"] = (best_start != start_dt)
            log_summary["original_target_utc"] = target_dt.isoformat()

            return log_summary

        except Exception as e:
            logger.error("拉取日志失败: %s", e)
            return None

    # ── 智能缓存辅助方法 ──

    @staticmethod
    def _load_cache_coverage(cache_dir: Path) -> dict:
        """加载缓存时间覆盖索引。"""
        cov_path = cache_dir / "time_coverage.json"
        if not cov_path.exists():
            return {}
        try:
            import json as _json
            with open(cov_path, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _is_window_covered(coverage: dict, start_dt: datetime,
                           end_dt: datetime, fw_dirs: list) -> bool:
        """检查缓存是否覆盖所需时间窗口和 FW 版本。"""
        if not coverage:
            return False
        # 检查 FW 版本是否匹配
        cached_fw = coverage.get("fw_dirs", [])
        if set(cached_fw) != set(fw_dirs):
            return False
        # 检查时间范围是否全覆盖
        ranges = coverage.get("ranges", [])
        for r in ranges:
            try:
                r_start = datetime.fromisoformat(r[0])
                r_end = datetime.fromisoformat(r[1])
                if r_start <= start_dt and r_end >= end_dt:
                    return True
            except (ValueError, IndexError):
                continue
        return False

    @staticmethod
    def _update_cache_coverage(cache_dir: Path, start_dt: datetime,
                               end_dt: datetime, fw_dirs: list):
        """更新缓存时间覆盖索引。"""
        cov_path = cache_dir / "time_coverage.json"
        import json as _json
        coverage = LogAnalysisIntegration._load_cache_coverage(cache_dir)
        ranges = coverage.get("ranges", [])
        # 合并重叠的时间范围
        new_range = [start_dt.isoformat(), end_dt.isoformat()]
        merged = []
        for r in ranges:
            try:
                r_start = datetime.fromisoformat(r[0])
                r_end = datetime.fromisoformat(r[1])
                if r_end >= start_dt and r_start <= end_dt:
                    # 范围重叠，合并
                    new_range[0] = min(r[0], new_range[0])
                    new_range[1] = max(r[1], new_range[1])
                else:
                    merged.append(r)
            except (ValueError, IndexError):
                merged.append(r)
        merged.append(new_range)
        coverage["ranges"] = merged
        coverage["fw_dirs"] = fw_dirs
        coverage["updated"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(cov_path, "w", encoding="utf-8") as f:
                _json.dump(coverage, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("更新缓存覆盖索引失败: %s", e)

    @staticmethod
    def _find_best_time_window(lines: list, year: str, anchor_dt: datetime,
                               window_minutes: int = LOG_WINDOW_MINUTES):
        """以用户报告时间为锚点，找到事件密度最高的时间窗口。

        在 anchor_dt 周围 ±2h 范围内扫描，综合评分：
        - 密度分: 窗口内 E/W/F 事件密度（归一化）
        - 距离分: 距用户报告时间越近越高（线性衰减，±2h 外为 0）
        - 最终得分 = 密度分 * 0.6 + 距离分 * 0.4

        Returns:
            (best_start_dt, best_end_dt): 最佳窗口起止 UTC 时间
        """
        from collections import defaultdict

        # DRC 日志格式: M-D H:MM:SS.fff/PROCESS LEVEL/source.cpp:line ...
        # 示例: 5-25 7:24:27.315/NP E/navimap_manager.cpp:123 ResetAll ...
        _LEVEL_RE = re.compile(
            r"(\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2}:\d{2}\.\d{3})/[A-Z]+\s+([DIWEF])/"
        )

        # 按1分钟桶收集加权事件数
        minute_buckets = defaultdict(int)
        for line in lines:
            m = _LEVEL_RE.match(line)
            if not m:
                continue
            level = m.group(3)
            weight = {"E": 5, "W": 3, "F": 3}.get(level, 0)
            if weight == 0:
                continue
            try:
                log_dt = datetime.strptime(
                    f"{year}-{m.group(1)} {m.group(2)}",
                    "%Y-%m-%d %H:%M:%S.%f"
                ).replace(tzinfo=timezone.utc)
                bucket_key = log_dt.replace(second=0, microsecond=0)
                minute_buckets[bucket_key] += weight
            except ValueError:
                continue

        if not minute_buckets:
            return (
                anchor_dt - timedelta(minutes=window_minutes),
                anchor_dt + timedelta(minutes=window_minutes),
            )

        # 在用户报告时间 ±2h 范围内扫描
        scan_radius = timedelta(hours=2)
        scan_start = anchor_dt - scan_radius
        scan_end = anchor_dt + scan_radius

        max_weight = max(minute_buckets.values()) if minute_buckets else 1

        best_score = -1.0
        best_start = anchor_dt - timedelta(minutes=window_minutes)
        best_end = anchor_dt + timedelta(minutes=window_minutes)
        best_density = 0.0

        step = timedelta(minutes=1)
        half_window = timedelta(minutes=window_minutes)
        cursor = scan_start

        while cursor + half_window * 2 <= scan_end:
            win_start = cursor
            win_end = cursor + half_window * 2

            density = 0
            for t in sorted(minute_buckets.keys()):
                if win_start <= t < win_end:
                    density += minute_buckets[t]

            density_score = density / max(max_weight * window_minutes, 1)

            win_center = win_start + half_window
            dist_hours = abs((win_center - anchor_dt).total_seconds()) / 3600.0
            proximity_score = max(0.0, 1.0 - dist_hours / 2.0)

            score = density_score * 0.6 + proximity_score * 0.4

            if score > best_score:
                best_score = score
                best_start = win_start
                best_end = win_end
                best_density = density_score

            cursor += step

        bj_best = best_start + timedelta(hours=8)
        bj_anchor = anchor_dt + timedelta(hours=8)
        logger.info(
            "时间探测完成: 用户报告 %s BJT, 最佳窗口 %s ~ %s BJT (密度分=%.2f, 得分=%.3f)",
            bj_anchor.strftime("%H:%M"),
            bj_best.strftime("%H:%M"),
            (bj_best + half_window * 2).strftime("%H:%M"),
            best_density,
            best_score,
        )

        return best_start, best_end

    def _detect_fw_on_server(self, sn: str, year: str, month: str, day: str,
                                fw_hint: str = "") -> list:
        """探测 DRC 服务器上指定日期存在的所有 FW 目录名。

        Args:
            fw_hint: 简短版本号（如 2.1.42），用于优先排列包含该字串的目录。

        Returns:
            FW 目录名列表（fw_hint 匹配的排在前面），失败返回空列表
        """
        from urllib.request import Request, urlopen
        from base64 import b64encode
        from html.parser import HTMLParser

        path = f"/{self.drc_model}/{sn}/{year}/{month}/{day}/"
        creds = b64encode(f"{self.drc_username}:{self.drc_password}".encode()).decode()
        try:
            req = Request(
                f"{self.drc_server}{path}",
                headers={"Authorization": f"Basic {creds}"},
            )
            with urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            class P(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.dirs = []
                def handle_starttag(self, tag, attrs):
                    if tag == "a":
                        d = dict(attrs)
                        h = d.get("href", "")
                        if h and h not in ("../", "?") and not h.startswith("?C="):
                            self.dirs.append(h.rstrip("/"))
            p = P()
            p.feed(html)
            fw_dirs = [n for n in p.dirs if n.startswith("AR-")]
            # fw_hint 匹配的排前面，确保优先搜索，但仍搜索全部版本
            if fw_hint:
                matched = [n for n in fw_dirs if fw_hint in n]
                others = [n for n in fw_dirs if fw_hint not in n]
                return matched + others
            return fw_dirs
        except Exception as e:
            logger.warning("探测 FW 失败: %s", e)
        return []

    def _format_comment(self, defect_info: dict, analysis: str,
                        vision_analysis: str = "",
                        pattern_matches: list = None,
                        html_attachment_id: str = "",
                        html_filename: str = "",
                        html_download_url: str = "") -> str:
        """格式化 AI 分析结果为 TB 评论文本。

        当提供了 HTML 附件时，生成简洁的 Markdown 摘要 + 附件引用；
        否则渲染完整的分析文本。
        """
        header = "🤖 **AI 日志分析结果**\n\n"

        # 故障模式匹配横幅
        if pattern_matches:
            banner_lines = []
            for pm in pattern_matches:
                banner_lines.append(f"  - **{pm.pattern_name}** (置信度 {pm.confidence:.0%})")
            header += "> **命中已知故障模式:**\n" + "\n".join(banner_lines) + "\n\n"

        meta = (
            f"> SN: `{defect_info.get('sn', '未知')}`\n"
            f"> 时间: {defect_info.get('time', '未知')} (±{LOG_WINDOW_MINUTES}分钟窗口)\n"
            f"> 固件: {defect_info.get('fw', '未知')}\n\n"
        )

        # 有 HTML 附件时生成简洁摘要
        if html_attachment_id:
            body_text = self._render_summary(analysis, vision_analysis)
            att_ref = "\n\n📎 **详细分析报告已上传为附件**"
            if html_download_url:
                att_ref = f"\n\n📎 **详细分析报告**: [{html_filename}]({html_download_url})"
            elif html_filename:
                att_ref += f" `{html_filename}`"
            body = "---\n\n" + body_text + att_ref
            body += "\n\n---\n*本分析由 AI 自动生成，仅供参考。*"
            return header + meta + body

        # 无附件时渲染完整分析
        body_text = self._render_analysis(analysis)
        body = "---\n\n" + body_text
        if vision_analysis:
            body += "\n\n" + vision_analysis
        body += "\n\n---\n*本分析由 AI 自动生成，仅供参考。*"
        return header + meta + body

    @staticmethod
    def _render_summary(analysis: str, vision_analysis: str = "") -> str:
        """从 AI 分析结果中提取关键信息，生成简洁 Markdown 摘要。"""
        data = LogAnalysisIntegration._parse_analysis_json(analysis)
        if not data:
            # 非结构化输出，截取前 300 字
            text = analysis.strip()
            if len(text) > 300:
                text = text[:300] + "……"
            return f"**摘要**\n{text}"

        parts = []

        summary = data.get("summary", "")
        if summary:
            parts.append(f"**摘要**: {summary}")

        confidence = data.get("confidence", "")
        severity = data.get("severity_reassessment") or data.get("severity", "")
        if confidence or severity:
            tags = []
            if confidence:
                tags.append(f"置信度: {confidence}")
            if severity:
                tags.append(f"严重度: {severity}")
            parts.append(" · ".join(tags))

        root_cause = data.get("root_cause", "")
        if root_cause:
            parts.append(f"\n**根因**: {root_cause}")

        key_findings = data.get("key_findings", []) or []
        if key_findings:
            parts.append("\n**关键发现**")
            for f in key_findings[:3]:
                parts.append(f"- {f}")
            if len(key_findings) > 3:
                parts.append(f"- …（共 {len(key_findings)} 条，详见附件报告）")

        evidence = data.get("evidence", []) or []
        if evidence:
            parts.append("\n**关键证据**")
            for e in evidence[:2]:
                parts.append(f"- {e}")
            if len(evidence) > 2:
                parts.append(f"- …（共 {len(evidence)} 条，详见附件报告）")

        suggestions = data.get("suggestions", []) or []
        if suggestions:
            parts.append("\n**改进建议**")
            for s in suggestions[:2]:
                parts.append(f"- {s}")
            if len(suggestions) > 2:
                parts.append(f"- …（共 {len(suggestions)} 条，详见附件报告）")

        # 视觉分析一句话提示
        if vision_analysis:
            parts.append("\n**视觉分析**: 已结合视频/图片进行综合分析，详见附件报告。")

        return "\n".join(parts) if parts else analysis

    @staticmethod
    def _render_analysis(analysis: str) -> str:
        """将 AI 返回的 JSON 或纯文本渲染为可读格式。"""
        # 尝试提取 JSON
        import json as _json
        json_str = analysis.strip()
        # 去掉可能的 markdown 代码块标记
        if json_str.startswith("```"):
            json_str = _json.loads.__module__  # skip, use regex below
            import re as _re
            m = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', analysis, _re.DOTALL)
            if m:
                json_str = m.group(1).strip()
            else:
                json_str = analysis.strip()

        try:
            data = _json.loads(json_str)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, _json.JSONDecodeError):
            # 非结构化输出，原样返回
            return analysis

        # 结构化渲染
        parts = []
        if data.get("summary"):
            parts.append(f"**摘要**: {data['summary']}")
        if data.get("confidence"):
            parts.append(f"**置信度**: {data['confidence']}")
        if data.get("severity"):
            parts.append(f"**严重程度**: {data['severity']}")
        if data.get("severity_reassessment"):
            parts.append(f"**严重程度重评**: {data['severity_reassessment']}")

        if data.get("root_cause"):
            parts.append(f"\n**根因分析**\n{data['root_cause']}")

        if data.get("system_status"):
            parts.append(f"\n**系统状态**: {data['system_status']}")
        if data.get("abnormal_modules"):
            parts.append("**异常模块**")
            for m in data["abnormal_modules"]:
                parts.append(f"- {m}")

        if data.get("correlation"):
            parts.append(f"\n**关联性分析**\n{data['correlation']}")

        if data.get("evidence"):
            parts.append("\n**关键证据**")
            for e in data["evidence"]:
                parts.append(f"- {e}")

        if data.get("key_findings"):
            parts.append("\n**关键发现**")
            for f in data["key_findings"]:
                parts.append(f"- {f}")

        if data.get("event_timeline"):
            parts.append("\n**事件时间线**")
            for t in data["event_timeline"]:
                parts.append(f"- {t}")

        if data.get("impact"):
            parts.append(f"\n**影响范围**\n{data['impact']}")
        if data.get("recovery_assessment"):
            parts.append(f"\n**恢复评估**\n{data['recovery_assessment']}")

        if data.get("suggestions"):
            parts.append("\n**改进建议**")
            for s in data["suggestions"]:
                parts.append(f"- {s}")

        return "\n".join(parts) if parts else analysis

    @staticmethod
    def _pre_detect_anomalies(log_summary: dict, defect_info: dict) -> str:
        """日志异常预检测：在LLM分析前快速识别日志中的异常模式。

        Returns:
            异常提示文本（如无异常返回空字符串）
        """
        hints = []
        key_logs = log_summary.get("key_logs", [])
        total_lines = log_summary.get("total_lines", 0)
        ew_count = log_summary.get("ew_count", 0)
        nav_transitions = log_summary.get("nav_transitions", [])
        fault_count = log_summary.get("fault_count", 0)

        # 1. E/W 密度异常
        if total_lines > 0:
            ew_ratio = ew_count / total_lines
            if ew_ratio > 0.05:
                hints.append(f"日志中错误/警告密度极高 ({ew_ratio:.1%})，可能存在系统性故障")
            elif ew_ratio > 0.02:
                hints.append(f"日志中错误/警告密度较高 ({ew_ratio:.1%})，建议重点关注")

        # 2. 故障上下文数量
        if fault_count >= 3:
            hints.append(f"窗口期内检测到 {fault_count} 个故障上下文，可能存在连锁故障或高频异常")
        elif fault_count >= 1:
            hints.append(f"窗口期内检测到 {fault_count} 个故障上下文")

        # 3. 状态转换异常检测
        status_sequence = [n.get("msg", "") for n in nav_transitions]
        status_text = " ".join(status_sequence)

        # 检测状态快速跳动（如 total_clean -> idle -> back_charge 在短时间）
        rapid_switch = False
        for i, st in enumerate(status_sequence):
            if "work_status_total_clean" in st or "work_status_area_clean" in st:
                # 向后查找是否快速出现 idle
                for j in range(i + 1, min(i + 3, len(status_sequence))):
                    if "work_status_idle" in status_sequence[j]:
                        rapid_switch = True
                        break
        if rapid_switch:
            hints.append("检测到清扫状态快速跳转到idle，可能存在任务异常终止或状态机缺陷")

        # 检测回充->充电的快速转换（未经历 base_station）
        if "work_status_back_charge" in status_text and "work_status_charging" in status_text:
            has_base_station = "work_status_base_station" in status_text
            if not has_base_station:
                hints.append("检测到回充直接转为充电但缺少base_station状态，可能存在充电状态虚假上报")

        # 4. 传感器异常检测
        sensor_errors = []
        for log in key_logs:
            msg = log.get("msg", "")
            if "yaw" in msg.lower() and ("error" in msg.lower() or "abnormal" in msg.lower()):
                sensor_errors.append("IMU yaw异常判定")
                break
        if sensor_errors:
            hints.append(f"传感器异常信号: {', '.join(sensor_errors)}")

        # 5. 模块崩溃/重启信号
        restart_signals = ["restart", "reboot", "reset", "panic", "oom", "out of memory"]
        restart_count = sum(
            1 for log in key_logs
            if any(s in log.get("msg", "").lower() for s in restart_signals)
        )
        if restart_count >= 3:
            hints.append(f"检测到 {restart_count} 次模块重启/崩溃信号，可能存在内存泄漏或稳定性问题")

        # 6. 通信异常
        comm_errors = sum(
            1 for log in key_logs
            if any(k in log.get("msg", "").lower() for k in ["timeout", "disconnect", "offline", "lost"])
        )
        if comm_errors >= 5:
            hints.append(f"检测到 {comm_errors} 次通信异常（超时/断连/离线），建议检查通信链路")

        # 7. 地图操作异常检测（在非初始化场景中出现地图重置为高危信号）
        map_reset_ops = []
        for log in key_logs:
            msg = log.get("msg", "")
            if "ResetAll" in msg or "ResetSomeMap" in msg:
                map_reset_ops.append(log.get("time", "?"))
        if map_reset_ops:
            hints.append(f"检测到 {len(map_reset_ops)} 次地图重置操作 (ResetAll/ResetSomeMap) at {', '.join(map_reset_ops[:5])}，可能触发重新建图")

        # 8. 定位状态异常检测
        has_pose_lost = any("no pose" in log.get("msg", "").lower() or "pose lost" in log.get("msg", "").lower() for log in key_logs)
        has_recovery = any("recovery map" in log.get("msg", "").lower() for log in key_logs)
        if has_pose_lost and has_recovery:
            hints.append("检测到定位丢失(no pose) → 地图恢复(Recovery map)的异常因果链，定位失败可能是地图异常的直接原因")
        elif has_pose_lost:
            hints.append("检测到定位丢失信号 (no pose / pose lost)，需评估对导航和建图的影响")

        # 9. 任务节点生命周期异常
        create_events = [log for log in key_logs if "start_node" in log.get("msg", "") or "GoBackStation recv id:start" in log.get("msg", "")]
        destroy_events = [log for log in key_logs if "stop_node" in log.get("msg", "") or "destroy" in log.get("msg", "")]
        if len(create_events) > len(destroy_events) + 2:
            hints.append(f"任务节点创建({len(create_events)})与销毁({len(destroy_events)})不匹配，可能存在节点生命周期异常")

        if not hints:
            return ""

        return "## 【日志异常预检测】规则引擎自动识别的高危信号\n" + "\n".join(f"- {h}" for h in hints)

    def _rewrite_analysis(self, original_analysis: str, defect_info: dict,
                          log_summary: dict, rewrite_reason: str,
                          pattern_hints: str = "", system_prompt: str = "",
                          domain_knowledge: str = "", mandatory_signals: str = "") -> Optional[str]:
        """当 _validate_analysis 检测到严重违规时，强制重写分析。

        构建修正 prompt 重新调用 LLM，要求纠正已知的错误。
        """
        title = defect_info.get("title", "")
        key_logs = log_summary.get("key_logs", [])
        log_text = "\n".join(
            f"  {l['time']} {l['level']}/{l.get('file', '')} {l['msg'][:200]}"
            for l in key_logs[:60]
        ) or "  无关键日志"

        nav_text = "\n".join(
            f"  {n['time']} {n['msg'][:150]}"
            for n in log_summary.get("nav_transitions", [])[:15]
        ) or "  无状态转换记录"

        # 提取标题关键词用于强制约束
        t_lower = title.lower()
        title_keywords = []
        for k in ["地毯", "毛毯", "carpet", "rug",
                  "避障", "绕障", "碰撞", "沿墙", "obstacle",
                  "回充", "充电", "基站", "dock",
                  "烘干", "dry", "洗拖布", "wash",
                  "ota", "升级", "app", "弹框",
                  "地图", "建图", "map", "slam",
                  "错图", "路径", "path"]:
            if k in t_lower:
                title_keywords.append(k)
        kw_constraint = f"摘要中必须包含以下关键词中的至少2个：{title_keywords[:6]}" if len(title_keywords) >= 2 else ""

        correction_prompt = f"""你之前对以下缺陷的日志分析被自动校验系统驳回，请基于原始日志重新分析并纠正错误。

## 驳回原因
{rewrite_reason}

## 缺陷信息
- 标题: {title}
- SN: {defect_info.get('sn', '')}
- 分类: {defect_info.get('category', '')}

## 被驳回的原分析（仅作参考，不要重复其错误）
{original_analysis[:500]}

## 原始关键日志
{log_text}

## 状态转换明细
{nav_text}

## 强制纠正要求（必须严格遵守）
1. 分析必须紧扣缺陷标题中的核心现象，禁止偏离主题讨论无关模块
2. {kw_constraint}
3. 禁止将 IMU yaw 累积值作为异常证据或根因
4. 引用 "line laser: sensor closed" 前必须先论证当前场景是否需要线激光；无法论证则完全忽略
5. 每个证据必须包含具体时间戳和来源模块，禁止无时间戳的泛泛描述
6. 禁止编造不存在的日志内容

请严格输出以下 JSON 格式（不要包含 markdown 代码块标记）：
{{
  "root_cause_type": "软件逻辑缺陷 / 传感器硬件故障 / 环境因素 / 配置问题",
  "root_cause": "根因分析（必须区分触发条件和软件逻辑缺陷，引用具体时间戳和模块）",
  "state_machine_analysis": "work_status 状态转换链分析",
  "evidence": ["证据1: [时间] [模块] 具体描述", "证据2: ..."],
  "key_findings": ["关键发现1", "关键发现2"],
  "correlation": "缺陷现象与日志异常之间的关联性分析",
  "suggestions": ["改进建议1", "改进建议2"],
  "severity_reassessment": "S/A/B/C 之一",
  "confidence": "高/中/低",
  "summary": "一句话总结（50字以内，必须包含缺陷标题核心关键词）"
}}"""

        # 使用主 LLM 进行重写，强制 temperature 更低以获得更遵守指令的输出
        logger.info("分析触发强制重写，原因: %s", rewrite_reason)
        result = self.analyzer._call_llm(correction_prompt, system_prompt=system_prompt)
        if result:
            logger.info("强制重写完成")
        else:
            logger.warning("强制重写失败，保留原分析")
        return result

    @staticmethod
    def _validate_analysis(analysis: str, defect_info: dict) -> tuple:
        """后处理校验：自动检测常见分析错误并修正。

        Returns:
            (修正后的分析文本, 警告列表, 重写原因)
            rewrite_reason: 空字符串表示无需重写；非空表示检测到严重违规，需要强制重写
        """
        warnings = []
        rewrite_reason = ""
        title = defect_info.get("title", "")
        text_lower = analysis.lower()

        # 校验1: IMU yaw 禁令
        # 检测分析中是否将yaw累积值作为异常证据（排除"正常""累积"等解释性词语）
        yaw_violation = False
        if "yaw" in text_lower:
            # 如果在提到yaw的上下文中没有"正常""累积"等词，且不是明确说yaw正常
            import re as _re
            # 查找yaw附近的上下文（前后50字符）
            for m in _re.finditer(r'yaw[=\s]*\d+', text_lower):
                start = max(0, m.start() - 50)
                end = min(len(text_lower), m.end() + 50)
                ctx = text_lower[start:end]
                if "正常" not in ctx and "累积" not in ctx and "零漂" not in ctx:
                    yaw_violation = True
                    break
            # 更简单的检测：如果标题是地毯/毛毯相关但分析提到yaw
            if any(k in title.lower() for k in ["地毯", "毛毯", "carpet", "rug"]):
                if "yaw" in text_lower and "正常" not in text_lower:
                    yaw_violation = True

        if yaw_violation:
            warnings.append("疑似违规引用IMU yaw作为缺陷证据，已标注")
            if any(k in title.lower() for k in ["地毯", "毛毯", "carpet", "rug"]):
                rewrite_reason = "分析偏离缺陷主题：地毯类缺陷不应引用IMU yaw作为根因，请围绕地毯检测/清洁模式切换重新分析"

        # 校验2: 线激光关闭禁令
        if "line laser" in text_lower and "sensor closed" in text_lower:
            # 检查是否将其作为根因
            if "根因" in analysis or "root_cause" in text_lower:
                if "未开启" in analysis or "关闭" in analysis or "缺少" in analysis:
                    warnings.append("疑似违规将线激光sensor closed作为缺陷证据，已标注")
                    rewrite_reason = "线激光引用违规：未论证场景必要性即将sensor closed作为根因，请重新分析并忽略此条日志或充分论证场景必要性"

        # 校验3: 跑题检测
        title_keywords = []
        t_lower = title.lower()
        # 提取标题核心关键词（长度≥2的中文词或英文词）
        for k in ["地毯", "毛毯", "carpet", "rug", "避毯",
                  "避障", "绕障", "碰撞", "沿墙", "obstacle",
                  "回充", "充电", "基站", "dock", "charge",
                  "地图", "建图", "map", "slam",
                  "烘干", "dry", "洗拖布", "wash",
                  "风机", "吸力", "suction", "fan",
                  "ota", "升级", "update",
                  "app", "弹框", "页面", "ui",
                  "集尘", "dust",
                  "错图", "路径", "path"]:
            if k in t_lower:
                title_keywords.append(k)

        # 尝试解析JSON检测摘要
        summary = ""
        try:
            import json as _json
            import re as _re
            json_str = analysis.strip()
            if json_str.startswith("```"):
                m = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', analysis, _re.DOTALL)
                if m:
                    json_str = m.group(1).strip()
            data = _json.loads(json_str)
            if isinstance(data, dict):
                summary = data.get("summary", "") + " " + data.get("root_cause", "")
        except Exception:
            summary = analysis[:500]

        summary_lower = summary.lower()
        matched_kw = [kw for kw in title_keywords if kw in summary_lower]
        # 如果标题有明确关键词但摘要中一个都没出现，可能跑题
        if len(title_keywords) >= 2 and not matched_kw:
            warnings.append("分析摘要与缺陷标题关键词关联度低，疑似跑题，已降低置信度")
            rewrite_reason = f"分析跑题：缺陷标题核心关键词为 {title_keywords[:5]}，但分析摘要/根因中一个都未出现，请围绕缺陷标题重新分析"

        # 如果有警告，尝试在JSON中插入并降低置信度
        if warnings and analysis.strip().startswith("{"):
            try:
                import json as _json
                import re as _re
                json_str = analysis.strip()
                if json_str.startswith("```"):
                    m = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', analysis, _re.DOTALL)
                    if m:
                        json_str = m.group(1).strip()
                data = _json.loads(json_str)
                if isinstance(data, dict):
                    # 降低置信度
                    data["confidence"] = "低"
                    # 在key_findings或根因前添加警告
                    warn_text = "【分析校验警告】" + "；".join(warnings)
                    if "key_findings" not in data or not isinstance(data.get("key_findings"), list):
                        data["key_findings"] = []
                    data["key_findings"].insert(0, warn_text)
                    corrected = _json.dumps(data, ensure_ascii=False, indent=2)
                    return corrected, warnings, rewrite_reason
            except Exception:
                pass

        # 如果不能修改JSON，在文本开头追加警告
        if warnings:
            warn_header = "【AI分析校验警告】\n" + "\n".join(f"- {w}" for w in warnings) + "\n\n"
            return warn_header + analysis, warnings, rewrite_reason

        return analysis, warnings, rewrite_reason

    @staticmethod
    def _parse_analysis_json(analysis: str) -> dict:
        """尝试从分析结果中解析 JSON dict，失败返回空 dict。"""
        import json as _json
        import re as _re
        json_str = analysis.strip()
        if json_str.startswith("```"):
            m = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', analysis, _re.DOTALL)
            if m:
                json_str = m.group(1).strip()
        try:
            data = _json.loads(json_str)
            return data if isinstance(data, dict) else {}
        except (ValueError, _json.JSONDecodeError):
            return {}

    @staticmethod
    def _extract_confidence(analysis: str) -> str:
        """从分析结果中提取置信度字段。返回 '高'/'中'/'低'/'未知'。"""
        data = LogAnalysisIntegration._parse_analysis_json(analysis)
        return data.get("confidence", "未知")

    def close(self):
        if self.analyzer:
            self.analyzer.close()
        if self.vision:
            self.vision.close()
        if self.knowledge_base.enabled:
            self.knowledge_base.apply_feedback_from_file()
