"""测试评论附件同步改造：占位符 + 单一上传通道 + file_id 去重。

覆盖场景：
- _replace_comment_media_with_placeholders: img/video 标签 → 占位符
- _submit_bug_comments: 占位符 → 真实文件名（成功/失败两种）
- _sync_attachments: 评论附件 + 重现步骤内联图片按 file_id 去重
- _sync_attachments: 视频附件保留禅道真实文件名
"""
from unittest.mock import MagicMock

import pytest

from src.sync_engine import SyncEngine
from src.models import ZentaoBug, AttachmentFile


def make_engine():
    """构造仅带必要属性的 SyncEngine（绕过 __init__ 的网络认证）"""
    e = SyncEngine.__new__(SyncEngine)
    e.severity_labels = {}
    e.cf_ids = {}
    e.attachment_retries = 1
    e.source = MagicMock()
    e.teambition = MagicMock()
    return e


class TestPlaceholderReplacement:
    """_replace_comment_media_with_placeholders"""

    def test_image_becomes_placeholder(self):
        engine = make_engine()
        file_ids = {}
        html = '<img src="/file-read-15411.html">'
        result = engine._replace_comment_media_with_placeholders(html, file_ids)
        assert "__ATTACH_15411__" in result
        assert file_ids == {"15411": "image"}

    def test_video_becomes_placeholder(self):
        engine = make_engine()
        file_ids = {}
        html = '<video src="/file/download/20001"></video>'
        result = engine._replace_comment_media_with_placeholders(html, file_ids)
        assert "__ATTACH_20001__" in result
        assert file_ids == {"20001": "video"}

    def test_video_source_tag_extracted(self):
        engine = make_engine()
        file_ids = {}
        # 禅道文件 URL 格式：file-read-{id}.html 或 /file/download/{id}
        html = '<video><source src="/file-read-300.html"></video>'
        result = engine._replace_comment_media_with_placeholders(html, file_ids)
        assert "__ATTACH_300__" in result
        assert file_ids == {"300": "video"}

    def test_same_file_id_dedup_across_comments(self):
        """同一 file_id 在多次调用中只记录一次"""
        engine = make_engine()
        file_ids = {}
        html = '<img src="/file-read-15411.html">'
        engine._replace_comment_media_with_placeholders(html, file_ids)
        engine._replace_comment_media_with_placeholders(html, file_ids)
        assert file_ids == {"15411": "image"}

    def test_multiple_distinct_file_ids(self):
        engine = make_engine()
        file_ids = {}
        html = (
            '<img src="/file-read-100.html">'
            '<img src="/file-read-200.html">'
        )
        engine._replace_comment_media_with_placeholders(html, file_ids)
        assert file_ids == {"100": "image", "200": "image"}

    def test_no_media_returns_original(self):
        engine = make_engine()
        file_ids = {}
        result = engine._replace_comment_media_with_placeholders(
            "纯文本评论", file_ids
        )
        assert result == "纯文本评论"
        assert file_ids == {}


class TestSubmitBugComments:
    """_submit_bug_comments 占位符回填"""

    def test_placeholder_replaced_with_real_filename(self):
        engine = make_engine()
        uploaded = {
            "15411": ("work-1", "image_15411.png", "http://tb/img"),
        }
        processed = [
            ("[图片: __ATTACH_15411__]", "李建豪", "2026-06-22 10:01:35"),
        ]
        engine._submit_bug_comments("task-1", processed, uploaded)
        content = engine.teambition.add_task_comment.call_args[0][1]
        assert "image_15411.png" in content
        assert "__ATTACH_15411__" not in content
        assert "李建豪" in content

    def test_video_placeholder_keeps_real_name(self):
        """视频附件保留禅道真实文件名（如 OTA...mp4）"""
        engine = make_engine()
        uploaded = {
            "20001": ("work-2", "OTA成功后几分钟内.mp4", "http://tb/v"),
        }
        processed = [("[视频: __ATTACH_20001__]", "张三", "")]
        engine._submit_bug_comments("task-1", processed, uploaded)
        content = engine.teambition.add_task_comment.call_args[0][1]
        assert "OTA成功后几分钟内.mp4" in content

    def test_failed_upload_placeholder_replaced(self):
        """未上传成功的 file_id 占位符 → '上传失败'"""
        engine = make_engine()
        uploaded = {}  # 空表示没上传成功
        processed = [("[图片: __ATTACH_99999__]", "李四", "")]
        engine._submit_bug_comments("task-1", processed, uploaded)
        content = engine.teambition.add_task_comment.call_args[0][1]
        assert "上传失败" in content
        assert "__ATTACH_99999__" not in content

    def test_mixed_success_and_failure(self):
        engine = make_engine()
        uploaded = {"100": ("w", "image_100.png", "u")}
        processed = [
            ("[图片: __ATTACH_100__] 和 [图片: __ATTACH_200__]", "actor", "date"),
        ]
        engine._submit_bug_comments("task-1", processed, uploaded)
        content = engine.teambition.add_task_comment.call_args[0][1]
        assert "image_100.png" in content
        assert "上传失败" in content


