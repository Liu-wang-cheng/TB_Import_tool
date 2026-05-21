#!/usr/bin/env python3
"""
AI缺陷分析工具 - 批量分析模块
Batch analyzer for HS4 robot logs.
Downloads memfile (MemFile/TopFile) and DRC logs for a time window,
extracts memory/CPU trends, faults, and generates a consolidated HTML report.
Supports checkpoint/resume to survive interruptions.
"""
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from report_generator import generate_report

# === Config ===
SERVER_URL = 'http://61.141.202.107:8008'
USERNAME = 'ldrobot-team'
PASSWORD = 'ldrobotlog4110'
MODEL = 'CLA_HS4'
SN = 'HQ5S00700002HC261300022'
YEAR, MONTH, DAY = '2026', '05', '19'
FW = 'AR-0.7.277.4377-2.1.41-23662-HQ5S00700002HC261300022-7caade1501fd'

# UTC 09:00-10:00 => Beijing 17:00-18:00
UTC_START_HOUR = 9
UTC_END_HOUR = 10

# Sampling: download 1 of every N DRC files to stay within time limits.
# Set to 1 for full download, 5 for ~20% sample.
DRC_SAMPLE_EVERY = 1

SCRIPT_DIR = Path(__file__).parent
CACHE_DIR = SCRIPT_DIR / 'cache' / f'{SN}_{YEAR}{MONTH}{DAY}'
CHECKPOINT = CACHE_DIR / 'checkpoint.json'
REPORT_PATH = SCRIPT_DIR / f'batch_report_{SN}_{YEAR}{MONTH}{DAY}_utc{UTC_START_HOUR}-{UTC_END_HOUR}.html'
MERGED_LOG = CACHE_DIR / 'merged_logs.txt'

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(CACHE_DIR / 'memfile', exist_ok=True)


def _auth_header():
    from base64 import b64encode
    creds = b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()
    return {'Authorization': f'Basic {creds}'}


def fetch_html(path: str) -> str:
    url = f'{SERVER_URL}{path}'
    req = Request(url, headers=_auth_header())
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode('utf-8', errors='replace')


def _parse_links(html: str) -> list:
    from html.parser import HTMLParser
    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
            self._href = None
            self._in_a = False
        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                d = dict(attrs)
                h = d.get('href', '')
                if h and h != '?' and not h.startswith('?C='):
                    self._href = h
                    self._in_a = True
        def handle_endtag(self, tag):
            if tag == 'a':
                self._in_a = False
                self._href = None
        def handle_data(self, data):
            if self._in_a and self._href:
                n = data.strip()
                if n and n != '[ICO]' and not n.startswith('['):
                    self.links.append((self._href, n))
    p = P()
    p.feed(html)
    res = []
    for href, name in p.links:
        if href in ('../', '/', '..') or name == 'Parent Directory' or href.startswith('?C='):
            continue
        res.append((href, name.rstrip('/')))
    return res


def download_file(remote_path: str, local_path: Path, progress=False) -> int:
    url = f'{SERVER_URL}{remote_path}'
    req = Request(url, headers=_auth_header())
    total = 0
    with urlopen(req, timeout=120) as resp:
        with open(local_path, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
    return total


# === Phase 1: List files ===
def list_memfiles():
    path = f'/{MODEL}/{SN}/{YEAR}/{MONTH}/{DAY}/{FW}/memfile'
    html = fetch_html(path)
    links = _parse_links(html)
    files = []
    for href, name in links:
        if not href.endswith('/') and name.endswith('.log'):
            files.append((f'{path}/{href}', name))
    return files


def list_drc_files():
    path = f'/{MODEL}/{SN}/{YEAR}/{MONTH}/{DAY}/{FW}/'
    html = fetch_html(path)
    links = _parse_links(html)
    files = []
    for href, name in links:
        if not href.endswith('/') and href.endswith('.drc.save'):
            files.append((f'{path}{href}', name))
    return files


def filter_by_utc_hour(files, start_h, end_h):
    """Filter files whose name contains timestamp in UTC range [start_h, end_h)."""
    res = []
    for remote, name in files:
        m = re.search(r'(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})', name)
        if not m:
            # memfile uses unix timestamp
            m2 = re.search(r'(\d{10,})', name)
            if m2:
                ts = int(m2.group(1))
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                if start_h <= dt.hour < end_h:
                    res.append((remote, name, ts))
            continue
        date_str, hh, mi, sec = m.groups()
        h = int(hh)
        if start_h <= h < end_h:
            # parse approximate timestamp for sorting
            ts = int(datetime(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]),
                              h, int(mi), int(sec), tzinfo=timezone.utc).timestamp())
            res.append((remote, name, ts))
    res.sort(key=lambda x: x[2])
    return res


