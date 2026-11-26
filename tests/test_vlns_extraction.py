"""VLNS/CPAX 提取：编辑历史里的旧标题标记不得用于去重判定"""
from src.zentao_client import ZentaoClient


def _client(raw):
    c = ZentaoClient.__new__(ZentaoClient)
    c._get_bug_raw = lambda *a, **k: raw
    return c


class TestVlnsExtraction:
    def test_ignores_removed_title_tag_in_history(self):
        """复制缺陷后改标题：历史 old 里的 VLNS 不能算已导入"""
        raw = {
            "title": "【ZY2L】清扫过程中从快速清扫切换到标准清扫无响应",
            "actions": [{
                "action": "edited",
                "comment": "",
                "history": [{
                    "field": "title",
                    "old": "【VLNS-78842】【ZY2L】连续短按两次清扫键不会触发定点清扫",
                    "new": "【ZY2L】清扫过程中从快速清扫切换到标准清扫无响应",
                }],
            }],
        }
        assert _client(raw).extract_vlns_numbers(67053) == []

    def test_current_title_tag_detected(self):
        raw = {"title": "【VLNS-78842】【ZY2L】已同步缺陷", "actions": []}
        assert _client(raw).extract_vlns_numbers(1) == ["78842"]

    def test_comment_reference_detected(self):
        raw = {
            "title": "【ZY2L】标题",
            "actions": [{"action": "commented",
                         "comment": "重复于 VLNS-78842 和 CPAX-12345"}],
        }
        assert _client(raw).extract_vlns_numbers(1) == ["78842", "12345"]

    def test_dedup_keeps_order(self):
        raw = {
            "title": "【VLNS-9】标题",
            "actions": [{"action": "commented",
                         "comment": "VLNS-9 CPAX-8 VLNS-7"}],
        }
        assert _client(raw).extract_vlns_numbers(1) == ["9", "8", "7"]

    def test_empty_data(self):
        assert _client(None).extract_vlns_numbers(1) == []
        assert _client({"title": "", "actions": []}).extract_vlns_numbers(1) == []