class TestSyncAttachmentsDedup:
    """_sync_attachments 接收 comment_file_ids 并按 file_id 去重"""

    def _make_bug(self, steps=""):
        return ZentaoBug(
            id=1234, title="测试bug", steps=steps, files=[],
        )

    def test_comment_file_id_dedup_with_inline(self):
        """评论 file_id 与重现步骤内联 file_id 重叠时只上传一次"""
        engine = make_engine()
        engine.source.download_image.return_value = AttachmentFile(
            filename="image_15411.png", data=b"\x89PNG..."
        )
        engine.teambition.upload_attachment.return_value = ("work-1", "url")

        bug = self._make_bug(steps='<img src="/file-read-15411.html">')
        uploaded = engine._sync_attachments(
            bug, "task-1",
            comment_file_ids={"15411": "image"},
        )
        # 只调用了一次 download_image（去重生效）
        assert engine.source.download_image.call_count == 1
        assert "15411" in uploaded
        assert uploaded["15411"][1] == "image_15411.png"

    def test_comment_video_uses_real_filename(self):
        """视频附件走 download_attachment，保留禅道返回的真实文件名"""
        engine = make_engine()
        engine.source.download_attachment.return_value = AttachmentFile(
            filename="OTA日志.mp4", data=b"\x00\x00...",
        )
        engine.teambition.upload_attachment.return_value = ("work-2", "url")

        bug = self._make_bug()
        uploaded = engine._sync_attachments(
            bug, "task-1",
            comment_file_ids={"20001": "video"},
        )
        engine.source.download_attachment.assert_called_once_with(20001)
        assert uploaded["20001"][1] == "OTA日志.mp4"

    def test_existing_filenames_skip_comment_attachment(self):
        """跨次同步：评论附件实际文件名已在 existing_filenames 中则跳过"""
        engine = make_engine()
        engine.source.download_image.return_value = AttachmentFile(
            filename="image_15411.png", data=b"\x89PNG...",
        )

        bug = self._make_bug()
        uploaded = engine._sync_attachments(
            bug, "task-1",
            existing_filenames={"image_15411.png"},
            comment_file_ids={"15411": "image"},
        )
        # 不应该调用 upload_attachment
        engine.teambition.upload_attachment.assert_not_called()
        assert uploaded == {}

    def test_bug_files_and_comment_distinct_both_uploaded(self):
        """bug.files 的真实附件 + 评论附件互不干扰"""
        engine = make_engine()
        engine.source.download_attachment.return_value = AttachmentFile(
            filename="real.jpg", data=b"...",
        )
        engine.source.download_image.return_value = AttachmentFile(
            filename="image_200.png", data=b"\x89PNG...",
        )
        engine.teambition.upload_attachment.return_value = ("w", "u")

        bug = ZentaoBug(
            id=1, title="t", steps="",
            files=[{"id": "100", "title": "real.jpg", "size": 1024}],
        )
        uploaded = engine._sync_attachments(
            bug, "task-1",
            comment_file_ids={"200": "image"},
        )
        assert "100" in uploaded  # bug.files
        assert "200" in uploaded  # 评论附件
        assert uploaded["100"][1] == "real.jpg"
        assert uploaded["200"][1] == "image_200.png"

    def test_returns_uploaded_map(self):
        """_sync_attachments 返回 uploaded map 供评论回填"""
        engine = make_engine()
        engine.source.download_image.return_value = AttachmentFile(
            filename="image_42.png", data=b"\x89PNG...",
        )
        engine.teambition.upload_attachment.return_value = ("w-id", "url")
        bug = self._make_bug()
        result = engine._sync_attachments(
            bug, "task-1",
            comment_file_ids={"42": "image"},
        )
        assert isinstance(result, dict)
        assert result["42"][0] == "w-id"
        assert result["42"][1] == "image_42.png"
