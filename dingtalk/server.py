"""钉钉机器人 HTTP 回调服务

在钉钉群里@机器人发送指令触发导入。

启动方式:
    python dingtalk_server.py

然后用 ngrok 暴露端口:
    ngrok http 8080

将 ngrok 提供的 https URL + /dingtalk 填入钉钉机器人回调地址。
"""

import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, request

from dingtalk.bot import DingTalkBot
from src.config_loader import load_configs
from src.config_resolver import ConfigResolver
from src.source_factory import create_source_client
from src.sync_engine import SyncEngine
from src.teambition_client import TeambitionClient
from src.utils import normalize_zentao_filters, resolve_assigned_to

app = Flask(__name__)
logger = logging.getLogger(__name__)

# 同步互斥锁：防止并发同步导致重复创建
_sync_lock = threading.Lock()

# ── 配置 ──────────────────────────────────────────

# 指令 → 动作映射
COMMAND_MAP = {
    "同步": "sync",
    "导入": "sync",
    "试运行": "dry_run",
    "列出bug": "list_bugs",
    "帮助": "help",
    "状态": "status",
}


def load_config() -> dict:
    """加载配置（优先 configs/ 目录，回退到 config.yaml）"""
    return load_configs("configs")


# ── 客户端初始化 ──────────────────────────────────

def init_clients(config: dict):
    """初始化源平台和 Teambition 客户端"""
    sync_cfg = config.get("sync", {})
    tb_cfg = config["teambition"]

    source = create_source_client(config)

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
    return source, teambition


# ── 回调处理 ──────────────────────────────────────

def _extract_command(text: str) -> str:
    """从消息文本中提取指令，去掉 @机器人 部分"""
    # 去掉 @xxx 的部分（钉钉会在文本中包含 @机器人 的内容）
    text = re.sub(r'@[\w\-\u4e00-\u9fff]+', '', text)
    text = text.strip()
    # 去掉可能的前导空格和标点
    text = text.lstrip('：: ')
    return text


def _parse_command(text: str) -> tuple:
    """解析指令，返回 (action, args)"""
    cmd_text = _extract_command(text)
    # 精确匹配
    if cmd_text in COMMAND_MAP:
        return COMMAND_MAP[cmd_text], cmd_text
    # 前缀匹配
    for key, action in COMMAND_MAP.items():
        if cmd_text.startswith(key):
            return action, cmd_text
    return None, cmd_text


# ── 指令执行 ──────────────────────────────────────

def run_sync(reply_webhook: str, dry_run: bool = False):
    """后台线程执行导入同步（非试运行模式加锁防并发）"""
    if not dry_run and not _sync_lock.acquire(blocking=False):
        DingTalkBot.reply_text(reply_webhook, "同步正在进行中，请稍后再试")
        return
    start_time = time.time()
    try:
        config = load_config()
        source, teambition = init_clients(config)

        # 源平台认证
        source.authenticate()

        if not dry_run:
            teambition.authenticate()

        # 解析配置中的中文名称 → ID
        resolver = ConfigResolver(config, source, teambition)
        resolver.resolve()

        filters = config.get("zentao", {}).get("filters", {})
        normalize_zentao_filters(filters)
        engine = SyncEngine(config, source, teambition)
        stats = engine.run(dry_run=dry_run)

        elapsed = time.time() - start_time
        tb_cfg = config.get("teambition", {})
        project_name = (
            tb_cfg.get("belong_project_value", "")
            or tb_cfg.get("project", {}).get("name", "")
        )
        source_name = {"zentao": "禅道", "jira": "Jira",
                       "teambition": "外部TB"}.get(
            getattr(source, "source_type", "zentao"), "禅道")
        DingTalkBot.reply_sync_result(reply_webhook, stats, elapsed,
                                      dry_run=dry_run,
                                      project_name=project_name,
                                      source_name=source_name)

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error("钉钉指令执行失败: %s", e, exc_info=True)
        DingTalkBot.reply_text(
            reply_webhook,
            f"执行失败 ({'试运行' if dry_run else '正式同步'})\n错误: {str(e)[:500]}"
        )
    finally:
        if not dry_run:
            _sync_lock.release()


