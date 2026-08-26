"""测试模块递归过滤、_probe_clean_url 修复、钉钉 0 条不推送、文件真实名探测"""
import pytest
from unittest.mock import Mock, patch, MagicMock

from src.utils import apply_module_filter
from src.zentao_client import ZentaoClient
from src.sync_engine import SyncEngine
from src.models import SyncStats, ZentaoBug


def make_client():
    client = ZentaoClient.__new__(ZentaoClient)
    client._token = "tok"
    client._cloud_session_auth = False
    client._clean_url = None
    client.base_url = "http://test.local"
    client.account = "testuser"
    client.password = "pass"
    client.api_delay = 0
    client._http = Mock()
    client._session_logged_in = False
    client._session_id = ""
    client._branch_id = 0
    client._product_modules_cache = {}
    client._product_modules_cache_lock = MagicMock()
    return client


class TestProbeCleanUrl:
    """_probe_clean_url 修复：动态路径返回200但非JSON时判定为 clean"""

    def test_dynamic_returns_200_json_means_dynamic(self):
        client = make_client()
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"status": "success"}
        client._http.get.return_value = resp
        assert client._probe_clean_url() is False

    def test_dynamic_returns_200_html_means_clean(self):
        """关键修复：动态路径返回200但内容是登录重定向HTML → clean"""
        client = make_client()
        resp = Mock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("Expecting value")
        client._http.get.return_value = resp
        assert client._probe_clean_url() is True

    def test_dynamic_returns_404_means_clean(self):
        client = make_client()
        resp = Mock()
        resp.status_code = 404
        client._http.get.return_value = resp
        assert client._probe_clean_url() is True

    def test_request_exception_means_dynamic(self):
        client = make_client()
        client._http.get.side_effect = Exception("timeout")
        assert client._probe_clean_url() is False


class TestModuleTreeFromBrowse:
    """从浏览页 JSON 的 modules 字段构建完整模块树"""

    def _browse_resp(self):
        inner = {
            "modules": {
                "0": "/",
                "122": "/HS341",
                "123": "/HS341/乐动方案",
                "136": "/HS341/乐动方案/软件测试",
                "137": "/HS341/乐动方案/硬件测试",
                "158": "/HS341/乐动方案/PLM问题",
                "159": "/HS341/乐动方案/需求反馈",
            }
        }
        import json
        data = {"status": "success",
                "data": json.dumps(inner, ensure_ascii=False)}
        resp = Mock()
        resp.status_code = 200
        resp.text = json.dumps(data, ensure_ascii=False)
        return resp

    def test_build_tree_with_hierarchy(self):
        client = make_client()
        client._session_logged_in = True
        client._http.get.return_value = self._browse_resp()
        tree = client._fetch_module_tree_from_browse(11, 0)
        assert tree is not None
        assert tree["123"]["parent"] == "122"
        assert tree["136"]["parent"] == "123"
        assert tree["158"]["parent"] == "123"
        assert tree["123"]["name"] == "乐动方案"
        assert tree["136"]["name"] == "软件测试"

    def test_build_tree_html_unescape(self):
        client = make_client()
        client._session_logged_in = True
        client._http.get.return_value = self._browse_resp()
        tree = client._fetch_module_tree_from_browse(11, 0)
        assert tree["123"]["path"] == "/HS341/乐动方案"

    def test_build_tree_failure_returns_none(self):
        client = make_client()
        resp = Mock()
        resp.status_code = 200
        resp.text = "<html>login page</html>"
        client._http.get.return_value = resp
        assert client._fetch_module_tree_from_browse(11, 0) is None


