"""测试外部 Teambition 源适配器 — 字段映射、筛选、去重编号提取"""
from unittest.mock import MagicMock

import pytest

from src.teambition_source_adapter import TeambitionSourceAdapter


def make_task(**overrides):
    task = {
        "_id": "69e1a55475a4096ed84a4e62",
        "content": "VLNS-62445遥控器 卡顿现象",
        "uniqueId": 24,
        "isDone": False,
        "note": "**重现步骤**\n\n**测试结果**",
        "_executorId": "64813b93d0c16391307c856f",
        "_creatorId": "61c97dcf947cb43084f3807c",
        "created": "2026-04-17T03:13:24.027Z",
        "_scenariofieldconfigId": "69c0b68c85216d39799e0f88",
        "_taskflowstatusId": "status_pending",
        "customfields": [
            {
                "type": "commongroup",
                "value": [{"title": "固件缺陷"}],
            },
            {
                "type": "dropDown",
                "value": [{"title": "中(≤30%)"}],
            },
        ],
    }
    task.update(overrides)
    return task


def make_adapter(status_map=None, field_ids=None):
    client = MagicMock()
    client.account = ""
    client.project_id = ""
    client.get_user_name = MagicMock(side_effect=lambda uid: {
        "64813b93d0c16391307c856f": "彭及禹",
        "61c97dcf947cb43084f3807c": "蒋海桂",
    }.get(uid, ""))
    client.get_taskflow_status_name = MagicMock(
        side_effect=lambda sid: (status_map or {
            "status_pending": "待处理",
            "status_reopen": "重新打开",
            "status_closed": "关闭",
        }).get(sid, ""))
    adapter = TeambitionSourceAdapter(
        client, project_id="69c0b68bf754000531ced0ce", field_ids=field_ids)
    return adapter


class TestTaskToBug:
    """_task_to_bug 字段映射"""

    def test_basic_mapping(self):
        adapter = make_adapter()
        bug = adapter._task_to_bug(make_task())
        assert bug.id == 24
        assert bug.title == "VLNS-62445遥控器 卡顿现象"
        assert bug.status == "待处理"  # 状态名来自 taskflowstatus
        assert bug.assignedTo == "彭及禹"
        assert bug.openedBy == "蒋海桂"
        assert bug.steps == "**重现步骤**\n\n**测试结果**"

    def test_status_name_mapping(self):
        adapter = make_adapter()
        bug = adapter._task_to_bug(make_task(_taskflowstatusId="status_reopen"))
        assert bug.status == "重新打开"
        bug2 = adapter._task_to_bug(make_task(_taskflowstatusId="status_closed"))
        assert bug2.status == "关闭"

    def test_customfields_extraction(self):
        adapter = make_adapter()
        bug = adapter._task_to_bug(make_task())
        assert bug.type == "固件缺陷"  # commongroup → category
        assert bug.frequency == "中(≤30%)"  # 含"概率"

    def test_no_unique_id_falls_back(self):
        """无 uniqueId 时用 hash 兜底，不产生 id=0"""
        adapter = make_adapter()
        bug = adapter._task_to_bug(make_task(uniqueId=None))
        assert bug.id != 0

    def test_no_unique_id_stable_across_processes(self):
        """无 uniqueId 时 bug_id 必须跨进程稳定（不能用 str hash，
        PYTHONHASHSEED 随机会导致去重标签跨运行不一致）"""
        import os
        adapter = make_adapter()
        tid = "69e1a55475a4096ed84a4e62"
        bug1 = adapter._task_to_bug(make_task(uniqueId=None, _id=tid))
        # 强制不同 hash seed 再建一次
        seed = os.environ.get("PYTHONHASHSEED", "0")
        os.environ["PYTHONHASHSEED"] = "42"
        try:
            adapter2 = make_adapter()
            bug2 = adapter2._task_to_bug(make_task(uniqueId=None, _id=tid))
        finally:
            if seed:
                os.environ["PYTHONHASHSEED"] = seed
            else:
                os.environ.pop("PYTHONHASHSEED", None)
        assert bug1.id == bug2.id
        # 应为 _id 的 hex 数值，而非随机 hash
        assert bug1.id == int(tid, 16)

    def test_found_time_normalized(self):
        """点分日期 '2026.8.21——15:50' 归一化为 ISO 格式，
        避免 _filter_bugs 的字典序比较出错（'.' > '-'）"""
        adapter = make_adapter()
        task = make_task(customfields=[
            {"type": "date", "value": [{"title": "2026.8.21——15:50"}]},
        ])
        bug = adapter._task_to_bug(task)
        assert bug.openedDate == "2026-08-21 15:50"

    def test_sn_with_mixed_case_extracted(self):
        """SN 值含小写（如 Philips263100137）通过 field_ids 精确映射提取"""
        adapter = make_adapter(field_ids={
            "sn_code": "6306e205c09533eb452f004c",
            "found_time": "6306e2057e5ecb33ee2221a2",
        })
        task = make_task(customfields=[
            {"_customfieldId": "6306e205c09533eb452f004c",
             "type": "text", "value": [{"title": "Philips263100137"}]},
            {"_customfieldId": "6306e2057e5ecb33ee2221a2",
             "type": "text", "value": [{"title": "8/26 15：25"}]},
        ])
        bug = adapter._task_to_bug(task)
        assert bug.snCode == "Philips263100137"
        assert bug.openedDate == "2026-08-26 15:25"

    def test_field_ids_priority_over_value_guess(self):
        """field_ids 精确映射优先于值特征猜测（如 version 值不被猜成 SN）"""
        adapter = make_adapter(field_ids={"sn_code": "cf-sn", "version": "cf-ver"})
        task = make_task(customfields=[
            {"_customfieldId": "cf-sn", "type": "text",
             "value": [{"title": "Philips263100137"}]},
            {"_customfieldId": "cf-ver", "type": "text",
             "value": [{"title": "1.0.39"}]},
        ])
        bug = adapter._task_to_bug(task)
        assert bug.snCode == "Philips263100137"
        assert bug.openedBuild == "1.0.39"

    def test_found_time_md_format_normalized(self):
        """M/D 时间格式（8/26 15：25）用任务创建时间补全年份"""
        adapter = make_adapter()
        task = make_task(created="2026-08-26T03:00:00.000Z", customfields=[
            {"type": "text", "value": [{"title": "8/26 15：25-15：40"}]},
        ])
        bug = adapter._task_to_bug(task)
        assert bug.openedDate == "2026-08-26 15:25"

    def test_found_time_dotted_normalized(self):
        adapter = make_adapter()
        task = make_task(customfields=[
            {"type": "text", "value": [{"title": "2026.8.21——15:50"}]},
        ])
        bug = adapter._task_to_bug(task)
        assert bug.openedDate == "2026-08-21 15:50"

    def test_severity_not_matched_from_commongroup(self):
        """commongroup 分类值（如"一般性建议类问题"）不应被猜成严重程度"""
        adapter = make_adapter()
        task = make_task(customfields=[
            {"type": "commongroup", "value": [{"title": "一般性建议类问题"}]},
            {"type": "dropDown", "value": [{"title": "严重"}]},
        ])
        bug = adapter._task_to_bug(task)
        assert bug.type == "一般性建议类问题"
        assert bug.severity == "严重"


