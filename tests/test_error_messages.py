"""提示/告警质量：认证失败、网络异常、配置缺失的可读提示"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.teambition_client import TeambitionAPIError, TeambitionClient


class TestTeambitionFriendlyErrors:
    def test_401_app_credentials(self):
        msg = TeambitionClient._friendly_error(401, "unauthorized", "/x")
        assert "app_id" in msg and "app_secret" in msg

    def test_403_app_not_installed(self):
        msg = TeambitionClient._friendly_error(
            403, "应用在该企业未安装", "/x")
        assert "未在该企业安装" in msg and "发布" in msg

    def test_404_resource(self):
        msg = TeambitionClient._friendly_error(404, "not found", "/x")
        assert "404" in msg and "项目" in msg

    def test_network_error_message(self):
        c = TeambitionClient(app_id="a", app_secret="s", org_id="o",
                             project_id="p")
        c._app_token_mode = True
        c._http = MagicMock()
        c._http.request.side_effect = requests.exceptions.ConnectionError(
            "boom")
        with patch("time.sleep", lambda *_: None):
            with pytest.raises(TeambitionAPIError) as ei:
                c._request("GET", "/x")
        msg = str(ei.value)
        assert "无法连接" in msg and "网络" in msg

    def test_verify_connection_missing_config(self):
        c = TeambitionClient(app_id="", app_secret="", org_id="",
                             project_id="p")
        with pytest.raises(TeambitionAPIError) as ei:
            c.verify_connection()
        assert "配置不完整" in str(ei.value)


class TestZentaoFriendlyErrors:
    def _client(self):
        from src.zentao_client import ZentaoClient
        return ZentaoClient("http://x/zentao", "u", "p")

    @staticmethod
    def _resp(status=400, body=None, text=""):
        r = MagicMock()
        r.status_code = status
        r.text = text or '{"error":"登录失败，请检查您的用户名或密码"}'
        r.json.return_value = body if body is not None else {
            "error": "登录失败，请检查您的用户名或密码"}
        return r

    def test_wrong_password_message(self):
        from src.zentao_client import ZentaoAPIError
        c = self._client()
        c._http = MagicMock()
        c._http.post.return_value = self._resp(400)
        with pytest.raises(ZentaoAPIError) as ei:
            c._ensure_token()
        msg = str(ei.value)
        assert "账号或密码错误" in msg
        assert "服务器提示" in msg

    def test_network_timeout_message(self):
        from src.zentao_client import ZentaoAPIError
        c = self._client()
        c._http = MagicMock()
        c._http.post.side_effect = requests.exceptions.Timeout("t")
        with pytest.raises(ZentaoAPIError) as ei:
            c._ensure_token()
        assert "超时" in str(ei.value) and "VPN" in str(ei.value)

    def test_wrong_base_url_non_json(self):
        from src.zentao_client import ZentaoAPIError
        c = self._client()
        c._http = MagicMock()
        r = self._resp(200, body=None)
        r.json.side_effect = ValueError("no json")
        c._http.post.return_value = r
        with pytest.raises(ZentaoAPIError) as ei:
            c._ensure_token()
        assert "不是 JSON" in str(ei.value) or "服务器地址" in str(ei.value)


class TestExternalTbAuthMessages:
    def test_login_failure_raises_clear_message(self):
        from src.teambition_source_client import TeambitionSourceClient
        c = TeambitionSourceClient(base_url="https://tb.example.com",
                                   account="u", password="p")
        c._load_cookie = lambda: ""
        c._login_via_selenium = lambda a, p: ""
        c._last_login_error = "验证码错误"
        with pytest.raises(RuntimeError) as ei:
            c.authenticate()
        msg = str(ei.value)
        assert "登录失败" in msg and "账号/密码" in msg
        assert "验证码错误" in msg

    def test_missing_url_raises(self):
        from src.teambition_source_client import TeambitionSourceClient
        c = TeambitionSourceClient(base_url="", account="u", password="p")
        with pytest.raises(RuntimeError) as ei:
            c.authenticate()
        assert "服务器地址未配置" in str(ei.value)


class TestConfigValidation:
    def test_init_clients_missing_tb_config(self):
        from gui.workers import _init_clients
        with pytest.raises(ValueError) as ei:
            _init_clients({"teambition": {"app_id": "a"},
                           "source": {"platform": "zentao"}})
        msg = str(ei.value)
        assert "app_secret" in msg and "org_id" in msg
        assert "配置" in msg


class TestConfigYamlErrors:
    def test_bad_yaml_reports_filename(self, tmp_path):
        from src.config_loader import load_configs
        (tmp_path / "good.yaml").write_text("a: 1\n", encoding="utf-8")
        (tmp_path / "broken.yaml").write_text(
            "a: [unclosed\n", encoding="utf-8")
        with pytest.raises(ValueError) as ei:
            load_configs(str(tmp_path))
        msg = str(ei.value)
        assert "broken.yaml" in msg
        assert "格式错误" in msg


class TestTeambitionBusinessErrors:
    def test_business_credential_error(self):
        msg = TeambitionClient._friendly_error(
            200, "应用凭证错误，请检查凭证是否正确。报错：authbase.Verify "
                 "failed: verify token: signature is invalid",
            "/org/member/list", err_code="10003")
        assert "app_id" in msg and "app_secret" in msg

    def test_business_app_not_installed(self):
        msg = TeambitionClient._friendly_error(
            200, "应用在该企业未安装", "/x", err_code="403")
        assert "未在该企业安装" in msg

    def test_error_str_omits_zero_code(self):
        e = TeambitionAPIError(0, "网络错误", "/x")
        assert "[0]" not in str(e)
        assert "网络错误" in str(e)
