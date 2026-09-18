"""回归测试：媒体校验（M15）、知识库检索索引（M1）、AI 分析 JSON 纯净性（H3）"""
import json
from unittest.mock import MagicMock

from src.knowledge_base import AnalysisRecord, KnowledgeBase


class TestMediaValidation:
    """下载内容校验：拒绝空文件与 HTML/JSON 错误页（防止坏缓存永久失效）"""

    def test_html_page_rejected(self):
        from src.tb_web_downloader import _is_valid_media
        assert _is_valid_media(b"<!DOCTYPE html><html>login</html>") is False

    def test_json_error_rejected(self):
        from src.tb_web_downloader import _is_valid_media
        assert _is_valid_media(b'{"error": "unauthorized"}') is False

    def test_empty_rejected(self):
        from src.tb_web_downloader import _is_valid_media
        assert _is_valid_media(b"") is False
        assert _is_valid_media(b"short") is False

    def test_png_accepted(self):
        from src.tb_web_downloader import _is_valid_media
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        assert _is_valid_media(png) is True

    def test_mp4_accepted(self):
        from src.vision_integration import _is_valid_media_bytes
        mp4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
        assert _is_valid_media_bytes(mp4) is True

    def test_invalid_cached_file_rejected(self, tmp_path):
        from src.vision_integration import _is_valid_media_file
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"<html>login page</html>")
        assert _is_valid_media_file(bad) is False
        good = tmp_path / "good.mp4"
        good.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32)
        assert _is_valid_media_file(good) is True


class TestKnowledgeRetrieveIndex:
    """检索按 record id 映射：状态过滤与返回记录不再错位（M1）"""

    def _make_kb(self, tmp_path):
        kb = KnowledgeBase.__new__(KnowledgeBase)
        kb._enabled = True
        kb._threshold = 0.05
        kb._max_examples = 3
        kb._cache_dir = str(tmp_path)
        kb._vectorizer = None
        kb._tfidf_matrix = None
        kb._record_ids = []
        kb._records = [
            AnalysisRecord(id="a1", title="回充找不到基站 对桩失败",
                           category="算法-回充&基站", status="approved",
                           root_cause="桩信号弱"),
            AnalysisRecord(id="a2", title="外直角沿墙概率发生碰撞",
                           category="算法-避障", status="approved",
                           root_cause="沿墙传感器延迟"),
            AnalysisRecord(id="r1", title="毛毯过渡轮子打滑",
                           category="算法-脱困、越障", status="rejected",
                           root_cause="轮子打滑"),
            AnalysisRecord(id="p1", title="待审核样本",
                           category="未分类缺陷", status="pending"),
        ]
        kb._rebuild_model()
        return kb

    def test_approved_filter_returns_correct_record(self, tmp_path):
        kb = self._make_kb(tmp_path)
        got = kb.retrieve_similar({"title": "外直角沿墙概率发生碰撞"}, {})
        assert [r.id for r in got][:1] == ["a2"]

    def test_rejected_filter_no_longer_empty(self, tmp_path):
        kb = self._make_kb(tmp_path)
        got = kb.retrieve_similar({"title": "毛毯过渡轮子打滑"}, {},
                                  status_filter="rejected")
        assert [r.id for r in got] == ["r1"]

    def test_status_filter_excludes_wrong_status(self, tmp_path):
        kb = self._make_kb(tmp_path)
        got = kb.retrieve_similar({"title": "外直角沿墙概率发生碰撞"}, {},
                                  status_filter="rejected")
        assert all(r.status == "rejected" for r in got)


class TestAiAnalyzerJsonPurity:
    """schema 校验失败不再把警告文本拼进 JSON（拼接会使下游解析全挂）"""

    def _make_analyzer(self):
        from src.ai_log_analyzer import AILogAnalyzer
        a = AILogAnalyzer.__new__(AILogAnalyzer)
        a._max_retries = 1
        a._http = MagicMock()
        return a

    def test_schema_warning_not_appended(self):
        a = self._make_analyzer()
        bad = json.dumps({
            "root_cause": "x",
            "evidence": ["e1"],
            "confidence": "42",          # 非法枚举 → schema 校验失败
            "summary": "长" * 150,        # 超长 → 校验失败
        }, ensure_ascii=False)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{
            "message": {"content": bad},
            "finish_reason": "stop",
        }]}
        a._http.post.return_value = resp
        out = a._call_api("http://x/v1", "k", "m", 5, "prompt")
        assert "JSON校验警告" not in out
        json.loads(out)  # 仍是合法 JSON

    def test_empty_content_doubles_tokens(self, monkeypatch):
        """思维链耗尽（length+空内容）→ 翻倍 token 重试"""
        monkeypatch.setattr("time.sleep", lambda *_: None)
        a = self._make_analyzer()
        r1 = MagicMock()
        r1.status_code = 200
        r1.json.return_value = {"choices": [{
            "message": {"content": "", "reasoning_content": "thinking"},
            "finish_reason": "length",
        }]}
        r2 = MagicMock()
        r2.status_code = 200
        r2.json.return_value = {"choices": [{
            "message": {"content": '{"root_cause": "t"}'},
            "finish_reason": "stop",
        }]}
        a._http.post.side_effect = [r1, r2]
        out = a._call_api("http://x/v1", "k", "m", 5, "p", max_tokens=4000)
        assert out == '{"root_cause": "t"}'
        assert a._http.post.call_count == 2
        second = json.loads(a._http.post.call_args_list[1].kwargs["data"]
                            .decode("utf-8"))
        assert second["max_tokens"] == 8000


