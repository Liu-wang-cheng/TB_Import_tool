"""领域知识 RAG - 按缺陷类别动态检索知识片段。

从 sweeper_knowledge_base.yaml 加载结构化知识，
根据缺陷分类返回最相关的知识片段，减少 SYSTEM_PROMPT 的静态长度。
"""

import logging
from pathlib import Path
from typing import Dict, List

import yaml

logger = logging.getLogger(__name__)


class KnowledgeRAG:
    """按缺陷类别动态检索领域知识。"""

    _DEFAULT_PATH = Path(__file__).parent.parent / "data" / "sweeper_knowledge_base.yaml"

    def __init__(self, path: str = None):
        self._path = Path(path) if path else self._DEFAULT_PATH
        self._data: dict = {}
        self._category_map: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if not self._path.exists():
            logger.warning("知识库文件不存在: %s", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
            self._build_category_map()
            logger.info("KnowledgeRAG 已加载，%d 个分类映射", len(self._category_map))
        except Exception as e:
            logger.warning("加载知识库失败: %s", e)

    # 类别别名映射：将TB/禅道中的细分分类映射到知识库主分类
    CATEGORY_ALIASES: Dict[str, str] = {
        "算法-避障": "算法",
        "算法-脱困、越障": "算法",
        "算法-运动控制": "算法",
        "算法-地图": "算法",
        "算法-回充": "算法",
        "算法-沿墙": "算法",
        "应用-状态机": "应用-状态机",
        "应用-地毯策略": "应用-地毯策略",
        "应用-基站交互": "应用-基站交互",
        "应用-UI交互": "APP端",
        "IOT-配网": "IOT",
        "IOT-客户APP问题": "IOT",
        "嵌入式--电池": "嵌入式",
        "硬件-信号质量": "硬件",
    }

    def _build_category_map(self):
        """构建类别 -> 知识片段索引。"""
        # 1. 分析关键词分类（直接映射）
        keywords = self._data.get("analysis_keywords", {})
        for cat_name, cat_data in keywords.items():
            self._category_map[cat_name] = {
                "focus_modules": cat_data.get("focus_modules", []),
                "keywords": cat_data.get("keywords", []),
                "banned_evidence": cat_data.get("banned_evidence", []),
                "root_cause_focus": cat_data.get("root_cause_focus", ""),
            }

        # 1.5 为避障碰撞类扩展专用 banned_evidence
        algo_data = self._category_map.get("算法", {})
        if algo_data:
            # 确保算法类有避障相关的禁止证据
            algo_banned = algo_data.setdefault("banned_evidence", [])
            obstacle_bans = [
                "禁止未论证场景就将'line laser: sensor closed'作为避障/碰撞缺陷的证据",
                "禁止将IMU yaw累积值作为避障/碰撞异常证据",
                "禁止将机器人被抱起时的pitch/roll变化作为避障失败的证据",
            ]
            for b in obstacle_bans:
                if b not in algo_banned:
                    algo_banned.append(b)

        # 2. 硬件保护（嵌入式、电机类）
        hw = self._data.get("hardware_protection", {})
        if hw:
            self._category_map["硬件保护"] = {"hardware_protection": hw}
            if "嵌入式" in self._category_map:
                self._category_map["嵌入式"]["hardware_protection"] = hw

        # 3. 系统架构（通用）
        arch = self._data.get("system_architecture", {})
        if arch:
            self._category_map["通用"] = {"system_architecture": arch}

        # 4. 状态机转换规则（应用-状态机）
        sm = self._data.get("state_machine", {})
        if sm:
            self._category_map.setdefault("应用-状态机", {})
            self._category_map["应用-状态机"]["state_machine"] = sm

        # 5. 区域/禁区逻辑（地图/区域类）
        zone = self._data.get("zone_forbidden_area_logic", {})
        if zone:
            self._category_map.setdefault("地图/区域/禁区", {})
            self._category_map["地图/区域/禁区"]["zone_logic"] = zone

        # 6. 断点续扫逻辑（应用-状态机）
        resume = self._data.get("resume_clean_logic", {})
        if resume:
            self._category_map.setdefault("应用-状态机", {})
            self._category_map["应用-状态机"]["resume_clean_logic"] = resume

        # 7. 自动分区逻辑（算法-地图）
        partition = self._data.get("auto_partition_logic", {})
        if partition:
            self._category_map.setdefault("地图/区域/禁区", {})
            self._category_map["地图/区域/禁区"]["auto_partition_logic"] = partition

        # 8. 故障码映射（通用）
        faults = self._data.get("fault_codes", {})
        if faults:
            for cat in self._category_map.values():
                cat.setdefault("fault_codes", faults)

        # 9. 传感器规格（通用）
        sensors = self._data.get("sensor_specs", {})
        if sensors:
            for cat in self._category_map.values():
                cat.setdefault("sensor_specs", sensors)

        # 10. 模块交互路径（通用）
        interactions = self._data.get("module_interactions", {})
        if interactions:
            for cat in self._category_map.values():
                cat.setdefault("module_interactions", interactions)

        # 11. 常见错误（通用，必须每个类别都有）
        mistakes = self._data.get("common_mistakes", [])
        if mistakes:
            for cat in self._category_map.values():
                cat.setdefault("common_mistakes", mistakes)

        # 12. 决策树（通用）
        opt = self._data.get("analysis_optimization", {})
        decision_tree = opt.get("decision_tree", [])
        if decision_tree:
            for cat in self._category_map.values():
                cat.setdefault("decision_tree", decision_tree)

        # 13. 建立别名反向索引（如 "算法-避障" -> "算法"）
        for alias, target in self.CATEGORY_ALIASES.items():
            if target in self._category_map and alias not in self._category_map:
                # 深拷贝避免互相污染
                import copy
                self._category_map[alias] = copy.deepcopy(self._category_map[target])
                # 为别名追加差异化标记
                self._category_map[alias]["_alias_of"] = target

    def retrieve(self, category: str) -> str:
        """根据缺陷分类检索相关知识文本。

        Args:
            category: 如 "算法-避障", "嵌入式", "应用-状态机" 等

        Returns:
            格式化的知识文本片段
        """
        if not self._data:
            return ""

        # 精确匹配 -> 前缀匹配
        data = self._category_map.get(category)
        if not data:
            for k, v in self._category_map.items():
                if category.startswith(k) or k.startswith(category):
                    data = v
                    break

        if not data:
            data = self._category_map.get("通用", {})

        parts = []

        # 按优先级组织知识
        if "focus_modules" in data:
            parts.append("### 重点关注模块")
            parts.append(", ".join(data["focus_modules"]))
            parts.append("")

        if "keywords" in data:
            parts.append("### 关键日志关键词")
            parts.append(", ".join(data["keywords"]))
            parts.append("")

        if "root_cause_focus" in data:
            parts.append(f"### 根因聚焦方向: {data['root_cause_focus']}")
            parts.append("")

        if "banned_evidence" in data:
            parts.append("### 禁止作为证据的内容")
            for b in data["banned_evidence"]:
                parts.append(f"- {b}")
            parts.append("")

        # 硬件保护
        hw = data.get("hardware_protection")
        if hw:
            parts.append("### 硬件保护阈值")
            for motor_name, motor_data in hw.items():
                parts.append(f"**{motor_data.get('name', motor_name)}**:")
                if "stall_protection" in motor_data:
                    sp = motor_data["stall_protection"]
                    parts.append(f"  堵转: {sp.get('threshold_current', '')} / {sp.get('trigger_time', '')} / {sp.get('recovery', '')}")
                if "integral_protection" in motor_data:
                    ip = motor_data["integral_protection"]
                    if "levels" in ip:
                        for lv in ip["levels"]:
                            parts.append(f"  积分: {lv.get('range', '')} @ {lv.get('rate', '')}速 → {lv.get('alarm_time', '')}报警")
                    else:
                        parts.append(f"  积分: {ip.get('threshold', '')} 累加{ip.get('accumulate_time', '')}停机")
                if "short_circuit" in motor_data:
                    parts.append(f"  短路: {motor_data['short_circuit']}")
            parts.append("")

        # 传感器规格（只取最相关的）
        sensors = data.get("sensor_specs", {})
        if sensors:
            relevant = []
            if any(k in category for k in ["imu", "传感器", "倾斜", "tilt"]):
                relevant.append("imu")
            if any(k in category for k in ["地检", "跌落", "悬崖", "cliff", "drop"]):
                relevant.append("cliff_sensor")
            if any(k in category for k in ["线激光", "避障", "沿墙", "laser"]):
                relevant.append("line_laser")
            if any(k in category for k in ["雷达", "lidar", "建图", "定位"]):
                relevant.append("lidar")
            if any(k in category for k in ["地毯", "carpet", "rug", "超声波"]):
                relevant.append("ultrasonic")
            if any(k in category for k in ["碰撞", "bumper", "卡困"]):
                relevant.append("bumper")
            if not relevant:
                relevant = list(sensors.keys())[:2]

            for key in relevant:
                if key in sensors:
                    s = sensors[key]
                    parts.append(f"### {s.get('description', key)}规格")
                    if "normal" in s:
                        parts.append(f"- 正常: {s['normal']}")
                    if "abnormal" in s:
                        parts.append(f"- 异常: {s['abnormal']}")
                    if "error_trigger_time" in s:
                        et = s["error_trigger_time"]
                        parts.append(f"- 报错时序: 单{et.get('single','')}/双{et.get('double','')}/三{et.get('triple','')}")
                    if "ai_ban" in s:
                        parts.append(f"- 禁止: {s['ai_ban']}")
                    parts.append("")

        # 状态机规则
        sm = data.get("state_machine")
        if sm:
            parts.append("### 状态机关键转换")
            for tr in sm.get("transitions", [])[:4]:
                parts.append(f"- {tr.get('from','')} + {tr.get('trigger','')} → 预期{tr.get('expected','')} / 实际BUG:{tr.get('actual_bug','')}")
            parts.append("")

        # 区域/禁区逻辑
        zone = data.get("zone_logic")
        if zone:
            parts.append("### 区域/禁区规则")
            zt = zone.get("zone_types", {})
            for zname, zd in zt.items():
                parts.append(f"- {zd.get('name', zname)}: {zd.get('behavior', '')}")
            parts.append("")

        # 断点续扫
        resume = data.get("resume_clean_logic")
        if resume:
            parts.append("### 断点续扫中断条件")
            for c in resume.get("interrupt_conditions", []):
                parts.append(f"- {c}")
            parts.append("")

        # 模块交互路径
        interactions = data.get("module_interactions", {})
        if interactions:
            parts.append("### 相关模块交互路径")
            for name, inter in list(interactions.items())[:3]:
                parts.append(f"- {inter.get('name', name)}: {' -> '.join(inter.get('path', []))}")
            parts.append("")

        # 常见错误反面教材
        mistakes = data.get("common_mistakes", [])
        if mistakes:
            parts.append("### 常见分析错误（禁止重复）")
            for m in mistakes[:3]:
                parts.append(f"- {m.get('mistake', '')}: {m.get('correct_approach', '')}")
            parts.append("")

        # 决策树相关分支
        dt = data.get("decision_tree", [])
        if dt:
            parts.append("### 快速定位指引")
            for branch in dt[:4]:
                cond = branch.get("condition", "")
                action = branch.get("action", "")
                skip = branch.get("skip", [])
                parts.append(f"- {cond} → {action}")
                if skip:
                    parts.append(f"  跳过: {', '.join(skip)}")
            parts.append("")

        return "\n".join(parts)

    def get_banned_evidence(self, category: str) -> List[str]:
        """获取某分类下禁止作为证据的内容列表。"""
        data = self._category_map.get(category, {})
        if not data:
            for k, v in self._category_map.items():
                if category.startswith(k) or k.startswith(category):
                    data = v
                    break
        return data.get("banned_evidence", [])
