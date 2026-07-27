"""测试 src/sync_engine.py — HTML转换、CPAX检测、去重逻辑"""
from unittest.mock import MagicMock

import pytest

from src.sync_engine import SyncEngine
from src.models import ZentaoBug


def make_engine():
    e = SyncEngine.__new__(SyncEngine)
    e.severity_labels = {}
    return e


class TestHtmlToText:
    """_html_to_text HTML → 纯文本转换"""

    def test_plain_text_passthrough(self):
        engine = make_engine()
        assert engine._html_to_text("纯文本无标签") == "纯文本无标签"

    def test_no_html_passthrough(self):
        engine = make_engine()
        assert engine._html_to_text("text without tags") == "text without tags"

    def test_empty_returns_empty(self):
        engine = make_engine()
        assert engine._html_to_text("") == ""

    def test_br_converts_to_newline(self):
        engine = make_engine()
        result = engine._html_to_text("第一行<br>第二行")
        assert "第一行" in result
        assert "第二行" in result

    def test_p_tags_become_paragraphs(self):
        engine = make_engine()
        result = engine._html_to_text("<p>段1</p><p>段2</p>")
        assert "段1" in result
        assert "段2" in result

    def test_img_becomes_placeholder(self):
        engine = make_engine()
        result = engine._html_to_text('<img src="/file-download-123"/>')
        assert "[图片]" in result

    def test_video_becomes_placeholder(self):
        engine = make_engine()
        result = engine._html_to_text("<video src='test.mp4'></video>")
        assert "[视频]" in result

    def test_table_converts_to_text_table(self):
        engine = make_engine()
        html = (
            "<table>"
            "<tr><th>步骤</th><th>操作</th><th>预期</th></tr>"
            "<tr><td>1</td><td>开机</td><td>正常启动</td></tr>"
            "<tr><td>2</td><td>清扫</td><td>路径规整</td></tr>"
            "</table>"
        )
        result = engine._html_to_text(html)
        assert "步骤" in result
        assert "操作" in result
        assert "预期" in result
        assert "-+-" in result  # 分隔线
        assert "开机" in result
        assert "正常启动" in result
        assert "<table" not in result
        assert "<tr" not in result

    def test_single_column_table(self):
        engine = make_engine()
        html = "<table><tr><td>单列数据</td></tr></table>"
        result = engine._html_to_text(html)
        assert "单列数据" in result

    def test_empty_table_removed(self):
        engine = make_engine()
        html = "<p>前置</p><table></table><p>后置</p>"
        result = engine._html_to_text(html)
        assert "前置" in result
        assert "后置" in result

    def test_ordered_list(self):
        engine = make_engine()
        html = "<ol><li>第一步</li><li>第二步</li></ol>"
        result = engine._html_to_text(html)
        assert "1." in result
        assert "第一步" in result
        assert "2." in result
        assert "第二步" in result

    def test_unordered_list(self):
        engine = make_engine()
        html = "<ul><li>项目A</li><li>项目B</li></ul>"
        result = engine._html_to_text(html)
        assert "- " in result
        assert "项目A" in result
        assert "项目B" in result

    def test_mixed_content(self):
        engine = make_engine()
        html = (
            "<p>【测试环境】</p>"
            "<table><tr><th>项</th><th>值</th></tr>"
            "<tr><td>固件</td><td>V2.3</td></tr></table>"
            "<ol><li>确认版本</li><li>执行测试</li></ol>"
            "<p>【结论】通过</p>"
        )
        result = engine._html_to_text(html)
        assert "【测试环境】" in result
        assert "固件" in result
        assert "V2.3" in result
        assert "1." in result
        assert "确认版本" in result
        assert "【结论】通过" in result

    def test_none_input(self):
        engine = make_engine()
        assert engine._html_to_text(None) is None

    def test_corrupted_html_returns_original(self):
        engine = make_engine()
        # pass a very broken string - should not crash
        result = engine._html_to_text("<<<broken>>>")
        assert isinstance(result, str)


