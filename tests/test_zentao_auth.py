"""测试 src/zentao_client.py — 认证错误检测"""
import pytest
from unittest.mock import Mock, patch, MagicMock

from src.zentao_client import ZentaoClient, ZentaoAPIError


class TestEnsureToken:
    """_ensure_token 认证失败检测"""

    def make_client(self):
        client = ZentaoClient.__new__(ZentaoClient)
        client._token = None
        client._cloud_session_auth = False
        client.base_url = "http://test.local"
        client.account = "testuser"
        client.password = "wrongpass"
        client._http = Mock()
        return client

    def test_http_error_raises(self):
        client = self.make_client()
        resp = Mock()
        resp.status_code = 401
        client._http.post.return_value = resp

        with pytest.raises(ZentaoAPIError):
            client._ensure_token()

    def test_status_fail_raises(self):
        client = self.make_client()
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"status": "fail", "message": "密码错误"}
        client._http.post.return_value = resp

        with pytest.raises(ZentaoAPIError, match="认证失败.*密码错误"):
            client._ensure_token()

    def test_result_fail_raises(self):
        client = self.make_client()
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"result": "failed", "reason": "账号不存在"}
        client._http.post.return_value = resp

        with pytest.raises(ZentaoAPIError, match="认证失败.*账号不存在"):
            client._ensure_token()

    def test_no_token_no_errcode_raises(self):
        client = self.make_client()
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {}
        client._http.post.return_value = resp

        with pytest.raises(ZentaoAPIError, match="未获取到token"):
            client._ensure_token()

    def test_errcode_detects_cloud(self):
        client = self.make_client()
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"errcode": 401, "errmsg": "缺少code参数"}
        client._http.post.return_value = resp

        client._ensure_token()
        assert client._cloud_session_auth is True
        assert client._token is None

    def test_token_valid_verified_with_user_api(self):
        client = self.make_client()
        token_resp = Mock()
        token_resp.status_code = 201
        token_resp.json.return_value = {"token": "abc123"}
        verify_resp = Mock()
        verify_resp.status_code = 200
        client._http.post.return_value = token_resp
        client._http.get.return_value = verify_resp

        client._ensure_token()
        assert client._token == "abc123"

    def test_token_invalid_detected_by_user_403(self):
        client = self.make_client()
        token_resp = Mock()
        token_resp.status_code = 201
        token_resp.json.return_value = {"token": "fake_token"}
        verify_resp = Mock()
        verify_resp.status_code = 403
        client._http.post.return_value = token_resp
        client._http.get.return_value = verify_resp

        with pytest.raises(ZentaoAPIError, match="账号或密码错误"):
            client._ensure_token()

    def test_token_already_set_returns_early(self):
        client = self.make_client()
        client._token = "existing"
        client._ensure_token()  # should not make any HTTP call

    def test_token_empty_string_raises(self):
        client = self.make_client()
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"token": ""}
        client._http.post.return_value = resp

        with pytest.raises(ZentaoAPIError, match="未获取到token"):
            client._ensure_token()


