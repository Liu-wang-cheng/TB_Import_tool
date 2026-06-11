"""测试重新激活功能：状态同步、评论过滤、任务流状态识别"""

from unittest.mock import MagicMock, patch, call
from datetime import timezone, timedelta

import pytest

from src.models import SyncAction, SyncResult, SyncStats, TeambitionTask, ZentaoBug
from src.sync_engine import SyncEngine


# ── 辅助：构建测试用 SyncEngine ──────────────────────────

def _make_engine(reactivate_closed=True, sync_attachments=True):
    """创建一个不依赖真实 API 的 SyncEngine"""
    config = {
        "sync": {
            "reactivate_closed": reactivate_closed,
            "sync_attachments": sync_attachments,
            "zentao_tag_in_tb": "【禅道{bug_id}】",
            "teambition_tag_in_zentao": "【{task_id}】",
        },
        "teambition": {"customfield_ids": {}},
        "zentao": {},
        "jira": {},
        "classifier": {},
    }
    source = MagicMock()
    teambition = MagicMock()
    engine = SyncEngine(config, source, teambition)
    return engine


def _make_bug(bug_id=100, status="active", title="测试Bug", assignedTo="张三"):
    return ZentaoBug(
        id=bug_id, title=title, severity="3", status=status,
        assignedTo=assignedTo, steps="<p>步骤</p>",
    )


def _make_task(task_id="tb001", status="closed_status_id", updated="2024-06-01T10:00:00.000Z"):
    return TeambitionTask(
        taskId=task_id, content=f"【禅道100】测试Bug", status=status,
        updated=updated,
    )


# ══════════════════════════════════════════════════════════
# 1. SyncAction / SyncStats 模型测试
# ══════════════════════════════════════════════════════════

class TestModels:
    def test_sync_action_has_reactivated(self):
        assert SyncAction.REACTIVATED.value == "reactivated"

    def test_sync_stats_reactivated_field(self):
        stats = SyncStats(total=10, created=3, reactivated=2,
                          skipped_dedup=4, errors=1)
        assert stats.reactivated == 2
        s = str(stats)
        assert "重新激活 2 条" in s
        assert "新建 3 条" in s

    def test_sync_stats_str_format(self):
        stats = SyncStats(total=5, created=1, reactivated=1, closed_synced=0, skipped_dedup=2, errors=1)
        expected = "导入同步: 共 5 条, 新建 1 条, 重新激活 1 条, 去重跳过 2 条, 筛选跳过 0 条, 错误 1 条"
        assert str(stats) == expected

    def test_sync_stats_str_with_close(self):
        """关闭同步有数据时显示独立行"""
        stats = SyncStats(total=0, created=0, reactivated=0, closed_synced=3,
                          skipped_dedup=0, errors=0)
        result = str(stats)
        assert "导入同步: 共 0 条" in result
        assert "关闭同步: 成功关闭 3 条" in result
        assert "\n" in result  # 两行

    def test_sync_stats_str_close_zero_omitted(self):
        """closed_synced=0 时不显示关闭同步行"""
        stats = SyncStats(total=5, created=2, reactivated=0, closed_synced=0,
                          skipped_dedup=3, errors=0)
        result = str(stats)
        assert "关闭同步" not in result
        assert "导入同步" in result


# ══════════════════════════════════════════════════════════
# 1b. 图片格式检测测试
# ══════════════════════════════════════════════════════════

