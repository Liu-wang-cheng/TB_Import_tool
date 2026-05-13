"""配置加载器：支持从 configs/ 目录加载多文件配置，或回退到单个 config.yaml"""

import logging
import os

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = "configs"
FALLBACK_CONFIG = "config.yaml"


def load_configs(config_dir: str = DEFAULT_CONFIG_DIR) -> dict:
    """
    加载配置文件。

    优先尝试从 configs/ 目录加载多个 YAML 文件并合并：
        configs/source.yaml      → config["source"]（多源平台适配）
        configs/zentao.yaml      → config["zentao"]（向后兼容）
        configs/teambition.yaml  → config["teambition"]
        configs/dingtalk.yaml    → config["dingtalk"]
        configs/sync.yaml        → config["sync"]
        configs/classifier.yaml  → config["classifier"]

    如果 configs/ 目录不存在，则回退到读取根目录的 config.yaml（单文件模式）。

    Returns:
        合并后的配置字典，结构与原来的单文件 config.yaml 完全一致。
    """
    if os.path.isdir(config_dir):
        config = _load_from_dir(config_dir)
        _ensure_source_compat(config, config_dir)
        return config

    if os.path.exists(FALLBACK_CONFIG):
        logger.info("配置目录 %s 不存在，回退到 %s", config_dir, FALLBACK_CONFIG)
        config = _load_single(FALLBACK_CONFIG)
        _ensure_source_compat(config, config_dir)
        return config

    raise FileNotFoundError(
        f"找不到配置：{config_dir}/ 目录和 {FALLBACK_CONFIG} 均不存在。"
        f"请创建 configs/ 目录并放置配置文件，或复制 config.yaml 到根目录。"
    )


def _ensure_source_compat(config: dict, config_dir: str) -> None:
    """确保 source.yaml 存在 platform 字段。没有 source.yaml 时自动创建（默认禅道）。"""
    source = config.get("source", {})
    if source:
        if "platform" not in source:
            source["platform"] = "zentao"
        return

    # 没有 source.yaml，默认禅道
    config["source"] = {"platform": "zentao"}
    # 写入文件（下次启动就有 source.yaml 了）
    source_path = os.path.join(config_dir, "source.yaml")
    try:
        with open(source_path, "w", encoding="utf-8") as f:
            f.write("# 源平台配置\nplatform: zentao\n")
    except Exception:
        pass
    logger.info("自动创建 source.yaml（默认平台: zentao）")


def _load_from_dir(config_dir: str) -> dict:
    config = {}
    loaded = []

    for filename in sorted(os.listdir(config_dir)):
        if not filename.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(config_dir, filename)
        key = filename.rsplit(".", 1)[0]
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        config[key] = data
        loaded.append(filename)

    if not loaded:
        raise FileNotFoundError(f"配置目录 {config_dir}/ 下没有找到 .yaml 文件")

    logger.info("已从 %s/ 加载配置: %s", config_dir, ", ".join(loaded))
    return config


def _load_single(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
