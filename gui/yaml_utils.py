"""YAML 文件工具：更新指定 key 的值，保留注释和格式"""

import re

# 日期格式字符串（会被 PyYAML 误解析为 datetime.date，必须加引号保护）
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def update_yaml_values(file_path: str, updates: dict):
    """更新 YAML 文件中指定 key 的值，保留注释和文件格式

    Args:
        file_path: YAML 文件路径
        updates: 要更新的 key→value 映射，支持嵌套用 '.' 分隔
                 如 {"filters.product": 11, "account": "user1"}
                 值为 None 时写入 null
                 特殊 key "__list__" 用于列表类型字段

    对于简单标量值，直接替换行内值。
    对于列表值，会替换从 key 行到下一个非注释/非子项行之间的所有内容。
    """
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for key, value in updates.items():
        lines = _apply_update(lines, key, value)

    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _apply_update(lines: list, key: str, value) -> list:
    """对行列表应用单个更新"""
    # 处理嵌套 key：filters.product → 先找 filters:，再找 product:
    parts = key.split(".")
    if len(parts) == 1:
        return _update_top_level(lines, key, value)

    # 嵌套 key：找到父级缩进，然后在其中找子 key
    parent = parts[0]
    child = ".".join(parts[1:])
    parent_indent = _find_key_indent(lines, parent, 0)
    if parent_indent is None:
        return lines

    # 在父级下方找子 key
    parent_line = parent_indent[0]
    parent_indent_str = parent_indent[1]
    # 子级缩进 = 父级缩进 + 2（YAML 标准缩进）
    child_indent_str = parent_indent_str + "  "

    return _update_child_key(lines, child, value, parent_line, child_indent_str)


def _find_key_indent(lines: list, key: str, start: int):
    """找到 key: 所在的行号和缩进（兼容带引号的 YAML key）"""
    # 匹配 key: 或 "key": 或 'key':
    pattern = re.compile(
        r'^(\s*)["\']?' + re.escape(key) + r'["\']?\s*:')
    for i in range(start, len(lines)):
        m = pattern.match(lines[i])
        if m:
            return (i, m.group(1))
    return None


def _update_top_level(lines: list, key: str, value) -> list:
    """更新顶层 key（仅匹配零缩进的根级键，不误匹配嵌套子键）。
    若 key 不存在则追加到文件末尾。"""
    pattern = re.compile(
        r'^(\s*)["\']?' + re.escape(key) + r'["\']?\s*:(.*)')
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            indent = m.group(1)
            # 有缩进的键是嵌套子键，跳过（如 collaborative_learning.enabled ≠ 顶层 enabled）
            if indent:
                continue
            if isinstance(value, list):
                return _replace_list_value(lines, i, indent, key, value)
            else:
                lines[i] = f"{indent}{key}: {_format_value(value)}\n"
                _strip_trailing_list_items(lines, i, indent)
                return lines
    # key 不存在 → 追加到末尾
    if isinstance(value, list):
        lines.append(f"{key}:\n")
        for v in value:
            lines.append(f"  - {_format_value(v)}\n")
    else:
        lines.append(f"{key}: {_format_value(value)}\n")
    return lines


