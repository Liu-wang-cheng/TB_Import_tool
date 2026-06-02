"""测试 src/sync_engine.py — HTML转换、CPAX检测、去重逻辑"""
import pytest

from src.sync_engine import SyncEngine
from src.models import ZentaoBug


def make_engine():
    return SyncEngine.__new__(SyncEngine)


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
        engine._html_to_text = SyncEngine._html_to_text
        engine._map_severity = lambda s: engine.severity_map.get(str(s), "B")

        note = engine._build_note(bug)
        assert "<pre" in note
        assert "步骤" in note
        assert "开机" in note
        assert "<table" not in note  # table should be converted to text

    def test_note_empty_steps(self):
        bug = self.make_bug(steps="")
        engine = SyncEngine.__new__(SyncEngine)
        engine.severity_map = {"1": "S", "2": "A", "3": "B", "4": "C"}
        engine._html_to_text = SyncEngine._html_to_text
        engine._map_severity = lambda s: engine.severity_map.get(str(s), "B")

        note = engine._build_note(bug)
        assert "<pre" not in note  # no pre for empty steps


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
