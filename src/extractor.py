"""从禅道 Bug 重现步骤中提取 SN 编码和缺陷产生时间。"""

import re
from collections import Counter
from datetime import datetime
from typing import Callable, List, Optional


# ── 模板文本清理 ──────────────────────────────────

def clean_template_text(text: str, strip_html: bool = True) -> str:
    """统一清理禅道 Bug 步骤里的 HTML 标签、&nbsp; 实体和多余空白。

    模板格式常含 `<br>` / `&nbsp;` 和多空格，提取前必须先归一化。
    各模块 (extract_sn / extract_datetime / zentao_client._extract_sn /
    sync_engine 自定义字段提取) 都需要相同处理，集中到此处避免漂移。
    """
    if not text:
        return ""
    if strip_html:
        text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&nbsp;', ' ').replace('&#160;', ' ')
    return re.sub(r'\s+', ' ', text).strip()


# ── SN 提取 ───────────────────────────────────────

# HCT 扫地机 SN 格式：HQ 开头 + 数字/字母，长度 ≥12
DEFAULT_SN_PATTERNS = [
    # 模板格式：SN码：48HCNFCN0054X 或 样机编号/SN码：48H-CN-FAN0052X
    # (?<![A-Za-z]) 防止 NSN/USN/PSN/SSN/BSN 等 3 字母缩写误匹配
    r'(?:样机编号(?:/SN码)?|SN码|(?<![A-Za-z])SN)\s*[:：]\s*([A-Za-z0-9\-]{8,})',
    r'\b(HQ[0-9A-Z]{10,})\b',
    r'\b([A-Z]{2}[0-9]{2}[A-Z][0-9]{4}[A-Z]{2}[0-9]{8,})\b',
]

# 需要排除的伪 SN（常见误匹配）
SN_BLACKLIST = {
    'HTTP', 'HTTPS', 'HTML', 'HQ5S00700002HC260600',
}


def learn_sn_patterns(sn_values: List[str]) -> List[str]:
    """从 TB 已有任务的 SN 值中学习正则模式。

    分析非空 SN 样本的前缀和长度分布，生成项目特定的正则模式。
    返回的模式按匹配频率排序。
    """
    valid = [v.strip().upper() for v in sn_values
             if v and v.strip() and v.strip() not in SN_BLACKLIST]
    if not valid:
        return DEFAULT_SN_PATTERNS.copy()

    patterns = []

    # 按前缀（前2字符）分组，找出最常见的前缀
    prefix_counter = Counter(v[:2] for v in valid if len(v) >= 2)
    for prefix, count in prefix_counter.most_common():
        if count < 2:
            continue
        group = [v for v in valid if v.startswith(prefix)]
        lengths = [len(v) for v in group]
        min_len, max_len = min(lengths), max(lengths)

        # 生成正则：前缀 + 字母数字混合
        # 如果长度差异小，用精确范围；否则用 min_len+ 的开放范围
        if max_len - min_len <= 3 and min_len > 2:
            quantifier = f"{{{min_len - 2},{max_len - 2}}}"
        else:
            quantifier = f"{{{min_len - 2},}}"
        pat = rf'\b({re.escape(prefix)}[0-9A-Z]{quantifier})\b'
        patterns.append(pat)

    # 学到前缀模式后仍需保留默认模板模式（"SN码：" 等最可靠格式），
    # 否则不命中已学前缀的 SN（如模板填写的 48HCNFB...）提取不到
    return patterns + DEFAULT_SN_PATTERNS.copy()


def extract_sn(text: str, patterns: list = None) -> Optional[str]:
    """从文本中提取 SN 编码，返回第一个匹配结果。"""
    if not text:
        return None
    clean = clean_template_text(text, strip_html=True)
    patterns = patterns or DEFAULT_SN_PATTERNS
    for pat in patterns:
        for match in re.finditer(pat, clean, re.IGNORECASE):
            sn = match.group(1).upper()
            if sn in SN_BLACKLIST:
                continue
            return sn
    return None


# ── 时间提取 ───────────────────────────────────────

# 日期模式（支持中文/英文/数字混合格式）
# 使用 (?<![\d]) / (?!\d) 避免从长数字串中误匹配
DATE_PATTERNS = [
    # 2026-05-08 / 2026/05/08 / 2026.05.08
    (r'(?<![\d])(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)', 'full'),
    # 206.5.8（三位数年份，缺首位 2）→ 视为 2026
    (r'(?<![\d])(\d{3})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)', 'three'),
    # 26-05-08 / 26/05/08（两位数年份）
    (r'(?<![\d])(\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)', 'short'),
]