class TestYearRollover:
    """M/D 补年：未来差值超 180 天才回退上一年（真实数据 +131 天不回退）"""

    def test_real_case_131_days_future_kept(self):
        from src.extractor import extract_datetime
        from datetime import datetime, timezone
        ref = datetime(2026, 4, 17, 3, 13, 24, tzinfo=timezone.utc)
        assert extract_datetime("8/26 15：25", ref) == "2026-08-26 15:25"

    def test_cross_year_rolls_back(self):
        from src.extractor import extract_datetime
        from datetime import datetime
        # 1 月步骤里写 12/28 → 指上一年
        assert extract_datetime("时间：12/28 10:00",
                                datetime(2026, 1, 5)) == "2025-12-28 10:00"

    def test_near_future_kept(self):
        from src.extractor import extract_datetime
        from datetime import datetime
        assert extract_datetime("时间：6/3 20:40",
                                datetime(2026, 6, 5)) == "2026-06-03 20:40"


class TestCollabMergePolicy:
    """知识库合并：同 id 冲突取时间戳更新的记录（审核结论不来回翻转）"""

    def _cl(self):
        from src.collaborative_learning import CollaborativeLearning
        return CollaborativeLearning.__new__(CollaborativeLearning)

    def test_newer_remote_feedback_wins(self):
        cl = self._cl()
        local = (b'{"id": "a", "status": "pending", '
                 b'"created_at": "2026-01-01 00:00:00"}\n')
        remote = (b'{"id": "a", "status": "approved", '
                  b'"created_at": "2026-01-01 00:00:00", '
                  b'"feedback_at": "2026-03-02 10:00:00"}\n')
        out = cl._merge_jsonl(local, remote).decode("utf-8")
        assert '"approved"' in out
        assert out.count('"id": "a"') == 1

    def test_newer_local_wins(self):
        cl = self._cl()
        local = (b'{"id": "b", "status": "rejected", '
                 b'"created_at": "2026-01-01 00:00:00", '
                 b'"feedback_at": "2026-05-01 09:00:00"}\n')
        remote = (b'{"id": "b", "status": "pending", '
                  b'"created_at": "2026-01-01 00:00:00"}\n')
        out = cl._merge_jsonl(local, remote).decode("utf-8")
        assert '"rejected"' in out

    def test_union_of_distinct_ids(self):
        cl = self._cl()
        local = b'{"id": "x", "status": "approved"}\n'
        remote = b'{"id": "y", "status": "approved"}\n'
        out = cl._merge_jsonl(local, remote).decode("utf-8")
        assert '"x"' in out and '"y"' in out


class TestCollabTsCompare:
    """合并冲突的时间戳比较：解析为 datetime（未补零/T 与空格混写）"""

    def _cl(self):
        from src.collaborative_learning import CollaborativeLearning
        return CollaborativeLearning.__new__(CollaborativeLearning)

    def test_unpadded_month_parsed_correctly(self):
        """'2026-9-30' vs '2026-10-01'：字符串比较会误判 9 月更新，
        解析后应取 10 月（右侧）的新结论"""
        cl = self._cl()
        local = (b'{"id": "c", "status": "rejected", '
                 b'"created_at": "2026-01-01 00:00:00", '
                 b'"feedback_at": "2026-9-30 10:00:00"}\n')
        remote = (b'{"id": "c", "status": "approved", '
                  b'"created_at": "2026-01-01 00:00:00", '
                  b'"feedback_at": "2026-10-01 09:00:00"}\n')
        out = cl._merge_jsonl(local, remote).decode("utf-8")
        assert '"approved"' in out

    def test_t_separator_not_inherently_newer(self):
        """同日 'T' 格式（09:00）不比空格格式（10:00）新，本地更新应保留"""
        cl = self._cl()
        local = (b'{"id": "d", "status": "rejected", '
                 b'"created_at": "2026-01-01 00:00:00", '
                 b'"feedback_at": "2026-05-01 10:00:00"}\n')
        remote = (b'{"id": "d", "status": "approved", '
                  b'"created_at": "2026-01-01 00:00:00", '
                  b'"feedback_at": "2026-05-01T09:00:00"}\n')
        out = cl._merge_jsonl(local, remote).decode("utf-8")
        assert '"rejected"' in out
