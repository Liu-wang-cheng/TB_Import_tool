"""智能缺陷管理平台 CLI 入口"""

import argparse
import logging
import os
import sys
from datetime import datetime

# PyInstaller excludes pandas 但传递依赖可能拉入残缺模块，
# 导致 sklearn is_pandas_df() 触发 AttributeError（只捕获 ImportError）。
if 'pandas' in sys.modules:
    try:
        import pandas
        pandas.DataFrame
    except (ImportError, AttributeError):
        del sys.modules['pandas']

from dingtalk.bot import DingTalkBot
from src.config_loader import load_configs
from src.config_resolver import ConfigResolver
from src.source_factory import create_source_client
from src.models import SEVERITY_LABELS
from src.sync_engine import SyncEngine
from src.teambition_client import TeambitionClient
from src.utils import apply_module_filter, normalize_zentao_filters, resolve_assigned_to


def setup_logging(config: dict, verbose: bool = False):
    log_cfg = config.get("logging", {})
    level = "DEBUG" if verbose else log_cfg.get("level", "INFO")
    log_dir = log_cfg.get("dir", "logs")
    os.makedirs(log_dir, exist_ok=True)

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format=log_format)

    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)


def list_bugs(source, filters: dict, severity_map: dict = None,
              severity_labels: dict = None):
    assigned_to = resolve_assigned_to(filters, zentao_account=source.account)
    bugs = source.fetch_all_bugs(
        product_id=filters.get("product_id"),
        project_id=filters.get("project_id"),
        statuses=filters.get("statuses"),
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
        assigned_to=assigned_to,
    )
    module_filter = (filters.get("module_filter") or "").strip()
    module_id_set = None
    if module_filter and not module_filter.isdigit() and filters.get("product_id"):
        module_id_set = source.resolve_module_ids_by_name(
            int(filters["product_id"]), module_filter)
    bugs = apply_module_filter(
        bugs, module_filter,
        fetch_detail_fn=source.fetch_bug_detail,
        module_id_set=module_id_set,
    )
    print(f"\n共 {len(bugs)} 条 Bug:\n")
    print(f"{'ID':<8} {'状态':<10} {'严重程度':<12} {'指派给':<10} {'标题'}")
    print("-" * 100)
    for bug in bugs:
        s = str(bug.severity).strip() if bug.severity else ""
        # 禅道页面翻译后的名称（致命/严重/一般/建议 或 A/B/C/D）
        label = severity_labels.get(s, s) if severity_labels else s
        # TB 映射等级（与 _map_severity 一致：先翻译标签，再回退原始值）
        tb_sev = None
        if severity_map:
            tb_sev = severity_map.get(label)
            if tb_sev is None and label.isdigit():
                tb_sev = severity_map.get(int(label))
            if tb_sev is None and label != s:
                tb_sev = severity_map.get(s)
                if tb_sev is None and s.isdigit():
                    tb_sev = severity_map.get(int(s))
        if tb_sev is not None:
            sev = f"{label}→{tb_sev}"
        else:
            sev = label or "-"
        assignee = bug.assignedTo[:8] if bug.assignedTo else "-"
        print(f"{bug.id:<8} {bug.status:<10} {sev:<12} {assignee:<10} {bug.title}")


