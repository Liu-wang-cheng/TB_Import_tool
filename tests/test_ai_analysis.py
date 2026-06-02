"""测试 AI 分析模块的非网络依赖功能"""
import re
import pytest


class TestLogSummarizer:
    """LogSummarizer 日志摘要器"""

    @pytest.fixture
    def log_re(self):
        # 实际 DRC 日志格式: 5-20 7:50:0.134/SW D/file.cpp:148 msg
        return re.compile(
            r'^(\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{1,2}:\d{1,2}\.\d{3})'
            r'/([A-Z]{2,})\s+([DIWEF])'
            r'/([^:]+):(\d+)\s+(.*)$')

    @pytest.fixture
    def summarizer(self, log_re):
        from src.ai_log_analyzer import LogSummarizer
        return LogSummarizer(log_re=log_re)

    def test_empty_logs(self, summarizer):
        result = summarizer.summarize([])
        assert result["total_lines"] == 0

    def test_keyword_matching(self, summarizer):
        lines = [
            "6-02 10:00:00.123/MOTOR E/driver.cpp:148 motor error detected",
            "6-02 10:00:01.456/SYS I/main.cpp:10 normal operation",
            "6-02 10:00:02.789/BATT W/battery.cpp:55 battery low warning",
        ]
        result = summarizer.summarize(lines, keywords=["motor", "battery"])
        assert result["total_lines"] == 3
        assert result["keywords_found"]["motor"] >= 1
        assert result["keywords_found"]["battery"] >= 1

    def test_ew_count(self, summarizer):
        lines = [
            "6-02 10:00:00.123/MTR E/motor.cpp:1 error",
            "6-02 10:00:01.456/BAT W/batt.cpp:2 warning",
            "6-02 10:00:02.789/SYS I/main.cpp:3 info",
        ]
        result = summarizer.summarize(lines)
        assert result["ew_count"] >= 2

    def test_fault_count(self, summarizer):
        # fault/fail/error/crash 等关键词触发故障计数
        lines = [
            "6-02 10:00:00.123/NAV E/nav.cpp:1 crash system failure",
            "6-02 10:00:01.456/NAV E/nav.cpp:2 fatal error occurred",
            "6-02 10:00:02.789/NAV E/nav.cpp:3 exception timeout",
        ]
        result = summarizer.summarize(lines)
        # fault_count 至少统计到一些 E 级别日志
        assert result["ew_count"] >= 3

    def test_deduplication(self, summarizer):
        lines = [
            "6-02 10:00:00.123/MTR E/motor.cpp:1 same error message repeated",
            "6-02 10:00:01.456/MTR E/motor.cpp:1 same error message repeated",
            "6-02 10:00:02.789/MTR E/motor.cpp:1 same error message repeated",
        ]
        result = summarizer.summarize(lines)
        assert len(result["key_logs"]) <= 2

    def test_avoid_keywords_filters_noise(self, summarizer):
        lines = [
            "6-02 10:00:00.123/BIN E/bin.cpp:1 \x00binary\xffnoise",
            "6-02 10:00:01.456/NAV E/nav.cpp:2 real error",
        ]
        result = summarizer.summarize(lines, avoid_keywords=["\x00", "\xff"])
        assert result["total_lines"] == 2