class TestFilterBugs:
    """_filter_bugs 客户端筛选"""

    def test_status_filter(self):
        adapter = make_adapter()
        bugs = [adapter._task_to_bug(make_task(uniqueId=1, _taskflowstatusId="status_pending")),
                adapter._task_to_bug(make_task(uniqueId=2, _taskflowstatusId="status_closed"))]
        result = adapter._filter_bugs(bugs, statuses=["待处理"], date_from=None,
                                      date_to=None, assigned_to=None)
        assert len(result) == 1
        assert result[0].id == 1

    def test_assignee_filter_by_name(self):
        adapter = make_adapter()
        bugs = [adapter._task_to_bug(make_task(uniqueId=1))]
        result = adapter._filter_bugs(bugs, statuses=None, date_from=None,
                                      date_to=None, assigned_to=["彭及禹"])
        assert len(result) == 1
        # 名字不匹配
        result2 = adapter._filter_bugs(bugs, statuses=None, date_from=None,
                                       date_to=None, assigned_to=["张三"])
        assert len(result2) == 0

    def test_assignee_filter_strips_dept_prefix(self):
        """指派人带白名单部门前缀也能匹配"""
        adapter = make_adapter()
        # 模拟 assignedTo 带白名单部门前缀
        task = make_task(uniqueId=1)
        bug = adapter._task_to_bug(task)
        bug.assignedTo = "IOT-彭及禹"
        result = adapter._filter_bugs([bug], statuses=None, date_from=None,
                                      date_to=None, assigned_to=["彭及禹"])
        assert len(result) == 1


class TestStripDeptPrefix:
    def test_strip(self):
        # 白名单部门前缀：去前缀
        assert TeambitionSourceAdapter._strip_dept_prefix("IOT-彭及禹") == "彭及禹"
        # 非白名单前缀（账号名）：保持完整
        assert TeambitionSourceAdapter._strip_dept_prefix("乐动开发-343") == "乐动开发-343"
        assert TeambitionSourceAdapter._strip_dept_prefix("彭及禹") == "彭及禹"
        assert TeambitionSourceAdapter._strip_dept_prefix("") == ""
        assert TeambitionSourceAdapter._strip_dept_prefix(None) == ""


