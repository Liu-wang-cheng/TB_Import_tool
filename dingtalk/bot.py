"""钉钉群机器人 Webhook 消息推送客户端"""

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Optional, Tuple

import requests

from src.models import SyncStats

logger = logging.getLogger(__name__)


class DingTalkBot:
    """钉钉自定义机器人（Webhook）消息推送

    使用方式：
        bot = DingTalkBot(webhook_url, secret)
        bot.send_text("同步完成")
        bot.send_sync_result(stats, elapsed=120.5)
    """

    def __init__(self, webhook_url: str, secret: str = ""):
        self.webhook_url = webhook_url
        self.secret = secret
        self._http = requests.Session()
        self._recent_sent: dict = {}  # 消息去重缓存 {hash: timestamp}
        self._recent_sent_lock = threading.Lock()

    # ── 加签 ──────────────────────────────────────────

    def _sign(self) -> Tuple[str, str]:
        """生成加签参数：timestamp 和 sign

        钉钉加签算法：
        string_to_sign = timestamp + '\n' + secret
        sign = base64(hmac-sha256(string_to_sign))
        """
        timestamp = str(round(time.time() * 1000))
        if not self.secret:
            return timestamp, ""
        string_to_sign = f"{timestamp}\n{self.secret}"
        sign = base64.b64encode(
            hmac.new(
                self.secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return timestamp, sign

    def _post(self, payload: dict) -> bool:
        """POST 消息到钉钉 Webhook（带 30 秒去重）"""
        # 消息去重：30 秒内相同内容不重复发送
        msg_key = hash(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        now = time.time()
        with self._recent_sent_lock:
            if msg_key in self._recent_sent:
                if now - self._recent_sent[msg_key] < 30:
                    logger.info("钉钉消息去重跳过（30秒内重复）")
                    return True
            self._recent_sent[msg_key] = now
            # 清理过期缓存
            self._recent_sent = {k: v for k, v in self._recent_sent.items() if now - v < 60}

        timestamp, sign = self._sign()
        url = self.webhook_url
        if sign:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={timestamp}&sign={sign}"

        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            resp = self._http.post(url, headers=headers,
                                   data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                   timeout=15)
            if resp.status_code != 200:
                logger.warning("钉钉消息发送失败: HTTP %d %s",
                               resp.status_code, resp.text[:200])
                return False
            data = resp.json()
            if data.get("errcode") != 0:
                logger.warning("钉钉消息发送失败: %s %s",
                               data.get("errcode"), data.get("errmsg"))
                return False
            logger.info("钉钉消息发送成功")
            return True
        except Exception as e:
            logger.warning("钉钉消息发送异常: %s", e)
            return False

    # ── 消息发送 ──────────────────────────────────────

    def send_text(self, content: str, at_mobiles: list = None,
                  at_all: bool = False) -> bool:
        """发送文本消息"""
        payload = {
            "msgtype": "text",
            "text": {"content": content},
        }
        if at_mobiles or at_all:
            payload["at"] = {
                "atMobiles": at_mobiles or [],
                "isAtAll": at_all,
            }
        return self._post(payload)

    def send_markdown(self, title: str, text: str,
                      at_mobiles: list = None, at_all: bool = False) -> bool:
        """发送 Markdown 消息"""
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
        }
        if at_mobiles or at_all:
            payload["at"] = {
                "atMobiles": at_mobiles or [],
                "isAtAll": at_all,
            }
        return self._post(payload)

    def send_sync_result(self, stats: SyncStats, elapsed: float,
                         title: str = "智能缺陷管理平台 同步结果",
                         dry_run: bool = False,
                         project_name: str = "") -> bool:
        """将同步统计格式化为 Markdown 消息发送"""
        text = self.format_sync_result(stats, elapsed, title, dry_run, project_name)
        return self.send_markdown(title, text)

    @staticmethod
    def format_sync_result(stats, elapsed: float,
                           title: str = "智能缺陷管理平台 同步结果",
                           dry_run: bool = False,
                           project_name: str = "") -> str:
        """格式化同步结果为 Markdown 文本（供 send 和 reply 共用）"""
        mode = "【试运行】" if dry_run else ""
        minutes, seconds = divmod(int(elapsed), 60)
        time_str = f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"

        lines = [
            f"## {mode}{title}",
            "",
            "| 指标 | 数值 |",
            "| --- | --- |",
        ]
        if project_name:
            lines.append(f"| TB所属项目 | {project_name} |")
        lines.extend([
            f"| 总计处理 | {stats.total} 条 |",
            f"| 新建成功 | {stats.created} 条 |",
            f"| 重新激活 | {stats.reactivated} 条 |",
            f"| 去重跳过 | {stats.skipped_dedup} 条 |",
            f"| 筛选跳过 | {stats.skipped_filtered} 条 |",
            f"| 错误 | {stats.errors} 条 |",
            f"| 耗时 | {time_str} |",
        ])

        if dry_run:
            lines.append("")
            lines.append("> 本次为试运行，未实际创建/修改任何数据")

        if stats.errors > 0:
            lines.append("")
            lines.append(f"> ⚠️ 有 **{stats.errors}** 条同步出错，请查看日志")

        return "\n".join(lines)

    # ── 回调签名验证 ──────────────────────────────────

    @staticmethod
    def verify_signature(secret: str, timestamp: str, sign: str) -> bool:
        """验证钉钉回调请求的签名"""
        if not secret or not sign:
            return False
        string_to_sign = f"{timestamp}\n{secret}"
        expected = base64.b64encode(
            hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return expected == sign

    # ── 通过 sessionWebhook 回复 ──────────────────────

    @staticmethod
    def reply_by_webhook(webhook_url: str, msgtype: str,
                         content: dict, secret: str = "") -> bool:
        """通过钉钉回调中的 sessionWebhook 回复消息

        参数:
            webhook_url: 回调 body 中的 sessionWebhook 字段
            msgtype: "text" 或 "markdown"
            content: 对应 msgtype 的内容 dict
            secret: 加签密钥（sessionWebhook 通常不需要加签）
        """
        payload = {msgtype: content, "msgtype": msgtype}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            resp = requests.post(
                webhook_url,
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("钉钉回复失败: HTTP %d %s",
                               resp.status_code, resp.text[:200])
                return False
            data = resp.json()
            if data.get("errcode") != 0:
                logger.warning("钉钉回复失败: %s %s",
                               data.get("errcode"), data.get("errmsg"))
                return False
            return True
        except Exception as e:
            logger.warning("钉钉回复异常: %s", e)
            return False

    @staticmethod
    def reply_text(webhook_url: str, content: str,
                   secret: str = "") -> bool:
        """通过 sessionWebhook 发送文本回复"""
        return DingTalkBot.reply_by_webhook(
            webhook_url, "text", {"content": content}, secret
        )

    @staticmethod
    def reply_markdown(webhook_url: str, title: str, text: str,
                       secret: str = "") -> bool:
        """通过 sessionWebhook 发送 Markdown 回复"""
        return DingTalkBot.reply_by_webhook(
            webhook_url, "markdown", {"title": title, "text": text}, secret
        )

    @staticmethod
    def reply_sync_result(webhook_url: str, stats, elapsed: float,
                          title: str = "智能缺陷管理平台 同步结果",
                          dry_run: bool = False, secret: str = "",
                          project_name: str = "") -> bool:
        """通过 sessionWebhook 发送同步结果 Markdown 回复"""
        text = DingTalkBot.format_sync_result(stats, elapsed, title, dry_run, project_name)
        return DingTalkBot.reply_markdown(webhook_url, title, text, secret)