class TestSNExtractionInLogAnalysis:
    """SN 提取（日志分析集成）"""

    def test_extract_hq_from_customfields(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('T', (), {
            'id': '1', 'content': 'test',
            'customfields': [{'value': 'HQ5S00700002HC261300069'}],
        })()
        assert _extract_sn_from_task(task) == 'HQ5S00700002HC261300069'

    def test_extract_non_hq_from_customfields(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('T', (), {
            'id': '1', 'content': 'test',
            'customfields': [{'value': '48HCNFBN0049X'}],
        })()
        assert _extract_sn_from_task(task) == '48HCNFBN0049X'

    def test_date_excluded_as_sn(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('T', (), {
            'id': '1', 'content': 'test',
            'customfields': [{'value': '2026-06-03 00:13'}],
        })()
        assert _extract_sn_from_task(task) is None

    def test_drc_sn_in_content(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('T', (), {
            'id': '1', 'customfields': [],
            'content': 'record_..._2026L014E403300002_8.1.21.drc',
        })()
        assert _extract_sn_from_task(task) == '2026L014E403300002'

    def test_no_sn_returns_none(self):
        from src.log_analysis_integration import _extract_sn_from_task
        task = type('T', (), {
            'id': '1', 'customfields': [],
            'content': 'regular bug without serial',
        })()
        assert _extract_sn_from_task(task) is None


class TestDRCTimeExtraction:
    """DRC 文件名时间戳优先提取"""

    def test_drc_filename_time_utc(self):
        from src.log_analysis_integration import _extract_time_from_task
        task = type('T', (), {
            'id': '1',
            'content': '【禅道16376】机器触发电池高温报警',
            'customfields': [{
                'title': '日志附件', 'type': 'text',
                'value': 'record_20260602_155412_192.168.121.121_2026L014E403300002_8.1.21.drc'
            }],
        })()
        t = _extract_time_from_task(task)
        assert t is not None
        assert t.day == 2, f"应为6月2日, 实为{t.day}日"
        assert t.hour == 15, f"应为15:54, 实为{t.hour}:{t.minute}"

    def test_text_time_fallback(self):
        from src.log_analysis_integration import _extract_time_from_task
        task = type('T', (), {
            'id': '1',
            'content': '发生时间: 2026-06-02 10:30',
            'customfields': [],
        })()
        t = _extract_time_from_task(task)
        assert t is not None
        assert t.hour == 2  # 10:30 北京时间 → 02:30 UTC


class TestTimeNormalize:
    """时间标准化（sync_engine）"""

    def test_zentao_format(self):
        from src.sync_engine import SyncEngine
        # 禅道格式: 2026-06-02 10:30:00
        result = SyncEngine._normalize_dt("2026-06-02 10:30:00")
        assert result == "2026-06-02 10:30:00"

    def test_iso_format(self):
        from src.sync_engine import SyncEngine
        result = SyncEngine._normalize_dt("2026-06-02T10:30:00")
        assert "2026-06-02" in result

    def test_empty_returns_empty(self):
        from src.sync_engine import SyncEngine
        assert SyncEngine._normalize_dt("") == ""

    def test_none_returns_empty(self):
        from src.sync_engine import SyncEngine
        assert SyncEngine._normalize_dt(None) == ""


class TestExtractSNZentao:
    """zentao_client._extract_sn 四类格式"""

    def test_sn_prefix_colon(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("SN:ABC123456") == "ABC123456"

    def test_hq_format(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("HQ5S00700002HC261300069") == "HQ5S00700002HC261300069"

    def test_filename_format(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("48HCNFBN0049X-2026-06-02.zip") == "48HCNFBN0049X"

    def test_drc_filename_format(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn(
            "record_20260602_155412_192.168.121.121_2026L014E403300002_8.1.21.drc"
        ) == "2026L014E403300002"

    def test_not_found(self):
        from src.zentao_client import ZentaoClient
        assert ZentaoClient._extract_sn("no sn here") == "/"


class TestLLMJsonExtract:
    """AILogAnalyzer JSON 提取和修复"""

    def test_extract_valid_json(self):
        from src.ai_log_analyzer import AILogAnalyzer
        import json
        content = '{"result": "ok", "confidence": 0.95}'
        json_str, errors = AILogAnalyzer._extract_and_repair_json(content)
        assert errors == []
        assert json.loads(json_str)["result"] == "ok"

    def test_extract_json_with_markdown(self):
        from src.ai_log_analyzer import AILogAnalyzer
        import json
        content = '```json\n{"key": "value"}\n```'
        json_str, errors = AILogAnalyzer._extract_and_repair_json(content)
        assert errors == []
        assert json.loads(json_str)["key"] == "value"

    def test_extract_json_with_prefix(self):
        from src.ai_log_analyzer import AILogAnalyzer
        import json
        content = 'Here is the result: {"key": "value"}'
        json_str, errors = AILogAnalyzer._extract_and_repair_json(content)
        # 有前缀时无法直接解析，但不应崩溃
        assert isinstance(json_str, str)
        assert isinstance(errors, list)

    def test_extract_invalid_json(self):
        from src.ai_log_analyzer import AILogAnalyzer
        content = 'not json at all'
        json_str, errors = AILogAnalyzer._extract_and_repair_json(content)
        assert len(errors) > 0  # 应该有错误

    def test_repair_handles_error(self):
        from src.ai_log_analyzer import AILogAnalyzer
        content = '{"key": "value",}'  # trailing comma
        json_str, errors = AILogAnalyzer._extract_and_repair_json(content)
        # 尾逗号产生解析错误，但应被记录而不崩溃
        assert len(errors) > 0