class TestImageFormatDetection:
    """ZentaoClient._detect_image_format 魔数检测"""

    def test_detect_png(self):
        from src.zentao_client import ZentaoClient
        data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        assert ext == 'png'
        assert mime == 'image/png'

    def test_detect_jpeg(self):
        from src.zentao_client import ZentaoClient
        data = b'\xff\xd8\xff\xe0' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        assert ext == 'jpg'
        assert mime == 'image/jpeg'

    def test_detect_gif87a(self):
        from src.zentao_client import ZentaoClient
        data = b'GIF87a' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        assert ext == 'gif'
        assert mime == 'image/gif'

    def test_detect_gif89a(self):
        from src.zentao_client import ZentaoClient
        data = b'GIF89a' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        assert ext == 'gif'

    def test_detect_webp(self):
        from src.zentao_client import ZentaoClient
        data = b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        assert ext == 'webp'
        assert mime == 'image/webp'

    def test_detect_bmp(self):
        from src.zentao_client import ZentaoClient
        data = b'BM' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        assert ext == 'bmp'
        assert mime == 'image/bmp'

    def test_detect_empty_fallback(self):
        """空数据回退为 png"""
        from src.zentao_client import ZentaoClient
        ext, mime = ZentaoClient._detect_image_format(b'')
        assert ext == 'png'
        assert mime == 'image/png'

    def test_detect_unknown_fallback(self):
        """未知格式回退为 png"""
        from src.zentao_client import ZentaoClient
        data = b'\x00\x01\x02\x03' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        assert ext == 'png'
        assert mime == 'image/png'

    def test_html_is_not_png(self):
        """HTML 页面不会误判为 PNG"""
        from src.zentao_client import ZentaoClient
        data = b'<!DOCTYPE html>\n<html>' + b'\x00' * 100
        ext, mime = ZentaoClient._detect_image_format(data)
        # HTML 开头是 <!DOCTYPE，不匹配任何图片格式，应回退
        assert ext == 'png'  # 回退默认值
        # 但调用方应该用 _is_valid (in _download_file) 先拦截


# ══════════════════════════════════════════════════════════
# 2. _normalize_dt 时间标准化测试
# ══════════════════════════════════════════════════════════

class TestNormalizeDt:
    def test_zentao_format(self):
        """禅道日期格式：YYYY-MM-DD HH:MM:SS（无时区，视为 CST）"""
        result = SyncEngine._normalize_dt("2024-06-01 12:00:00")
        assert result == "2024-06-01 12:00:00"

    def test_tb_utc_format(self):
        """TB UTC 格式：YYYY-MM-DDTHH:MM:SS.sssZ"""
        result = SyncEngine._normalize_dt("2024-06-01T04:00:00.000Z")
        # UTC 04:00 = CST 12:00
        assert result == "2024-06-01 12:00:00"

    def test_empty_string(self):
        assert SyncEngine._normalize_dt("") == ""

    def test_cst_vs_utc_same_moment(self):
        """同一时刻，禅道 CST 和 TB UTC 应该标准化到相同的值"""
        cst = SyncEngine._normalize_dt("2024-06-01 12:00:00")
        utc = SyncEngine._normalize_dt("2024-06-01T04:00:00.000Z")
        assert cst == utc

    def test_cst_later_than_utc(self):
        """CST 13:00 比 UTC 04:00(=CST 12:00) 晚"""
        cst = SyncEngine._normalize_dt("2024-06-01 13:00:00")
        utc = SyncEngine._normalize_dt("2024-06-01T04:00:00.000Z")
        assert cst > utc


# ══════════════════════════════════════════════════════════
# 3. _should_reactivate 判断逻辑测试
# ══════════════════════════════════════════════════════════