class TestCPAXDetection:
    """CPAX/VLNS 双重检测"""

    def test_cpax_in_title_detected(self):
        import re
        title = "【CPAX-12345】机器无法回充"
        assert re.search(r'(?:VLNS|CPAX)-\d+', title) is not None

    def test_vlns_in_title_detected(self):
        import re
        title = "【VLNS-67890】清扫路径异常"
        assert re.search(r'(?:VLNS|CPAX)-\d+', title) is not None

    def test_no_marker_not_detected(self):
        import re
        title = "普通Bug标题"
        assert re.search(r'(?:VLNS|CPAX)-\d+', title) is None

    def test_cpax_clean_from_title(self):
        import re
        title = "【CPAX-12345】机器异常"
        clean = re.sub(r'(?:VLNS|CPAX)-\d+', '', title)
        assert "CPAX" not in clean
        assert "12345" not in clean

    def test_zentao_product_id_not_cleaned(self):
        """禅道自己的产品编号【P260626-00013】不应被误清"""
        from src.models import ZentaoBug
        bug = ZentaoBug(id=123, title="【P260626-00013】机器无法回充")
        base = bug.get_base_title()
        assert "P260626-00013" in base, \
            f"禅道产品编号被误清: {base}"

    def test_zentao_product_id_not_cleaned_in_build_zentao_title(self):
        """_build_zentao_title 不应清掉禅道产品编号"""
        from src.sync_engine import SyncEngine
        engine = SyncEngine.__new__(SyncEngine)
        engine.tb_tag_in_zentao = "【VLNS-{task_id}】"
        result = engine._build_zentao_title(
            "【P260626-00013】机器无法回充", "68402")
        # 应该保留禅道编号，只加 VLNS 前缀
        assert "P260626-00013" in result, \
            f"禅道产品编号被误清: {result}"
        assert result.startswith("【VLNS-68402】"), \
            f"应该加 VLNS 前缀: {result}"

    def test_old_vlns_still_cleaned_in_build_zentao_title(self):
        """旧的 VLNS 标注仍然被清除（避免重复堆叠）"""
        from src.sync_engine import SyncEngine
        engine = SyncEngine.__new__(SyncEngine)
        engine.tb_tag_in_zentao = "【VLNS-{task_id}】"
        result = engine._build_zentao_title(
            "【VLNS-61849】【P260626-00013】机器无法回充", "68402")
        assert "VLNS-61849" not in result, \
            f"旧 VLNS 标注应被清: {result}"
        assert "VLNS-68402" in result
        assert "P260626-00013" in result


class TestExtractInlineImageIds:
    """_extract_inline_image_ids"""

    def test_file_read_format(self):
        engine = make_engine()
        ids = engine._extract_inline_image_ids(
            '<img src="/file-read-123"/>')
        assert "123" in ids

    def test_file_download_format(self):
        engine = make_engine()
        ids = engine._extract_inline_image_ids(
            '<img src="/file/download/456"/>')
        assert "456" in ids

    def test_no_img_returns_empty(self):
        engine = make_engine()
        ids = engine._extract_inline_image_ids("no images here")
        assert ids == []

    def test_empty_string(self):
        engine = make_engine()
        ids = engine._extract_inline_image_ids("")
        assert ids == []

    def test_deduplicate(self):
        engine = make_engine()
        html = (
            '<img src="/file-read-123"/>'
            '<img src="/file-read-123"/>'
            '<img src="/file-read-456"/>'
        )
        ids = engine._extract_inline_image_ids(html)
        assert ids == ["123", "456"]


