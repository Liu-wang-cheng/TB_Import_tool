"""动态提示词构建器 - 根据缺陷类别选择专业化分析提示词"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class PromptBuilder:
    """根据缺陷分类动态构建专业化分析提示词"""

    def __init__(self, config_path: str = "configs/prompts.yaml"):
        self._categories = {}
        self._default = {}
        self._enabled = False
        self._load(config_path)

    def _load(self, config_path: str):
        path = Path(config_path)
        if not path.exists():
            logger.warning("模块化提示词配置不存在: %s", config_path)
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            logger.warning("加载模块化提示词配置失败: %s", e)
            return

        if not cfg.get("enabled", False):
            logger.info("模块化提示词已禁用")
            return

        categories = cfg.get("categories", {})
        default = cfg.get("default", {})

        self._categories = categories
        self._default = default
        self._enabled = True
        logger.info("模块化提示词已加载 %d 个类别", len(self._categories))

    def get_category_config(self, category: str) -> dict:
        if not self._enabled or not category:
            return self._default

        # 按前缀长度降序匹配，确保更具体的类别优先（如 "算法-避障" 优先于 "算法"）
        sorted_items = sorted(
            self._categories.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for prefix, config in sorted_items:
            if category.startswith(prefix) or prefix in category:
                return config

        return self._default

    def get_specialized_system_prompt(self, category: str,
                                      base_prompt: str = "") -> str:
        if not self._enabled:
            return base_prompt

        config = self.get_category_config(category)
        additional = config.get("additional_knowledge", "").strip()
        if not additional:
            return base_prompt

        focus_areas = config.get("focus_areas", [])
        focus_text = "\n".join(f"  - {fa}" for fa in focus_areas)

        specialization = f"""

## 当前缺陷类别专项分析指导（{category}）

### 重点关注的分析领域：
{focus_text}

### 领域专业知识：
{additional}
"""
        if base_prompt:
            return base_prompt + specialization
        return specialization

    def get_key_modules(self, category: str) -> list:
        config = self.get_category_config(category)
        return config.get("key_modules", [])

    def get_focus_keywords(self, category: str) -> list:
        config = self.get_category_config(category)
        return config.get("focus_keywords", [])

    def get_focus_areas(self, category: str) -> list:
        config = self.get_category_config(category)
        return config.get("focus_areas", [])

    @property
    def enabled(self) -> bool:
        return self._enabled
