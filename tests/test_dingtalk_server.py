"""钉钉回调路由冒烟测试

回归目标：指令提交曾挂在不存在的 handle_dingtalk_callback 上（NameError），
所有指令在该步 500、后台任务从未提交——此前本模块零测试未能发现。
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def server(monkeypatch):
    import dingtalk.server as srv
    monkeypatch.setattr(srv, "load_config", lambda: {
        "dingtalk": {"secret": "s", "webhook_url": "http://wh"}})
    monkeypatch.setattr(srv.DingTalkBot, "verify_signature",
                        staticmethod(lambda *a: True))
    monkeypatch.setattr(srv.DingTalkBot, "reply_text",
                        staticmethod(lambda *a, **k: True))
    executor = MagicMock()
    monkeypatch.setattr(srv, "_executor", executor)
    return srv


def _post(server, content):
    client = server.app.test_client()
    return client.post("/dingtalk", json={
        "msgtype": "text",
        "text": {"content": content},
        "sessionWebhook": "http://wh",
    }, headers={"timestamp": "1", "sign": "x"})


def test_sync_command_submits_to_executor(server):
    resp = _post(server, "@机器人 同步")
    assert resp.status_code == 200
    assert server._executor.submit.called, "同步指令必须提交后台任务"
    args = server._executor.submit.call_args[0]
    assert args[0] is server.run_sync
    assert args[2] is False  # dry_run=False


def test_dry_run_command(server):
    _post(server, "@机器人 试运行")
    args = server._executor.submit.call_args[0]
    assert args[0] is server.run_sync
    assert args[2] is True


def test_list_bugs_command(server):
    _post(server, "@机器人 列出bug")
    args = server._executor.submit.call_args[0]
    assert args[0] is server.run_list_bugs


def test_unknown_command_replies_without_submit(server):
    resp = _post(server, "@机器人 随便说说")
    assert resp.status_code == 200
    server._executor.submit.assert_not_called()
