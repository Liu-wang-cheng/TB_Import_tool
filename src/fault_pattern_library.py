"""故障模式库 - 已知故障模式匹配引擎"""

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class PatternMatch:
    """模式匹配结果"""
    pattern_id: str
    pattern_name: str
    confidence: float
    root_cause_hint: str
    suggestions: list
    auto_comment: str
    matched_evidence: list = field(default_factory=list)
    category_hint: str = ""
    severity: str = ""


class FaultPatternLibrary:
    """已知故障模式匹配引擎"""

    def __init__(self, config_path: str = "configs/fault_patterns.yaml"):
        self.patterns = []
        self._enabled = False
        self._compiled = {}
        self._load(config_path)

    def _load(self, config_path: str):
        path = Path(config_path)
        if not path.exists():
            logger.warning("故障模式库配置不存在: %s", config_path)
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            logger.warning("加载故障模式库失败: %s", e)
            return

        if not cfg.get("enabled", False):
            logger.info("故障模式库已禁用")
            return

        raw_patterns = cfg.get("patterns", [])
        for p in raw_patterns:
            try:
                self._compile_pattern(p)
                self.patterns.append(p)
            except Exception as e:
                logger.warning("编译模式 [%s] 失败: %s", p.get("id", "?"), e)

        self._enabled = True
        logger.info("故障模式库已加载 %d 个模式", len(self.patterns))

    def _compile_pattern(self, pattern: dict):
        pid = pattern["id"]
        self._compiled[pid] = {}
        for rule in pattern.get("match_rules", []):
            rtype = rule.get("type", "regex")
            if rtype == "regex":
                key = rule["regex"]
                if key not in self._compiled[pid]:
                    self._compiled[pid][key] = re.compile(key, re.IGNORECASE)
            elif rtype == "sequence":
                for step in rule.get("steps", []):
                    key = step["regex"]
                    if key not in self._compiled[pid]:
                        self._compiled[pid][key] = re.compile(key, re.IGNORECASE)
            elif rtype == "combined":
                for cond in rule.get("conditions", []):
                    key = cond["regex"]
                    if key not in self._compiled[pid]:
                        self._compiled[pid][key] = re.compile(key, re.IGNORECASE)
            if rule.get("must_not_match"):
                key = rule["must_not_match"]
                if key not in self._compiled[pid]:
                    self._compiled[pid][key] = re.compile(key, re.IGNORECASE)
            if rule.get("must_not_context"):
                key = rule["must_not_context"]
                if key not in self._compiled[pid]:
                    self._compiled[pid][key] = re.compile(key, re.IGNORECASE)

    def match(self, log_summary: dict, key_logs: list,
              fault_contexts: list = None, category: str = "") -> list:
        if not self._enabled or not self.patterns:
            return []

        log_text = self._build_log_text(log_summary, key_logs, fault_contexts)
        if not log_text:
            return []

        results = []
        for pattern in self.patterns:
            match_result = self._match_single(pattern, log_text, key_logs, category)
            if match_result:
                results.append(match_result)

        results.sort(key=lambda m: m.confidence, reverse=True)
        return results

    def _build_log_text(self, log_summary: dict, key_logs: list,
                        fault_contexts: list) -> str:
        parts = []
        if key_logs:
            for entry in key_logs:
                if isinstance(entry, dict):
                    parts.append(f"[{entry.get('time', '')}] {entry.get('level', '')} "
                                 f"{entry.get('source', '')} {entry.get('msg', '')}")
                else:
                    parts.append(str(entry))

        if fault_contexts:
            for fc in fault_contexts:
                event_chain = fc.get("event_chain", [])
                for evt in event_chain:
                    parts.append(str(evt))
                logs = fc.get("logs", [])
                for log_line in logs:
                    parts.append(str(log_line))

        if log_summary:
            transitions = log_summary.get("nav_transitions", [])
            for t in transitions:
                parts.append(f"[{t.get('time', '')}] {t.get('msg', '')}")

        return "\n".join(parts)

    def _match_single(self, pattern: dict, log_text: str,
                      key_logs: list, category: str = "") -> Optional[PatternMatch]:
        pid = pattern["id"]
        compiled = self._compiled.get(pid, {})
        rules = pattern.get("match_rules", [])

        if not rules:
            return None

        # ── 类别过滤：如果缺陷有分类且模式定义了适用类别，不匹配则跳过 ──
        applicable = pattern.get("applicable_categories", [])
        if category and applicable:
            # 支持前缀匹配：category="应用-地毯策略" 可以匹配 applicable="应用-地毯清洁策略"
            matched_cat = any(
                category == app or category.startswith(app) or app.startswith(category)
                for app in applicable
            )
            if not matched_cat:
                logger.debug("模式 [%s] 不适用分类 '%s'，跳过", pid, category)
                return None

        best_confidence = 0.0
        best_evidence = []

        for rule in rules:
            matched, confidence, evidence = self._evaluate_rule(
                rule, compiled, log_text, key_logs
            )
            if matched and confidence > best_confidence:
                best_confidence = confidence
                best_evidence = evidence

        if best_confidence <= 0:
            return None

        # must_not_match 检查（单个规则级别）
        must_not = rules[0].get("must_not_match") if rules else None
        if must_not and must_not in compiled:
            if compiled[must_not].search(log_text):
                return None

        # must_not_context 检查（全局上下文级别，排除特定场景）
        for rule in rules:
            must_not_ctx = rule.get("must_not_context")
            if must_not_ctx and must_not_ctx in compiled:
                if compiled[must_not_ctx].search(log_text):
                    logger.debug("模式 [%s] 命中 must_not_context，排除", pid)
                    return None

        base_confidence = pattern.get("confidence", 0.5)
        final_confidence = min(base_confidence * best_confidence, 1.0)

        return PatternMatch(
            pattern_id=pid,
            pattern_name=pattern.get("name", pid),
            confidence=round(final_confidence, 2),
            root_cause_hint=pattern.get("root_cause", ""),
            suggestions=pattern.get("suggestions", []),
            auto_comment=pattern.get("auto_comment", ""),
            matched_evidence=best_evidence[:5],
            category_hint=pattern.get("category_hint", ""),
            severity=pattern.get("severity", ""),
        )

    def _evaluate_rule(self, rule: dict, compiled: dict,
                       log_text: str, key_logs: list):
        rtype = rule.get("type", "regex")

        if rtype == "regex":
            return self._eval_regex_rule(rule, compiled, log_text, key_logs)
        elif rtype == "sequence":
            return self._eval_sequence_rule(rule, compiled, log_text)
        elif rtype == "combined":
            return self._eval_combined_rule(rule, compiled, log_text)
        return False, 0.0, []

    def _eval_regex_rule(self, rule: dict, compiled: dict,
                         log_text: str, key_logs: list):
        regex_str = rule["regex"]
        pat = compiled.get(regex_str)
        if not pat:
            return False, 0.0, []

        min_count = rule.get("min_count", 1)
        matches = pat.findall(log_text)

        if len(matches) >= min_count:
            evidence = [m[:200] if isinstance(m, str) else str(m)[:200]
                        for m in matches[:5]]
            confidence = min(len(matches) / max(min_count, 1), 1.0)
            return True, confidence, evidence
        return False, 0.0, []

    def _eval_sequence_rule(self, rule: dict, compiled: dict,
                            log_text: str):
        steps = rule.get("steps", [])
        required = rule.get("required_matches", len(steps))
        time_window = rule.get("time_window_seconds", 300)

        lines = log_text.split("\n")
        matched_steps = []
        evidence = []

        for step in steps:
            regex_str = step["regex"]
            pat = compiled.get(regex_str)
            if not pat:
                continue

            for i, line in enumerate(lines):
                if pat.search(line):
                    matched_steps.append(step.get("order", len(matched_steps) + 1))
                    evidence.append(line[:200])
                    break

        if len(matched_steps) >= required:
            if self._check_sequence_order(matched_steps):
                confidence = len(matched_steps) / max(len(steps), 1)
                return True, confidence, evidence
        return False, 0.0, []

    def _eval_combined_rule(self, rule: dict, compiled: dict,
                            log_text: str):
        conditions = rule.get("conditions", [])
        required = rule.get("required_conditions", len(conditions))

        met = 0
        evidence = []

        for cond in conditions:
            regex_str = cond["regex"]
            pat = compiled.get(regex_str)
            if not pat:
                continue

            matches = pat.findall(log_text)
            min_count = cond.get("min_count", 1)

            if len(matches) >= min_count:
                met += 1
                evidence.extend(
                    [m[:200] if isinstance(m, str) else str(m)[:200]
                     for m in matches[:3]]
                )

        if met >= required:
            confidence = met / max(len(conditions), 1)
            return True, confidence, evidence
        return False, 0.0, []

    @staticmethod
    def _check_sequence_order(orders: list) -> bool:
        if not orders:
            return False
        filtered = [o for o in orders if o is not None]
        return filtered == sorted(filtered)

    def format_pattern_hints(self, matches: list) -> str:
        if not matches:
            return ""

        parts = ["## 【预分析结论】基于已知故障模式库的智能匹配结果\n"]
        parts.append("以下故障模式已由规则引擎自动识别，请在此基础上进行精细化根因定位，")
        parts.append("**不要忽略这些高置信度提示**，也不要与这些已知模式矛盾。\n")

        for i, m in enumerate(matches, 1):
            parts.append(f"### 预分析 {i}: [{m.pattern_id}] {m.pattern_name}")
            parts.append(f"- 规则引擎置信度: {m.confidence:.0%}")
            if m.severity:
                parts.append(f"- 已知严重程度: {m.severity}")
            parts.append(f"- **建议聚焦方向**: {m.root_cause_hint}")
            if m.suggestions:
                parts.append("- **必须检查项**:")
                for s in m.suggestions:
                    parts.append(f"  - {s}")
            if m.matched_evidence:
                parts.append("- **规则引擎匹配到的关键证据**:")
                for e in m.matched_evidence[:3]:
                    parts.append(f"  > {e[:150]}")
            parts.append("")

        # 追加分析指令
        top = matches[0]
        parts.append("### 【分析指令】")
        parts.append(f"1. 你的分析应围绕 '{top.root_cause_hint}' 展开细化，找出具体的时间线和责任模块")
        parts.append("2. 如果日志证据与上述预分析结论一致，请在结论中明确引用该模式")
        parts.append("3. 如果日志证据与预分析结论矛盾，必须解释矛盾原因，不能简单忽略")
        parts.append("4. 优先验证规则引擎给出的'必须检查项'是否在日志中有对应证据\n")

        return "\n".join(parts)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pattern_count(self) -> int:
        return len(self.patterns)