class TestResolveModuleDescendantIds:
    """模块ID + 全部后代集合"""

    def test_descendants(self):
        client = make_client()
        client.fetch_module_tree = Mock(return_value={
            "122": {"name": "HS341", "parent": "0"},
            "123": {"name": "乐动方案", "parent": "122"},
            "136": {"name": "软件测试", "parent": "123"},
            "137": {"name": "硬件测试", "parent": "123"},
            "158": {"name": "PLM问题", "parent": "123"},
        })
        result = client.resolve_module_descendant_ids(11, "123")
        assert result == {"123", "136", "137", "158"}

    def test_leaf_module_returns_self(self):
        client = make_client()
        client.fetch_module_tree = Mock(return_value={
            "122": {"name": "HS341", "parent": "0"},
            "123": {"name": "乐动方案", "parent": "122"},
        })
        assert client.resolve_module_descendant_ids(11, "123") == {"123"}

    def test_module_not_in_tree_returns_none(self):
        client = make_client()
        client.fetch_module_tree = Mock(return_value={
            "122": {"name": "HS341", "parent": "0"},
        })
        assert client.resolve_module_descendant_ids(11, "999") is None

    def test_tree_unavailable_returns_none(self):
        client = make_client()
        client.fetch_module_tree = Mock(return_value=None)
        assert client.resolve_module_descendant_ids(11, "123") is None


class TestSyncEngineModuleFilter:
    """sync_engine.run() 数字模块ID递归过滤"""

    def _make_engine(self, bugs, source):
        engine = SyncEngine.__new__(SyncEngine)
        engine.module_filter = "123"
        engine.sync_closed_status = ""
        engine.source = source
        engine.config = {"assignee": {}}
        engine.severity_labels = {}
        engine._module_id_set = None
        engine.dingtalk_bot = None
        engine.dry_run = False
        engine.batch_size = 100
        engine.ai_analysis_enabled = False
        engine.sync_attachments = False
        return engine

    def _bug(self, bid, module):
        return ZentaoBug(id=bid, title=f"bug{bid}", module=str(module))

    def test_digit_filter_includes_descendants(self):
        source = Mock()
        source.source_type = "zentao"
        source.account = "me"
        source.resolve_module_descendant_ids.return_value = {"123", "136", "158"}
        source.fetch_all_bugs.return_value = [
            self._bug(1, "123"), self._bug(2, "136"),
            self._bug(3, "158"), self._bug(4, "137"),
            self._bug(5, "999"),
        ]
        engine = self._make_engine(None, source)
        stats = SyncStats()
        bugs = source.fetch_all_bugs()
        from src.utils import resolve_assigned_to
        # 模拟 run() 中数字ID递归分支
        desc_set = source.resolve_module_descendant_ids(11, "123")
        filtered = [b for b in bugs if str(b.module) in desc_set]
        assert [b.id for b in filtered] == [1, 2, 3]

    def test_digit_filter_fallback_exact_when_tree_unavailable(self):
        source = Mock()
        source.source_type = "zentao"
        source.account = "me"
        source.resolve_module_descendant_ids.return_value = None
        bugs = [self._bug(1, "123"), self._bug(2, "136")]
        filtered = [b for b in bugs if str(b.module) == "123"]
        assert [b.id for b in filtered] == [1]


class TestFetchFileName:
    """探测文件真实文件名（files 字段缺失时的回退）"""

    def make_client(self):
        client = make_client()
        client._file_name_cache = {}
        client._file_name_cache_lock = MagicMock()
        return client

    def _resp(self, cd=""):
        resp = Mock()
        resp.status_code = 200
        resp.headers = {"Content-Disposition": cd}
        resp.close = Mock()
        return resp

    def test_returns_real_name_from_content_disposition(self):
        client = self.make_client()
        client._http.get.return_value = self._resp(
            'attachment; filename="241540550803a09.png"')
        assert client.fetch_file_name(17629) == "241540550803a09.png"

    def test_returns_empty_when_no_cd(self):
        client = self.make_client()
        client._http.get.return_value = self._resp("")
        assert client.fetch_file_name(17629) == ""

    def test_cached_second_call_no_request(self):
        client = self.make_client()
        client._http.get.return_value = self._resp(
            'attachment; filename="a.png"')
        assert client.fetch_file_name(17629) == "a.png"
        client.fetch_file_name(17629)
        assert client._http.get.call_count == 1