def run_list_bugs(reply_webhook: str):
    """后台线程列出禅道 Bug"""
    try:
        config = load_config()
        source, _ = init_clients(config)
        source.authenticate()

        filters = config.get("zentao", {}).get("filters", {})
        normalize_zentao_filters(filters)
        assigned_to = resolve_assigned_to(filters, source.account)

        bugs = source.fetch_all_bugs(
            product_id=filters.get("product_id"),
            project_id=filters.get("project_id"),
            statuses=filters.get("statuses"),
            date_from=filters.get("date_from"),
            date_to=filters.get("date_to"),
            assigned_to=assigned_to,
        )

        sev_map = config.get("teambition", {}).get("severity_map", {})
        sev_labels = source.fetch_severity_labels(filters.get("product_id"))
        lines = [f"共 {len(bugs)} 条 Bug:", ""]
        lines.append("| ID | 状态 | 严重程度 | 指派给 | 标题 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for bug in bugs[:30]:
            s = str(bug.severity).strip() if bug.severity else ""
            label = sev_labels.get(s, s) if sev_labels else s
            tb_sev = sev_map.get(s)
            if tb_sev is None and s.isdigit():
                tb_sev = sev_map.get(int(s))
            sev = f"{label}→{tb_sev}" if tb_sev is not None else label
            assignee = bug.assignedTo[:8] if bug.assignedTo else "-"
            title = bug.title
            lines.append(f"| {bug.id} | {bug.status} | {sev} | {assignee} | {title} |")

        if len(bugs) > 30:
            lines.append(f"\n> 仅显示前 30 条，共 {len(bugs)} 条")

        DingTalkBot.reply_markdown(reply_webhook, "禅道 Bug 列表", "\n".join(lines))

    except Exception as e:
        logger.error("列出Bug失败: %s", e, exc_info=True)
        DingTalkBot.reply_text(reply_webhook, f"列出Bug失败: {str(e)[:500]}")


def send_help(reply_webhook: str):
    """发送帮助信息"""
    text = (
        "**智能缺陷管理平台**\n\n"
        "支持的指令:\n"
        "- `@机器人 同步` / `@机器人 导入` — 执行全量同步\n"
        "- `@机器人 试运行` — 模拟运行（不实际创建）\n"
        "- `@机器人 列出bug` — 列出当前筛选条件下的禅道 Bug\n"
        "- `@机器人 状态` — 查看系统状态\n"
        "- `@机器人 帮助` — 显示本帮助\n\n"
        "同步结果会通过 Markdown 消息汇报到群里。"
    )
    DingTalkBot.reply_markdown(reply_webhook, "帮助", text)


def send_status(reply_webhook: str):
    """发送系统状态"""
    try:
        config = load_config()
        z_cfg = config.get("zentao", {})
        tb_cfg = config.get("teambition", {})
        project_cfg = tb_cfg.get("project", {})
        project_name = project_cfg.get("name", project_cfg.get("project_name", "未配置"))
        project_id = tb_cfg.get("project_id", project_cfg.get("id", "未配置"))
        text = (
            f"**系统状态**\n\n"
            f"- 禅道地址: {z_cfg.get('base_url', '未配置')}\n"
            f"- 禅道账号: {z_cfg.get('account', '未配置')}\n"
            f"- Teambition 项目: {project_name} ({str(project_id)[:8]}...)\n"
            f"- 当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        DingTalkBot.reply_markdown(reply_webhook, "系统状态", text)
    except Exception as e:
        DingTalkBot.reply_text(reply_webhook, f"获取状态失败: {str(e)[:300]}")


# ── Flask 路由 ────────────────────────────────────

@app.route('/dingtalk', methods=['POST'])
def dingtalk_callback():
    """接收钉钉机器人回调"""
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}

    # 1. 验证签名
    config = load_config()
    secret = config.get("dingtalk", {}).get("secret", "")
    if secret:
        timestamp = request.headers.get("timestamp", "")
        sign = request.headers.get("sign", "")
        if not DingTalkBot.verify_signature(secret, timestamp, sign):
            logger.warning("钉钉回调签名验证失败")
            return ""
    else:
        logger.error("钉钉回调未配置 secret，拒绝处理请求（请配置 dingtalk.secret）")
        return ""

    # 2. 提取消息内容
    msg_type = body.get("msgtype", "")
    if msg_type == "text":
        content = body.get("text", {}).get("content", "")
    else:
        content = ""

    # 3. 解析指令
    action, raw_cmd = _parse_command(content)
    logger.info("钉钉指令: action=%s raw='%s'", action, raw_cmd)

    # 4. 获取回复用的 webhook（优先使用 sessionWebhook）
    reply_webhook = body.get("sessionWebhook", "")
    if not reply_webhook:
        reply_webhook = config.get("dingtalk", {}).get("webhook_url", "")

    # 5. 立即回复确认（钉钉回调3秒超时）
    if action in ("sync", "dry_run"):
        mode = "试运行" if action == "dry_run" else "同步"
        DingTalkBot.reply_text(reply_webhook, f"收到指令：{mode}，正在执行...")
    elif action == "list_bugs":
        DingTalkBot.reply_text(reply_webhook, "收到指令：列出 Bug，正在查询...")
    elif action == "status":
        send_status(reply_webhook)
        return ""
    elif action == "help":
        send_help(reply_webhook)
        return ""
    else:
        DingTalkBot.reply_text(
            reply_webhook,
            f"未知指令: '{raw_cmd}'\n发送 '@机器人 帮助' 查看支持的指令"
        )
        return ""

    # 6. 在后台线程中异步执行（限制并发数，避免资源耗尽）
    if not hasattr(handle_dingtalk_callback, '_executor'):
        handle_dingtalk_callback._executor = ThreadPoolExecutor(max_workers=2)

    if action == "sync":
        handle_dingtalk_callback._executor.submit(run_sync, reply_webhook, False)
    elif action == "dry_run":
        handle_dingtalk_callback._executor.submit(run_sync, reply_webhook, True)
    elif action == "list_bugs":
        handle_dingtalk_callback._executor.submit(run_list_bugs, reply_webhook)

    return ""


@app.route('/health', methods=['GET'])
def health():
    return {"status": "ok"}


# ── 入口 ──────────────────────────────────────────

if __name__ == '__main__':
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()
    port = config.get("dingtalk", {}).get("callback_port", 8080)

    logger.info("=" * 50)
    logger.info("钉钉机器人服务启动")
    logger.info("监听端口: %d", port)
    logger.info("回调地址: http://你的IP:%d/dingtalk", port)
    logger.info("=" * 50)
    logger.info("提示: 如需公网访问，请使用 ngrok 暴露端口")
    logger.info("  ngrok http %d", port)

    app.run(host='0.0.0.0', port=port, debug=False)
