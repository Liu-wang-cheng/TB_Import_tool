"""RAG 知识库 - 历史分析结果存储、检索、反馈闭环"""

import hashlib
import json
import logging
import os
import pickle
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def _safe_segment(text: str) -> str:
    try:
        import jieba
        return " ".join(jieba.lcut(text))
    except ImportError:
        return text


@dataclass
class AnalysisRecord:
    """单条分析记录"""
    id: str = ""
    task_id: str = ""
    title: str = ""
    category: str = ""
    sn: str = ""
    fw: str = ""
    severity: str = ""
    root_cause_type: str = ""
    root_cause: str = ""
    summary: str = ""
    confidence: str = ""
    evidence: list = field(default_factory=list)
    log_signature: str = ""
    pattern_matches: list = field(default_factory=list)
    status: str = "pending"
    created_at: str = ""
    feedback_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisRecord":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)


class KnowledgeBase:
    """RAG 知识库 - TF-IDF 检索 + 反馈闭环"""

    _MODEL_FILENAME = "knowledge_base_model.pkl"
    _DATA_FILENAME = "knowledge_base.jsonl"

    def __init__(self, config: dict = None):
        config = config or {}
        kb_cfg = config.get("knowledge_base", {})

        self._cache_dir = kb_cfg.get("cache_dir", "data")
        self._threshold = kb_cfg.get("similarity_threshold", 0.5)
        self._max_examples = kb_cfg.get("max_examples", 3)
        self._auto_approve_days = kb_cfg.get("auto_approve_days", 0)
        self._enabled = kb_cfg.get("enabled", False)

        self._records: list[AnalysisRecord] = []
        self._vectorizer = None
        self._tfidf_matrix = None
        self._record_ids: list[str] = []

        if self._enabled:
            self._load_data()
            self._load_model()
            self._apply_auto_approve()
            logger.info("知识库已加载 %d 条记录", len(self._records))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _data_path(self) -> Path:
        return Path(self._cache_dir) / self._DATA_FILENAME

    def _model_path(self) -> Path:
        return Path(self._cache_dir) / self._MODEL_FILENAME

    def _load_data(self):
        path = self._data_path()
        if not path.exists():
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        self._records.append(AnalysisRecord.from_dict(d))
                    except json.JSONDecodeError:
                        logger.warning("知识库第 %d 行 JSON 解析失败，跳过", line_no)
        except Exception as e:
            logger.warning("加载知识库数据失败: %s", e)

    def _load_model(self) -> bool:
        path = self._model_path()
        if not path.exists():
            if self._records:
                self._rebuild_model()
            return False

        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self._vectorizer = data.get("vectorizer")
            self._tfidf_matrix = data.get("tfidf_matrix")
            self._record_ids = data.get("record_ids", [])
            return True
        except Exception as e:
            logger.warning("加载知识库模型失败: %s", e)
            if self._records:
                self._rebuild_model()
            return False

    def reload_data(self):
        """重新从磁盘加载知识库数据（先清空再加载）。协同学习合并后调用。"""
        self._records.clear()
        self._record_ids.clear()
        self._load_data()
        logger.info("知识库数据已重新加载，当前 %d 条记录", len(self._records))

    def rebuild_model(self):
        """公开模型重建入口。协同学习合并数据后调用以更新 TF-IDF 索引。"""
        self._rebuild_model()

    def _rebuild_model(self):
        approved = [r for r in self._records if r.status == "approved"]
        if not approved:
            return

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
        except ImportError:
            logger.warning("sklearn 未安装，知识库检索不可用")
            return

        features = []
        self._record_ids = []
        for r in approved:
            feat = self._build_feature(r)
            features.append(feat)
            self._record_ids.append(r.id)

        if not features:
            return

        segmented = [_safe_segment(f) for f in features]

        self._vectorizer = TfidfVectorizer(
            max_features=5000,
            ngram_range=(1, 2),
            min_df=1,
            max_df=1.0,
            sublinear_tf=True,
        )
        self._tfidf_matrix = self._vectorizer.fit_transform(segmented)
        self._save_model()
        logger.info("知识库 TF-IDF 模型已重建，%d 条记录", len(approved))

    def _build_feature(self, record: AnalysisRecord) -> str:
        parts = [record.title, record.category, record.root_cause,
                 record.root_cause_type, record.summary, record.log_signature]
        return " ".join(p for p in parts if p)

    def _save_model(self):
        path = self._model_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "vectorizer": self._vectorizer,
                "tfidf_matrix": self._tfidf_matrix,
                "record_ids": self._record_ids,
                "saved_time": time.time(),
            }
            with open(path, "wb") as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            logger.warning("保存知识库模型失败: %s", e)

    def _apply_auto_approve(self):
        if self._auto_approve_days <= 0:
            return

        import datetime
        now = datetime.datetime.utcnow()
        changed = False

        for r in self._records:
            if r.status != "pending" or not r.created_at:
                continue
            try:
                created = datetime.datetime.fromisoformat(
                    r.created_at.replace("Z", "+00:00").replace("+00:00", "")
                )
                age_days = (now - created).days
                if age_days >= self._auto_approve_days:
                    r.status = "approved"
                    changed = True
            except (ValueError, TypeError):
                pass

        if changed:
            self._save_data()
            self._rebuild_model()

    def _save_data(self):
        path = self._data_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for r in self._records:
                    f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("保存知识库数据失败: %s", e)

    @staticmethod
    def _make_id(task_id: str, created_at: str) -> str:
        raw = f"{task_id}|{created_at}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def store_analysis(self, defect_info: dict, analysis_json: dict,
                       log_summary: dict, pattern_matches: list = None) -> str:
        if not self._enabled:
            return ""

        task_id = defect_info.get("task_id", "")
        created_at = _now_iso()
        record_id = self._make_id(task_id, created_at)

        existing = [r for r in self._records if r.task_id == task_id]
        if existing:
            latest = existing[-1]
            if latest.status == "approved":
                return latest.id

        root_cause = ""
        root_cause_type = ""
        summary = ""
        confidence = ""
        evidence = []

        if isinstance(analysis_json, dict):
            root_cause = analysis_json.get("root_cause", "")
            root_cause_type = analysis_json.get("root_cause_type", "")
            summary = analysis_json.get("summary", "")
            confidence = analysis_json.get("confidence", "")
            evidence = analysis_json.get("evidence", [])
            if not root_cause:
                root_cause = analysis_json.get("root_cause_event", "")

        log_sig = self._build_log_signature(log_summary)

        pat_ids = []
        if pattern_matches:
            for pm in pattern_matches:
                pid = getattr(pm, "pattern_id", str(pm))
                pat_ids.append(pid)

        record = AnalysisRecord(
            id=record_id,
            task_id=task_id,
            title=defect_info.get("title", ""),
            category=defect_info.get("category", ""),
            sn=defect_info.get("sn", ""),
            fw=defect_info.get("fw", ""),
            severity=defect_info.get("severity", ""),
            root_cause_type=root_cause_type,
            root_cause=root_cause,
            summary=summary,
            confidence=confidence,
            evidence=evidence[:5] if evidence else [],
            log_signature=log_sig,
            pattern_matches=pat_ids,
            status="pending",
            created_at=created_at,
        )

        self._records.append(record)
        self._append_record(record)
        logger.info("知识库已存储分析记录 [%s]", record_id)
        return record_id

    def _append_record(self, record: AnalysisRecord):
        path = self._data_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("追加知识库记录失败: %s", e)

    @staticmethod
    def _build_log_signature(log_summary: dict) -> str:
        if not log_summary:
            return ""

        key_logs = log_summary.get("key_logs", [])
        msgs = []
        for entry in key_logs[:20]:
            if isinstance(entry, dict):
                msgs.append(entry.get("msg", ""))
            else:
                msgs.append(str(entry)[:80])

        transitions = log_summary.get("nav_transitions", [])
        for t in transitions[:5]:
            msgs.append(t.get("msg", ""))

        return " ".join(msgs)[:500]

    def retrieve_similar(self, defect_info: dict, log_summary: dict,
                         top_k: int = None, status_filter: str = "approved") -> list:
        """检索相似案例。

        Args:
            status_filter: "approved" | "rejected" | None(全部)
        """
        if not self._enabled or self._vectorizer is None:
            return []

        top_k = top_k or self._max_examples
        if self._tfidf_matrix is None or self._tfidf_matrix.shape[0] == 0:
            return []

        title = defect_info.get("title", "")
        category = defect_info.get("category", "")
        # 标题重复3次以增加权重，类别重复1次
        query_parts = [title, title, title, category]

        if log_summary:
            key_logs = log_summary.get("key_logs", [])
            for entry in key_logs[:10]:
                if isinstance(entry, dict):
                    query_parts.append(entry.get("msg", ""))
                else:
                    query_parts.append(str(entry)[:80])

        query_text = " ".join(p for p in query_parts if p)
        if not query_text.strip():
            return []

        try:
            from sklearn.metrics.pairwise import cosine_similarity
        except ImportError:
            return []

        segmented = _safe_segment(query_text)
        query_vec = self._vectorizer.transform([segmented])
        scores = cosine_similarity(query_vec, self._tfidf_matrix)[0]

        # 按状态过滤
        if status_filter:
            valid_indices = [
                i for i, rid in enumerate(self._record_ids)
                if i < len(self._records) and self._records[i].status == status_filter
            ]
        else:
            valid_indices = list(range(min(len(self._records), len(self._record_ids))))

        if not valid_indices:
            return []

        scored = [(i, scores[i]) for i in valid_indices if scores[i] >= self._threshold]
        scored.sort(key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in scored[:top_k]:
            if idx < len(self._records):
                results.append(self._records[idx])
        return results

    def format_few_shot_examples(self, records: list) -> str:
        if not records:
            return ""

        parts = ["## 历史相似案例（仅作参考，勿照搬结论）\n"]
        for i, r in enumerate(records, 1):
            parts.append(f"### 参考案例 {i}: {r.title}")
            parts.append(f"- 类别: {r.category}")
            parts.append(f"- 根因类型: {r.root_cause_type}")
            parts.append(f"- 根因分析: {r.root_cause}")
            if r.summary:
                parts.append(f"- 概要: {r.summary}")
            if r.evidence:
                parts.append("- 关键证据:")
                for e in r.evidence[:3]:
                    parts.append(f"  > {e[:150]}")
            parts.append("")

        return "\n".join(parts)

    def format_rejected_examples(self, records: list) -> str:
        """格式化被拒绝的案例作为反面教材。"""
        if not records:
            return ""

        parts = ["## 常见错误分析（以下分析被确认为错误，你绝对不能重复同样的错误）\n"]
        for i, r in enumerate(records, 1):
            parts.append(f"### 错误案例 {i}: {r.title}")
            parts.append(f"- 错误根因: {r.root_cause}")
            lesson = getattr(r, 'lesson', '') or '分析偏离实际根因，已被测试工程师标记为错误'
            parts.append(f"- 教训: {lesson}")
            parts.append(f"- 为什么错: 此分析被明确拒绝，说明其推断不符合实际。你必须避免同样的推理路径。")
            parts.append("")

        return "\n".join(parts)

    def apply_feedback(self, approved_ids: list = None,
                       rejected_ids: list = None):
        if not self._enabled:
            return

        changed = False

        if approved_ids:
            for r in self._records:
                if r.id in approved_ids and r.status == "pending":
                    r.status = "approved"
                    r.feedback_at = _now_iso()
                    changed = True

        if rejected_ids:
            for r in self._records:
                if r.id in rejected_ids and r.status in ("pending", "approved"):
                    r.status = "rejected"
                    r.feedback_at = _now_iso()
                    changed = True

        if changed:
            self._save_data()
            self._rebuild_model()
            logger.info("反馈已应用，模型已更新")

    def apply_feedback_from_file(self, filepath: str = None):
        import yaml
        filepath = filepath or str(Path(self._cache_dir) / "knowledge_feedback.yaml")
        path = Path(filepath)
        if not path.exists():
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                fb = yaml.safe_load(f)
        except Exception as e:
            logger.warning("加载反馈文件失败: %s", e)
            return

        if not fb:
            return

        approved = [item["id"] for item in fb.get("approved", []) if "id" in item]
        rejected = [item["id"] for item in fb.get("rejected", []) if "id" in item]

        if approved or rejected:
            self.apply_feedback(approved, rejected)
            logger.info("从文件应用反馈: %d 批准, %d 拒绝", len(approved), len(rejected))

    @property
    def record_count(self) -> int:
        return len(self._records)

    @property
    def approved_count(self) -> int:
        return sum(1 for r in self._records if r.status == "approved")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