class TestBuildNoteFileNameProbe:
    """_build_note 对 steps 内联图片补全真实文件名"""

    def _make_engine(self, source):
        engine = SyncEngine.__new__(SyncEngine)
        engine.source = source
        engine.source_type = "zentao"
        engine.config = {}
        engine.project_name = "测试项目"
        engine._map_severity = Mock(return_value="A")
        engine.severity_labels = {}
        return engine

    def test_note_placeholder_uses_probed_name(self):
        source = Mock()
        source.fetch_file_name = Mock(return_value="241540550803a09.png")
        engine = self._make_engine(source)
        bug = ZentaoBug(
            id=8207,
            title="测试",
            steps='<img src="https://zentao/zentao/file-read-17629.png" alt="" />',
            files=[],
        )
        note = engine._build_note(bug)
        assert "image_17629.png" not in note
        assert "[图片: 241540550803a09.png]" in note
        source.fetch_file_name.assert_called_once_with("17629")

    def test_note_keeps_real_name_from_files(self):
        source = Mock()
        engine = self._make_engine(source)
        bug = ZentaoBug(
            id=1,
            title="t",
            steps='<img src="file-read-100.png" />',
            files=[{"id": 100, "title": "原图.png"}],
        )
        note = engine._build_note(bug)
        assert "[图片: 原图.png]" in note
        # files 已有映射时不探测
        source.fetch_file_name.assert_not_called()

    def test_probe_failure_falls_back_to_image_id(self):
        source = Mock()
        source.fetch_file_name = Mock(return_value="")
        engine = self._make_engine(source)
        bug = ZentaoBug(
            id=1, title="t",
            steps='<img src="file-read-17629.png" />',
            files=[],
        )
        note = engine._build_note(bug)
        assert "[图片: image_17629.png]" in note


class TestApplyModuleFilterDigitWithSet:
    """数字模块ID + 预解析后代集合时，必须用集合过滤（递归语义）"""

    def _bug(self, bid, module):
        return ZentaoBug(id=bid, title=f"bug{bid}", module=str(module))

    def test_digit_uses_descendant_set(self):
        """v2.7.2 回归：数字快路径此前忽略 module_id_set，只精确匹配"""
        bugs = [self._bug(1, "123"), self._bug(2, "136"),
                self._bug(3, "158"), self._bug(4, "999")]
        result = apply_module_filter(bugs, "123",
                                     module_id_set={"123", "136", "158"})
        assert [b.id for b in result] == [1, 2, 3]

    def test_digit_without_set_exact_match(self):
        bugs = [self._bug(1, "123"), self._bug(2, "136")]
        result = apply_module_filter(bugs, "123")
        assert [b.id for b in result] == [1]

    def test_digit_empty_set_returns_none(self):
        bugs = [self._bug(1, "123")]
        result = apply_module_filter(bugs, "123", module_id_set=set())
        assert result == []

    def test_name_uses_set(self):
        bugs = [self._bug(1, "123"), self._bug(2, "136")]
        result = apply_module_filter(bugs, "乐动方案",
                                     module_id_set={"123", "136"})
        assert [b.id for b in result] == [1, 2]