# === Phase 2: Download with checkpoint ===
def load_checkpoint():
    if CHECKPOINT.exists():
        with open(CHECKPOINT, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_checkpoint(data):
    with open(CHECKPOINT, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def download_memfiles(memfiles):
    cp = load_checkpoint()
    done = set(cp.get('memfiles_done', []))
    out_dir = CACHE_DIR / 'memfile'
    new_cnt = 0
    for remote, name, ts in memfiles:
        if name in done:
            continue
        local = out_dir / name
        try:
            download_file(remote, local)
            done.add(name)
            new_cnt += 1
        except Exception as e:
            print(f'  [ERR] {name}: {e}')
        if new_cnt % 10 == 0:
            cp['memfiles_done'] = sorted(done)
            save_checkpoint(cp)
    cp['memfiles_done'] = sorted(done)
    save_checkpoint(cp)
    print(f'MemFile/TopFile downloaded: {new_cnt} new, {len(done)} total')


def download_and_merge_drc(drc_files):
    cp = load_checkpoint()
    done = set(cp.get('drc_done', []))
    merged_exists = MERGED_LOG.exists()
    new_cnt = 0
    skipped = 0
    total = len(drc_files)
    # Sampling
    sampled = drc_files[::DRC_SAMPLE_EVERY]
    print(f'DRC total in range: {total}, sample every {DRC_SAMPLE_EVERY} => {len(sampled)} to download')

    with open(MERGED_LOG, 'a' if merged_exists else 'w', encoding='utf-8') as out:
        for idx, (remote, name, ts) in enumerate(sampled):
            if name in done:
                skipped += 1
                continue
            local = CACHE_DIR / name
            try:
                download_file(remote, local)
                # Parse immediately and append text
                from drc_parser import extract_logs
                with open(local, 'rb') as f:
                    data = f.read()
                logs = extract_logs(data)
                for line in logs:
                    out.write(line + '\n')
                # Remove to save disk
                local.unlink()
                done.add(name)
                new_cnt += 1
                if (idx + 1) % 20 == 0:
                    cp['drc_done'] = sorted(done)
                    save_checkpoint(cp)
                    print(f'  Progress: {idx+1}/{len(sampled)}')
            except Exception as e:
                print(f'  [ERR] {name}: {e}')
    cp['drc_done'] = sorted(done)
    save_checkpoint(cp)
    print(f'DRC processed: {new_cnt} new, {skipped} skipped, {len(done)} total unique')


# === Phase 3: Parse memfile data ===
def parse_memfile(local_path: Path):
    """Parse MemFile log into structured dict."""
    text = local_path.read_text(encoding='utf-8', errors='replace')
    # Extract timestamp from filename
    m = re.search(r'(\d{10,})', local_path.name)
    ts = int(m.group(1)) if m else 0

    processes = []
    total_ram = {}
    for line in text.splitlines():
        # RAM summary line
        ram_m = re.search(r'RAM:\s+(\d+)K total,\s+(\d+)K free,\s+(\d+)K buffers,\s+(\d+)K cached,\s+(\d+)K shmem,\s+(\d+)K slab', line)
        if ram_m:
            total_ram = {
                'total_kb': int(ram_m.group(1)),
                'free_kb': int(ram_m.group(2)),
                'buffers_kb': int(ram_m.group(3)),
                'cached_kb': int(ram_m.group(4)),
                'shmem_kb': int(ram_m.group(5)),
                'slab_kb': int(ram_m.group(6)),
            }
            continue
        # Process line:  PID Vss Rss Pss Uss cmdline
        parts = line.strip().split()
        if len(parts) >= 6 and parts[0].isdigit():
            try:
                processes.append({
                    'pid': int(parts[0]),
                    'vss_kb': int(parts[1].rstrip('K')),
                    'rss_kb': int(parts[2].rstrip('K')),
                    'pss_kb': int(parts[3].rstrip('K')),
                    'uss_kb': int(parts[4].rstrip('K')),
                    'cmdline': ' '.join(parts[5:]),
                })
            except Exception:
                pass
    return {'ts': ts, 'processes': processes, 'ram': total_ram}


def parse_topfile(local_path: Path):
    """Parse TopFile log into structured dict."""
    text = local_path.read_text(encoding='utf-8', errors='replace')
    m = re.search(r'(\d{10,})', local_path.name)
    ts = int(m.group(1)) if m else 0

    cpu_summary = {}
    processes = []
    for line in text.splitlines():
        # CPU:  50% usr  50% sys   0% nic   0% idle   0% io   0% irq   0% sirq
        cm = re.search(r'CPU:\s+([\d.]+)%\s+usr\s+([\d.]+)%\s+sys\s+([\d.]+)%\s+nic\s+([\d.]+)%\s+idle\s+([\d.]+)%\s+io\s+([\d.]+)%\s+irq\s+([\d.]+)%\s+sirq', line)
        if cm:
            cpu_summary = {
                'usr': float(cm.group(1)),
                'sys': float(cm.group(2)),
                'nic': float(cm.group(3)),
                'idle': float(cm.group(4)),
                'io': float(cm.group(5)),
                'irq': float(cm.group(6)),
                'sirq': float(cm.group(7)),
            }
            continue
        # Load average: 11.37 11.54 11.56 2/231 1902
        lm = re.search(r'Load average:\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)', line)
        if lm:
            cpu_summary['load_avg_1'] = float(lm.group(1))
            cpu_summary['load_avg_5'] = float(lm.group(2))
            cpu_summary['load_avg_15'] = float(lm.group(3))
            continue
        # Process line: PID PPID USER STAT VSZ %VSZ %CPU COMMAND
        # Note top output has ANSI codes, strip them
        clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        parts = clean.split()
        if len(parts) >= 8 and parts[0].isdigit() and parts[1].isdigit():
            try:
                processes.append({
                    'pid': int(parts[0]),
                    'ppid': int(parts[1]),
                    'user': parts[2],
                    'stat': parts[3],
                    'vsz_kb': int(float(parts[4].rstrip('m')) * 1024) if 'm' in parts[4] else int(parts[4]),
                    'vsz_pct': float(parts[5].rstrip('%')),
                    'cpu_pct': float(parts[6].rstrip('%')),
                    'cmdline': ' '.join(parts[7:]),
                })
            except Exception:
                pass
    return {'ts': ts, 'processes': processes, 'cpu': cpu_summary}


# === Phase 4: Aggregate mem/top data ===
def aggregate_mem_top():
    mem_dir = CACHE_DIR / 'memfile'
    mem_records = []
    top_records = []
    for f in sorted(mem_dir.iterdir()):
        if f.name.startswith('MemFile'):
            mem_records.append(parse_memfile(f))
        elif f.name.startswith('TopFile'):
            top_records.append(parse_topfile(f))
    mem_records.sort(key=lambda x: x['ts'])
    top_records.sort(key=lambda x: x['ts'])
    return mem_records, top_records


# === Phase 5: Analyze DRC logs ===
LOG_RE = re.compile(
    r'^(\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{1,2}:\d{1,2}\.\d{3})'
    r'/([A-Z]{2,})\s+([DIWEF])'
    r'/([^:]+):(\d+)\s+(.*)$'
)


def parse_time(t):
    parts = t.split(':')
    sec = parts[2].split('.')
    ms = int(sec[1]) if len(sec) > 1 else 0
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(sec[0]) + ms / 1000.0


def analyze_merged_logs():
    if not MERGED_LOG.exists():
        return None, 0
    with open(MERGED_LOG, 'r', encoding='utf-8') as f:
        lines = [l.rstrip() for l in f if l.strip()]
    total = len(lines)
    modules = Counter()
    levels = Counter()
    errors_warns = []
    nav_transitions = []
    time_first = None
    time_last = None
    parsed = 0

    # Regex for specific patterns
    nav_re = re.compile(r'nav_\w+\s*--?>\s*nav_\w+')
    work_status_re = re.compile(r'work status change from (\w+) to (\w+)')
    nav_state_re = re.compile(r'navigator state change:\[(.+?)\]-->\[(.+?)\]')
    status_change_re = re.compile(r'(\w+)\s+status change:\s*(\w+)')

    # Known component status values
    STATUS_VALUE_MAP = {
        ('double_rotate_rag', '1'): '安装(复位)',
        ('double_rotate_rag', '2'): '取出/未安装',
        ('double_rotate_rag', '0'): '未知/初始',
    }

    # Transition type labels (Chinese)
    TYPE_LABELS = {'nav': '导航状态', 'work_status': '工作状态', 'nav_state': '导航器状态', 'status': '组件状态'}

    for line in lines:
        m = LOG_RE.match(line)
        if not m:
            continue
        date, time_str, mod, level, file, lineno, msg = m.groups()
        t = parse_time(time_str)
        if time_first is None:
            time_first = t
        time_last = t
        parsed += 1
        modules[mod] += 1
        levels[level] += 1

        if level in ('W', 'E'):
            _RARE = set('~@%^&*<>{}|\\')
            _garbage_pattern = re.match(r'^0\s+([\S]{1,3}\s+){3,}', msg)
            _low_value = any(p in msg for p in ('dataSourceChange', 'isWall:', 'isWall:0', 'isWall:1'))
            is_garbage = (
                len(msg.strip()) < 5
                or sum(1 for c in msg[:40] if c in _RARE) >= 1
                or bool(_garbage_pattern)
                or _low_value
            )
            if not is_garbage:
                display_mod = file if mod == 'NA' else mod
                errors_warns.append({
                    'time': time_str, 'module': display_mod, 'level': level,
                    'file': file, 'line': lineno, 'msg': msg,
                })

        if nav_re.search(msg):
            nav_transitions.append({'time': time_str, 'msg': msg, 'type': 'nav'})

        ws_m = work_status_re.search(msg)
        if ws_m:
            nav_transitions.append({'time': time_str, 'msg': f'工作状态: {ws_m.group(1)} -> {ws_m.group(2)}', 'type': 'work_status'})

        ns_m = nav_state_re.search(msg)
        if ns_m:
            nav_transitions.append({'time': time_str, 'msg': f'导航器: {ns_m.group(1)} -> {ns_m.group(2)}', 'type': 'nav_state'})

        sc_m = status_change_re.search(msg)
        if sc_m and 'work_status' not in msg and 'nav_state' not in msg:
            comp, val = sc_m.group(1), sc_m.group(2)
            desc = STATUS_VALUE_MAP.get((comp, val), val)
            nav_transitions.append({'time': time_str, 'msg': f'{comp} 状态变更: {desc}', 'type': 'status'})

    # === Fault Analysis ===
    error_transitions = [
        (parse_time(nt['time']), nt)
        for nt in nav_transitions
        if nt['type'] == 'work_status' and '-> work_status_error' in nt['msg']
    ]
    recovery_transitions = [
        (parse_time(nt['time']), nt)
        for nt in nav_transitions
        if nt['type'] == 'work_status' and 'work_status_error ->' in nt['msg']
    ]

    # Pre-filter all potentially relevant lines once (O(N+M))
    fault_window_lines = []
    for line in lines:
        m = LOG_RE.match(line)
        if not m:
            continue
        _, time_str, mod, level, file, lineno, msg = m.groups()
        is_relevant = (
            level in ('W', 'E')
            or 'RobotEventReport' in msg
            or 'event_id' in msg
            or 'status change' in msg
        )
        if is_relevant:
            fault_window_lines.append((parse_time(time_str), time_str, mod, level, msg))

    KNOWN_EVENTS = {
        'double_rotate_rag_out': '拖布取出/未安装',
        'double_rotate_rag_in': '拖布安装复位',
        'double_rotate_rag_install': '拖布安装',
        'double_rotate_rag_not_install': '拖布未安装',
    }

    def _fmt_time(sec):
        return f'{int(sec//3600):02d}:{int((sec%3600)//60):02d}:{int(sec%60):02d}'

    REGULAR_EVENTS = {
        'get_worked_area', 'get_navi_area', 'navigator_mop_arm_motor_extend',
        'navigator_side_brush_extend', 'notify_ai_avoid_pose',
        'change_water_pump_in_servce_manager', 'water_level_active_change',
        'line_laser_set_power_off', 'lidar2d_set_speed', 'music_play_notify',
        'carrier_ctrl_led',
    }

    fault_contexts = []
    for et_time, et_info in error_transitions:
        # Full window for root cause search: 120s before, 30s after
        context_logs = [
            (t, ts_str, mod, lvl, msg)
            for t, ts_str, mod, lvl, msg in fault_window_lines
            if et_time - 120 <= t <= et_time + 30
        ]

        # Phase 1: Find component status change before error
        trigger_comp = ''
        for t, ts_str, mod, lvl, msg in context_logs:
            if t > et_time:
                break
            m_sc = re.search(r'(\w+)\s+status change:\s*(\d+)', msg)
            if m_sc and 'work_status' not in msg and 'nav_state' not in msg:
                trigger_comp = m_sc.group(1)

        # Phase 2: Find the triggering RobotEventReport matching the component
        root_cause_event = ''
        if trigger_comp:
            for t, ts_str, mod, lvl, msg in context_logs:
                if t > et_time:
                    break
                m_rc = re.search(r'RobotEventReport:\s*(\S+)', msg)
                if m_rc and trigger_comp in m_rc.group(1):
                    root_cause_event = m_rc.group(1)
                    break
        else:
            # No status change found — use last non-regular RobotEventReport
            for t, ts_str, mod, lvl, msg in context_logs:
                if t > et_time:
                    break
                m_rc = re.search(r'RobotEventReport:\s*(\S+)', msg)
                if m_rc and m_rc.group(1) not in REGULAR_EVENTS:
                    root_cause_event = m_rc.group(1)

        # Phase 3: Human-readable root cause
        if root_cause_event:
            root_cause = KNOWN_EVENTS.get(root_cause_event, root_cause_event)
        elif trigger_comp:
            root_cause = f'{trigger_comp} 异常'
        else:
            root_cause = '未确定'

        # Find closest recovery
        recovery = None
        for rt_time, rt_info in recovery_transitions:
            if rt_time > et_time:
                recovery = (rt_time, rt_info)
                break

        duration_sec = int(recovery[0] - et_time) if recovery else None
        from_state = et_info['msg'].split('->')[0].split(':')[-1].strip()

        # Build event chain — only show core fault cascade events
        NOISE_PATTERNS = ('robot in small corner', 'fail to get', 'dataSourceChange', '0')
        event_chain = []
        for t, ts_str, mod, lvl, msg in context_logs:
            if t < et_time - 10:
                continue
            tag = 'info'
            if 'RobotEventReport' in msg:
                evt_name = re.search(r'RobotEventReport:\s*(\S+)', msg)
                if evt_name and evt_name.group(1) in REGULAR_EVENTS:
                    continue
                tag = 'trigger'
            elif 'status change' in msg and 'work_status' not in msg and 'work status' not in msg:
                tag = 'trigger'
            elif 'work_status' in msg or 'work status' in msg:
                tag = 'state_change'
            elif lvl in ('E', 'F'):
                tag = 'error'
            elif lvl == 'W':
                if any(p in msg for p in NOISE_PATTERNS):
                    continue  # Skip repetitive noise warnings
                tag = 'warning'
            else:
                continue
            event_chain.append({'time': ts_str, 'level': lvl, 'module': mod, 'msg': msg, 'tag': tag})

        # Add recovery event to chain
        if recovery:
            event_chain.append({
                'time': recovery[1]['time'],
                'level': 'I', 'module': '',
                'msg': recovery[1]['msg'],
                'tag': 'recovery',
            })

        fault_contexts.append({
            'error_time': _fmt_time(et_time),
            'from_state': from_state,
            'root_cause': root_cause,
            'root_cause_event': root_cause_event,
            'recovery_time': _fmt_time(recovery[0]) if recovery else '未恢复',
            'duration_sec': duration_sec,
            'duration_str': f'{duration_sec}s' if duration_sec else '未知',
            'event_chain': event_chain,
            'logs': [{'time': ts, 'module': mod, 'level': lvl, 'msg': msg}
                     for _, ts, mod, lvl, msg in context_logs],
        })

    result = {
        'total': total,
        'parsed': parsed,
        'time_first': time_first,
        'time_last': time_last,
        'modules': modules,
        'levels': levels,
        'errors_warns': errors_warns,
        'nav_transitions': nav_transitions,
        'fault_contexts': fault_contexts,
    }
    return result, total


# === Phase 6: HTML Report ===
# === Main ===
def main():
    print('=' * 60)
    print('HS4 Batch Analyzer')
    bj_start = (UTC_START_HOUR + 8) % 24
    bj_end = (UTC_END_HOUR + 8) % 24
    print(f'Target: {SN} {YEAR}-{MONTH}-{DAY} UTC {UTC_START_HOUR:02d}:00-{UTC_END_HOUR:02d}:00 (BJ {bj_start:02d}:00-{bj_end:02d}:00)')
    print('=' * 60)

    # Step 1: List
    print('\n[1/5] Listing remote files...')
    memfiles = filter_by_utc_hour(list_memfiles(), UTC_START_HOUR, UTC_END_HOUR)
    drc_files = filter_by_utc_hour(list_drc_files(), UTC_START_HOUR, UTC_END_HOUR)
    print(f'  MemFile/TopFile/FdFile: {len(memfiles)}')
    print(f'  DRC files: {len(drc_files)} (sample every {DRC_SAMPLE_EVERY})')

    # Step 2: Download memfiles
    print('\n[2/5] Downloading memfile logs...')
    download_memfiles(memfiles)

    # Step 3: Download & merge DRC
    print('\n[3/5] Downloading & parsing DRC logs...')
    download_and_merge_drc(drc_files)

    # Step 4: Parse aggregates
    print('\n[4/5] Aggregating mem/top data...')
    mem_records, top_records = aggregate_mem_top()
    print(f'  MemFile records: {len(mem_records)}')
    print(f'  TopFile records: {len(top_records)}')

    # Extract work_status timeline from DRC filenames
    drc_status_timeline = []
    status_re = re.compile(r'-(work_status_\w+)\.drc\.save$')
    for remote, name, ts in drc_files:
        m = status_re.search(name)
        status = m.group(1) if m else 'unknown'
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        dt_bj = dt.replace(hour=(dt.hour + 8) % 24)
        drc_status_timeline.append({
            'time_utc': dt.strftime('%H:%M:%S'),
            'time_bj': dt_bj.strftime('%H:%M:%S'),
            'status': status,
            'name': name,
        })
    # Filter to status changes only
    drc_status_changes = []
    prev = None
    for e in drc_status_timeline:
        if e['status'] != prev:
            drc_status_changes.append(e)
            prev = e['status']

    print('\n[4.5] Analyzing merged DRC logs...')
    drc_result, drc_total = analyze_merged_logs()
    if drc_result:
        fault_cnt = len(drc_result.get('fault_contexts', []))
        print(f'  Parsed {drc_result["parsed"]} lines, {len(drc_result["errors_warns"])} errors/warns, {len(drc_result["nav_transitions"])} transitions, {fault_cnt} fault contexts')

    # Step 5: Report
    print('\n[5/5] Generating HTML report...')
    report_path = generate_report(mem_records, top_records, drc_result, drc_total, REPORT_PATH,
                                  {"sn": SN, "fw": FW, "year": YEAR, "month": MONTH, "day": DAY,
                                   "utc_start": UTC_START_HOUR, "utc_end": UTC_END_HOUR},
                                  drc_status_changes)
    print(f'  Report: {report_path}')
    print('\nDone.')


if __name__ == '__main__':
    main()