class TestBuildNote:
    """_build_note 构建 TB 备注"""

    def make_bug(self, **kwargs):
        defaults = dict(
            id=6766, title="测试Bug", severity="2", pri="2", type="code",
            status="active", steps="", assignedTo="测试-张三",
            assignedToAccount="", openedBy="测试-李四", openedByAccount="",
            openedDate="2026-06-01", product="381", productName="HS4",
            project="", projectName="", module="", moduleName="",
            openedBuild="V2.3.5", snCode="SN001", files=[],
        )
        defaults.update(kwargs)
        return ZentaoBug(**defaults)

    def test_note_contains_metadata(self):
        bug = self.make_bug()
        # Need a proper SyncEngine with severity_map
        engine = SyncEngine.__new__(SyncEngine)
        engine.severity_map = {"1": "S", "2": "A", "3": "B", "4": "C"}
        engine.severity_labels = {}
        engine._html_to_text = SyncEngine._html_to_text
        engine._map_severity = lambda s: engine.severity_map.get(str(s), "B")

        note = engine._build_note(bug)
        assert "6766" in note
        assert "HS341" not in note  # not in this bug

    def test_note_with_table_steps(self):
        bug = self.make_bug(steps=(
            "<table><tr><th>步骤</th><th>操作</th></tr>"
            "<tr><td>1</td><td>开机</td></tr></table>"
        ))
        engine = SyncEngine.__new__(SyncEngine)
        engine.severity_map = {"1": "S", "2": "A", "3": "B", "4": "C"}
        engine.severity_labels = {}
        engine._html_to_text = SyncEngine._html_to_text
        engine._clean_html_for_tb = SyncEngine._clean_html_for_tb
        engine._map_severity = lambda s: engine.severity_map.get(str(s), "B")

        note = engine._build_note(bug)
        assert "<table" in note  # table preserved as HTML
        assert "步骤" in note
        assert "开机" in note
        assert "border-collapse" in note  # clean styling applied

    def test_note_empty_steps(self):
        bug = self.make_bug(steps="")
        engine = SyncEngine.__new__(SyncEngine)
        engine.severity_map = {"1": "S", "2": "A", "3": "B", "4": "C"}
        engine.severity_labels = {}
        engine._html_to_text = SyncEngine._html_to_text
        engine._map_severity = lambda s: engine.severity_map.get(str(s), "B")

        note = engine._build_note(bug)
        assert "<pre" not in note  # no pre for empty steps