class TestEnsureSession:
    """_ensure_session 云版登录错误检测"""

    def make_client(self):
        client = ZentaoClient.__new__(ZentaoClient)
        client._session_logged_in = False
        client._session_id = None
        client._cloud_session_auth = True
        client.base_url = "https://freedynamics.chandao.com"
        client.account = "testuser"
        client.password = "wrongpass"
        client._http = Mock()
        return client

    def _setup_session_id(self, client):
        """模拟获取 sessionID 成功"""
        sid_resp = Mock()
        sid_resp.status_code = 200
        sid_resp.json.return_value = {"data": '{"sessionID":"sid123"}'}
        client._http.get.return_value = sid_resp

    def test_result_fail_detected(self):
        client = self.make_client()
        self._setup_session_id(client)
        login_resp = Mock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"result": "fail", "message": "密码错误"}

        # First GET for session ID, then POST for login
        client._http.get.return_value = Mock(status_code=200, json=Mock(
            return_value={"data": '{"sessionID":"sid123"}'}))
        client._http.post.return_value = login_resp

        with pytest.raises(ZentaoAPIError, match="登录失败.*密码错误"):
            client._ensure_session()

    def test_status_fail_detected(self):
        client = self.make_client()
        login_resp = Mock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"status": "failed", "reason": "账号或密码不正确"}
        client._http.get.return_value = Mock(status_code=200, json=Mock(
            return_value={"data": '{"sessionID":"sid123"}'}))
        client._http.post.return_value = login_resp

        with pytest.raises(ZentaoAPIError, match="登录失败.*账号或密码不正确"):
            client._ensure_session()

    def test_errcode_nonzero_detected(self):
        client = self.make_client()
        login_resp = Mock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"errcode": 401, "errmsg": "密码错误"}
        client._http.get.return_value = Mock(status_code=200, json=Mock(
            return_value={"data": '{"sessionID":"sid123"}'}))
        client._http.post.return_value = login_resp

        with pytest.raises(ZentaoAPIError, match="登录失败.*401.*密码错误"):
            client._ensure_session()

    def test_result_null_with_message_detected(self):
        client = self.make_client()
        login_resp = Mock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"result": None, "message": "密码错误"}
        client._http.get.return_value = Mock(status_code=200, json=Mock(
            return_value={"data": '{"sessionID":"sid123"}'}))
        client._http.post.return_value = login_resp

        with pytest.raises(ZentaoAPIError, match="登录失败.*密码错误"):
            client._ensure_session()

    def test_success_sets_logged_in(self):
        client = self.make_client()
        login_resp = Mock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"result": "success", "user": {"id": 1}}
        client._http.get.return_value = Mock(status_code=200, json=Mock(
            return_value={"data": '{"sessionID":"sid123"}'}))
        client._http.post.return_value = login_resp

        client._ensure_session()
        assert client._session_logged_in is True

    def test_http_error_raises(self):
        client = self.make_client()
        client._http.get.return_value = Mock(status_code=200, json=Mock(
            return_value={"data": '{"sessionID":"sid123"}'}))
        login_resp = Mock()
        login_resp.status_code = 500
        client._http.post.return_value = login_resp

        with pytest.raises(ZentaoAPIError):
            client._ensure_session()

    def test_already_logged_in_returns_early(self):
        client = self.make_client()
        client._session_logged_in = True
        client._ensure_session()  # no HTTP call, no error


class TestAuthenticate:
    """authenticate 总入口"""

    def test_cloud_auth_calls_session(self):
        client = ZentaoClient.__new__(ZentaoClient)
        client._token = None
        client._cloud_session_auth = False
        client._session_logged_in = False
        client.base_url = "https://freedynamics.chandao.com"
        client.account = "user"
        client.password = "pass"
        client._http = Mock()

        # _ensure_token detects cloud
        token_resp = Mock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"errcode": 401}
        # _ensure_session succeeds
        sid_resp = Mock()
        sid_resp.status_code = 200
        sid_resp.json.return_value = {"data": '{"sessionID":"sid"}'}
        login_resp = Mock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"result": "success", "user": {"id": 1}}

        client._http.post.side_effect = [token_resp, login_resp]
        client._http.get.return_value = sid_resp

        client.authenticate()
        assert client._cloud_session_auth is True
        assert client._session_logged_in is True