class TestCloseSyncModuleFilter:
    """关闭同步的数字模块ID也走递归后代集合"""

    def _make_engine(self, source, teambition):
        engine = SyncEngine.__new__(SyncEngine)
        engine.sync_closed_status = "closed"
        engine.source_type = "zentao"
        engine.module_filter = "123"
        engine.source = source
        engine.teambition = teambition
        engine.config = {"zentao": {"filters": {"product": 11}}}
        engine.dingtalk_bot = None
        return engine

    def test_close_sync_digit_uses_descendant_set(self):
        source = Mock()
        source.source_type = "zentao"
        source.resolve_module_descendant_ids = Mock(return_value={"123", "136"})
        b1 = ZentaoBug(id=1, title="【VLNS-100】a", module="136")
        b2 = ZentaoBug(id=2, title="【VLNS-101】b", module="999")
        source.fetch_all_bugs.return_value = [b1, b2]
        teambition = Mock()
        teambition.get_task_by_identifier.return_value = None
        teambition.search_tasks.return_value = []
        engine = self._make_engine(source, teambition)
        stats = SyncStats()

        engine._run_close_sync_phase(stats, False)

        # 走递归解析而非精确匹配
        source.resolve_module_descendant_ids.assert_called_once_with(11, "123")
        # 模块999的bug被递归集合滤掉，只处理模块136那条
        assert teambition.get_task_by_identifier.call_count == 1

    def test_close_sync_fallback_exact_when_tree_unavailable(self):
        source = Mock()
        source.source_type = "zentao"
        source.resolve_module_descendant_ids = Mock(return_value=None)
        b1 = ZentaoBug(id=1, title="【VLNS-100】a", module="123")
        b2 = ZentaoBug(id=2, title="【VLNS-101】b", module="136")
        source.fetch_all_bugs.return_value = [b1, b2]
        teambition = Mock()
        teambition.get_task_by_identifier.return_value = None
        teambition.search_tasks.return_value = []
        engine = self._make_engine(source, teambition)
        stats = SyncStats()

        engine._run_close_sync_phase(stats, False)

        # 树不可用时回退精确匹配：只处理模块123那条
        assert teambition.get_task_by_identifier.call_count == 1


class TestLearnSnPatternsKeepsDefaults:
    """learn_sn_patterns 学到前缀后必须保留默认模板模式（H4）"""

    def test_learned_patterns_keep_template_default(self):
        from src.extractor import learn_sn_patterns, extract_sn, DEFAULT_SN_PATTERNS
        learned = learn_sn_patterns(["HQABC123456789", "HQXYZ987654321"])
        # 学到的前缀 + 默认模板模式都应保留
        assert any("HQ" in p for p in learned)
        assert any("SN" in p for p in learned)
        # 模板格式 SN 仍能提取（修复前返回 None）
        assert extract_sn("SN码：48HCNFBN0049X 其他", learned) == "48HCNFBN0049X"


class TestDingTalkZeroBugs:
    """缺陷数量为 0 时不推送钉钉"""

    def test_no_push_when_total_zero(self):
        engine = SyncEngine.__new__(SyncEngine)
        engine.dingtalk_bot = Mock()
        engine.sync_closed_status = ""
        engine.source_type = "zentao"
        engine.project_name = "项目"
        stats = SyncStats()
        stats.total = 0
        stats.closed_synced = 0
        elapsed = 1.0
        # 复刻 run() 末尾的发送条件
        should_send = (
            engine.dingtalk_bot
            and (stats.total > 0 or stats.closed_synced > 0)
            and (not engine.sync_closed_status
                 or stats.created > 0 or stats.reactivated > 0)
        )
        assert should_send is False
        engine.dingtalk_bot.send_sync_result.assert_not_called()

    def test_push_when_total_positive(self):
        engine = SyncEngine.__new__(SyncEngine)
        engine.dingtalk_bot = Mock()
        engine.sync_closed_status = ""
        engine.source_type = "zentao"
        engine.project_name = "项目"
        stats = SyncStats()
        stats.total = 5
        stats.closed_synced = 0
        should_send = (
            engine.dingtalk_bot
            and (stats.total > 0 or stats.closed_synced > 0)
            and (not engine.sync_closed_status
                 or stats.created > 0 or stats.reactivated > 0)
        )
        assert should_send is True

    def test_push_when_closed_synced_positive(self):
        """主同步0条但关闭同步拉到已关闭缺陷时仍推送"""
        engine = SyncEngine.__new__(SyncEngine)
        engine.dingtalk_bot = Mock()
        engine.sync_closed_status = ""
        engine.source_type = "zentao"
        engine.project_name = "项目"
        stats = SyncStats()
        stats.total = 0
        stats.closed_synced = 3
        should_send = (
            engine.dingtalk_bot
            and (stats.total > 0 or stats.closed_synced > 0)
            and (not engine.sync_closed_status
                 or stats.created > 0 or stats.reactivated > 0)
        )
        assert should_send is True
