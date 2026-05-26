"""AI 分析 HTML 报告生成器

将 AI 日志分析结果渲染为美观的 HTML 报告（单文件、内联 CSS），
便于上传到 Teambition 附件后在浏览器中直接查看。
"""

import html
import json
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 日志分析报告 - {title}</title>
<style>
:root {{
  --bg: #f4f6f8;
  --card-bg: #ffffff;
  --text: #1f2937;
  --muted: #6b7280;
  --border: #e5e7eb;
  --primary: #2563eb;
  --primary-light: #dbeafe;
  --danger: #dc2626;
  --danger-light: #fee2e2;
  --warning: #d97706;
  --warning-light: #fef3c7;
  --success: #059669;
  --success-light: #d1fae5;
  --radius: 8px;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, "Noto Sans SC", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  padding: 24px 16px;
}}
.container {{
  max-width: 900px;
  margin: 0 auto;
}}
header {{
  margin-bottom: 20px;
}}
header h1 {{
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 4px;
}}
header .subtitle {{
  color: var(--muted);
  font-size: 13px;
}}
.card {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}}
.card h2 {{
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 6px;
}}
.meta-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}}
.meta-item {{
  font-size: 13px;
}}
.meta-item .label {{
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 2px;
}}
.meta-item .value {{
  font-weight: 500;
  word-break: break-all;
}}
.badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}}
.badge-primary {{ background: var(--primary-light); color: var(--primary); }}
.badge-danger  {{ background: var(--danger-light);  color: var(--danger); }}
.badge-warning {{ background: var(--warning-light); color: var(--warning); }}
.badge-success {{ background: var(--success-light); color: var(--success); }}
.overview {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
}}
.overview-item {{
  text-align: center;
  padding: 12px;
  border-radius: var(--radius);
  background: #f9fafb;
}}
.overview-item .num {{
  font-size: 22px;
  font-weight: 700;
  color: var(--primary);
}}
.overview-item .label {{
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
}}
.patterns {{
  list-style: none;
}}
.patterns li {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}}
.patterns li:last-child {{ border-bottom: none; }}
.pre-detect {{
  background: #fffbeb;
  border-left: 4px solid var(--warning);
  padding: 12px;
  border-radius: var(--radius);
  font-size: 13px;
}}
.pre-detect ul {{
  list-style: disc;
  padding-left: 18px;
  margin-top: 6px;
}}
.pre-detect li {{
  margin-bottom: 4px;
}}
/* 因果链时间轴 */
.causal-chain {{
  position: relative;
  padding-left: 32px;
  margin: 8px 0;
}}
.causal-chain::before {{
  content: "";
  position: absolute;
  left: 11px;
  top: 4px;
  bottom: 4px;
  width: 2px;
  background: linear-gradient(to bottom, var(--primary), var(--danger));
  border-radius: 1px;
}}
.cc-step {{
  position: relative;
  margin-bottom: 16px;
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 13px;
  line-height: 1.5;
}}
.cc-step:last-child {{
  margin-bottom: 0;
}}
.cc-step::before {{
  content: attr(data-step);
  position: absolute;
  left: -25px;
  top: 12px;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  color: #fff;
}}
.cc-step:nth-child(1) {{ background: #eff6ff; border-left: 3px solid #3b82f6; }}
.cc-step:nth-child(1)::before {{ background: #3b82f6; }}
.cc-step:nth-child(2) {{ background: #fff7ed; border-left: 3px solid #f97316; }}
.cc-step:nth-child(2)::before {{ background: #f97316; }}
.cc-step:nth-child(3) {{ background: #fef2f2; border-left: 3px solid #ef4444; }}
.cc-step:nth-child(3)::before {{ background: #ef4444; }}
.cc-step:nth-child(4) {{ background: #fef2f2; border-left: 3px solid #dc2626; }}
.cc-step:nth-child(4)::before {{ background: #dc2626; }}
.cc-time {{
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 2px;
  font-family: "SF Mono", "Cascadia Code", Consolas, monospace;
}}
.section-body {{
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
}}
.evidence-list, .finding-list, .timeline-list, .suggestion-list {{
  list-style: none;
  padding: 0;
}}
.evidence-list li, .finding-list li, .timeline-list li, .suggestion-list li {{
  padding: 8px 12px;
  margin-bottom: 6px;
  background: #f9fafb;
  border-radius: var(--radius);
  border-left: 3px solid var(--primary);
  font-size: 13px;
}}
.timeline-list li {{
  border-left-color: var(--success);
}}
.suggestion-list li {{
  border-left-color: var(--warning);
}}
.finding-list li {{
  border-left-color: #7c3aed;
}}
.state-machine {{
  background: #eff6ff;
  border-radius: var(--radius);
  padding: 12px;
  font-size: 13px;
  white-space: pre-wrap;
}}
.vision-section {{
  background: #fdf4ff;
  border-radius: var(--radius);
  padding: 12px;
  font-size: 13px;
  white-space: pre-wrap;
}}
.footer {{
  text-align: center;
  font-size: 12px;
  color: var(--muted);
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}}
code {{
  font-family: "SF Mono", Monaco, "Cascadia Code", Consolas, monospace;
  background: #f3f4f6;
  padding: 1px 4px;
  border-radius: 4px;
  font-size: 12px;
}}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>AI 日志分析报告</h1>
    <div class="subtitle">生成时间: {generated_at} · 分析窗口: ±10分钟</div>
  </header>

  <div class="card">
    <h2>缺陷信息</h2>
    <div class="meta-grid">
      <div class="meta-item">
        <div class="label">缺陷标题</div>
        <div class="value">{title}</div>
      </div>
      <div class="meta-item">
        <div class="label">设备 SN</div>
        <div class="value"><code>{sn}</code></div>
      </div>
      <div class="meta-item">
        <div class="label">时间范围</div>
        <div class="value">{time_range}</div>
      </div>
      <div class="meta-item">
        <div class="label">固件版本</div>
        <div class="value">{fw}</div>
      </div>
      <div class="meta-item">
        <div class="label">缺陷分类</div>
        <div class="value"><span class="badge badge-primary">{category}</span></div>
      </div>
      <div class="meta-item">
        <div class="label">严重程度</div>
        <div class="value">{severity}</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>分析概览</h2>
    <div class="overview">
      <div class="overview-item">
        <div class="num">{confidence}</div>
        <div class="label">置信度</div>
      </div>
      <div class="overview-item">
        <div class="num">{ew_count}</div>
        <div class="label">E/W 日志</div>
      </div>
      <div class="overview-item">
        <div class="num">{total_lines}</div>
        <div class="label">解析总行数</div>
      </div>
      <div class="overview-item">
        <div class="num">{fault_count}</div>
        <div class="label">故障上下文</div>
      </div>
    </div>
  </div>

  {pattern_section}

  {pre_detect_section}

  <div class="card">
    <h2>摘要</h2>
    <div class="section-body">{summary}</div>
  </div>

  <div class="card">
    <h2>根因分析</h2>
    <div class="section-body">{root_cause}</div>
  </div>

  {causal_chain_section}

  {state_machine_section}

  {evidence_section}

  {timeline_section}

  {findings_section}

  {impact_section}

  {suggestions_section}

  {vision_section}

  <div class="footer">
    本报告由 AI 自动生成，仅供参考 · 生成时间: {generated_at}
  </div>
</div>
</body>
</html>"""


def _safe(data: dict, key: str, default: str = "—") -> str:
    val = data.get(key)
    if val is None or val == "":
        return default
    return str(val)


def _esc(text: str) -> str:
    return html.escape(str(text))


def _parse_analysis_json(analysis: str) -> dict:
    """从 AI 返回的文本中提取 JSON 对象。"""
    text = analysis.strip()
    # 去掉 markdown 代码块
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def _build_list_section(title: str, items: list, css_class: str) -> str:
    if not items:
        return ""
    lis = "\n".join(f"<li>{_esc(item)}</li>" for item in items)
    return f"""<div class="card">
    <h2>{_esc(title)}</h2>
    <ul class="{css_class}">
      {lis}
    </ul>
  </div>"""


def generate_html_report(
    defect_info: dict,
    analysis: str,
    log_summary: Optional[dict] = None,
    pattern_matches: Optional[list] = None,
    pre_detect_signals: str = "",
    vision_analysis: str = "",
) -> str:
    """生成完整的 HTML 分析报告。

    Args:
        defect_info: 缺陷信息字典
        analysis: AI 分析返回的原始文本（JSON 或纯文本）
        log_summary: 日志摘要字典
        pattern_matches: 故障模式匹配结果列表
        pre_detect_signals: 预检测信号文本
        vision_analysis: 视觉分析文本

    Returns:
        HTML 字符串（完整单文件）
    """
    data = _parse_analysis_json(analysis)
    is_structured = bool(data)

    # 基础字段
    title = _esc(defect_info.get("title", "未知"))
    sn = _esc(defect_info.get("sn", "未知"))
    fw = _esc(defect_info.get("fw", "未知"))
    time_range = _esc(defect_info.get("time", "未知"))
    category = _esc(defect_info.get("category", "未知"))
    severity = _esc(
        data.get("severity_reassessment", data.get("severity", defect_info.get("severity", "未知")))
    )
    confidence = _esc(data.get("confidence", "未知"))
    summary = _esc(data.get("summary", "—"))
    root_cause = _esc(data.get("root_cause", analysis if not is_structured else "—"))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 日志统计
    ls = log_summary or {}
    total_lines = ls.get("total_lines", 0)
    ew_count = ls.get("ew_count", 0)
    fault_count = ls.get("fault_count", 0)

    # 故障模式匹配区
    pattern_section = ""
    if pattern_matches:
        rows = []
        for pm in pattern_matches:
            conf_str = f"{pm.confidence:.0%}" if hasattr(pm, "confidence") else "—"
            name = _esc(pm.pattern_name if hasattr(pm, "pattern_name") else str(pm))
            rows.append(f"""<li><span>{name}</span><span class="badge badge-warning">置信度 {conf_str}</span></li>""")
        pattern_section = f"""<div class="card">
    <h2>故障模式匹配</h2>
    <ul class="patterns">
      {"\n".join(rows)}
    </ul>
  </div>"""

    # 预检测信号区
    pre_detect_section = ""
    if pre_detect_signals:
        # 去掉 markdown 标题标记，转为列表
        clean = pre_detect_signals.replace("## 【日志异常预检测】规则引擎自动识别的高危信号", "").strip()
        lines = [line.lstrip("- ").strip() for line in clean.splitlines() if line.strip().startswith("-")]
        if lines:
            lis = "\n".join(f"<li>{_esc(l)}</li>" for l in lines)
            pre_detect_section = f"""<div class="card">
    <h2>预检测高危信号</h2>
    <div class="pre-detect">
      <ul>
        {lis}
      </ul>
    </div>
  </div>"""

    # 因果链时间轴
    causal_chain = data.get("causal_chain", [])
    causal_chain_section = ""
    if causal_chain and isinstance(causal_chain, list) and len(causal_chain) > 0:
        steps_html = []
        for i, step in enumerate(causal_chain):
            text = str(step).strip()
            # 解析 "[HH:MM:SS] 步骤N: 描述" 格式
            time_str = ""
            desc = text
            m = re.match(r'\[([^\]]+)\]\s*(.*)', text)
            if m:
                time_str = m.group(1)
                desc = m.group(2)
            steps_html.append(
                f'<div class="cc-step" data-step="{i + 1}">'
                f'<div class="cc-time">{_esc(time_str)}</div>'
                f'<div class="cc-desc">{_esc(desc)}</div>'
                f'</div>'
            )
        causal_chain_section = f"""<div class="card">
    <h2>因果链分析</h2>
    <div class="causal-chain">
      {chr(10).join(steps_html)}
    </div>
  </div>"""

    # 状态机分析
    state_machine = data.get("state_machine_analysis", "")
    state_machine_section = ""
    if state_machine:
        state_machine_section = f"""<div class="card">
    <h2>状态机分析</h2>
    <div class="state-machine">{_esc(state_machine)}</div>
  </div>"""

    # 关键证据
    evidence_section = _build_list_section("关键证据", data.get("evidence", []), "evidence-list")

    # 事件时间线
    timeline_section = _build_list_section("事件时间线", data.get("event_timeline", []), "timeline-list")

    # 关键发现
    findings_section = _build_list_section("关键发现", data.get("key_findings", []), "finding-list")

    # 影响范围
    impact = data.get("impact", "")
    impact_section = ""
    if impact:
        impact_section = f"""<div class="card">
    <h2>影响范围</h2>
    <div class="section-body">{_esc(impact)}</div>
  </div>"""

    # 改进建议
    suggestions_section = _build_list_section("改进建议", data.get("suggestions", []), "suggestion-list")

    # 视觉分析
    vision_section_html = ""
    if vision_analysis:
        vision_section_html = f"""<div class="card">
    <h2>视觉分析</h2>
    <div class="vision-section">{_esc(vision_analysis)}</div>
  </div>"""

    return _HTML_TEMPLATE.format(
        title=title,
        sn=sn,
        fw=fw,
        time_range=time_range,
        category=category,
        severity=severity,
        confidence=confidence,
        summary=summary,
        root_cause=root_cause,
        generated_at=generated_at,
        total_lines=total_lines,
        ew_count=ew_count,
        fault_count=fault_count,
        pattern_section=pattern_section,
        pre_detect_section=pre_detect_section,
        causal_chain_section=causal_chain_section,
        state_machine_section=state_machine_section,
        evidence_section=evidence_section,
        timeline_section=timeline_section,
        findings_section=findings_section,
        impact_section=impact_section,
        suggestions_section=suggestions_section,
        vision_section=vision_section_html,
    )


def generate_html_bytes(
    defect_info: dict,
    analysis: str,
    log_summary: Optional[dict] = None,
    pattern_matches: Optional[list] = None,
    pre_detect_signals: str = "",
    vision_analysis: str = "",
) -> bytes:
    """生成 HTML 报告并返回 UTF-8 字节。"""
    html_text = generate_html_report(
        defect_info=defect_info,
        analysis=analysis,
        log_summary=log_summary,
        pattern_matches=pattern_matches,
        pre_detect_signals=pre_detect_signals,
        vision_analysis=vision_analysis,
    )
    return html_text.encode("utf-8")