def _update_child_key(lines: list, key: str, value, parent_line: int,
                      indent_str: str) -> list:
    """在父级下方更新子 key；不存在时插入到父级块末尾。"""
    # 支持 key 仍含 "." 则继续递归
    parts = key.split(".")
    if len(parts) > 1:
        # 找中间父级
        mid_key = parts[0]
        mid_info = _find_key_indent(lines, mid_key, parent_line + 1)
        if mid_info:
            new_indent = indent_str + "  "
            return _update_child_key(lines, ".".join(parts[1:]), value,
                                     mid_info[0], new_indent)
        return lines

    # 叶子 key：先查找是否存在，同时记录父级块的最后一行（用于不存在时插入）
    pattern = re.compile(
        r'^' + re.escape(indent_str) + r'["\']?' + re.escape(key) + r'["\']?\s*:(.*)')
    last_in_block = parent_line
    for i in range(parent_line + 1, len(lines)):
        m = pattern.match(lines[i])
        if m:
            if isinstance(value, list):
                return _replace_list_value(lines, i, indent_str, key, value)
            else:
                lines[i] = f"{indent_str}{key}: {_format_value(value)}\n"
                _strip_trailing_list_items(lines, i, indent_str)
                return lines
        stripped = lines[i].rstrip()
        if not stripped:
            # 空行：不一定是块结束，继续观察
            continue
        if not stripped.startswith(indent_str):
            # 同级或更少缩进的非空行 → 父级块已结束
            break
        last_in_block = i

    # 不存在则插入到父级块末尾
    insert_pos = last_in_block + 1
    if isinstance(value, list):
        new_lines = [f"{indent_str}{key}:\n"]
        item_indent = indent_str + "  "
        for v in value:
            new_lines.append(f"{item_indent}- {_format_value(v)}\n")
        lines[insert_pos:insert_pos] = new_lines
    else:
        lines.insert(insert_pos, f"{indent_str}{key}: {_format_value(value)}\n")
    return lines


def _strip_trailing_list_items(lines: list, key_line: int, indent: str):
    """Remove old list items when a list-valued key is changed to scalar/null."""
    _strip_trailing_block_lines(lines, key_line, indent)


def _strip_trailing_block_lines(lines: list, key_line: int, indent: str):
    """清除 key 行后所有更深缩进的旧块子行（列表项 "- x" 或块映射 "k: v"）。

    用于标量/内联 JSON 替换时清除旧的多行块（如 scheduled_sync 旧块映射），
    否则残留的子行会与内联值拼成非法 YAML。空行与注释保留。
    """
    child_indent = indent + "  "
    to_delete = []
    for j in range(key_line + 1, len(lines)):
        stripped = lines[j]
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue  # 空行/注释保留
        if stripped.startswith(child_indent):
            to_delete.append(j)
        else:
            break
    for j in reversed(to_delete):
        del lines[j]


def _replace_list_value(lines: list, key_line: int, indent: str,
                        key: str, values: list) -> list:
    """替换列表类型的值，保留 key 行，替换其下的 - item 行"""
    # 重写 key 行为纯 key:（清除旧的标量值如 null/None 残留）
    lines[key_line] = f"{indent}{key}:\n"

    # 找到列表项的结束位置（停在第一个非列表项行，
    # 不吞掉列表后的空行与下一节的注释）
    item_indent = indent + "  "
    end = key_line + 1
    while end < len(lines):
        stripped = lines[end].rstrip()
        if stripped.startswith(item_indent + "- "):
            end += 1
        else:
            break

    # 生成新的列表项
    new_items = [f"{item_indent}- {_format_value(v)}\n" for v in values]
    lines[key_line + 1:end] = new_items
    return lines


def _format_value(value) -> str:
    """格式化 YAML 值"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # dict/list：用 JSON 序列化（YAML 兼容 JSON，PyYAML 可解析回原类型）。
    # 不能 str(dict)（Python 字面量非法 YAML，读回变成字符串导致配置失效）
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, ensure_ascii=False)
    # 字符串：含特殊字符或日期格式时加引号
    s = str(value)
    if not s or s in ("null", "true", "false", "yes", "no"):
        return f'"{s}"'
    # 纯数字字符串必须加引号，否则 PyYAML 会解析为 int（如指派人 "343"）
    if s.isdigit():
        return f'"{s}"'
    # 日期格式字符串必须加引号，否则 PyYAML 会解析为 datetime.date
    if _DATE_RE.match(s):
        return f'"{s}"'
    if any(c in s for c in ':#{}[]&*!|>\'"%@`'):
        # 用 json.dumps 生成合法转义（内嵌引号/反斜杠/换行），YAML 双引号串兼容
        import json
        return json.dumps(s, ensure_ascii=False)
    return s
