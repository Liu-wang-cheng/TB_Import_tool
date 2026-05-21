#!/usr/bin/env python3
"""AI缺陷分析工具 - HTML报告生成器
HTML report generator for HS4 batch analyzer.
Fault-centric layout: root cause → event chain → system health → details.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TAG_LABELS = {
    'trigger': '触发', 'state_change': '状态变更', 'error': '错误',
    'warning': '警告', 'recovery': '恢复', 'info': '信息',
}

STATUS_BADGE_MAP = {
    'work_status_error': 'error', 'work_status_total_clean': 'total',
    'work_status_back_charge': 'back', 'work_status_wash_mop': 'wash',
    'work_status_base_station': 'base',
}


def _utc_to_bj(time_str):
    if not time_str or ':' not in time_str:
        return time_str
    parts = time_str.split(':')
    h = (int(parts[0]) + 8) % 24
    return f'{h:02d}:{parts[1]}:{parts[2]}'


def generate_report(mem_records, top_records, drc_result, drc_total, report_path: Path, meta: dict, drc_status_changes=None):
    sn = meta.get('sn', '')
    fw = meta.get('fw', '')
    year = meta.get('year', '')
    month = meta.get('month', '')
    day = meta.get('day', '')
    utc_start = meta.get('utc_start', 0)
    utc_end = meta.get('utc_end', 0)
    bj_start = (utc_start + 8) % 24
    bj_end = (utc_end + 8) % 24

    def ts_label(ts):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        dt_bj = dt.replace(hour=(dt.hour + 8) % 24)
        return dt_bj.strftime('%H:%M:%S')

    # === Fault data ===
    fault_contexts = drc_result.get('fault_contexts', []) if drc_result else []
    fault_count = len(fault_contexts)
    fault_time_labels_bj = [_utc_to_bj(fc['error_time']) for fc in fault_contexts]

    # === Memory data ===
    mem_times = [ts_label(r['ts']) for r in mem_records]
    ram_used = [(r['ram'].get('total_kb', 0) - r['ram'].get('free_kb', 0)) / 1024 for r in mem_records]
    ram_free = [r['ram'].get('free_kb', 0) / 1024 for r in mem_records]

    KEY_PROCS = ['network', 'navigator_ll', 'app_sweeper2', 'slam_pose_provider_lds',
                 'sensor_node_carrier', 'debug_proxy', 'apos_server', 'sensor_node_lidar']
    proc_mem = defaultdict(list)
    for r in mem_records:
        found = {p: 0 for p in KEY_PROCS}
        for proc in r['processes']:
            cmd = proc['cmdline'].strip().split()[0] if proc['cmdline'] else ''
            for kp in KEY_PROCS:
                if kp in cmd:
                    found[kp] = proc['pss_kb'] / 1024
                    break
        for kp in KEY_PROCS:
            proc_mem[kp].append(found[kp])

    # === CPU data ===
    top_times = [ts_label(r['ts']) for r in top_records]
    cpu_usr = [r['cpu'].get('usr', 0) for r in top_records]
    cpu_sys = [r['cpu'].get('sys', 0) for r in top_records]
    cpu_idle = [r['cpu'].get('idle', 0) for r in top_records]

    proc_cpu = defaultdict(list)
    for r in top_records:
        found = {p: 0 for p in KEY_PROCS}
        for proc in r['processes']:
            cmd = proc['cmdline'].strip().split()[0] if proc['cmdline'] else ''
            for kp in KEY_PROCS:
                if kp in cmd:
                    found[kp] = proc['cpu_pct']
                    break
        for kp in KEY_PROCS:
            proc_cpu[kp].append(found[kp])

    # Memory/CPU peaks
    mem_peak = max(ram_used) if ram_used else 0
    cpu_peak_usr = max(cpu_usr) if cpu_usr else 0
    cpu_peak_sys = max(cpu_sys) if cpu_sys else 0

    # === DRC tables ===
    if drc_result:
        mod_rows = ''.join(
            f'<tr><td>{mod}</td><td>{cnt}</td><td>{cnt/drc_result["parsed"]*100:.1f}%</td></tr>'
            for mod, cnt in drc_result['modules'].most_common()
        )
        level_rows = ''.join(
            f'<tr><td>{lvl}</td><td>{cnt}</td><td>{cnt/drc_result["parsed"]*100:.1f}%</td></tr>'
            for lvl, cnt in drc_result['levels'].most_common()
        )
        error_rows = ''.join(
            f'<tr><td>{_utc_to_bj(e["time"])}</td><td><span class="badge-{e["level"].lower()}">{e["level"]}</span></td>'
            f'<td>{e["module"]}</td><td class="msg" style="white-space:normal;word-break:break-all">{e["msg"]}</td></tr>'
            for e in drc_result['errors_warns'][:60]
        )
        _TYPE_LABELS = {'nav': '导航状态', 'work_status': '工作状态', 'nav_state': '导航器状态', 'status': '组件状态'}
        nav_table_rows = ''.join(
            f'<tr><td>{_utc_to_bj(e["time"])}</td><td>{_TYPE_LABELS.get(e.get("type",""), e.get("type",""))}</td><td class="msg" style="white-space:normal;word-break:break-all">{e["msg"]}</td></tr>'
            for e in drc_result['nav_transitions'][:80]
        )
        nav_count = len(drc_result['nav_transitions'])
        err_count = len(drc_result['errors_warns'])
    else:
        mod_rows = level_rows = error_rows = nav_table_rows = ''
        nav_count = err_count = 0

    # === DRC filename status timeline ===
    if drc_status_changes:
        status_rows = ''.join(
            f'<tr><td>{e["time_bj"]}</td><td><span class="badge-{STATUS_BADGE_MAP.get(e["status"], "base")}">{e["status"]}</span></td><td class="msg" style="white-space:normal;word-break:break-all">{e["name"]}</td></tr>'
            for e in drc_status_changes
        )
    else:
        status_rows = ''

    # === Build fault analysis HTML ===
    fault_html = ''
    if fault_contexts:
        for idx, fc in enumerate(fault_contexts):
            bj_error = _utc_to_bj(fc['error_time'])
            bj_recovery = _utc_to_bj(fc['recovery_time']) if fc['recovery_time'] != '未恢复' else '未恢复'

            chain_html = ''.join(
                f'<div class="chain-item chain-{e["tag"]}">'
                f'<span class="chain-time">{_utc_to_bj(e["time"])}</span>'
                f'<span class="badge-{e["level"].lower()}">{e["level"]}</span>'
                f'<span class="chain-module">{e.get("module","")}</span>'
                f'<span class="chain-msg">{e["msg"]}</span>'
                f'<span class="chain-tag tag-{e["tag"]}">{TAG_LABELS.get(e["tag"],"")}</span>'
                f'</div>'
                for e in fc['event_chain']
            )

            logs_html = ''.join(
                f'<tr><td>{_utc_to_bj(l["time"])}</td><td><span class="badge-{l["level"].lower()}">{l["level"]}</span></td>'
                f'<td>{l["module"]}</td><td class="msg" style="white-space:normal;word-break:break-all">{l["msg"]}</td></tr>'
                for l in fc['logs']
            )

            fault_html += f'''
        <div class="fault-card">
          <div class="fault-header">
            <span class="fault-badge">故障 #{idx+1}</span>
          </div>
          <div class="fault-grid">
            <div class="fault-info"><div class="fault-label">发生时间</div><div class="fault-value">{bj_error} (BJ)</div></div>
            <div class="fault-info"><div class="fault-label">前置状态</div><div class="fault-value">{fc["from_state"]}</div></div>
            <div class="fault-info fault-root-cell"><div class="fault-label">根因</div><div class="fault-value fault-root">{fc["root_cause"]}</div></div>
            <div class="fault-info"><div class="fault-label">触发事件</div><div class="fault-value">{fc["root_cause_event"] or "无"}</div></div>
            <div class="fault-info"><div class="fault-label">持续时间</div><div class="fault-value">{fc["duration_str"]}</div></div>
            <div class="fault-info"><div class="fault-label">恢复时间</div><div class="fault-value">{bj_recovery} (BJ)</div></div>
          </div>
          <h3>事件链</h3>
          <div class="event-chain">{chain_html}</div>
          <details><summary>详细上下文日志 ({len(fc["logs"])} 条)</summary>
            <div class="scroll"><table>
              <tr><th>时间</th><th>级别</th><th>模块</th><th>消息</th></tr>
              {logs_html}
            </table></div>
          </details>
        </div>'''
    else:
        fault_html = '<div class="no-fault">该时段未检测到故障事件</div>'

    # Alert banner
    if fault_count > 0:
        root_causes = '；'.join(set(fc['root_cause'] for fc in fault_contexts))
        alert_html = f'<div class="alert alert-danger"><span class="alert-dot">&#9679;</span> 检测到 <strong>{fault_count}</strong> 次故障 | 根因: {root_causes}</div>'
    elif err_count > 0:
        alert_html = f'<div class="alert alert-warning"><span class="alert-dot">&#9679;</span> 检测到 {err_count} 条警告/错误日志（无故障状态转换）</div>'
    else:
        alert_html = '<div class="alert alert-ok"><span class="alert-dot">&#9679;</span> 该时段运行正常，未检测到故障或异常</div>'

    # Chart data JSON
    chart_data = json.dumps({
        'mem_times': mem_times, 'ram_used': ram_used, 'ram_free': ram_free,
        'proc_mem': {k: v for k, v in proc_mem.items()},
        'top_times': top_times, 'cpu_usr': cpu_usr, 'cpu_sys': cpu_sys, 'cpu_idle': cpu_idle,
        'proc_cpu': {k: v for k, v in proc_cpu.items()},
        'fault_times': fault_time_labels_bj,
    }, ensure_ascii=False)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HS4 诊断报告 {sn} {year}-{month}-{day} {bj_start:02d}:00-{bj_end:02d}:00 (BJ)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
<style>
  :root {{
    --color-primary: #1E40AF;
    --color-secondary: #3B82F6;
    --color-accent: #D97706;
    --color-bg: #F8FAFC;
    --color-fg: #1E3A8A;
    --color-muted: #E9EEF6;
    --color-border: #DBEAFE;
    --color-destructive: #DC2626;
    --color-surface: #FFFFFF;
    --color-text: #0F172A;
    --color-text-secondary: #475569;
    --font-heading: 'Microsoft YaHei', '微软雅黑', sans-serif;
    --font-body: 'Microsoft YaHei', '微软雅黑', sans-serif;
    --radius: 6px;
    --shadow: 0 1px 2px rgba(30,58,138,0.06), 0 4px 8px rgba(30,58,138,0.04);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --color-bg: #0B1220;
      --color-fg: #93C5FD;
      --color-muted: #1E293B;
      --color-border: #1E3A8A;
      --color-surface: #0F172A;
      --color-text: #F1F5F9;
      --color-text-secondary: #94A3B8;
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 4px 8px rgba(0,0,0,0.2);
    }}
    .alert-danger {{ background: rgba(220,38,38,0.15); color: #FCA5A5; }}
    .alert-warning {{ background: rgba(217,119,6,0.15); color: #FDBA74; }}
    .alert-ok {{ background: rgba(5,150,105,0.15); color: #6EE7B7; }}
    .fault-card {{ border-color: #7F1D1D; }}
    .chain-trigger {{ background: rgba(220,38,38,0.08); }}
    .chain-error {{ background: rgba(220,38,38,0.10); }}
    .chain-warning {{ background: rgba(217,119,6,0.08); }}
    .chain-recovery {{ background: rgba(5,150,105,0.08); }}
    details[open] {{ border-color: #334155; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: var(--font-body); margin: 0; padding: 16px;
    background: var(--color-bg); color: var(--color-text);
    line-height: 1.5; -webkit-font-smoothing: antialiased;
  }}
  .container {{ max-width: 1440px; margin: 0 auto; }}
  header {{
    display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 8px; margin-bottom: 16px;
    padding-bottom: 12px; border-bottom: 2px solid var(--color-primary);
  }}
  h1 {{ font-family: var(--font-heading); font-size: 22px; font-weight: 700; color: var(--color-primary); margin: 0; }}
  .meta {{ font-size: 12px; color: var(--color-text-secondary); }}
  h2 {{
    font-family: var(--font-heading); font-size: 15px; font-weight: 600;
    color: var(--color-fg); margin: 24px 0 10px; padding-bottom: 6px;
    border-bottom: 1px solid var(--color-border);
  }}
  h2 .section-tag {{
    display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 3px;
    vertical-align: middle; margin-left: 6px; font-weight: 700; letter-spacing: 0.3px;
  }}
  .tag-fault {{ background: var(--color-destructive); color: #fff; }}
  .tag-health {{ background: #059669; color: #fff; }}
  .tag-detail {{ background: #6B7280; color: #fff; }}
  h3 {{ font-family: var(--font-heading); font-size: 13px; font-weight: 600; color: var(--color-fg); margin: 12px 0 8px; }}

  /* Alert */
  .alert {{
    display: flex; align-items: center; gap: 10px;
    padding: 12px 16px; border-radius: var(--radius); margin-bottom: 16px;
    font-size: 14px; font-weight: 500; border-left: 4px solid transparent;
  }}
  .alert-danger {{ background: rgba(220,38,38,0.08); color: #991B1B; border-left-color: var(--color-destructive); }}
  .alert-warning {{ background: rgba(217,119,6,0.08); color: #92400E; border-left-color: var(--color-accent); }}
  .alert-ok {{ background: rgba(5,150,105,0.08); color: #065F46; border-left-color: #059669; }}
  .alert-dot {{ font-size: 14px; }}

  /* KPI */
  .kpi-row {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px; margin-bottom: 16px;
  }}
  .kpi {{
    background: var(--color-surface); border-radius: var(--radius);
    padding: 12px 14px; box-shadow: var(--shadow); border: 1px solid var(--color-border);
    border-top: 3px solid var(--color-secondary);
  }}
  .kpi.accent {{ border-top-color: var(--color-accent); }}
  .kpi.danger {{ border-top-color: var(--color-destructive); }}
  .kpi.ok {{ border-top-color: #059669; }}
  .kpi-val {{ font-family: var(--font-heading); font-size: 22px; font-weight: 700; color: var(--color-fg); line-height: 1.2; }}
  .kpi-val.danger-text {{ color: var(--color-destructive); }}
  .kpi-label {{ font-size: 11px; color: var(--color-text-secondary); text-transform: uppercase; letter-spacing: 0.4px; margin-top: 4px; }}

  /* Fault card */
  .fault-card {{
    background: var(--color-surface); border-radius: var(--radius);
    padding: 16px; box-shadow: var(--shadow); border: 2px solid rgba(220,38,38,0.3);
    margin-bottom: 16px;
  }}
  .fault-header {{ margin-bottom: 12px; }}
  .fault-badge {{
    display: inline-block; background: var(--color-destructive); color: #fff;
    padding: 3px 10px; border-radius: 4px; font-size: 13px; font-weight: 700;
    font-family: var(--font-heading); letter-spacing: 0.3px;
  }}
  .fault-grid {{
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 10px; margin-bottom: 12px;
  }}
  @media (max-width: 768px) {{ .fault-grid {{ grid-template-columns: 1fr 1fr; }} }}
  .fault-info {{
    background: var(--color-muted); border-radius: 4px; padding: 8px 10px;
  }}
  .fault-label {{
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px;
    color: var(--color-text-secondary); margin-bottom: 2px; font-weight: 600;
  }}
  .fault-value {{
    font-size: 13px; font-weight: 600; color: var(--color-text);
    font-family: var(--font-heading); word-break: break-all;
  }}
  .fault-root {{ color: var(--color-destructive); font-size: 15px; }}
  .fault-root-cell {{ grid-column: span 1; }}
  .no-fault {{
    background: rgba(5,150,105,0.06); border: 1px solid rgba(5,150,105,0.2);
    border-radius: var(--radius); padding: 16px; text-align: center;
    color: #065F46; font-weight: 500;
  }}

  /* Event chain */
  .event-chain {{ margin-bottom: 8px; }}
  .chain-item {{
    display: flex; align-items: center; gap: 6px;
    padding: 5px 10px; margin-bottom: 1px; font-size: 12px;
    border-left: 3px solid transparent; border-radius: 0 3px 3px 0;
  }}
  .chain-trigger {{ border-left-color: #DC2626; background: rgba(220,38,38,0.04); }}
  .chain-state_change {{ border-left-color: #D97706; background: rgba(217,119,6,0.04); }}
  .chain-error {{ border-left-color: #DC2626; background: rgba(220,38,38,0.06); }}
  .chain-warning {{ border-left-color: #D97706; background: rgba(217,119,6,0.06); }}
  .chain-recovery {{ border-left-color: #059669; background: rgba(5,150,105,0.06); }}
  .chain-info {{ border-left-color: #3B82F6; background: rgba(59,130,246,0.03); }}
  .chain-time {{
    font-family: var(--font-heading); font-size: 11px; color: var(--color-text-secondary);
    min-width: 70px; flex-shrink: 0;
  }}
  .chain-module {{ color: var(--color-text-secondary); font-size: 11px; min-width: 80px; flex-shrink: 0; }}
  .chain-msg {{ flex: 1; color: var(--color-text); white-space: normal; word-break: break-all; line-height: 1.4; }}
  .chain-tag {{
    font-size: 9px; padding: 1px 5px; border-radius: 3px;
    font-weight: 700; flex-shrink: 0; letter-spacing: 0.3px;
  }}
  .tag-trigger {{ background: #FEE2E2; color: #991B1B; }}
  .tag-state_change {{ background: #FEF3C7; color: #92400E; }}
  .tag-error {{ background: #FEE2E2; color: #991B1B; }}
  .tag-warning {{ background: #FEF3C7; color: #92400E; }}
  .tag-recovery {{ background: #D1FAE5; color: #065F46; }}
  .tag-info {{ background: #DBEAFE; color: #1E3A8A; }}

  /* Details / collapsible */
  details {{
    border: 1px solid var(--color-border); border-radius: 4px;
    padding: 6px 10px; margin-top: 8px;
  }}
  details summary {{
    cursor: pointer; font-size: 12px; font-weight: 600;
    color: var(--color-secondary); user-select: none;
  }}
  details summary:hover {{ color: var(--color-primary); }}

  /* Cards / Charts */
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-top: 10px; }}
  @media (max-width: 768px) {{ .grid {{ grid-template-columns: 1fr; }} body {{ padding: 10px; }} }}
  .card {{
    background: var(--color-surface); border-radius: var(--radius); padding: 12px;
    box-shadow: var(--shadow); border: 1px solid var(--color-border);
    min-height: 280px; position: relative;
  }}
  .full {{ grid-column: 1 / -1; }}

  /* Tables */
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }}
  th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top; }}
  th {{
    background: var(--color-muted); font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.3px; color: var(--color-text-secondary);
    position: sticky; top: 0;
  }}
  tr:hover td {{ background: var(--color-muted); }}
  .msg {{ max-width: 520px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--color-text-secondary); }}

  /* Badges */
  .badge-e {{ display: inline-block; background: var(--color-destructive); color: #fff; padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 700; }}
  .badge-w {{ display: inline-block; background: var(--color-accent); color: #fff; padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 700; }}
  .badge-d, .badge-i {{ display: inline-block; background: var(--color-muted); color: var(--color-text-secondary); padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 600; }}
  .badge-error {{ display: inline-block; background: var(--color-destructive); color: #fff; padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 700; }}
  .badge-total {{ display: inline-block; background: #059669; color: #fff; padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 700; }}
  .badge-back {{ display: inline-block; background: #7C3AED; color: #fff; padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 700; }}
  .badge-wash {{ display: inline-block; background: #0891B2; color: #fff; padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 700; }}
  .badge-base {{ display: inline-block; background: #475569; color: #fff; padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 700; }}

  /* Scroll */
  .scroll {{ overflow-x: auto; max-height: 420px; overflow-y: auto; }}
  .scroll::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  .scroll::-webkit-scrollbar-thumb {{ background: var(--color-border); border-radius: 3px; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>HS4 Diagnostic Report</h1>
    <div class="meta">{sn} &bull; {fw} &bull; {year}-{month}-{day} {bj_start:02d}:00-{bj_end:02d}:00 BJ &bull; {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
  </header>

  {alert_html}

  <!-- KPIs -->
  <div class="kpi-row">
    <div class="kpi danger"><div class="kpi-val {'danger-text' if fault_count > 0 else ''}">{fault_count}</div><div class="kpi-label">故障次数</div></div>
    <div class="kpi {'accent' if err_count > 0 else ''}"><div class="kpi-val">{err_count}</div><div class="kpi-label">错误/警告</div></div>
    <div class="kpi"><div class="kpi-val">{drc_result["parsed"] if drc_result else 0:,}</div><div class="kpi-label">解析日志行</div></div>
    <div class="kpi ok"><div class="kpi-val">{mem_peak:.0f} MB</div><div class="kpi-label">内存峰值</div></div>
    <div class="kpi ok"><div class="kpi-val">usr {cpu_peak_usr:.0f}% / sys {cpu_peak_sys:.0f}%</div><div class="kpi-label">CPU峰值</div></div>
    <div class="kpi"><div class="kpi-val">{len(mem_records)}/{len(top_records)}</div><div class="kpi-label">采样点(Mem/CPU)</div></div>
  </div>

  <!-- ============ Section 1: Fault Analysis ============ -->
  <h2>故障分析 <span class="section-tag tag-fault">FAULT</span></h2>
  {fault_html}

  <!-- ============ Section 2: System Health ============ -->
  <h2>系统资源监控 <span class="section-tag tag-health">HEALTH</span></h2>
  <div class="grid">
    <div class="card full"><canvas id="memChart" height="300"></canvas></div>
    <div class="card full"><canvas id="procMemChart" height="300"></canvas></div>
    <div class="card full"><canvas id="cpuChart" height="300"></canvas></div>
    <div class="card full"><canvas id="procCpuChart" height="300"></canvas></div>
  </div>

  <!-- ============ Section 3: Details ============ -->
  <h2>辅助数据 <span class="section-tag tag-detail">DETAILS</span></h2>

  <h3>DRC 文件名工作状态变化</h3>
  <div class="grid">
    <div class="card full"><div class="scroll"><table>
      <tr><th>北京时间</th><th>状态</th><th>文件名</th></tr>
      {status_rows}
    </table></div></div>
  </div>

  <h3>状态转换明细 ({nav_count})</h3>
  <div class="grid">
    <div class="card full"><div class="scroll"><table>
      <tr><th>时间</th><th>类型</th><th>转换</th></tr>
      {nav_table_rows}
    </table></div></div>
  </div>

  <h3>错误 / 警告明细 (Top 60)</h3>
  <div class="grid">
    <div class="card full"><div class="scroll"><table>
      <tr><th>时间</th><th>级别</th><th>模块</th><th>消息</th></tr>
      {error_rows}
    </table></div></div>
  </div>

  <h3>日志模块与级别分布</h3>
  <div class="grid">
    <div class="card"><div class="scroll"><table><tr><th>模块</th><th>行数</th><th>%</th></tr>{mod_rows}</table></div></div>
    <div class="card"><div class="scroll"><table><tr><th>级别</th><th>行数</th><th>%</th></tr>{level_rows}</table></div></div>
  </div>
</div>

<script>
const cd = {chart_data};
const DS_COLORS = ['#1E40AF','#3B82F6','#D97706','#059669','#7C3AED','#BE185D','#0891B2','#4F46E5'];
Chart.defaults.font.family = "'Microsoft YaHei', '微软雅黑', sans-serif";
Chart.defaults.color = '#475569';
Chart.defaults.scale.grid.color = '#E9EEF6';
Chart.register(ChartDataLabels);

const DL_CFG = {{ align: 'top', offset: 4, color: ctx => ctx.dataset.borderColor, font: {{ size: 12, weight: '600', family: "'Microsoft YaHei', '微软雅黑', sans-serif" }}, formatter: v => v != null ? v.toFixed(1) : '' }};
const DL_CFG_INT = {{ align: 'top', offset: 4, color: ctx => ctx.dataset.borderColor, font: {{ size: 12, weight: '600', family: "'Microsoft YaHei', '微软雅黑', sans-serif" }}, formatter: v => v != null ? Math.round(v) + '' : '' }};

// Fault line plugin: draw vertical dashed line at fault times on charts
const faultLinePlugin = {{
  id: 'faultLine',
  afterDraw(chart) {{
    const times = chart.options.plugins?.faultLine?.times || [];
    if (!times.length) return;
    const {{ ctx }} = chart;
    const xAxis = chart.scales.x;
    const yAxis = chart.scales.y;
    function toSec(s) {{ const p = s.split(':'); return parseInt(p[0])*3600 + parseInt(p[1])*60 + parseInt(p[2]||0); }}
    times.forEach(target => {{
      let best = 0, bestD = Infinity;
      chart.data.labels.forEach((l, i) => {{
        const d = Math.abs(toSec(l) - toSec(target));
        if (d < bestD) {{ bestD = d; best = i; }}
      }});
      const x = xAxis.getPixelForValue(best);
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, yAxis.top);
      ctx.lineTo(x, yAxis.bottom);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = 'rgba(220,38,38,0.5)';
      ctx.setLineDash([4, 4]);
      ctx.stroke();
      ctx.fillStyle = 'rgba(220,38,38,0.85)';
      ctx.font = "12px 'Microsoft YaHei', '微软雅黑', sans-serif";
      ctx.fillText('故障', x + 4, yAxis.top + 14);
      ctx.restore();
    }});
  }}
}};
Chart.register(faultLinePlugin);

const faultOpts = {{ plugins: {{ faultLine: {{ times: cd.fault_times }} }} }};

new Chart(document.getElementById('memChart'), {{
  type: 'line',
  data: {{
    labels: cd.mem_times,
    datasets: [
      {{ label: 'Used', data: cd.ram_used, borderColor: '#1E40AF', backgroundColor: 'rgba(30,64,175,0.08)', fill: true, tension: 0.3, borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 }},
      {{ label: 'Free', data: cd.ram_free, borderColor: '#059669', backgroundColor: 'rgba(5,150,105,0.06)', fill: true, tension: 0.3, borderWidth: 2, pointRadius: 0 }},
    ]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ position: 'top', align: 'end', labels: {{ usePointStyle: true, boxWidth: 8 }} }}, datalabels: DL_CFG, faultLine: {{ times: cd.fault_times }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'MB' }} }}, x: {{ grid: {{ display: false }} }} }}
  }}
}});

new Chart(document.getElementById('procMemChart'), {{
  type: 'line',
  data: {{
    labels: cd.mem_times,
    datasets: Object.keys(cd.proc_mem).map((k,i) => ({{
      label: k, data: cd.proc_mem[k], borderColor: DS_COLORS[i % DS_COLORS.length],
      backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 1.5
    }}))
  }},
  options: {{ responsive: true, maintainAspectRatio: false, interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ position: 'top', align: 'end', labels: {{ usePointStyle: true, boxWidth: 8, font: {{ size: 11 }} }} }}, datalabels: DL_CFG, faultLine: {{ times: cd.fault_times }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'MB (Pss)' }} }}, x: {{ grid: {{ display: false }} }} }}
  }}
}});

new Chart(document.getElementById('cpuChart'), {{
  type: 'line',
  data: {{
    labels: cd.top_times,
    datasets: [
      {{ label: 'usr', data: cd.cpu_usr, borderColor: '#1E40AF', backgroundColor: 'transparent', tension: 0.3, borderWidth: 2, pointRadius: 0 }},
      {{ label: 'sys', data: cd.cpu_sys, borderColor: '#D97706', backgroundColor: 'transparent', tension: 0.3, borderWidth: 2, pointRadius: 0 }},
      {{ label: 'idle', data: cd.cpu_idle, borderColor: '#059669', backgroundColor: 'transparent', tension: 0.3, borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 }},
    ]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ position: 'top', align: 'end', labels: {{ usePointStyle: true, boxWidth: 8 }} }}, datalabels: DL_CFG_INT, faultLine: {{ times: cd.fault_times }} }},
    scales: {{ y: {{ title: {{ display: true, text: '%' }}, min: 0, max: 100 }}, x: {{ grid: {{ display: false }} }} }}
  }}
}});

new Chart(document.getElementById('procCpuChart'), {{
  type: 'line',
  data: {{
    labels: cd.top_times,
    datasets: Object.keys(cd.proc_cpu).map((k,i) => ({{
      label: k, data: cd.proc_cpu[k], borderColor: DS_COLORS[i % DS_COLORS.length],
      backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 1.5
    }}))
  }},
  options: {{ responsive: true, maintainAspectRatio: false, interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ position: 'top', align: 'end', labels: {{ usePointStyle: true, boxWidth: 8, font: {{ size: 11 }} }} }}, datalabels: DL_CFG_INT, faultLine: {{ times: cd.fault_times }} }},
    scales: {{ y: {{ title: {{ display: true, text: '%CPU' }} }}, x: {{ grid: {{ display: false }} }} }}
  }}
}});
</script>
</body>
</html>'''

    report_path.write_text(html, encoding='utf-8')
    return report_path