class TestShouldReactivate:
    def test_active_bug_closed_task_should_reactivate(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001", "closed_002"}
        bug = _make_bug(status="active")
        task = _make_task(status="closed_001")
        assert engine._should_reactivate(bug, task) is True

    def test_active_bug_open_task_should_not(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        bug = _make_bug(status="active")
        task = _make_task(status="pending_001")
        assert engine._should_reactivate(bug, task) is False

    def test_closed_bug_closed_task_should_not(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        bug = _make_bug(status="closed")
        task = _make_task(status="closed_001")
        assert engine._should_reactivate(bug, task) is False

    def test_resolved_bug_closed_task_should_not(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        bug = _make_bug(status="resolved")
        task = _make_task(status="closed_001")
        assert engine._should_reactivate(bug, task) is False

    def test_no_closed_status_ids_should_not(self):
        """没有识别到关闭状态列表时，无法判断，不应重新激活"""
        engine = _make_engine()
        engine._closed_status_ids = set()
        bug = _make_bug(status="active")
        task = _make_task(status="some_status")
        assert engine._should_reactivate(bug, task) is False

    def test_empty_status_bug_should_not(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        bug = _make_bug(status="")
        task = _make_task(status="closed_001")
        assert engine._should_reactivate(bug, task) is False

    def test_reactivate_closed_disabled(self):
        engine = _make_engine(reactivate_closed=False)
        # 即使状态匹配，功能被禁用也不应该走到 _should_reactivate
        # 但如果走到，_closed_status_ids 为空（因为未调用 _init_taskflow_status_map）
        engine._closed_status_ids = set()
        bug = _make_bug(status="active")
        task = _make_task(status="closed_001")
        assert engine._should_reactivate(bug, task) is False


# ══════════════════════════════════════════════════════════
# 4. _init_taskflow_status_map 任务流状态识别测试
# ══════════════════════════════════════════════════════════

class TestInitTaskflowStatusMap:
    def test_identify_closed_statuses(self):
        engine = _make_engine()
        engine.teambition.get_taskflow_status_map.return_value = {
            "id_pending": "待处理",
            "id_progress": "修复中",
            "id_resolved": "已解决",
            "id_closed": "关闭",
            "id_done": "已完成",
            "id_cancel": "已取消",
        }
        engine._init_taskflow_status_map()
        assert "id_closed" in engine._closed_status_ids
        assert "id_done" in engine._closed_status_ids
        assert "id_cancel" in engine._closed_status_ids
        # 已解决、待处理、修复中 不属于关闭状态
        assert "id_resolved" not in engine._closed_status_ids
        assert "id_pending" not in engine._closed_status_ids
        assert "id_progress" not in engine._closed_status_ids

    def test_identify_reopen_status(self):
        engine = _make_engine()
        engine.teambition.get_taskflow_status_map.return_value = {
            "id_pending": "待处理",
            "id_closed": "关闭",
            "id_reopen": "重新打开",
        }
        engine._init_taskflow_status_map()
        assert engine._reopen_status_id == "id_reopen"

    def test_fallback_to_pending_if_no_reopen(self):
        engine = _make_engine()
        engine.teambition.get_taskflow_status_map.return_value = {
            "id_pending": "待处理",
            "id_closed": "关闭",
        }
        engine._init_taskflow_status_map()
        assert engine._reopen_status_id == "id_pending"

    def test_empty_status_map(self):
        engine = _make_engine()
        engine.teambition.get_taskflow_status_map.return_value = {}
        engine._init_taskflow_status_map()
        assert engine._closed_status_ids == set()
        assert engine._reopen_status_id == ""

    def test_english_status_names(self):
        engine = _make_engine()
        engine.teambition.get_taskflow_status_map.return_value = {
            "id_todo": "Todo",
            "id_closed": "Closed",
        }
        engine._init_taskflow_status_map()
        assert "id_closed" in engine._closed_status_ids
        # "Todo" matches "todo" keyword for reopen
        assert engine._reopen_status_id == "id_todo"


# ══════════════════════════════════════════════════════════
# 5. _reactivate_task 重新激活流程测试
# ══════════════════════════════════════════════════════════

class TestReactivateTask:
    def test_dry_run_returns_reactivated(self):
        engine = _make_engine()
        engine._reopen_status_id = "id_reopen"
        bug = _make_bug()
        task = _make_task()

        result = engine._reactivate_task(bug, task, dry_run=True)

        assert result.action == SyncAction.REACTIVATED
        assert result.teambition_task_id == "tb001"
        # dry_run 不应该调用任何 API
        engine.teambition.update_task_status.assert_not_called()
        engine.teambition.add_task_comment.assert_not_called()
        engine.source.fetch_bug_detail.assert_not_called()

    def test_full_reactivation_flow(self):
        engine = _make_engine()
        engine._reopen_status_id = "id_reopen"

        bug = _make_bug()
        task = _make_task()

        full_bug = _make_bug()
        full_bug.files = []
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = [
            {"actor": "李四", "date": "2024-06-02 09:00:00",
             "comment": "新评论内容", "action": "commented"},
        ]

        result = engine._reactivate_task(bug, task, dry_run=False)

        assert result.action == SyncAction.REACTIVATED
        assert result.teambition_task_id == "tb001"

        # 验证调用顺序：1.更新状态 2.添加重新激活评论 3.同步新评论
        engine.teambition.update_task_status.assert_called_once_with(
            "tb001", "id_reopen")

        # 验证添加了重新激活评论
        comment_calls = engine.teambition.add_task_comment.call_args_list
        assert len(comment_calls) >= 1
        first_comment = comment_calls[0][0][1]
        assert "禅道重新激活" in first_comment
        assert "Bug#100" in first_comment

    def test_no_reopen_status_still_syncs_comments(self):
        """没有重新打开状态 ID 时，仍然同步评论和附件"""
        engine = _make_engine()
        engine._reopen_status_id = ""

        bug = _make_bug()
        task = _make_task()

        full_bug = _make_bug()
        full_bug.files = []
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = []

        result = engine._reactivate_task(bug, task, dry_run=False)

        assert result.action == SyncAction.REACTIVATED
        # 不应该调用状态更新
        engine.teambition.update_task_status.assert_not_called()
        # 但应该添加了重新激活评论
        engine.teambition.add_task_comment.assert_called()

    def test_status_update_failure_does_not_block(self):
        """状态更新失败不影响评论和附件同步"""
        engine = _make_engine()
        engine._reopen_status_id = "id_reopen"
        engine.teambition.update_task_status.side_effect = Exception("API error")

        bug = _make_bug()
        task = _make_task()

        full_bug = _make_bug()
        full_bug.files = []
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = []

        result = engine._reactivate_task(bug, task, dry_run=False)

        # 不应该抛异常，应该继续同步评论
        assert result.action == SyncAction.REACTIVATED
        engine.teambition.add_task_comment.assert_called()


# ══════════════════════════════════════════════════════════
# 6. _sync_bug_comments 评论过滤测试（含 cutoff_time）
# ══════════════════════════════════════════════════════════

class TestSyncBugComments:
    def test_sync_all_comments_without_cutoff(self):
        engine = _make_engine()
        engine.source.fetch_bug_comments.return_value = [
            {"actor": "张三", "date": "2024-06-01 09:00:00",
             "comment": "评论1", "action": "commented"},
            {"actor": "李四", "date": "2024-06-02 10:00:00",
             "comment": "评论2", "action": "commented"},
        ]
        engine._sync_bug_comments(_make_bug(), "task001")
        assert engine.teambition.add_task_comment.call_count == 2

    def test_sync_only_new_comments_with_cutoff(self):
        """cutoff_time 为 TB 任务更新时间（UTC），禅道评论时间为 CST"""
        engine = _make_engine()
        # cutoff = TB updated = 2024-06-01T10:00:00.000Z = CST 18:00
        cutoff = "2024-06-01T10:00:00.000Z"
        engine.source.fetch_bug_comments.return_value = [
            {"actor": "张三", "date": "2024-06-01 15:00:00",
             "comment": "旧评论（CST 15:00 < CST 18:00）", "action": "commented"},
            {"actor": "李四", "date": "2024-06-01 20:00:00",
             "comment": "新评论（CST 20:00 > CST 18:00）", "action": "commented"},
        ]
        engine._sync_bug_comments(_make_bug(), "task001", cutoff_time=cutoff)
        assert engine.teambition.add_task_comment.call_count == 1
        call_args = engine.teambition.add_task_comment.call_args[0][1]
        assert "新评论" in call_args

    def test_sync_all_if_cutoff_empty(self):
        engine = _make_engine()
        engine.source.fetch_bug_comments.return_value = [
            {"actor": "张三", "date": "2024-06-01 09:00:00",
             "comment": "评论1", "action": "commented"},
        ]
        engine._sync_bug_comments(_make_bug(), "task001", cutoff_time="")
        assert engine.teambition.add_task_comment.call_count == 1

    def test_skip_empty_comments(self):
        engine = _make_engine()
        engine.source.fetch_bug_comments.return_value = [
            {"actor": "张三", "date": "2024-06-01 09:00:00",
             "comment": "  ", "action": "commented"},
            {"actor": "李四", "date": "2024-06-01 09:00:00",
             "comment": "", "action": "commented"},
            {"actor": "王五", "date": "2024-06-01 09:00:00",
             "comment": "有效评论", "action": "commented"},
        ]
        engine._sync_bug_comments(_make_bug(), "task001")
        assert engine.teambition.add_task_comment.call_count == 1

    def test_no_comments(self):
        engine = _make_engine()
        engine.source.fetch_bug_comments.return_value = []
        engine._sync_bug_comments(_make_bug(), "task001")
        engine.teambition.add_task_comment.assert_not_called()


# ══════════════════════════════════════════════════════════
# 7. _sync_single_bug 集成测试（去重→重新激活→跳过）
# ══════════════════════════════════════════════════════════

class TestSyncSingleBugIntegration:
    def test_active_bug_with_closed_task_triggers_reactivation(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        engine._reopen_status_id = "reopen_001"

        bug = _make_bug(status="active")
        closed_task = _make_task(status="closed_001")
        engine._find_existing_task = MagicMock(return_value=closed_task)

        full_bug = _make_bug(status="active")
        full_bug.files = []
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = []

        result = engine._sync_single_bug(bug, dry_run=False)

        assert result.action == SyncAction.REACTIVATED

    def test_active_bug_with_open_task_skips(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}

        bug = _make_bug(status="active")
        open_task = _make_task(status="pending_001")
        engine._find_existing_task = MagicMock(return_value=open_task)

        result = engine._sync_single_bug(bug, dry_run=False)

        assert result.action == SyncAction.SKIPPED_DEDUP

    def test_closed_bug_with_closed_task_skips(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}

        bug = _make_bug(status="closed")
        closed_task = _make_task(status="closed_001")
        engine._find_existing_task = MagicMock(return_value=closed_task)

        result = engine._sync_single_bug(bug, dry_run=False)

        assert result.action == SyncAction.SKIPPED_DEDUP

    def test_no_existing_task_creates_new(self):
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        engine._find_existing_task = MagicMock(return_value=None)
        engine.source.check_bug_has_vlns.return_value = False

        bug = _make_bug(status="active")
        full_bug = _make_bug(status="active")
        full_bug.files = []
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = []
        engine.teambition.create_task.return_value = ("new_task_id", "VLNS-123")
        engine.teambition.get_task.return_value = None

        result = engine._sync_single_bug(bug, dry_run=False)

        assert result.action == SyncAction.CREATED
        assert result.teambition_task_id == "new_task_id"

    def test_reactivate_closed_disabled_skips_even_if_match(self):
        engine = _make_engine(reactivate_closed=False)
        # reactivate_closed=False → _closed_status_ids 保持为空
        # → _should_reactivate 返回 False

        bug = _make_bug(status="active")
        closed_task = _make_task(status="closed_001")
        engine._find_existing_task = MagicMock(return_value=closed_task)

        result = engine._sync_single_bug(bug, dry_run=False)

        assert result.action == SyncAction.SKIPPED_DEDUP

    def test_end_to_end_active_bug_closed_task_full_flow(self):
        """端到端：禅道 active + TB 关闭 → 重新打开 + 评论 + 附件全链路"""
        engine = _make_engine(sync_attachments=True)
        engine._closed_status_ids = {"closed_001"}
        engine._reopen_status_id = "reopen_001"

        bug = _make_bug(status="active", assignedTo="王五")
        closed_task = _make_task(
            status="closed_001",
            updated="2024-06-01T06:00:00.000Z",  # UTC 06:00 = CST 14:00
        )
        engine._find_existing_task = MagicMock(return_value=closed_task)

        full_bug = _make_bug(status="active", assignedTo="王五")
        full_bug.files = [{"id": 10, "title": "log.txt", "size": 2048}]
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = [
            {"actor": "张三", "date": "2024-06-01 10:00:00",
             "comment": "旧评论（CST 10:00 < cutoff CST 14:00）", "action": "commented"},
            {"actor": "李四", "date": "2024-06-01 16:00:00",
             "comment": "新评论（CST 16:00 > cutoff CST 14:00）", "action": "commented"},
        ]
        engine.source.download_attachment.return_value = MagicMock()
        engine.teambition.upload_attachment.return_value = "work_new"

        result = engine._sync_single_bug(bug, dry_run=False)

        # 1. 结果是 REACTIVATED
        assert result.action == SyncAction.REACTIVATED
        assert result.teambition_task_id == "tb001"

        # 2. 调用了状态更新（重新打开）
        engine.teambition.update_task_status.assert_called_once_with(
            "tb001", "reopen_001")

        # 3. 添加了重新激活评论 + 1 条新评论（旧评论被 cutoff 过滤）
        comment_calls = engine.teambition.add_task_comment.call_args_list
        assert len(comment_calls) == 2
        # 第一条：重新激活标记
        assert "禅道重新激活" in comment_calls[0][0][1]
        assert "Bug#100" in comment_calls[0][0][1]
        assert "王五" in comment_calls[0][0][1]
        # 第二条：新评论（旧评论被过滤掉）
        assert "新评论" in comment_calls[1][0][1]

        # 4. 同步了附件
        engine.source.download_attachment.assert_called()


# ══════════════════════════════════════════════════════════
# 7.5 VLNS 跳过分支状态校验测试
# ══════════════════════════════════════════════════════════

class TestVLNSBranchReactivation:
    def test_vlns_title_bug_triggers_reactivation(self):
        """VLNS 跳过分支：标题含 VLNS 的 active Bug 也能触发重新激活"""
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        engine._reopen_status_id = "reopen_001"

        bug = _make_bug(status="active", title="【VLNS-12345】测试Bug")
        closed_task = _make_task(status="closed_001")
        engine._find_existing_task = MagicMock(return_value=closed_task)

        full_bug = _make_bug(status="active", title="【VLNS-12345】测试Bug")
        full_bug.files = []
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = []

        result = engine._sync_single_bug(bug, dry_run=False)

        # 之前会返回 SKIPPED_DEDUP，现在应返回 REACTIVATED
        assert result.action == SyncAction.REACTIVATED
        engine.teambition.update_task_status.assert_called_once_with(
            "tb001", "reopen_001")

    def test_vlns_title_bug_closed_in_tb_stays_skipped(self):
        """VLNS 跳过分支：标题含 VLNS 但 Bug 状态非 active → 仍跳过"""
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}

        bug = _make_bug(status="closed", title="【VLNS-12345】测试Bug")
        result = engine._sync_single_bug(bug, dry_run=False)

        assert result.action == SyncAction.SKIPPED_DEDUP
        engine.teambition.update_task_status.assert_not_called()

    def test_vlns_title_bug_no_existing_task_allows_reimport(self):
        """VLNS 历史标记 + TB 任务已删除 → 允许重新导入"""
        engine = _make_engine()
        engine._closed_status_ids = {"closed_001"}
        engine._find_existing_task = MagicMock(return_value=None)
        engine.source = MagicMock()
        engine.source.check_bug_has_vlns = MagicMock(return_value=True)
        engine.source.fetch_bug_detail = MagicMock(return_value=_make_bug())
        engine._build_teambition_title = MagicMock(return_value="title")
        engine._build_note = MagicMock(return_value="note")
        engine._map_severity = MagicMock(return_value="A")
        engine._map_assignee = MagicMock(return_value=None)
        engine._build_customfields = MagicMock(return_value=[])
        engine._create_teambition_task = MagicMock(
            return_value=("task_001", "TB-001"))
        engine.teambition = MagicMock()
        engine.teambition.taskflow_map = {}
        engine.classifier = None
        engine._map_type_to_category = MagicMock(return_value="IOT-其他问题")
        engine._sync_attachments_to_task = MagicMock(return_value="")

        # dry_run → 应允许创建（不再因 VLNS 备注标记跳过）
        bug = _make_bug(status="active", title="【VLNS-12345】测试Bug")
        result = engine._sync_single_bug(bug, dry_run=True)
        assert result.action == SyncAction.CREATED


# ══════════════════════════════════════════════════════════
# 8. TeambitionClient.get_taskflow_status_map 测试
# ══════════════════════════════════════════════════════════

class TestTeambitionClientTaskflow:
    def _make_client(self):
        from src.teambition_client import TeambitionClient
        client = TeambitionClient(
            app_id="test_app", app_secret="test_secret",
            org_id="org123", project_id="proj456",
        )
        return client

    def test_cache_returned_on_second_call(self):
        client = self._make_client()
        client._request = MagicMock(return_value={"result": [
            {"id": "s1", "name": "待处理"},
            {"id": "s2", "name": "关闭"},
        ]})
        result1 = client.get_taskflow_status_map()
        assert "s1" in result1
        assert result1["s1"] == "待处理"
        # 第二次调用不应再请求 API
        call_count = client._request.call_count
        result2 = client.get_taskflow_status_map()
        assert result2 == result1
        assert client._request.call_count == call_count

    def test_fallback_to_second_endpoint(self):
        client = self._make_client()
        call_count = [0]
        def mock_request(method, path, **kwargs):
            call_count[0] += 1
            if "taskflowstatus/search" in path:
                raise Exception("not found")
            return {"result": [
                {"id": "s1", "name": "待处理"},
            ]}
        client._request = MagicMock(side_effect=mock_request)
        result = client.get_taskflow_status_map()
        assert "s1" in result

    def test_both_endpoints_fail_returns_empty(self):
        client = self._make_client()
        client._request = MagicMock(side_effect=Exception("fail"))
        result = client.get_taskflow_status_map()
        assert result == {}


# ══════════════════════════════════════════════════════════
# 9. TeambitionClient.update_task_status 测试
# ══════════════════════════════════════════════════════════

class TestUpdateTaskStatus:
    def _make_client(self):
        from src.teambition_client import TeambitionClient
        client = TeambitionClient(
            app_id="test_app", app_secret="test_secret",
            org_id="org123", project_id="proj456",
            operator_id="operator_001",
        )
        return client

    def test_v3_endpoint_succeeds(self):
        client = self._make_client()
        client._request = MagicMock(return_value={"result": {}})
        client.update_task_status("task001", "status_new")
        assert client._request.call_count == 1
        called_path = client._request.call_args[0][1]
        assert called_path == "/v3/task/task001/taskflowstatus"

    def test_request_body_has_status_id(self):
        client = self._make_client()
        client._request = MagicMock(return_value={"result": {}})
        client.update_task_status("task001", "status_new")
        call_kwargs = client._request.call_args[1]
        assert call_kwargs["json"] == {"taskflowstatusId": "status_new"}

    def test_request_failure_raises(self):
        client = self._make_client()
        client._request = MagicMock(side_effect=Exception("fail"))
        with pytest.raises(Exception):
            client.update_task_status("task001", "status_new")


# ══════════════════════════════════════════════════════════
# 10. 边界条件测试
# ══════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_reactivate_task_with_no_comments(self):
        engine = _make_engine()
        engine._reopen_status_id = "reopen_001"
        engine.source.fetch_bug_detail.return_value = _make_bug()
        engine.source.fetch_bug_comments.return_value = []

        bug = _make_bug()
        task = _make_task()
        result = engine._reactivate_task(bug, task, dry_run=False)

        assert result.action == SyncAction.REACTIVATED
        # 只有一条重新激活评论
        assert engine.teambition.add_task_comment.call_count == 1

    def test_reactivate_task_with_attachments(self):
        engine = _make_engine()
        engine._reopen_status_id = "reopen_001"

        full_bug = _make_bug()
        full_bug.files = [{"id": 1, "title": "test.png", "size": 1024}]
        engine.source.fetch_bug_detail.return_value = full_bug
        engine.source.fetch_bug_comments.return_value = []
        engine.source.download_attachment.return_value = MagicMock()
        engine.teambition.upload_attachment.return_value = "work_001"

        bug = _make_bug()
        task = _make_task()
        result = engine._reactivate_task(bug, task, dry_run=False)

        assert result.action == SyncAction.REACTIVATED
        # 应该尝试下载并上传附件
        engine.source.download_attachment.assert_called()

    def test_normalize_dt_unparseable_format(self):
        """无法解析的时间格式应降级处理"""
        result = SyncEngine._normalize_dt("not-a-date")
        # 不崩溃即可
        assert isinstance(result, str)

    def test_comment_date_without_cutoff_normalizes_correctly(self):
        """验证关键场景：禅道 CST 评论 vs TB UTC 任务更新时间"""
        engine = _make_engine()
        # TB 任务最后更新: UTC 2024-06-01T06:00:00Z = CST 14:00
        cutoff = "2024-06-01T06:00:00.000Z"
        engine.source.fetch_bug_comments.return_value = [
            # CST 10:00 < CST 14:00 → 旧评论，跳过
            {"actor": "A", "date": "2024-06-01 10:00:00",
             "comment": "旧评论", "action": "commented"},
            # CST 14:00 = CST 14:00 → 同一时间，跳过（<=）
            {"actor": "B", "date": "2024-06-01 14:00:00",
             "comment": "同时评论", "action": "commented"},
            # CST 16:00 > CST 14:00 → 新评论，同步
            {"actor": "C", "date": "2024-06-01 16:00:00",
             "comment": "新评论", "action": "commented"},
        ]
        engine._sync_bug_comments(_make_bug(), "task001", cutoff_time=cutoff)

        assert engine.teambition.add_task_comment.call_count == 1
        call_content = engine.teambition.add_task_comment.call_args[0][1]
        assert "新评论" in call_content

