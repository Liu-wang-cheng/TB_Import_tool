"""TB 权限兜底：无权限时按任务创建人 → 邢宏普顺序切换操作人重试"""
import json
from unittest.mock import MagicMock

from src.teambition_client import TeambitionClient


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _client():
    c = TeambitionClient("a", "s", "o", "p", api_delay=0,
                         operator_id="creator")
    c._ensure_token = lambda: None
    c._get_headers = lambda: {"X-Operator-Id": c.operator_id}
    c.permission_task_creator = "taskcreator"
    c.permission_fallback_operator = "admin"
    return c


def test_permission_fallback_to_task_creator():
    c = _client()
    calls = []

    def request(method, url, headers=None, timeout=None, **kwargs):
        calls.append(headers.get("X-Operator-Id"))
        if len(calls) == 1:
            return _Resp(403, {"code": 403,
                               "errorMessage": "The operation has no permission"})
        return _Resp(200, {"result": []})

    c._http = MagicMock()
    c._http.request.side_effect = request
    assert c._request("POST", "/x", json={}) == {"result": []}
    assert calls == ["creator", "taskcreator"]
    assert c.operator_id == "taskcreator"


def test_permission_fallback_to_admin_when_creator_fails():
    c = _client()
    calls = []

    def request(method, url, headers=None, timeout=None, **kwargs):
        calls.append(headers.get("X-Operator-Id"))
        if len(calls) <= 2:
            return _Resp(403, {"code": 403,
                               "errorMessage": "The operation has no permission"})
        return _Resp(200, {"result": []})

    c._http = MagicMock()
    c._http.request.side_effect = request
    assert c._request("GET", "/x") == {"result": []}
    assert calls == ["creator", "taskcreator", "admin"]
    assert c.operator_id == "admin"


def test_no_fallback_for_non_permission_error():
    c = _client()
    calls = []

    def request(method, url, headers=None, timeout=None, **kwargs):
        calls.append(headers.get("X-Operator-Id"))
        return _Resp(422, {"code": 422, "errorMessage": "invalid request"})

    c._http = MagicMock()
    c._http.request.side_effect = request
    try:
        c._request("GET", "/x")
    except Exception:
        pass
    assert calls == ["creator"]