# M/D 格式（如 5/7），无年份，需要外部传入参考年份
MD_DATE_PATTERN = (r'(?<![\d])(\d{1,2})/(\d{1,2})(?!\d)', 'md')

# 时间模式（支持全角/半角冒号）
TIME_PATTERNS = [
    r'(\d{1,2})[：:](\d{2})',
]


def _normalize_year(year_str: str, year_type: str = "short") -> int:
    """补全年份：206 → 2026, 26 → 2026"""
    year = int(year_str)
    if year_type == "three":
        # 三位数年份处理：206 → 2026
        # 假设：三位数是四位数中间位丢失或错乱的 typo（如 2026 → 206）
        # 取首尾两位组成年份后两位: 2__6 → 26 → 2026
        # 注意：216 → 26 → 2026（可能本意是 2016，但三位数本身歧义大，按首尾拼接）
        s = str(year)
        if len(s) == 3:
            yy = int(s[0]) * 10 + int(s[2])
            return 2000 + yy
        return 2000 + (year % 100)
    if year < 100:
        if year >= 50:
            return year + 1900
        return year + 2000
    return year


def extract_datetime(text: str, reference_date: datetime = None) -> Optional[str]:
    """从文本中提取日期时间，返回 YYYY-MM-DD HH:MM 格式。

    支持的格式：
      - 模板格式: 时间：6/3 20:40 或 时间：6/3  20：47（测试模板常见格式）
      - 完整日期时间: 2026-05-08 20:31 / 2026/05/08 20:31
      - 缩写年份: 26-05-08 / 206.5.8
      - M/D 格式: 5/7 20:31（用 reference_date 补全年份）
      - 纯时间: 20:31（无日期时用 reference_date 补全）

    Args:
        text: 输入文本（如禅道重现步骤）
        reference_date: 参考日期时间，用于补全 M/D 和纯时间格式。
                        通常传入 bug.openedDate 解析后的 datetime。
    """
    if not text:
        return None

    # 统一清理（不带 HTML 剥离，保持纯文本 & 空白归一化）
    clean = clean_template_text(text, strip_html=False)
    # 将 <br> 归一化为空格，避免模板格式中日期时间被 <br> 分隔导致匹配失败
    clean = re.sub(r'<br\s*/?>', ' ', clean)
    # 中文时间 "10 时 55 分" / "10时55分" / "3 点 20 分" → 冒号格式 "10:55"
    # （分钟补零，TIME_PATTERNS 要求两位分钟；"10时5分" → "10:05"）
    clean = re.sub(r'(\d{1,2})\s*[时点]\s*(\d{1,2})\s*分?',
                   lambda m: f"{m.group(1)}:{int(m.group(2)):02d}", clean)

    # 0. 优先匹配模板格式 "时间：6/3 20:40" 或 "时间: 6/3 20：47"
    ref_year = reference_date.year if reference_date else None
    if ref_year and 2020 <= ref_year <= 2035:
        tpl = re.search(
            r'时间[：:]\s*(\d{1,2})/(\d{1,2})\s*(\d{1,2})[：:](\d{2})',
            clean)
        if tpl:
            mon, day, hour, minute = (
                int(tpl.group(1)), int(tpl.group(2)),
                int(tpl.group(3)), int(tpl.group(4)))
            if 1 <= mon <= 12 and 1 <= day <= 31 \
                    and 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{ref_year:04d}-{mon:02d}-{day:02d} {hour:02d}:{minute:02d}"
        # 中文模板格式 "时间：8月24日 16:35" / "时间：2026年8月24日"
        # （数字与"月""日"之间可能有空格，如 "8 月 24 日"）。
        # 放在斜杠无时间格式之前：中文带时间的比斜杠无时间更精确
        tpl3 = re.search(
            r'时间[：:]\s*(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日'
            r'(?:\s*(\d{1,2})[：:](\d{2}))?',
            clean)
        if tpl3:
            year = int(tpl3.group(1)) if tpl3.group(1) else ref_year
            mon, day = int(tpl3.group(2)), int(tpl3.group(3))
            hour = int(tpl3.group(4)) if tpl3.group(4) else 0
            minute = int(tpl3.group(5)) if tpl3.group(5) else 0
            if 1 <= mon <= 12 and 1 <= day <= 31 \
                    and 0 <= hour <= 23 and 0 <= minute <= 59 \
                    and 2020 <= year <= 2035:
                return f"{year:04d}-{mon:02d}-{day:02d} {hour:02d}:{minute:02d}"
        # 模板格式无时间部分 "时间：6/3"
        tpl2 = re.search(r'时间[：:]\s*(\d{1,2})/(\d{1,2})(?!\s*\d)', clean)
        if tpl2:
            mon, day = int(tpl2.group(1)), int(tpl2.group(2))
            if 1 <= mon <= 12 and 1 <= day <= 31:
                return f"{ref_year:04d}-{mon:02d}-{day:02d} 00:00"

    best_result = None
    best_score = 0  # 优先选择有时间的、年份完整的
    used_time_ranges = []  # 记录已被日期模式使用的时间位置，避免纯时间重复

    # 1. 处理带年份的模式（full, three, short）
    for date_pat, year_type in DATE_PATTERNS:
        for dm in re.finditer(date_pat, clean):
            year_str, mon_str, day_str = dm.groups()
            year = _normalize_year(year_str, year_type)
            month = int(mon_str)
            day = int(day_str)

            # 基本合法性检查
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            if not (2020 <= year <= 2035):
                continue

            # 在日期附近查找时间（前后 30 字符）
            nearby = clean[max(0, dm.start()-30):dm.end()+30]
            hour, minute = 0, 0
            has_time = False
            for tm in re.finditer(TIME_PATTERNS[0], nearby):
                h, m = int(tm.group(1)), int(tm.group(2))
                if 0 <= h <= 23 and 0 <= m <= 59:
                    hour, minute = h, m
                    has_time = True
                    # 记录时间位置（转换为全局坐标）
                    time_start = max(0, dm.start() - 30) + tm.start()
                    time_end = dm.start() - 30 + tm.end()
                    used_time_ranges.append((time_start, time_end))
                    break

            score = 0
            if year_type == 'full':
                score += 2
            if has_time:
                score += 3
            # 优先选更靠近文本末尾的（通常[备注]或[结果]里的时间更准）
            score += dm.start() / len(clean)

            if score > best_score:
                best_score = score
                best_result = (year, month, day, hour, minute)

    # 2. 处理 M/D 格式（如 5/7 20:31），使用 reference_date 补全年份
    reference_year = reference_date.year if reference_date else None
    if reference_year and 2020 <= reference_year <= 2035:
        for dm in re.finditer(MD_DATE_PATTERN[0], clean):
            mon_str, day_str = dm.groups()
            month = int(mon_str)
            day = int(day_str)

            # 合法性检查
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue

            # 在 M/D 附近查找时间
            nearby = clean[max(0, dm.start()-30):dm.end()+30]
            hour, minute = 0, 0
            has_time = False
            for tm in re.finditer(TIME_PATTERNS[0], nearby):
                h, m = int(tm.group(1)), int(tm.group(2))
                if 0 <= h <= 23 and 0 <= m <= 59:
                    hour, minute = h, m
                    has_time = True
                    time_start = max(0, dm.start() - 30) + tm.start()
                    time_end = dm.start() - 30 + tm.end()
                    used_time_ranges.append((time_start, time_end))
                    break

            # M/D 格式只有带时间时才考虑，否则容易误匹配很多数字
            if not has_time:
                continue

            score = 1  # M/D 比完整日期弱
            score += 3  # has_time 加分
            score += dm.start() / len(clean)

            if score > best_score:
                best_score = score
                best_result = (reference_year, month, day, hour, minute)

    # 2.5 中文日期格式：2026年8月24日 / 8月24日（无年份用 reference 年补全）
    # "X月X日" 带汉字标记，误匹配风险低；无时间也接受（时间取 00:00）
    for dm in re.finditer(
            r'(?<![\d])(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日',
            clean):
        year_str, mon_str, day_str = dm.groups()
        year = int(year_str) if year_str else reference_year
        if not year or not (2020 <= year <= 2035):
            continue
        month = int(mon_str)
        day = int(day_str)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue

        nearby = clean[max(0, dm.start()-30):dm.end()+30]
        hour, minute = 0, 0
        has_time = False
        for tm in re.finditer(TIME_PATTERNS[0], nearby):
            h, m = int(tm.group(1)), int(tm.group(2))
            if 0 <= h <= 23 and 0 <= m <= 59:
                hour, minute = h, m
                has_time = True
                time_start = max(0, dm.start() - 30) + tm.start()
                time_end = dm.start() - 30 + tm.end()
                used_time_ranges.append((time_start, time_end))
                break

        score = 1  # 与 M/D 同级，弱于带年份完整格式
        if year_str:
            score += 2  # 显式年份
        if has_time:
            score += 3
        score += dm.start() / len(clean)

        if score > best_score:
            best_score = score
            best_result = (year, month, day, hour, minute)

    # 3. 兜底：纯时间模式（如 20:31），只有未找到任何日期时间时才触发
    if best_result is None and reference_date:
        for tm in re.finditer(TIME_PATTERNS[0], clean):
            # 跳过已被日期模式覆盖的时间
            t_start, t_end = tm.start(), tm.end()
            if any(used[0] <= t_start < used[1] for used in used_time_ranges):
                continue

            h, m = int(tm.group(1)), int(tm.group(2))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                continue

            # 检查前后是否有日期上下文（避免误匹配）
            # 要求前后 15 字符内没有明显的日期数字
            before = clean[max(0, t_start-15):t_start]
            after = clean[t_end:t_end+15]
            # 如果紧邻的是 "年""月""日" 或 YYYY-MM-DD 格式，说明已有日期上下文
            date_context = re.search(r'\d{2,4}[-/.]\d{1,2}[-/.]?\d{0,2}', before) or \
                           re.search(r'\d{1,2}[-/.]\d{1,2}', before[-5:]) or \
                           re.search(r'\d{2,4}[-/.]\d{1,2}[-/.]?\d{0,2}', after) or \
                           re.search(r'\d{1,2}[-/.]\d{1,2}', after[:5])
            if date_context:
                continue

            score = 2  # 纯时间得分低于有日期的匹配
            score += tm.start() / len(clean)

            if score > best_score:
                best_score = score
                best_result = (reference_date.year, reference_date.month,
                               reference_date.day, h, m)

    if best_result:
        y, m, d, h, mn = best_result
        return f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mn:02d}"
    return None