def main():
    parser = argparse.ArgumentParser(
        description="智能缺陷管理平台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py --list-bugs\n"
            "  python main.py --dry-run --product-id 1\n"
            "  python main.py --date-from 2026-01-01\n"
        ),
    )
    parser.add_argument("--config-dir", default="configs",
                        help="配置文件夹路径 (默认: configs/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式，不实际创建/修改")
    parser.add_argument("--product-id", type=int,
                        help="覆盖配置: 禅道产品ID")
    parser.add_argument("--project-id", type=int,
                        help="覆盖配置: 禅道项目ID")
    parser.add_argument("--status", nargs="+",
                        help="覆盖配置: 要同步的Bug状态")
    parser.add_argument("--date-from",
                        help="覆盖配置: 起始日期 (YYYY-MM-DD)")
    parser.add_argument("--date-to",
                        help="覆盖配置: 截止日期 (YYYY-MM-DD)")
    parser.add_argument("--assigned-to", nargs="+",
                        help="覆盖配置: 指派人筛选，如 --assigned-to me 或 --assigned-to zhangsan lisi")
    parser.add_argument("--sync-attachments", action="store_true",
                        help="启用附件同步")
    parser.add_argument("--no-attachments", action="store_true",
                        help="禁用附件同步")
    parser.add_argument("--list-bugs", action="store_true",
                        help="仅列出匹配的Bug，不同步")
    parser.add_argument("--auth-only", action="store_true",
                        help="仅执行 Teambition 认证")
    parser.add_argument("--dingtalk", action="store_true",
                        help="强制启用钉钉通知（覆盖配置）")
    parser.add_argument("--no-dingtalk", action="store_true",
                        help="强制禁用钉钉通知（覆盖配置）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志输出")

    args = parser.parse_args()

    # 加载配置（支持 configs/ 多文件或 config.yaml 单文件回退）
    try:
        config = load_configs(args.config_dir)
    except FileNotFoundError as e:
        print(f"配置加载失败: {e}")
        sys.exit(1)

    setup_logging(config, args.verbose)
    logger = logging.getLogger(__name__)

    # CLI 参数覆盖配置
    filters = config.setdefault("zentao", {}).setdefault("filters", {})
    if args.product_id:
        filters["product_id"] = args.product_id
    if args.project_id:
        filters["project_id"] = args.project_id
    if args.status:
        filters["statuses"] = args.status
    if args.date_from:
        filters["date_from"] = args.date_from
    if args.date_to:
        filters["date_to"] = args.date_to
    if args.assigned_to:
        filters["assigned_to"] = args.assigned_to
        # 指派人已迁移到公用 assignee.yaml，同步引擎从这读
        config.setdefault("assignee", {})["assigned_to"] = args.assigned_to
    if args.sync_attachments:
        config.setdefault("sync", {})["sync_attachments"] = True
    if args.no_attachments:
        config.setdefault("sync", {})["sync_attachments"] = False

    # 兼容新配置：product/project → product_id/project_id（数字ID直接转换）
    normalize_zentao_filters(filters)

    # 初始化客户端
    zentao_cfg = config.get("zentao", {})
    sync_cfg = config.get("sync", {})
    tb_cfg = config.get("teambition", {})
    source_cfg = config.get("source", {})
    platform = source_cfg.get("platform", "zentao")

    # 必要配置项校验（禅道平台）
    if platform == "zentao":
        for key in ("base_url", "account", "password"):
            if not zentao_cfg.get(key):
                logger.error("缺少必要配置: zentao.%s，请检查 configs/zentao.yaml", key)
                sys.exit(1)
    for key in ("app_id", "app_secret", "org_id"):
        if not tb_cfg.get(key):
            logger.error("缺少必要配置: teambition.%s，请检查 configs/teambition.yaml", key)
            sys.exit(1)

    source = create_source_client(config)

    # 先用配置中的 project_id（或空字符串）初始化，后续 ConfigResolver 会解析
    initial_project_id = tb_cfg.get("project_id", "")
    if not initial_project_id:
        project_cfg = tb_cfg.get("project", {})
        initial_project_id = project_cfg.get("id", "") or project_cfg.get("project_id", "")

    fallback_id = tb_cfg.get("creator_id") or tb_cfg.get("operator_id")
    teambition = TeambitionClient(
        app_id=tb_cfg["app_id"],
        app_secret=tb_cfg["app_secret"],
        org_id=tb_cfg["org_id"],
        project_id=initial_project_id,
        api_delay=sync_cfg.get("api_delay", 0.5),
        scenariofieldconfig_id=tb_cfg.get("scenariofieldconfig_id"),
        operator_id=fallback_id,
    )

    # 初始化钉钉机器人（默认启用，--no-dingtalk 可显式关闭）
    dingtalk_bot = None
    dt_cfg = config.get("dingtalk", {})
    dt_enabled = dt_cfg.get("enabled", True)
    if args.dingtalk:
        dt_enabled = True
    if args.no_dingtalk:
        dt_enabled = False
    if dt_enabled and dt_cfg.get("webhook_url"):
        dingtalk_bot = DingTalkBot(
            webhook_url=dt_cfg["webhook_url"],
            secret=dt_cfg.get("secret", ""),
        )
        logger.info("钉钉通知已启用")

    # 认证
    try:
        source.authenticate()
    except Exception as e:
        platform_name = "禅道" if platform == "zentao" else "Jira"
        logger.error("%s认证失败: %s", platform_name, e)
        sys.exit(1)

    # 仅认证模式
    if args.auth_only:
        try:
            teambition.authenticate()
            print("Teambition 认证成功，token 已缓存")
        except Exception as e:
            logger.error("Teambition 认证失败: %s", e)
            sys.exit(1)
        return

    # 列出Bug模式（不需要 Teambition 认证和名称解析）
    if args.list_bugs:
        severity_map = tb_cfg.get("severity_map", {})
        product_id = filters.get("product_id")
        severity_labels = source.fetch_severity_labels(product_id)
        list_bugs(source, filters, severity_map, severity_labels)
        if dingtalk_bot:
            try:
                assigned_to = resolve_assigned_to(filters, source.account)
                bugs = source.fetch_all_bugs(
                    product_id=filters.get("product_id"),
                    project_id=filters.get("project_id"),
                    statuses=filters.get("statuses"),
                    date_from=filters.get("date_from"),
                    date_to=filters.get("date_to"),
                    assigned_to=assigned_to,
                )
                module_filter_dt = (filters.get("module_filter") or "").strip()
                module_id_set_dt = None
                if module_filter_dt and not module_filter_dt.isdigit() \
                        and filters.get("product_id"):
                    module_id_set_dt = source.resolve_module_ids_by_name(
                        int(filters["product_id"]), module_filter_dt)
                bugs = apply_module_filter(
                    bugs, module_filter_dt,
                    fetch_detail_fn=source.fetch_bug_detail,
                    module_id_set=module_id_set_dt,
                )
                sev_map = tb_cfg.get("severity_map", {})
                lines = [f"共 {len(bugs)} 条 Bug:", "", "| ID | 状态 | 严重程度 | 指派给 | 标题 |",
                         "| --- | --- | --- | --- | --- |"]
                for bug in bugs[:20]:
                    s = str(bug.severity).strip() if bug.severity else ""
                    label = severity_labels.get(s, s) if severity_labels else s
                    tb_sev = sev_map.get(s)
                    if tb_sev is None and s.isdigit():
                        tb_sev = sev_map.get(int(s))
                    sev = f"{label}→{tb_sev}" if tb_sev is not None else label
                    assignee = bug.assignedTo[:8] if bug.assignedTo else "-"
                    title = bug.title
                    lines.append(f"| {bug.id} | {bug.status} | {sev} | {assignee} | {title} |")
                if len(bugs) > 20:
                    lines.append(f"\n> 仅显示前 20 条，共 {len(bugs)} 条")
                dingtalk_bot.send_markdown("禅道 Bug 列表", "\n".join(lines))
            except Exception as e:
                logger.warning("钉钉通知发送失败: %s", e)
        return

    # Teambition 认证（试运行模式也需要认证，用于去重和字段检测）
    try:
        teambition.authenticate()
    except Exception as e:
        logger.error("Teambition 认证失败: %s", e)
        sys.exit(1)

    # 解析配置中的中文名称 → ID
    resolver = ConfigResolver(config, source, teambition)
    resolver.resolve()

    # 如果配置了 creator_name，自动查找对应的 Teambition 用户 ID
    creator_name = tb_cfg.get("creator_name", "").strip()
    if creator_name:
        logger.info("正在查找 Teambition 用户: %s", creator_name)
        resolved_id = teambition.search_member(creator_name)
        if resolved_id:
            teambition.operator_id = resolved_id
            logger.info("创建人已解析: %s → %s", creator_name, resolved_id)
        else:
            logger.warning("未找到用户 '%s'，使用配置的 creator_id: %s", creator_name, fallback_id)

    if args.dry_run:
        logger.info("===== 试运行模式：不实际创建/修改 Teambition 任务 =====")

    engine = SyncEngine(config, source, teambition, dingtalk_bot=dingtalk_bot)
    stats = engine.run(dry_run=args.dry_run)
    print(f"\n{stats}")


if __name__ == "__main__":
    main()
