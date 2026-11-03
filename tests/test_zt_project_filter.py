"""禅道项目/执行ID过滤：产品接口漏掉挂项目的缺陷时回退执行接口"""
from unittest.mock import MagicMock

import pytest

from src.zentao_client import ZentaoAPIError, ZentaoClient


class TestFetchBugsExecutionFallback:
    def _client(self):
        c = ZentaoClient("http://10.10.3.67:8080", "u", "p")
        c._cloud_session_auth = False
        return c

    def test_project_endpoint_fallback_to_execution(self):
        c = self._client()
        c._request = MagicMock(side_effect=[
            ZentaoAPIError(400, '{"error":"error"}', "/projects/649/bugs"),
            {"bugs": [{"id": 65579, "product": 413, "title": "t",
                       "status": "active"}], "total": 1},
        ])
        bugs, total = c.fetch_bugs(product_id=413, project_id=649,
                                   statuses=["active"])
        assert total == 1 and [b.id for b in bugs] == [65579]
        second = c._request.call_args_list[1]
        assert second.args[1] == "/api.php/v1/executions/649/bugs"
        assert second.kwargs["params"].get("product") == 413

    def test_other_errors_not_swallowed(self):
        c = self._client()
        c._request = MagicMock(side_effect=ZentaoAPIError(500, "boom", "/x"))
        with pytest.raises(ZentaoAPIError):
            c.fetch_bugs(product_id=413, project_id=649)

    def test_product_endpoint_untouched(self):
        c = self._client()
        c._request = MagicMock(return_value={"bugs": [], "total": 0})
        c.fetch_bugs(product_id=413)
        assert c._request.call_args.args[1] == "/api.php/v1/products/413/bugs"


class TestProjectFilterApply:
    def _mw(self):
        from gui.main_window import MainWindow
        mw = MagicMock()
        mw.config = {}
        mw._vpn_config = {}
        mw.filter_platform.currentIndex.return_value = 0
        mw.edit_zentao_base_url.text.return_value = "http://x"
        mw.edit_zentao_account.text.return_value = "u"
        mw.edit_zentao_password.text.return_value = "p"
        mw.filter_product.text.return_value = "413,419"
        mw.filter_module.text.return_value = ""
        mw.filter_status.currentText.return_value = "激活"
        mw._status_code_map = {"激活": ["active"]}
        mw.filter_date_mode.currentIndex.return_value = 0
        mw.filter_assigned = MagicMock()
        mw.filter_assigned.count.return_value = 0
        mw._collect_assignee_selection = MagicMock()
        mw._apply_filters_to_config = (
            lambda: MainWindow._apply_filters_to_config(mw))
        return mw

    def test_single_project(self):
        mw = self._mw()
        mw.filter_zt_project.text.return_value = "649"
        mw._apply_filters_to_config()
        assert mw.config["zentao"]["filters"]["project"] == 649

    def test_multiple_projects(self):
        mw = self._mw()
        mw.filter_zt_project.text.return_value = "649, 650"
        mw._apply_filters_to_config()
        assert mw.config["zentao"]["filters"]["project"] == [649, 650]

    def test_empty_project(self):
        mw = self._mw()
        mw.filter_zt_project.text.return_value = ""
        mw._apply_filters_to_config()
        assert mw.config["zentao"]["filters"]["project"] is None
        assert "project_ids" not in mw.config["zentao"]["filters"]