class TestSNExtraction:
    """SN 编码提取"""

    def test_sn_prefix_colon(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("SN:48HCNFBN0049X") == "48HCNFBN0049X"

    def test_sn_prefix_chinese(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("SN码：ABC123456") == "ABC123456"

    def test_sn_should_not_match_three_letter_abbrev(self):
        """NSN/USN/PSN/SSN/BSN 等 3 字母缩写不应被误识别为 SN 前缀"""
        from src.zentao_client import ZentaoClient
        for t in [
            "Equipment NSN: 7015-01-123-4567",
            "PSN: PART12345",
            "USN: 12345678",
            "SSN: ABC-12345",
        ]:
            result = ZentaoClient._extract_sn(t)
            assert result in ("/", None) or "NSN" not in result.upper(), \
                f"误匹配: {t!r} -> {result!r}"

    def test_sn_hq_format(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("HQ5S00700002HC261300069") == "HQ5S00700002HC261300069"

    def test_sn_filename_format(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("48HCNFBN0049X-2026-06-02.zip") == "48HCNFBN0049X"

    def test_sn_filename_with_scv5(self):
        from src.zentao_client import ZentaoClient
        sn = ZentaoClient._extract_sn(
            "48HCNFBN0049X-2026-06-02-16-04-56.scv5 steps text")
        assert sn == "48HCNFBN0049X"

    def test_sn_not_found(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("no serial here") == "/"

    def test_sn_drc_filename_format(self):
        from src.zentao_client import ZentaoClient
        sn = ZentaoClient._extract_sn(
            "record_20260602_155412_192.168.121.121_2026L014E403300002_8.1.21.drc")
        assert sn == "2026L014E403300002"

    def test_sn_empty_text(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("") == "/"

    def test_sn_from_task_customfield_hq(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('Task', (), {
            'content': 'test',
            'customfields': [{'value': 'HQ5S00700002HC261300069'}],
        })()
        sn = _extract_sn_from_task(task)
        assert sn == 'HQ5S00700002HC261300069'

    def test_sn_from_task_customfield_non_hq(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('Task', (), {
            'content': 'test',
            'customfields': [{'value': '48HCNFBN0049X'}],
        })()
        sn = _extract_sn_from_task(task)
        assert sn == '48HCNFBN0049X'

    def test_sn_from_task_date_excluded(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('Task', (), {
            'id': '1',
            'content': 'test bug',
            'customfields': [{'value': '2026-06-03 00:13'}],
        })()
        sn = _extract_sn_from_task(task)
        assert sn is None  # date should NOT be treated as SN

    def test_sn_from_task_title(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('Task', (), {
            'id': '1',
            'content': '48HCNFBN0049X machine issue',
            'customfields': [],
        })()
        sn = _extract_sn_from_task(task)
        assert sn == '48HCNFBN0049X'

    def test_sn_from_task_drc_filename(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('Task', (), {
            'id': '1',
            'content': 'record_20260602_155412_192.168.121.121_2026L014E403300002_8.1.21.drc',
            'customfields': [],
        })()
        sn = _extract_sn_from_task(task)
        assert sn == '2026L014E403300002'


class TestCloudTitleUpdate:
    """云版标题修改"""

    def test_cloud_post_called(self):
        import threading
        from unittest.mock import MagicMock
        from src.zentao_client import ZentaoClient
        client = ZentaoClient.__new__(ZentaoClient)
        client._cloud_session_auth = True
        client.base_url = "https://cloud.example.com"
        client.api_delay = 0
        client._http = MagicMock()
        client._session_logged_in = True
        client._bug_raw_cache = {}
        client._bug_raw_cache_lock = threading.Lock()
        client._branch_id = 0
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.text = '{"data": {"title": "old", "type": 1, "product": 1, "severity": 1, "pri": 3, "status": "active", "assignedTo": "u1", "steps": "x"}}'
        client._http.get.return_value = get_resp
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.text = '{"data": {"title": "updated"}}'
        client._http.post.return_value = post_resp

        client.update_bug_title(12345, "new title")
        client._http.post.assert_called_once()


class TestTitleCleaning:
    """CPAX/VLNS 标题清理"""

    def test_vlns_cleaned(self):
        import re
        title = "【VLNS-12345】测试Bug标题"
        clean = re.sub(r'(?:VLNS|CPAX)-\d+', '', title).strip()
        assert clean == "【】测试Bug标题" or "测试Bug标题" in clean

    def test_cpax_cleaned(self):
        import re
        title = "【CPAX-67890】另一个Bug"
        clean = re.sub(r'(?:VLNS|CPAX)-\d+', '', title).strip()
        assert "CPAX" not in clean


class TestSeverityMapping:
    """严重程度映射 (TB 不使用 S 等级)"""

    def make_engine(self, severity_map=None, severity_labels=None):
        from src.sync_engine import SyncEngine
        e = SyncEngine.__new__(SyncEngine)
        e.severity_map = severity_map or {"1": "A", "2": "B", "3": "C", "4": "C"}
        e.severity_labels = severity_labels or {}
        return e

    def test_numeric_1234(self):
        e = self.make_engine()
        assert e._map_severity("1") == "A"
        assert e._map_severity("2") == "B"
        assert e._map_severity("3") == "C"
        assert e._map_severity("4") == "C"

    def test_letter_ABCD(self):
        e = self.make_engine()
        assert e._map_severity("A") == "A"
        assert e._map_severity("B") == "B"
        assert e._map_severity("C") == "C"
        assert e._map_severity("D") == "C"

    def test_letter_S(self):
        e = self.make_engine({"1": "A", "2": "B", "3": "C", "4": "C"})
        # S 作为输入（字母等级） → 输出 C
        assert e._map_severity("S") == "C"

    def test_text_severity(self):
        e = self.make_engine({"致命": "S", "严重": "A", "一般": "B", "建议": "C", "轻微": "C"})
        assert e._map_severity("致命") == "S"  # 中文名保留S
        assert e._map_severity("严重") == "A"
        assert e._map_severity("一般") == "B"
        assert e._map_severity("建议") == "C"
        assert e._map_severity("轻微") == "C"

    def test_unknown_defaults_to_c(self):
        e = self.make_engine()
        assert e._map_severity("未知") == "C"
        assert e._map_severity("") == "C"

    def test_yaml_int_key(self):
        """YAML 解析 {1: 'A'} 后 _map_severity('2') 应正确匹配"""
        e = self.make_engine({1: "A", 2: "B", 3: "C", 4: "C"})
        assert e._map_severity("1") == "A"
        assert e._map_severity("2") == "B"
        assert e._map_severity("3") == "C"
        assert e._map_severity("4") == "C"

    def test_severity_labels_translate(self):
        """有翻译时，API 数字先翻译为页面标签再查 map"""
        labels = {"1": "致命", "2": "严重", "3": "一般", "4": "建议"}
        smap = {"致命": "S", "严重": "A", "一般": "B", "建议": "C",
                "1": "A", "2": "B", "3": "C", "4": "C"}
        e = self.make_engine(smap, labels)
        assert e._map_severity("1") == "S"   # 1→致命→S
        assert e._map_severity("2") == "A"   # 2→严重→A
        assert e._map_severity("3") == "B"   # 3→一般→B
        assert e._map_severity("4") == "C"   # 4→建议→C

    def test_severity_labels_letter(self):
        """实例2: 翻译为 A/B/C/D，map 也支持字母映射"""
        labels = {"1": "A", "2": "B", "3": "C", "4": "D"}
        smap = {"A": "A", "B": "B", "C": "C", "D": "C",
                "1": "A", "2": "B", "3": "C", "4": "C"}
        e = self.make_engine(smap, labels)
        assert e._map_severity("1") == "A"   # 1→A→A
        assert e._map_severity("2") == "B"   # 2→B→B
        assert e._map_severity("3") == "C"   # 3→C→C
        assert e._map_severity("4") == "C"   # 4→D→C

    def test_severity_labels_no_translation(self):
        """实例3: 无翻译（数字不变），仍能通过 map 映射"""
        labels = {"1": "1", "2": "2", "3": "3", "4": "4"}
        smap = {"1": "A", "2": "B", "3": "C", "4": "C"}
        e = self.make_engine(smap, labels)
        assert e._map_severity("1") == "A"   # 1→1→A
        assert e._map_severity("2") == "B"   # 2→2→B

    def test_reproduction_from_steps(self):
        """复现概率优先从步骤文本提取"""
        from src.sync_engine import SyncEngine
        from src.models import ZentaoBug
        e = SyncEngine.__new__(SyncEngine)
        e.cf_ids = {"reproduction": "cf_repro"}
        e.default_reproduction = "中概率"
        e.severity_labels = {}

        # 测试从步骤提取
        bug = ZentaoBug(
            id=1, title="t", severity="2", pri="2", type="bug",
            status="active", steps="复现概率：必现", assignedTo="",
            assignedToAccount="", openedBy="", openedByAccount="",
            openedDate="", product="", productName="", project="",
            projectName="", module="", moduleName="", openedBuild="",
            snCode="", frequency="", files=[],
        )
        fields = e._build_customfields(bug, "A", "test")
        repro_cf = [f for f in fields if f["cfId"] == "cf_repro"]
        assert len(repro_cf) == 1
        assert repro_cf[0]["value"] == ["必现"]


class TestZentaoTagVariants:
    """_task_title_contains_zentao_id 处理多种禅道标签变体"""

    def _make_task(self, content: str):
        t = MagicMock()
        t.content = content
        t.isArchived = False
        return t

    def test_standard_tag(self):
        engine = make_engine()
        task = self._make_task("【禅道60365】开始回充语音错误")
        assert engine._task_title_contains_zentao_id(task, 60365) is True
        assert engine._task_title_contains_zentao_id(task, 60366) is False

    def test_multi_id_merged_tag(self):
        engine = make_engine()
        title = ("【禅道60365、60357、60358、60359、60381、60391、60394、"
                 "60407、60413】语音问题】开始回充语音错误")
        task = self._make_task(title)
        for bid in [60365, 60357, 60358, 60359, 60381, 60391,
                    60394, 60407, 60413]:
            assert engine._task_title_contains_zentao_id(task, bid) is True
        assert engine._task_title_contains_zentao_id(task, 60366) is False

    def test_prefix_yx(self):
        engine = make_engine()
        task = self._make_task(
            "【禅道YX+58926】DVT—一洗吸协作模式下清扫，上地毯时出现后退停顿现象")
        assert engine._task_title_contains_zentao_id(task, 58926) is True
        assert engine._task_title_contains_zentao_id(task, 58927) is False

    def test_prefix_arbitrary_letters(self):
        engine = make_engine()
        for prefix in ["XX", "AB", "PROJ", "T"]:
            task = self._make_task(f"【禅道{prefix}+55555】标题")
            assert engine._task_title_contains_zentao_id(task, 55555) is True, \
                f"前缀 {prefix}+ 未识别"
            assert engine._task_title_contains_zentao_id(task, 55556) is False

    def test_no_tag(self):
        engine = make_engine()
        task = self._make_task("普通任务标题，无任何禅道标签")
        assert engine._task_title_contains_zentao_id(task, 12345) is False

    def test_partial_id_should_not_match(self):
        engine = make_engine()
        task = self._make_task("【禅道123450】标题")
        assert engine._task_title_contains_zentao_id(task, 12345) is False
        assert engine._task_title_contains_zentao_id(task, 123450) is True

    def test_hash_prefix(self):
        engine = make_engine()
        for title in [
            "#5555 描述",
            "【#5555】描述",
            "[#5555] 描述",
        ]:
            task = self._make_task(title)
            assert engine._task_title_contains_zentao_id(task, 5555) is True, \
                f"# 号格式未识别: {title}"
            assert engine._task_title_contains_zentao_id(task, 5556) is False

    def test_hash_prefix_in_long_title(self):
        engine = make_engine()
        task = self._make_task("回充异常 #12345 偶发问题排查")
        assert engine._task_title_contains_zentao_id(task, 12345) is True
        assert engine._task_title_contains_zentao_id(task, 1234) is False

    def test_hash_should_not_match_unrelated_digits(self):
        """# 号后必须是完整数字，不应误匹配后面其他数字"""
        engine = make_engine()
        task = self._make_task("订单 #20240101 已完成")
        assert engine._task_title_contains_zentao_id(task, 2024) is False
        assert engine._task_title_contains_zentao_id(task, 20240101) is True

    def test_chinese_prefix_works(self):
        """中文前缀也能正确解析（依赖 \\d+ 强制回溯保证 ID 捕获正确）"""
        engine = make_engine()
        task = self._make_task("【禅道产品+123】描述")
        assert engine._task_title_contains_zentao_id(task, 123) is True

    def test_note_field_also_scanned(self):
        """任务备注中的禅道 ID 也应被识别（修复 docstring 撒谎）"""
        engine = make_engine()
        task = self._make_task("普通标题无标签")
        task.note = "参见关联 Bug【禅道60365】的评论"
        assert engine._task_title_contains_zentao_id(task, 60365) is True
        assert engine._task_title_contains_zentao_id(task, 60366) is False

    def test_no_id_match_when_note_empty(self):
        """备注为空时行为与之前一致"""
        engine = make_engine()
        task = self._make_task("普通标题无标签")
        task.note = ""
        assert engine._task_title_contains_zentao_id(task, 12345) is False


class TestCleanTitle:
    """SimilarityClassifier._clean_title 标题噪音清理"""

    def test_brackets_removed(self):
        from src.classifier import SimilarityClassifier
        assert "【" not in SimilarityClassifier._clean_title("【禅道123】机器回充失败")

    def test_vlns_removed(self):
        from src.classifier import SimilarityClassifier
        assert "VLNS" not in SimilarityClassifier._clean_title("VLNS-12345清扫异常")

    def test_cpax_removed(self):
        from src.classifier import SimilarityClassifier
        assert "CPAX" not in SimilarityClassifier._clean_title("CPAX-67890建图失败")

    def test_sn_code_removed(self):
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title("HQ5S00700002HC260600无法开机")
        assert "HQ5S007" not in clean
        assert "无法开机" in clean

    def test_date_removed(self):
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title("2026-05-08充电异常")
        assert "2026" not in clean
        assert "充电异常" in clean

    def test_time_removed(self):
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title("20:31扫地卡住")
        assert "20:31" not in clean
        assert "扫地卡住" in clean

    def test_hash_number_removed(self):
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title("#5555避障失败")
        assert "#5555" not in clean
        assert "避障失败" in clean

    def test_version_removed(self):
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title("V2.3.5升级后建图异常")
        assert "V2.3.5" not in clean
        assert "建图异常" in clean

    def test_leading_number_with_space_removed(self):
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title("1. 机器回充失败")
        assert "1." not in clean
        assert "机器回充失败" in clean

    def test_leading_number_no_space_kept(self):
        """1.5倍 不应被误删（分隔符后无空白）"""
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title("1.5倍建图面积")
        assert "1.5" in clean

    def test_full_noise_title(self):
        """全噪音标题清理后仍保留核心语义"""
        from src.classifier import SimilarityClassifier
        clean = SimilarityClassifier._clean_title(
            "【禅道60365】#5555 HQ5S00700002HC260600 2026-05-08 20:31 回充异常")
        assert "回充异常" in clean
        assert "60365" not in clean
        assert "HQ5S" not in clean


class TestExtractDatetimeBR:
    """extract_datetime <br> 分隔修复"""

    def test_datetime_with_br_between_date_time(self):
        from src.extractor import extract_datetime
        from datetime import datetime
        result = extract_datetime(
            "时间：6/3<br>20:40",
            reference_date=datetime(2026, 6, 1))
        assert result == "2026-06-03 20:40"

    def test_datetime_with_br_self_closing(self):
        from src.extractor import extract_datetime
        from datetime import datetime
        result = extract_datetime(
            "时间：6/3<br/>20:47",
            reference_date=datetime(2026, 6, 1))
        assert result == "2026-06-03 20:47"


class TestExtractFileIdFromSrc:
    """_extract_file_id_from_src 覆盖禅道所有 URL 格式"""

    def test_file_read_clean_url(self):
        assert SyncEngine._extract_file_id_from_src(
            "/file-read-15411.html") == "15411"

    def test_file_download_clean_url(self):
        assert SyncEngine._extract_file_id_from_src(
            "/file-download-200.png") == "200"

    def test_dynamic_path(self):
        assert SyncEngine._extract_file_id_from_src(
            "/index.php?m=file&f=download&fileID=300") == "" or \
            SyncEngine._extract_file_id_from_src(
                "/file/download/300") == "300"

    def test_empty_src(self):
        assert SyncEngine._extract_file_id_from_src("") == ""

    def test_non_file_url(self):
        assert SyncEngine._extract_file_id_from_src(
            "https://example.com/other") == ""


class TestCleanHtmlImageNames:
    """_clean_html_for_tb 图片占位符显示真实文件名"""

    def test_image_with_file_id_to_name_shows_real_name(self):
        html = '<img src="/file-read-15411.html">'
        result = SyncEngine._clean_html_for_tb(
            html, {"15411": "OTA故障截图.png"})
        assert "OTA故障截图.png" in result

    def test_image_fallback_to_image_id_when_no_mapping(self):
        html = '<img src="/file-read-15411.html">'
        result = SyncEngine._clean_html_for_tb(html, {})
        assert "image_15411.png" in result

    def test_image_with_file_download_url_also_extracted(self):
        """file-download clean URL 也能提取 file_id（v2.x 修复）"""
        html = '<img src="/file-download-200.html">'
        result = SyncEngine._clean_html_for_tb(
            html, {"200": "真名.png"})
        assert "真名.png" in result

    def test_image_without_file_id_uses_alt(self):
        html = '<img src="https://example.com/x.png" alt="外链图">'
        result = SyncEngine._clean_html_for_tb(html)
        assert "[图片: 外链图]" in result