class TestResolveAssignedToCloud:
    """_resolve_assigned_to_cloud 云版指派人解析"""

    def make_client(self):
        client = ZentaoClient.__new__(ZentaoClient)
        client._cloud_session_auth = True
        client._cloud_user_name_to_account = {}
        return client

    def test_bare_name_exact_match(self):
        client = self.make_client()
        client._cloud_user_name_to_account = {"邓建和": "dengjianhe"}
        result = client._resolve_assigned_to_cloud(["邓建和"])
        assert "邓建和" in result
        assert "dengjianhe" in result

    def test_bare_name_suffix_match(self):
        client = self.make_client()
        client._cloud_user_name_to_account = {"部门-邓建和": "dengjianhe"}
        result = client._resolve_assigned_to_cloud(["邓建和"])
        assert "dengjianhe" in result
        assert "部门-邓建和" in result

    def test_prefixed_name_splits_suffix(self):
        client = self.make_client()
        client._cloud_user_name_to_account = {"陈斌": "chenbin"}
        result = client._resolve_assigned_to_cloud(["IOT-陈斌"])
        assert "IOT-陈斌" in result
        assert "陈斌" in result
        assert "chenbin" in result

    def test_not_cloud_returns_raw(self):
        client = self.make_client()
        client._cloud_session_auth = False
        result = client._resolve_assigned_to_cloud(["邓建和"])
        assert result == {"邓建和"}


class TestPassesFilters:
    """_passes_filters_with_assignees 客户端筛选"""

    def make_bug(self, **kwargs):
        from src.models import ZentaoBug
        defaults = dict(
            id=1, title="test", severity="1", pri="1", type="bug",
            status="active", steps="", assignedTo="", assignedToAccount="",
            openedBy="", openedByAccount="", openedDate="2026-06-01",
            product="1", productName="", project="", projectName="",
            module="", moduleName="", openedBuild="", snCode="", files=[],
        )
        defaults.update(kwargs)
        return ZentaoBug(**defaults)

    def make_client(self):
        client = ZentaoClient.__new__(ZentaoClient)
        client._cloud_session_auth = False
        client.account = "testuser"
        return client

    def test_date_from_filters_out_early_bug(self):
        client = self.make_client()
        bug = self.make_bug(openedDate="2025-12-01")
        assert not client._passes_filters_with_assignees(
            bug, None, "2026-01-01", None, set())

    def test_date_from_keeps_later_bug(self):
        client = self.make_client()
        bug = self.make_bug(openedDate="2026-03-01")
        assert client._passes_filters_with_assignees(
            bug, None, "2026-01-01", None, set())

    def test_date_to_filters_out_late_bug(self):
        client = self.make_client()
        bug = self.make_bug(openedDate="2026-05-01")
        assert not client._passes_filters_with_assignees(
            bug, None, None, "2026-03-31", set())

    def test_date_range_keeps_middle_bug(self):
        client = self.make_client()
        bug = self.make_bug(openedDate="2026-02-15")
        assert client._passes_filters_with_assignees(
            bug, None, "2026-01-01", "2026-03-31", set())

    def test_status_filter(self):
        client = self.make_client()
        bug = self.make_bug(status="resolved")
        assert not client._passes_filters_with_assignees(
            bug, ["active", "confirmed"], None, None, set())

    def test_assigned_suffix_match(self):
        client = self.make_client()
        bug = self.make_bug(assignedTo="部门-邓建和", assignedToAccount="dengjianhe")
        # 筛选 "邓建和" 应匹配 "部门-邓建和"（后缀匹配）
        assert client._passes_filters_with_assignees(
            bug, None, None, None, {"邓建和"})

    def test_assigned_account_match(self):
        client = self.make_client()
        bug = self.make_bug(assignedToAccount="dengjianhe")
        assert client._passes_filters_with_assignees(
            bug, None, None, None, {"dengjianhe"})


class TestPassesFiltersSelfHosted:
    """自建版指派人筛选"""

    def make_client(self):
        client = ZentaoClient.__new__(ZentaoClient)
        client._cloud_session_auth = False
        client.account = "myaccount"
        return client

    def test_me_resolves_to_account(self):
        client = self.make_client()
        from src.models import ZentaoBug
        bug = ZentaoBug(
            id=1, title="t", severity="1", pri="1", type="bug",
            status="active", steps="", assignedTo="myaccount",
            assignedToAccount="myaccount", openedBy="", openedByAccount="",
            openedDate="2026-01-01", product="1", productName="",
            project="", projectName="", module="", moduleName="",
            openedBuild="", snCode="", files=[],
        )
        assert client._passes_filters(
            bug, None, None, None, ["me"])
