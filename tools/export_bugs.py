"""将禅道 Bug 按照 Teambition 缺陷模板格式导出为 Excel"""

import logging
import os
import re
from datetime import datetime

import openpyxl

# 确保可以导入 src 模块（从 tools/ 子目录运行时）
import sys
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.config_loader import load_configs
from src.models import BUG_TYPE_NAMES, SEVERITY_NAMES
from src.source_factory import create_source_client
from src.utils import (
    apply_module_filter, normalize_zentao_filters, resolve_assigned_to,
)

logger = logging.getLogger(__name__)


def export_bugs(config: dict, output_path: str = ""):
    source = create_source_client(config)
    source.authenticate()

    filters = config.get("zentao", {}).get("filters", {})
    normalize_zentao_filters(filters)
    # 指派人从公用 assignee.yaml 读（外部 TB 和禅道共用）
    assigned_to = resolve_assigned_to(config.get("assignee", {}), source.account)

    bugs = source.fetch_all_bugs(
        product_id=filters.get("product_id"),
        project_id=filters.get("project_id"),
        statuses=filters.get("statuses"),
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
        assigned_to=assigned_to,
    )
    logger.info("获取到 %d 条 Bug", len(bugs))

    # 模块过滤：数字按"模块+全部子模块"递归；名称优先用模块API预解析为ID集合
    module_filter = (filters.get("module_filter") or "").strip()
    module_id_set = None
    if module_filter:
        if module_filter.isdigit():
            # 数字ID：递归包含子模块（与网页 byModule 一致）
            resolve_desc = getattr(source, "resolve_module_descendant_ids", None)
            if resolve_desc and filters.get("product_id"):
                module_id_set = resolve_desc(
                    int(filters["product_id"]), module_filter)
            bugs = apply_module_filter(
                bugs, module_filter, module_id_set=module_id_set)
            logger.info("模块ID过滤后剩余 %d 条", len(bugs))
        elif filters.get("product_id"):
            module_id_set = source.resolve_module_ids_by_name(
                int(filters["product_id"]), module_filter)
            # 区分空集合（API成功但没匹配）与 None（API失败）
            if module_id_set is not None:
                before = len(bugs)
                bugs = apply_module_filter(
                    bugs, module_filter, module_id_set=module_id_set)
                logger.info(
                    "模块名称 '%s' 命中 %d 个ID，过滤 %d→%d 条",
                    module_filter, len(module_id_set), before, len(bugs))
            else:
                logger.warning(
                    "模块API不可用，将在 fetch_bug_detail 循环中逐条比对 moduleName")

    # 过滤标题中带 VLNS 的已导入 Bug
    export_list = []
    for bug in bugs:
        if re.search(r'VLNS-\d+', bug.title):
            logger.info("跳过已导入: Bug#%d %s", bug.id, bug.title)
        else:
            export_list.append(bug)
    bugs = export_list
    logger.info("过滤后待导出 %d 条", len(bugs))

    tb_cfg = config.get("teambition", {})
    severity_map = tb_cfg.get("severity_map", {
        "1": "A", "2": "B", "3": "C", "4": "C",
        "致命": "S", "严重": "A", "一般": "B", "建议": "C", "轻微": "C",
    })
    type_category_map = tb_cfg.get("type_category_map", {})
    default_reproduction = tb_cfg.get("default_reproduction", "中概率")

    # 创建 Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "缺陷"

    # 表头（按 Teambition 缺陷模板）
    headers = [
        "标题*", "任务状态*", "执行者*", "备注*",
        "所属项目*", "缺陷分类*", "严重程度*", "复现概率*",
        "所属版本*", "SN编码*", "缺陷产生时间*",
    ]
    ws.append(headers)

    # 设置列宽
    col_widths = [50, 10, 12, 60, 15, 25, 10, 10, 20, 15, 18]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    # 填充数据
    skipped_module = 0
    for bug_summary in bugs:
        bug = source.fetch_bug_detail(bug_summary.id)

        # 模块名称过滤回退：数字ID已在前面过滤过；module_id_set 已预解析也已过滤；
        # 只有当 product_id 缺失或模块API不可用时才在此逐条比对 moduleName
        if module_filter and not module_filter.isdigit() and module_id_set is None:
            if module_filter not in (bug.moduleName or ""):
                skipped_module += 1
                logger.info("跳过-模块不匹配: Bug#%d 模块='%s'", bug.id, bug.moduleName)
                continue

        # 字段映射
        tag = f"【禅道{bug.id}】"
        title = f"{tag}{bug.get_base_title()}"
        status = _map_status(bug.status)
        executor = bug.assignedTo or "/"
        steps = _clean_steps(bug.steps)
        project_cfg = tb_cfg.get("project", {})
        project = project_cfg.get("name", project_cfg.get("project_name", bug.projectName))
        category = type_category_map.get(str(bug.type), "应用-其他问题")
        tb_severity = severity_map.get(str(bug.severity), "B")
        reproduction = default_reproduction
        version = bug.openedBuild or "/"
        sn = bug.snCode or "/"
        found_time = _format_datetime(bug.openedDate)
        severity_name = SEVERITY_NAMES.get(str(bug.severity), bug.severity)
        type_name = BUG_TYPE_NAMES.get(str(bug.type), str(bug.type))

        row = [
            title, status, executor, steps,
            project, category, tb_severity, reproduction,
            version, sn, found_time,
        ]
        ws.append(row)
        logger.info("已处理 Bug#%d: %s", bug.id, bug.title)

    # 冻结首行
    ws.freeze_panes = "A2"

    # 保存
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 打包后 exe 同级 exports/，源码环境项目根 exports/
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_dir = os.path.join(base, "exports")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"teambition_import_{ts}.xlsx")

    wb.save(output_path)
    exported = len(bugs) - skipped_module
    print(f"\n导出完成！文件: {output_path}")
    if skipped_module:
        print(f"共导出 {exported} 条缺陷（按模块过滤跳过 {skipped_module} 条）")
    else:
        print(f"共导出 {exported} 条缺陷")
    return output_path


def _format_datetime(dt_str: str) -> str:
    """ISO 8601 → YYYY-MM-DD HH:MM（转为本地时间）"""
    if not dt_str:
        return "/"
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt_str[:16].replace("T", " ")


def _map_status(zentao_status: str) -> str:
    """禅道状态 → Teambition 任务状态"""
    mapping = {
        "active": "待处理",
        "confirmed": "待处理",
        "resolved": "已解决",
        "closed": "关闭",
        "feedback": "待处理",
        "deferred": "待处理",
    }
    return mapping.get(zentao_status, "待处理")


def _clean_steps(steps: str) -> str:
    """清理 steps HTML 标签，保留纯文本"""
    if not steps:
        return "/"
    # 去掉 HTML 标签
    text = re.sub(r'<img[^>]*>', '[图片]', steps)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<hr[^>]*>', '\n---\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip() or "/"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="将禅道 Bug 导出为 Teambition 缺陷模板格式")
    parser.add_argument("--config-dir", default="configs", help="配置文件夹路径")
    parser.add_argument("-o", "--output", default="", help="输出文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_configs(args.config_dir)
    export_bugs(config, args.output)
