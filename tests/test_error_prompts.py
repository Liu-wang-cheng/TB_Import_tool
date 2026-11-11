"""错误提示完善：配置解析路径、钉钉错误码、敏感信息脱敏"""
from unittest.mock import MagicMock


def test_load_single_yaml_error_has_path(tmp_path):
    import pytest
    from src.config_loader import _load_single
    path = tmp_path / "broken.yaml"
    path.write_text("a: [1,\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        _load_single(str(path))
    text = str(exc.value)
    assert str(path) in text
    assert "格式错误" in text


def test_load_profiles_ex_reports_corruption(tmp_path):
    from gui.ui_profiles import load_profiles_ex
    path = tmp_path / "ui_profiles.yaml"
    path.write_text("a: [1,\n", encoding="utf-8")
    profiles, error = load_profiles_ex(str(path))
    assert profiles == []
    assert str(path) in error
    assert "读取失败" in error


def test_fetch_relay_models(monkeypatch):
    import src.classifier as mod

    class Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": [{"id": "m1"}, {"id": "m2"}, {"id": "m1"}]}

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers or {}, timeout))
        return Resp()

    monkeypatch.setattr(mod.requests, "get", fake_get)
    models = mod.fetch_relay_models(
        "http://192.168.160.145:8081/v1", "k", timeout=5)
    assert models == ["m1", "m2"]
    assert calls[0][0].endswith("/v1/models")
    assert calls[0][1]["Authorization"] == "Bearer k"
    assert calls[0][2] == 5


def test_dingtalk_errcode_hints():
    from dingtalk.bot import DingTalkBot
    assert "加签" in DingTalkBot._errcode_hint(310000)
    assert "频率" in DingTalkBot._errcode_hint(130101)
    assert "安全设置" in DingTalkBot._errcode_hint(999999)


def test_config_dialog_redacts_secret_from_error():
    from gui.config_dialog import ConfigDialog
    dlg = MagicMock()
    dlg.zt_password.text.return_value = "super-secret-password"
    text = ConfigDialog._redact_error(
        dlg, RuntimeError("写入失败: super-secret-password"))
    assert "super-secret-password" not in text
    assert "***" in text
