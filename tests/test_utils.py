"""测试 src/utils.py — 指派人解析、URL解析、部门前缀提取"""
import pytest

from src.utils import (
    resolve_assigned_to,
    extract_department_prefix,
    parse_zentao_url,
    normalize_zentao_filters,
)


class TestResolveAssignedTo:
    """resolve_assigned_to 指派人解析"""

    def test_none_returns_none(self):
        assert resolve_assigned_to({"assigned_to": None}) is None

    def test_empty_list_returns_none(self):
        result = resolve_assigned_to({"assigned_to": []})
        assert result is None or result == []

    def test_single_name(self):
        result = resolve_assigned_to({"assigned_to": ["张三"]})
        assert "张三" in result

    def test_me_replaced_by_account(self):
        result = resolve_assigned_to({"assigned_to": ["me"]}, "myaccount")
        assert "myaccount" in result

    def test_prefixed_name_splits_suffix(self):
        filters = {
            "assigned_to": ["IOT-陈斌"],
            "assigned_to_known": ["IOT-陈斌"],
        }
        result = resolve_assigned_to(filters)
        assert "IOT-陈斌" in result
        assert "陈斌" in result

    def test_duplicate_suffix_not_added(self):
        filters = {
            "assigned_to": ["IOT-陈斌"],
            "assigned_to_known": ["IOT-陈斌", "应用-陈斌"],
        }
        result = resolve_assigned_to(filters)
        # 后缀 "陈斌" 有冲突，不应被加入
        assert "陈斌" not in result

    def test_mixed_with_me(self):
        filters = {
            "assigned_to": ["IOT-陈斌", "me"],
            "assigned_to_known": ["IOT-陈斌", "应用-罗林旺", "me"],
        }
        result = resolve_assigned_to(filters, "myaccount")
        assert "IOT-陈斌" in result
        assert "陈斌" in result
        assert "myaccount" in result

    def test_no_known_falls_back_to_val(self):
        result = resolve_assigned_to({"assigned_to": ["IOT-陈斌"]})
        assert "IOT-陈斌" in result
        assert "陈斌" in result  # no conflict if only one


class TestExtractDepartmentPrefix:
    """extract_department_prefix 部门前缀提取"""

    def test_iot_prefix(self):
        assert extract_department_prefix("IOT-陈斌") == "IOT"

    def test_application_prefix(self):
        assert extract_department_prefix("应用-罗林旺") == "应用"

    def test_algorithm_prefix(self):
        assert extract_department_prefix("算法-乐钦杰") == "算法"

    def test_no_hyphen_returns_empty(self):
        assert extract_department_prefix("张三") == ""

    def test_empty_returns_empty(self):
        assert extract_department_prefix("") == ""

    def test_none_returns_empty(self):
        assert extract_department_prefix(None) == ""


class TestNormalizeZentaoFilters:
    """normalize_zentao_filters 筛选条件归一化"""

    def test_product_string_to_int(self):
        filters = {"product": "381"}
        normalize_zentao_filters(filters)
        assert filters["product_id"] == 381

    def test_product_int_preserved(self):
        filters = {"product": 381}
        normalize_zentao_filters(filters)
        assert filters["product_id"] == 381

    def test_product_id_already_exists(self):
        filters = {"product": "111", "product_id": 999}
        normalize_zentao_filters(filters)
        assert filters["product_id"] == 999  # not overwritten

    def test_project_string_to_int(self):
        filters = {"project": "123"}
        normalize_zentao_filters(filters)
        assert filters["project_id"] == 123

    def test_non_digit_product_skipped(self):
        filters = {"product": "HS4"}
        normalize_zentao_filters(filters)
        assert "product_id" not in filters

    def test_empty_filters(self):
        filters = {}
        normalize_zentao_filters(filters)
        assert filters == {}


class TestParseZentaoUrl:
    """parse_zentao_url URL 解析"""

    def test_standard_bug_url(self):
        url = "https://zentao.example.com/zentao/bug-browse-11-0--0--20-1.html"
        result = parse_zentao_url(url)
        assert result["product_id"] == 11  # returns int

    def test_url_with_module(self):
        url = "https://zentao.example.com/bug-browse-78-491-byModule-122.html"
        result = parse_zentao_url(url)
        assert result["product_id"] == 78
        assert result["module_id"] == 122

    def test_non_zentao_url(self):
        result = parse_zentao_url("https://google.com")
        assert result == {} or result.get("product_id") is None

    def test_empty_url(self):
        result = parse_zentao_url("")
        assert result == {} or result.get("product_id") is None


class TestModels:
    """测试数据模型"""

    def test_zentao_bug_defaults(self):
        from src.models import ZentaoBug
        bug = ZentaoBug(
            id=1, title="test", severity="1", pri="1", type="bug",
            status="active", steps="", assignedTo="", assignedToAccount="",
            openedBy="", openedByAccount="", openedDate="",
            product="1", productName="", project="", projectName="",
            module="", moduleName="", openedBuild="", snCode="", files=[],
        )
        assert bug.id == 1
        assert bug.title == "test"

    def test_attachment_file(self):
        from src.models import AttachmentFile
        af = AttachmentFile(
            filename="test.png",
            content_type="image/png",
            data=b"fake_data",
            size=9,
        )
        assert af.filename == "test.png"
        assert af.size == 9

    def test_sync_result_created(self):
        from src.models import SyncResult, SyncAction
        sr = SyncResult(1, SyncAction.CREATED, "task_1", "创建成功")
        assert sr.action == SyncAction.CREATED
        assert "创建成功" in str(sr)

    def test_sync_result_skipped(self):
        from src.models import SyncResult, SyncAction
        sr = SyncResult(2, SyncAction.SKIPPED_FILTERED, "", "过滤跳过")
        assert sr.action == SyncAction.SKIPPED_FILTERED
