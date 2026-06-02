"""测试 gui/yaml_utils.py — YAML 原地编辑"""
import os
import tempfile

import pytest
import yaml

from gui.yaml_utils import update_yaml_values


@pytest.fixture
def tmp_yaml():
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False,
                                    encoding="utf-8")
    yield f
    f.close()
    if os.path.exists(f.name):
        os.unlink(f.name)


class TestNullToList:
    """标量 null → 列表 转换（修复 null- 前缀 bug）"""

    def test_null_becomes_list(self, tmp_yaml):
        tmp_yaml.write("filters:\n  assigned_to: null\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"filters.assigned_to": ["项目-李珍"]})
        with open(tmp_yaml.name) as f:
            content = f.read()
        data = yaml.safe_load(content)
        assert data["filters"]["assigned_to"] == ["项目-李珍"]
        assert "assigned_to: null" not in content

    def test_list_becomes_null(self, tmp_yaml):
        tmp_yaml.write("filters:\n  assigned_to:\n    - a\n    - b\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"filters.assigned_to": None})
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["filters"]["assigned_to"] is None

    def test_null_to_list_to_null_roundtrip(self, tmp_yaml):
        tmp_yaml.write("filters:\n  assigned_to: null\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"filters.assigned_to": ["x"]})
        update_yaml_values(tmp_yaml.name, {"filters.assigned_to": None})
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["filters"]["assigned_to"] is None

    def test_assigned_to_and_known_together(self, tmp_yaml):
        tmp_yaml.write(
            "filters:\n"
            "  assigned_to: null\n"
            "  assigned_to_known:\n"
            "    - u1\n"
            "    - u2\n"
        )
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {
            "filters.assigned_to": ["only_me"],
            "filters.assigned_to_known": ["u1", "u2", "only_me"],
        })
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["filters"]["assigned_to"] == ["only_me"]
        assert data["filters"]["assigned_to_known"] == ["u1", "u2", "only_me"]


class TestDateFormat:
    """日期字符串加引号保护（防止 PyYAML 误解析为 datetime.date）"""

    def test_date_string_quoted(self, tmp_yaml):
        tmp_yaml.write("filters:\n  date_from: null\n  date_to: null\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {
            "filters.date_from": "2026-01-15",
            "filters.date_to": "2026-06-30",
        })
        with open(tmp_yaml.name) as f:
            content = f.read()
        assert '"2026-01-15"' in content
        assert '"2026-06-30"' in content
        data = yaml.safe_load(content)
        assert isinstance(data["filters"]["date_from"], str)
        assert isinstance(data["filters"]["date_to"], str)

    def test_date_null_handled(self, tmp_yaml):
        tmp_yaml.write("filters:\n  date_from: \"2026-01-01\"\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"filters.date_from": None})
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["filters"]["date_from"] is None


class TestListShrink:
    """列表缩小/扩大"""

    def test_shrink_3_to_1(self, tmp_yaml):
        tmp_yaml.write("filters:\n  statuses:\n    - active\n    - confirmed\n    - resolved\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"filters.statuses": ["active"]})
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["filters"]["statuses"] == ["active"]

    def test_empty_list_removes_items(self, tmp_yaml):
        tmp_yaml.write("filters:\n  statuses:\n    - active\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"filters.statuses": []})
        data = yaml.safe_load(open(tmp_yaml.name))
        # 空列表写入后解析为 None（YAML key 无值）
        assert data["filters"]["statuses"] is None


class TestScalarUpdate:
    """标量值更新"""

    def test_string_to_string(self, tmp_yaml):
        tmp_yaml.write("base_url: \"http://old.example.com\"\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"base_url": "http://new.example.com"})
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["base_url"] == "http://new.example.com"

    def test_int_to_int(self, tmp_yaml):
        tmp_yaml.write("filters:\n  product: 11\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"filters.product": 999})
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["filters"]["product"] == 999

    def test_bool_to_bool(self, tmp_yaml):
        tmp_yaml.write("enabled: false\n")
        tmp_yaml.flush()
        update_yaml_values(tmp_yaml.name, {"enabled": True})
        data = yaml.safe_load(open(tmp_yaml.name))
        assert data["enabled"] is True
