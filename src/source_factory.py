"""源平台客户端工厂

根据 configs/source.yaml 中的 platform 字段创建对应适配器。
禅道：ZentaoAdapter（透传 ZentaoClient）
Jira：JiraAdapter（预留，暂未实现）
"""

import logging

logger = logging.getLogger(__name__)

# ZentaoClient 单例缓存：base_url+account → client
# 避免 GUI 多个入口（测试连接、列出Bug、试运行、正式导入）反复创建 client 触发重复认证
_CLIENT_CACHE: dict = {}


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
        cache_key = (zt_cfg.get("base_url", ""), zt_cfg.get("account", ""), zt_cfg.get("password", ""))
        client = _CLIENT_CACHE.get(cache_key)
        if client is None:
            client = ZentaoClient(
                base_url=zt_cfg.get("base_url", ""),
                account=zt_cfg.get("account", ""),
                password=zt_cfg.get("password", ""),
                api_delay=sync_cfg.get("api_delay", 0.5),
            )
            _CLIENT_CACHE[cache_key] = client
        # 确保 branch_id 始终同步（即使是缓存的 client）
        filters = zt_cfg.get("filters", {})
        if "branch" in filters:
            client.set_branch_id(int(filters["branch"]))
        return ZentaoAdapter(client)

    elif platform == "teambition":
        from src.teambition_source_adapter import TeambitionSourceAdapter
        from src.teambition_source_client import TeambitionSourceClient

        tb_src_cfg = config.get("teambition_source", {})
        url = tb_src_cfg.get("url", "")
        project_id = tb_src_cfg.get("project_id", "")
        client = TeambitionSourceClient(
            base_url=url,
            account=tb_src_cfg.get("account", ""),
            password=tb_src_cfg.get("password", ""),
            cookie_file=tb_src_cfg.get("cookie_file", ""),
            project_id=project_id,
        )
        # 优先从 url 解析 project_id
        if url and not project_id:
            project_id = client.extract_project_id(url)
        return TeambitionSourceAdapter(
            client, project_id=project_id,
            field_ids=tb_src_cfg.get("field_ids", {}),
            field_names=tb_src_cfg.get("field_names", {}))

    elif platform == "jira":
        raise NotImplementedError(
            "Jira 平台适配器尚未实现。"
            "请将 configs/source.yaml 中的 platform 改为 zentao。"
        )

    else:
        raise ValueError(f"不支持的源平台: {platform}")