class TestExtractVlnsNumbers:
    def test_extract_from_comments(self):
        adapter = make_adapter()
        adapter._task_cache = {24: {"_id": "task_hex_id"}}
        adapter._client.fetch_task_comments = MagicMock(return_value=[
            {"comment": "VLNS-61849 已在内部确认"},
            {"comment": "重复 VLNS-61849 和 CPAX-12345"},
        ])
        nums = adapter.extract_vlns_numbers(24)
        assert nums == ["61849", "12345"]

    def test_no_vlns(self):
        adapter = make_adapter()
        adapter._task_cache = {24: {"_id": "task_hex_id"}}
        adapter._client.fetch_task_comments = MagicMock(return_value=[
            {"comment": "普通评论"},
        ])
        assert adapter.extract_vlns_numbers(24) == []


class TestTaskIdComposition:
    """task_id 组合（uniqueIdPrefix + uniqueId）"""

    def test_task_id_composed(self):
        adapter = make_adapter()
        adapter._unique_id_prefix = "323A"
        bug = adapter._task_to_bug(make_task())
        assert bug.task_id == "323A-24"

    def test_task_id_without_prefix(self):
        adapter = make_adapter()
        adapter._unique_id_prefix = ""
        bug = adapter._task_to_bug(make_task())
        assert bug.task_id == "24"


class TestWriteback:
    """编号回写（先标题后评论）+ 回写评论过滤"""

    def test_fetch_bug_comments_filters_writeback(self):
        adapter = make_adapter()
        adapter._client.fetch_task_comments = MagicMock(return_value=[
            {"actor": "a", "comment": "【内部TB同步】内部TB任务编号: VLNS-72536"},
            {"actor": "b", "comment": "正常评论"},
        ])
        adapter._task_cache = {24: {"_id": "task_hex_id"}}
        comments = adapter.fetch_bug_comments(24)
        assert len(comments) == 1
        assert comments[0]["comment"] == "正常评论"

    def test_update_bug_title_falls_back_to_comment(self):
        adapter = make_adapter()
        adapter._client.update_title = MagicMock(return_value=False)
        adapter._client.add_comment = MagicMock(return_value=True)
        adapter._task_cache = {24: {"_id": "task_hex_id"}}
        adapter.update_bug_title(24, "【VLNS-72536】原标题")
        adapter._client.update_title.assert_called_once_with(
            "task_hex_id", "【VLNS-72536】原标题")
        adapter._client.add_comment.assert_called_once_with(
            "task_hex_id", "【内部TB同步】内部TB任务编号: VLNS-72536")

    def test_update_bug_title_success_no_comment(self):
        adapter = make_adapter()
        adapter._client.update_title = MagicMock(return_value=True)
        adapter._client.add_comment = MagicMock()
        adapter._task_cache = {24: {"_id": "task_hex_id"}}
        adapter.update_bug_title(24, "【VLNS-72536】原标题")
        adapter._client.add_comment.assert_not_called()


class TestCommentAttachmentNames:
    """评论附件名引用（真实文件名）"""

    def _make_engine(self, comments):
        from src.sync_engine import SyncEngine
        engine = SyncEngine.__new__(SyncEngine)
        engine.source_type = "teambition"
        engine.source = MagicMock()
        engine.source.fetch_bug_comments = MagicMock(return_value=comments)
        return engine

    def test_pure_attachment_comment(self):
        engine = self._make_engine([
            {"actor": "张三", "date": "2026-01-01", "comment": "",
             "attachments": [{"name": "normal_video.mp4"}]},
        ])
        processed, _ = engine._parse_bug_comments(MagicMock(id=24))
        assert len(processed) == 1
        assert processed[0][0] == "[附件: normal_video.mp4]"

    def test_comment_with_text_and_attachment(self):
        engine = self._make_engine([
            {"actor": "李四", "date": "2026-01-02", "comment": "看下这个问题",
             "attachments": [{"name": "image.png"}]},
        ])
        processed, _ = engine._parse_bug_comments(MagicMock(id=24))
        assert len(processed) == 1
        assert "看下这个问题" in processed[0][0]
        assert "image.png" in processed[0][0]

    def test_multiple_attachments_joined(self):
        engine = self._make_engine([
            {"actor": "王五", "date": "2026-01-03", "comment": "",
             "attachments": [{"name": "a.drc"}, {"name": "b.mp4"}]},
        ])
        processed, _ = engine._parse_bug_comments(MagicMock(id=24))
        assert "[附件: a.drc, b.mp4]" in processed[0][0]
