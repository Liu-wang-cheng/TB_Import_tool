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


def load_profiles(path: str) -> list:
    """读取方案列表；文件不存在/损坏时返回空列表（静默降级）"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("界面方案文件读取失败（忽略）: %s", e)
        return []
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, list):
        return []
    return [p for p in profiles if isinstance(p, dict) and p.get("name")]


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
    """新增或按 name 覆盖保存方案（原子写）"""
    name = str((profile or {}).get("name", "")).strip()
    if not name:
        return False
    profiles = load_profiles(path)
    replaced = False
    for i, p in enumerate(profiles):
        if p.get("name") == name:
            profiles[i] = profile
            replaced = True
            break
    if not replaced:
        profiles.append(profile)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump({"profiles": profiles}, f,
                           allow_unicode=True, sort_keys=False)
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning("界面方案保存失败: %s", e)
        return False
