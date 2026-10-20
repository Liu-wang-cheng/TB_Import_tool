"""界面方案配置：保存/恢复界面上的源平台参数。

方案文件位于 data/ui_profiles.yaml —— data/* 已被 gitignore，且打包 spec
只收集白名单内的 data 文件，因此含账号密码的方案不会进入 Git 或发布包。
本模块只被"方案下拉框"使用，不参与 config_loader 的配置加载，未保存/未
选择方案时对现有配置与同步流程零影响。
"""

import logging
import os

import yaml

logger = logging.getLogger(__name__)


def _read_doc(path: str) -> dict:
    """读取方案文件完整内容；不存在/损坏时返回空 dict（静默降级）"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("界面方案文件读取失败（忽略）: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _write_doc(path: str, doc: dict) -> bool:
    """原子写入方案文件"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning("界面方案保存失败: %s", e)
        return False


def load_profiles(path: str) -> list:
    """读取方案列表；文件不存在/损坏时返回空列表（静默降级）"""
    profiles = _read_doc(path).get("profiles")
    if not isinstance(profiles, list):
        return []
    return [p for p in profiles if isinstance(p, dict) and p.get("name")]


def load_last_selected(path: str) -> str:
    """读取上次选择的方案名（启动时恢复下拉框用）"""
    name = _read_doc(path).get("last_selected")
    return str(name) if name else ""


def save_last_selected(path: str, name: str) -> bool:
    """记录上次选择的方案名（保留已有方案列表）"""
    doc = _read_doc(path)
    doc["last_selected"] = name or ""
    return _write_doc(path, doc)


def resolve_profiles(all_profiles: list, names: list) -> tuple:
    """按 names 顺序返回存在的方案与缺失名单。

    Returns:
        (found_profiles, missing_names)
    """
    by_name = {p.get("name"): p for p in all_profiles if p.get("name")}
    found, missing = [], []
    for name in names or []:
        if not name:
            continue
        profile = by_name.get(name)
        if profile is not None:
            found.append(profile)
        else:
            missing.append(name)
    return found, missing


def format_profiles_summary(names: list, max_len: int = 28) -> str:
    """定时方案按钮的显示文本"""
    names = [n for n in (names or []) if n]
    if not names:
        return "（按当前界面配置）"
    text = " → ".join(names)
    return text if len(text) <= max_len else text[:max_len - 2] + "…"


def save_profile(path: str, profile: dict) -> bool:
    """新增或按 name 覆盖保存方案（原子写，保留 last_selected 等其它字段）"""
    name = str((profile or {}).get("name", "")).strip()
    if not name:
        return False
    profile = dict(profile, name=name)
    doc = _read_doc(path)
    profiles = doc.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    replaced = False
    for i, p in enumerate(profiles):
        if isinstance(p, dict) and p.get("name") == name:
            profiles[i] = profile
            replaced = True
            break
    if not replaced:
        profiles.append(profile)
    doc["profiles"] = profiles
    return _write_doc(path, doc)