# ── LLM 兜底（复杂格式） ───────────────────────────

def extract_with_llm(text: str, llm_call_fn: Callable[[str], str] = None) -> dict:
    """使用 LLM 提取 SN 和时间，返回 {"sn": ..., "datetime": ...}。

    Args:
        text: 输入文本
        llm_call_fn: LLM 调用函数，接收 prompt 字符串，返回 response 字符串。
                     通常传入 classifier.BugClassifier._call_llm_api。
    """
    if not llm_call_fn:
        return {"sn": None, "datetime": None}

    # 判断文本是否为英文
    latin_count = sum(1 for c in text[:200] if c.isascii() and c.isalpha())
    chinese_count = sum(1 for c in text[:200] if '一' <= c <= '鿿')
    is_english = latin_count > chinese_count * 2 and latin_count > 10

    if is_english:
        prompt = (
            "Extract the following information from the robot vacuum defect "
            "reproduction steps. Return as JSON, no explanation:\n"
            "- SN code: device serial number, usually starts with HQ\n"
            "- Defect time: date and time recorded in the steps\n\n"
            f"Text: {text[:800]}\n\n"
            'Format: {"sn": "..." or null, "datetime": "YYYY-MM-DD HH:MM" or null}'
        )
    else:
        prompt = (
            "从以下扫地机缺陷复现步骤中提取信息，按 JSON 返回，不要解释:\n"
            "- SN编码：设备序列号，通常是HQ开头的长字符串\n"
            "- 缺陷发生时间：步骤中记录的日期和时间\n\n"
            f"文本：{text[:800]}\n\n"
            '格式：{"sn": "..." or null, "datetime": "YYYY-MM-DD HH:MM" or null}'
        )
    try:
        result = llm_call_fn(prompt)
        if not result:
            return {"sn": None, "datetime": None}
        # 简单解析 JSON
        import json
        # LLM 可能在 markdown 代码块中返回 JSON
        json_str = result
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]
        data = json.loads(json_str.strip())
        return {
            "sn": data.get("sn"),
            "datetime": data.get("datetime"),
        }
    except Exception:
        return {"sn": None, "datetime": None}
