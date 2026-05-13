"""源平台客户端工厂

根据 configs/source.yaml 中的 platform 字段创建对应适配器。
禅道：ZentaoAdapter（透传 ZentaoClient）
Jira：JiraAdapter（预留，暂未实现）
"""

import logging

logger = logging.getLogger(__name__)


def create_source_client(config: dict):
    """根据配置创建源平台适配器实例。

    Returns:
        满足 SourceClient Protocol 的适配器实例
    """
    source_cfg = config.get("source", {})
    platform = source_cfg.get("platform", "zentao")
    sync_cfg = config.get("sync", {})

    if platform == "zentao":
        from src.zentao_adapter import ZentaoAdapter
        from src.zentao_client import ZentaoClient

        zt_cfg = config.get("zentao", {})
        client = ZentaoClient(
            base_url=zt_cfg.get("base_url", ""),
            account=zt_cfg.get("account", ""),
            password=zt_cfg.get("password", ""),
            api_delay=sync_cfg.get("api_delay", 0.5),
        )
        return ZentaoAdapter(client)

    elif platform == "jira":
        raise NotImplementedError(
            "Jira 平台适配器尚未实现。"
            "请将 configs/source.yaml 中的 platform 改为 zentao。"
        )

    else:
        raise ValueError(f"不支持的源平台: {platform}")
