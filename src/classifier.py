"""缺陷分类器：TF-IDF 相似度 + LLM 大模型 + 规则兜底"""

import difflib
import hashlib
import json
import logging
import os
import pickle
import re
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

from src.models import BUG_TYPE_NAMES

logger = logging.getLogger(__name__)


def is_mostly_chinese(text: str) -> bool:
    """判断文本是否以中文为主（中文字符占比 > 30%）"""
    if not text:
        return False
    chinese_count = sum(1 for c in text if '一' <= c <= '鿿')
    alpha_count = sum(1 for c in text if c.isalpha())
    return alpha_count > 0 and chinese_count / alpha_count > 0.3


def _sanitize_pandas_module():
    """PyInstaller excludes pandas 但传递依赖可能拉入残缺模块，
    sklearn is_pandas_df() 只捕获 ImportError 不捕获 AttributeError。
    在每次 sklearn 调用前清理残缺 pandas 模块。
    """
    if 'pandas' in sys.modules:
        try:
            import pandas as _pd
            _pd.DataFrame
        except (ImportError, AttributeError):
            del sys.modules['pandas']


# ── TF-IDF 相似度分类器（本地学习，无需外部 API）────────────────

class SimilarityClassifier:
    """基于 TF-IDF + 类别质心的本地分类器。

    训练时将同分类下所有样本的 TF-IDF 向量合并为类别质心（centroid），
    分类时计算新文本与每个类别质心的余弦相似度，取最高分对应的分类。
    同时结合 K 近邻投票作为辅助，提升鲁棒性。
    无需 GPU，无需外部 API。
    """

    _CACHE_FILENAME = "classifier_model.pkl"

    def __init__(self, config: dict):
        sim_cfg = config.get("similarity", {})
        self._enabled: bool = sim_cfg.get("enabled", True)
        self._threshold: float = sim_cfg.get("threshold", 0.3)
        self._cache_dir: str = sim_cfg.get("cache_dir", "data")
        self._samples: List[Tuple[str, str]] = []
        self._vectorizer = None
        self._tfidf_matrix = None
        self._categories: List[str] = []
        self._centroids = None           # 类别质心矩阵 (n_categories, n_features)
        self._centroid_labels: List[str] = []  # 质心对应的分类名
        self._trained = False
        self._saved_time: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def trained(self) -> bool:
        return self._trained and self._vectorizer is not None

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    @staticmethod
    def _is_mostly_chinese(text: str) -> bool:
        """判断文本是否以中文为主（中文字符占比 > 30%）"""
        return is_mostly_chinese(text)

    def _segment(self, text: str) -> str:
        """分词：中文用 jieba，英文按空格拆分，供 TF-IDF 使用"""
        if not text:
            return ""
        # 混合文本：中文部分用 jieba，英文部分按空格
        try:
            import jieba
            words = jieba.lcut(text)
            return " ".join(w for w in words if len(w) > 1 or w.isalpha())
        except ImportError:
            logger.warning("jieba 未安装，使用字符级分词，分类精度会下降")
            return " ".join(text)

    def _build_features(self, title: str, description: str = "") -> str:
        """构建 TF-IDF 特征文本：标题（权重高）+ 描述补充"""
        cleaned = self._clean_title(title)
        parts = [cleaned]
        # 描述取前 200 字符，清洗后拼接到特征中
        if description:
            desc_clean = self._clean_title(description[:200])
            if desc_clean:
                parts.append(desc_clean)
        return " ".join(parts)

    def train(self, samples: List[Tuple[str, str]]):
        """用 (标题, 分类) 对训练 TF-IDF 模型，并计算类别质心"""
        if not samples:
            return

        self._samples = samples
        _sanitize_pandas_module()
        from sklearn.feature_extraction.text import TfidfVectorizer

        logger.info("开始训练 TF-IDF 模型: %d 条样本，正在进行中文分词...",
                     len(samples))

        texts = [self._build_features(t) for t, _ in samples]
        self._categories = [c for _, c in samples]
        segmented = [self._segment(t) for t in texts]

        logger.info("分词完成，正在构建 TF-IDF 矩阵...")

        self._vectorizer = TfidfVectorizer(
            max_features=8000,
            ngram_range=(1, 2),
            min_df=1,
            max_df=0.95,
            sublinear_tf=True,
        )
        self._tfidf_matrix = self._vectorizer.fit_transform(segmented)
        self._trained = True

        # 计算类别质心
        self._compute_centroids()

        cat_counts: Dict[str, int] = {}
        for c in self._categories:
            cat_counts[c] = cat_counts.get(c, 0) + 1
        top_cats = sorted(cat_counts.items(), key=lambda x: -x[1])[:5]
        top_desc = ", ".join(f"{c}({n})" for c, n in top_cats)
        logger.info("TF-IDF 模型训练完成: %d 条样本, %d 个分类, %d 个质心, "
                     "词表 %d 词 (TOP5: %s)",
                     len(samples), len(cat_counts),
                     len(self._centroid_labels),
                     self._tfidf_matrix.shape[1], top_desc)

    def _compute_centroids(self):
        """计算每个分类的 TF-IDF 质心向量"""
        import numpy as np
        from sklearn.metrics.pairwise import normalize

        # 按分类聚合样本索引
        cat_indices: Dict[str, List[int]] = {}
        for i, cat in enumerate(self._categories):
            cat_indices.setdefault(cat, []).append(i)

        self._centroid_labels = []
        centroid_rows = []
        for cat, indices in cat_indices.items():
            self._centroid_labels.append(cat)
            # 取该分类下所有样本向量的平均值作为质心
            cat_matrix = self._tfidf_matrix[indices]
            centroid = cat_matrix.mean(axis=0)
            # 转为 dense 再压平（sparse matrix 的 mean 返回 matrix）
            if hasattr(centroid, 'A1'):
                centroid = centroid.A1
            elif hasattr(centroid, 'toarray'):
                centroid = centroid.toarray().ravel()
            centroid_rows.append(centroid)

        centroids_array = np.array(centroid_rows)
        # L2 归一化，使余弦相似度 = 点积
        self._centroids = normalize(centroids_array, norm='l2', axis=1)

    def classify(self, title: str, description: str = "",
                 threshold: float = None) -> Optional[str]:
        """对新 Bug 分类：K 近邻加权投票 + 质心辅助

        1. 计算与所有训练样本的相似度，取 K=7 个最近邻
        2. 加权投票（相似度平方作为权重，放大差异）
        3. 投票胜出者需满足最低相似度阈值
        4. 质心辅助：投票不确定时参考类别质心

        Returns:
            分类名或 None（无把握）
        """
        if not self._trained:
            return None

        text = self._build_features(title, description)
        segmented = self._segment(text)
        vec = self._vectorizer.transform([segmented])

        _sanitize_pandas_module()
        from sklearn.metrics.pairwise import cosine_similarity

        sample_scores = cosine_similarity(vec, self._tfidf_matrix)[0]

        # ── K 近邻加权投票 ──
        top_k = 7
        top_indices = sample_scores.argsort()[-top_k:][::-1]
        top_cats = [self._categories[i] for i in top_indices]
        top_scores = [float(sample_scores[i]) for i in top_indices]

        # 用相似度平方作为权重，让高相似度的样本权重更突出
        vote_weights: Dict[str, float] = {}
        for cat, score in zip(top_cats, top_scores):
            vote_weights[cat] = vote_weights.get(cat, 0.0) + score * score

        total_weight = sum(vote_weights.values())
        if not vote_weights:
            return None
        vote_winner = max(vote_weights, key=vote_weights.get)
        vote_confidence = vote_weights[vote_winner] / total_weight if total_weight > 0 else 0

        # 最高相似度必须达到阈值
        best_single_score = top_scores[0] if top_scores else 0
        thresh = threshold if threshold is not None else self._threshold

        if best_single_score < thresh:
            return None

        # 投票置信度 > 40% 或只有一个分类 → 直接返回投票结果
        if vote_confidence > 0.4 or len(vote_weights) == 1:
            return vote_winner

        # 投票不确定时参考质心
        centroid_scores = cosine_similarity(vec, self._centroids)[0]
        best_centroid_idx = int(centroid_scores.argmax())
        best_centroid_cat = self._centroid_labels[best_centroid_idx]

        if best_centroid_cat in vote_weights:
            return best_centroid_cat
        return vote_winner

    def classify_with_score(self, title: str, description: str = ""
                            ) -> Tuple[Optional[str], float]:
        """返回 (分类名, 置信度分数)"""
        if not self._trained:
            return None, 0.0

        text = self._build_features(title, description)
        segmented = self._segment(text)
        vec = self._vectorizer.transform([segmented])

        _sanitize_pandas_module()
        from sklearn.metrics.pairwise import cosine_similarity

        sample_scores = cosine_similarity(vec, self._tfidf_matrix)[0]
        best_idx = int(sample_scores.argmax())
        best_score = float(sample_scores[best_idx])

        if best_score >= self._threshold:
            return self._categories[best_idx], best_score
        return None, best_score

    def add_samples(self, samples: List[Tuple[str, str]]):
        """增量添加样本并重新训练"""
        existing_set = {(t, c) for t, c in self._samples}
        new = [(t, c) for t, c in samples if (t, c) not in existing_set]
        if new:
            self._samples.extend(new)
            self.train(self._samples)
            logger.info("TF-IDF 增量学习: 新增 %d 条样本", len(new))

    @staticmethod
    def _get_app_version() -> str:
        """读取当前应用版本号"""
        try:
            if getattr(sys, 'frozen', False):
                base = sys._MEIPASS
            else:
                base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            vpath = os.path.join(base, "VERSION")
            with open(vpath, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""

    def save(self, path: str = None):
        """保存训练好的模型到磁盘"""
        if not self._trained:
            return
        import time as _time
        path = path or os.path.join(self._cache_dir, self._CACHE_FILENAME)
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        self._saved_time = _time.time()
        data = {
            "samples": self._samples,
            "vectorizer": self._vectorizer,
            "tfidf_matrix": self._tfidf_matrix,
            "categories": self._categories,
            "threshold": self._threshold,
            "saved_time": self._saved_time,
            "centroids": self._centroids,
            "centroid_labels": self._centroid_labels,
            "app_version": self._get_app_version(),
        }
        with open(path, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("TF-IDF 模型已保存: %s (%d 条样本, %d 个质心, v%s)",
                     path, len(self._samples), len(self._centroid_labels),
                     data["app_version"])

    def _resolve_model_path(self) -> str:
        """解析模型文件路径，优先使用本地 data/，其次从 _internal/data/ 复制。"""
        import shutil
        local_path = os.path.join(self._cache_dir, self._CACHE_FILENAME)
        if os.path.exists(local_path):
            return local_path
        # 打包模式：从 _internal/data/ 原子复制到本地 data/（更新后首次运行）
        if getattr(sys, 'frozen', False):
            bundled = os.path.join(sys._MEIPASS, 'data', self._CACHE_FILENAME)
            if os.path.exists(bundled):
                os.makedirs(self._cache_dir, exist_ok=True)
                tmp_path = local_path + '.tmp'
                try:
                    shutil.copy2(bundled, tmp_path)
                    os.replace(tmp_path, local_path)
                    logger.info("从更新包复制 TF-IDF 模型: %s → %s",
                                bundled, local_path)
                except Exception:
                    # 部分复制时清理
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    return local_path
                return local_path
        return local_path

    def load(self, path: str = None) -> bool:
        """从磁盘加载模型，返回是否成功。"""
        if path is None:
            path = self._resolve_model_path()
        if not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)

            self._samples = data["samples"]
            self._vectorizer = data["vectorizer"]
            self._tfidf_matrix = data["tfidf_matrix"]
            self._categories = data["categories"]
            self._threshold = data.get("threshold", self._threshold)
            self._saved_time = data.get("saved_time", 0.0)
            # 兼容：旧模型无质心数据时重新计算
            if "centroids" in data and "centroid_labels" in data:
                self._centroids = data["centroids"]
                self._centroid_labels = data["centroid_labels"]
            else:
                self._compute_centroids()
            self._trained = True
            saved_version = data.get("app_version", "")
            logger.info("TF-IDF 模型已加载: %s (%d 条样本, %d 个质心, v%s)",
                         path, len(self._samples), len(self._centroid_labels),
                         saved_version)
            return True
        except Exception as e:
            logger.warning("TF-IDF 模型加载失败: %s", e)
            return False

    def is_stale(self, days: float = 7.0) -> bool:
        """模型缓存是否超过指定天数"""
        if not self._trained or self._saved_time <= 0:
            return True
        import time as _time
        return (_time.time() - self._saved_time) >= days * 86400

    @staticmethod
    def _clean_title(title: str) -> str:
        """去掉标题中的标签、编号、版本号、SN编码、日期时间等噪音"""
        clean = re.sub(r'【[^】]*】', '', title)
        # TB 任务编号
        clean = re.sub(r'(?:VLNS|CPAX)-\d+', '', clean)
        clean = re.sub(r'MPPFW-\d+', '', clean)
        # 禅道/Bug 编号
        clean = re.sub(r'禅道\d+', '', clean)
        clean = re.sub(r'SS\d+-\d+', '', clean)
        clean = re.sub(r'Bug\s*\d+', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'P\d+-Bug\s*', '', clean, flags=re.IGNORECASE)
        # #号前缀编号（#5555）
        clean = re.sub(r'#\d{3,}\s*', '', clean)
        # 版本号 V1.2.3 / 1.2.3（不加 [\w]* 避免 1.5倍 等被误删）
        clean = re.sub(r'V?\d+\.\d+\.\d+', '', clean)
        # 日期 YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
        clean = re.sub(r'\d{4}[-/.]\d{1,2}[-/.]\d{1,2}', '', clean)
        # 时间 HH:MM / HH：MM
        clean = re.sub(r'\d{1,2}[：:]\d{2}(?::\d{2})?', '', clean)
        # SN 编码：长字母数字串（≥8字符，无中文）
        clean = re.sub(r'\b[A-Za-z0-9\-]{10,}\b', '', clean)
        # 独立数字（≥3位，可能是编号/ID）
        clean = re.sub(r'(?<![A-Za-z])\d{3,}(?![A-Za-z])', '', clean)
        # 前导序号（如 "1. xxx" "2、xxx"，要求分隔符后有空白，避免误删 "1.5倍"）
        clean = re.sub(r'^\d{1,2}[.、)]\s+', '', clean)
        # 多余空白
        clean = re.sub(r'\s{2,}', ' ', clean)
        return clean.strip()


# ── 主分类器 ──────────────────────────────────────────────

class BugClassifier:
    """缺陷分类器

    分类优先级：
      1. TF-IDF 相似度匹配（本地学习，从 TB 已分类 Bug 中深度学习）
      2. LLM 大模型分类（API 调用，批量处理）
      3. LLM 审核（兜底前最后一次 AI 尝试）
      4. 规则兜底（Bug 类型映射）
    """

    def __init__(self, config: dict):
        cfg = config.get("classifier", {})
        # 兼容双层嵌套：config_loader 用文件名作 key
        if "classifier" in cfg and "category_descriptions" not in cfg:
            cfg = cfg["classifier"]

        # 分类描述：提供给 LLM 帮助理解分类范围，同时作为分类名来源
        self._category_desc: Dict[str, str] = cfg.get("category_descriptions", {})

        # LLM 配置
        llm_cfg = cfg.get("llm", {})
        self._llm_enabled = llm_cfg.get("enabled", False)
        self._api_key: str = llm_cfg.get("api_key", "")
        self._base_url: str = llm_cfg.get("base_url", "")
        self._model: str = llm_cfg.get("model", "deepseek-chat")
        self._timeout: int = llm_cfg.get("timeout", 30)
        self._max_retries: int = llm_cfg.get("max_retries", 1)
        self._batch_size: int = llm_cfg.get("batch_size", 10)

        provider = llm_cfg.get("provider", "")
        if provider == "deepseek" and not self._base_url:
            self._base_url = "https://api.deepseek.com/v1"
        elif provider == "qwen" and not self._base_url:
            self._base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

        # 兜底 LLM（主 LLM 全部重试失败后使用）
        fb_cfg = llm_cfg.get("fallback", {})
        self._fb_enabled: bool = fb_cfg.get("enabled", False)
        self._fb_api_key: str = fb_cfg.get("api_key", "")
        self._fb_base_url: str = fb_cfg.get("base_url",
                                             "https://open.bigmodel.cn/api/paas/v4")
        self._fb_model: str = fb_cfg.get("model", "glm-4-flash")
        self._fb_timeout: int = fb_cfg.get("timeout", 60)

        self._fallback_enabled = cfg.get("fallback_enabled", True)

        # TF-IDF 相似度分类器
        self._sim_classifier = SimilarityClassifier(cfg)

        # 有效分类列表（运行时由 set_valid_categories 设置）
        self._valid_categories: List[str] = []

        # 缓存：hash(text) → category
        self._cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()

        # HTTP 会话
        self._http = requests.Session() if self._llm_enabled and self._api_key else None

        if self._llm_enabled and not self._api_key:
            logger.warning("分类器 LLM 已启用但未配置 api_key，LLM 分类将跳过")

        # Jira 英文名 → 部门映射（用于部门过滤）
        jira_cfg = config.get("jira", {})
        self._jira_user_map: Dict[str, dict] = jira_cfg.get("user_map") or {}
        self._jira_user_map_lower: Dict[str, dict] = {}
        for name, info in self._jira_user_map.items():
            self._jira_user_map_lower[name.lower().strip()] = info

    def get_category_names(self) -> List[str]:
        """返回 YAML 中定义的所有分类名称"""
        return list(self._category_desc.keys())

    def set_valid_categories(self, categories: List[str]):
        """设置有效分类列表（用于 LLM 输出校验）"""
        self._valid_categories = list(categories)
        logger.info("分类器已加载 %d 个有效分类", len(categories))

    def train_similarity(self, samples: List[Tuple[str, str]],
                         save: bool = True):
        """用 TB 已分类 Bug 训练 TF-IDF 相似度分类器"""
        loaded = self._sim_classifier.load()
        if loaded:
            self._sim_classifier.add_samples(samples)
        else:
            self._sim_classifier.train(samples)

        if save and self._sim_classifier.trained:
            self._sim_classifier.save()

    def load_similarity_model(self) -> bool:
        """从磁盘加载 TF-IDF 模型"""
        return self._sim_classifier.load()

    def review_training_data(self, max_per_category: int = 3,
                              target_indices: range = None) -> int:
        """用 LLM 审核 TF-IDF 训练样本，剔除分类不合理的样本并重新训练。

        Args:
            max_per_category: 每个分类抽检的最大条数，0 表示全部审核。
                仅在 target_indices 为 None 时生效。
            target_indices: 指定要审核的样本索引范围（增量学习后新增样本的索引）。
                设置后忽略 max_per_category，只审核这些索引对应的样本。
        返回被剔除的样本数量。
        """
        sim = self._sim_classifier
        if not sim.trained or not self._llm_enabled or not self._api_key:
            return 0

        samples = sim._samples
        if len(samples) < 20:
            return 0

        if target_indices is not None:
            # 按索引范围精确匹配新增样本
            review_items = []
            for i in target_indices:
                if 0 <= i < len(samples):
                    title, cat = samples[i]
                    review_items.append((i, title, cat))
            logger.info("开始 AI 审核新增训练数据: %d 条新增样本...",
                        len(review_items))
        else:
            all_mode = max_per_category <= 0
            if all_mode:
                logger.info("开始 AI 全量审核训练数据: %d 条样本...", len(samples))
            else:
                logger.info("开始 AI 审核训练数据: 从 %d 条样本中抽检...", len(samples))

            import random
            by_cat: Dict[str, List[Tuple[int, Tuple[str, str]]]] = {}
            for i, (title, cat) in enumerate(samples):
                by_cat.setdefault(cat, []).append((i, (title, cat)))

            review_items = []
            for cat, items in by_cat.items():
                if all_mode:
                    for idx, (title, _) in items:
                        review_items.append((idx, title, cat))
                else:
                    random.shuffle(items)
                    for idx, (title, _) in items[:max_per_category]:
                        review_items.append((idx, title, cat))

        if not review_items:
            return 0

        # 分批发给 LLM 审核（推理模型需要足够 token 做思维链）
        categories_text = self._build_categories_prompt(
            self._valid_categories or list(self._category_desc.keys()))
        removed_indices = set()
        batch_size = getattr(self, '_batch_size', 10)
        total_batches = (len(review_items) + batch_size - 1) // batch_size

        for batch_num, batch_start in enumerate(range(0, len(review_items), batch_size), 1):
            batch = review_items[batch_start:batch_start + batch_size]
            logger.info("AI 审核进度: %d/%d (已剔除 %d 条)",
                        batch_num, total_batches, len(removed_indices))
            lines = []
            for i, (_, title, cat) in enumerate(batch, 1):
                lines.append(f"{i}. 标题: {title}  当前分类: {cat}")

            prompt = (
                "你是扫地机器人缺陷分类质检员。以下是训练数据中的样本，请检查每条的"
                "\"当前分类\"是否合理。\n\n"
                f"可用分类：\n{categories_text}\n\n"
                "样本列表：\n" + "\n".join(lines) + "\n\n"
                "对每条输出判定结果，格式为：\n"
                "序号. OK（分类正确）\n"
                "序号. 建议→正确分类名（分类不合理时给出建议）\n"
                "只输出有问题的条目也可以，没有问题的不用全部列出。"
            )

            content = self._call_llm_api(prompt, max_tokens=4000)
            if not content:
                logger.warning("AI 审核请求失败，跳过本批")
                continue

            for line in content.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'(\d+)\s*[.、)]\s*(.+)', line)
                if not m:
                    continue
                idx = int(m.group(1))
                verdict = m.group(2).strip()
                if 1 <= idx <= len(batch) and "OK" not in verdict.upper():
                    # LLM 认为这条不合理
                    orig_idx = batch[idx - 1][0]
                    orig_cat = batch[idx - 1][2]
                    # 尝试解析 LLM 建议的分类
                    suggested = verdict
                    if "→" in suggested:
                        suggested = suggested.split("→", 1)[1].strip()
                    elif "建议" in suggested:
                        suggested = re.sub(r'.*建议[：:→]?\s*', '', suggested).strip()
                    validated = self._validate_category(suggested)
                    if validated and validated != orig_cat:
                        removed_indices.add(orig_idx)
                        logger.info("  AI 审核剔除: \"%s\" 从 %s → 建议为 %s",
                                     batch[idx - 1][1][:30], orig_cat, validated)
                    elif not validated:
                        logger.debug("  AI 审核存疑(无法验证建议): \"%s\" "
                                     "(%s) → LLM建议: %s",
                                     batch[idx - 1][1][:30], orig_cat,
                                     suggested[:30])

        if not removed_indices:
            logger.info("AI 审核完成: 抽检 %d 条，全部合理", len(review_items))
            return 0

        # 剔除不合理样本，重新训练
        cleaned = [s for i, s in enumerate(samples) if i not in removed_indices]
        logger.info("AI 审核完成: 抽检 %d 条，剔除 %d 条不合理样本，"
                     "剩余 %d 条，重新训练模型",
                     len(review_items), len(removed_indices), len(cleaned))
        sim.train(cleaned)
        sim.save()
        return len(removed_indices)

    def close(self):
        if self._http:
            self._http.close()

    # ── 分类入口 ─────────────────────────────────────────

    def classify(self, bug_title: str, bug_steps: str = "",
                 bug_type: str = "", assigned_to: str = "",
                 rule_fallback_fn: Callable = None) -> str:
        """对单条 Bug 进行分类

        分类流程：TF-IDF 相似度 → LLM 分类 → LLM 审核 → 规则兜底
        返回分类名称字符串。永不抛异常，任何失败静默降级。
        """
        cache_key = self._cache_key(bug_title, bug_steps[:200], assigned_to)
        with self._cache_lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        # ── 第一层：TF-IDF 相似度匹配 ──
        if self._sim_classifier.trained:
            try:
                result = self._sim_classifier.classify(
                    bug_title, description=bug_steps)
                if result:
                    with self._cache_lock:
                        self._cache[cache_key] = result
                    return result
            except Exception as e:
                logger.debug("TF-IDF 分类异常: %s", e)

        # ── 第二层：LLM 分类 ──
        if self._llm_enabled and self._api_key:
            result = self._llm_classify_single(
                bug_title, bug_steps, bug_type, assigned_to)
            if result:
                with self._cache_lock:
                    self._cache[cache_key] = result
                return result

        # ── 第三层：LLM 审核 ──
        if self._llm_enabled and self._api_key:
            result = self._llm_review(
                bug_title, bug_steps, bug_type, assigned_to)
            if result:
                with self._cache_lock:
                    self._cache[cache_key] = result
                return result

        # ── 第四层：兜底 ──
        result = self._fallback_category(bug_type, assigned_to, rule_fallback_fn)
        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    def classify_batch(self, bugs: list) -> Dict[int, str]:
        """批量分类 Bug

        Args:
            bugs: [{"id": int, "title": str, "steps": str,
                    "type": str, "assigned_to": str}, ...]
        Returns:
            {bug_id: category_name}
        """
        results: Dict[int, str] = {}
        need_llm: list = list(bugs)

        # 第一层：TF-IDF 相似度匹配
        sim_count = 0
        if self._sim_classifier.trained:
            still_need = []
            for bug in need_llm:
                try:
                    sim_result = self._sim_classifier.classify(
                        bug.get("title", ""),
                        description=bug.get("steps", ""))
                    if sim_result:
                        results[bug["id"]] = sim_result
                        sim_count += 1
                        ck = self._cache_key(
                            bug.get("title", ""),
                            (bug.get("steps", "") or "")[:200],
                            bug.get("assigned_to", ""))
                        with self._cache_lock:
                            self._cache[ck] = sim_result
                    else:
                        still_need.append(bug)
                except Exception:
                    still_need.append(bug)
            need_llm = still_need

        if not need_llm:
            logger.info("批量分类: TF-IDF 命中 %d/%d 条",
                         sim_count, len(bugs))
            return results

        # 第二层：LLM 批量分类
        llm_count = 0
        if self._llm_enabled and self._api_key and need_llm:
            llm_results = self._llm_classify_batch(need_llm)
            results.update(llm_results)
            llm_count = len(llm_results)
            need_llm = [b for b in need_llm if b["id"] not in llm_results]

        # 未分类的保持为空（由调用者在 _sync_single_bug 中用规则兜底）
        logger.info("批量分类完成: TF-IDF %d, LLM %d, 待兜底 %d",
                     sim_count, llm_count, len(need_llm))
        return results

    # ── 兜底分类 ─────────────────────────────────────────

    def _fallback_category(self, bug_type: str, assigned_to: str,
                           rule_fallback_fn: Callable = None) -> str:
        """TF-IDF 和 LLM 都未命中时，直接用 Bug 类型映射兜底（部门前缀兜底已移除）"""
        if rule_fallback_fn:
            return rule_fallback_fn(bug_type, assigned_to)
        return "应用-其他问题"

    # ── LLM 单条分类 ────────────────────────────────────

    def _build_categories_prompt(self, valid: List[str]) -> str:
        """构建带描述的分类列表文本，供 LLM prompt 使用"""
        lines = []
        for cat in valid:
            desc = self._category_desc.get(cat, "")
            if desc:
                lines.append(f"  - {cat}: {desc}")
            else:
                lines.append(f"  - {cat}")
        return "\n".join(lines)

    def _llm_classify_single(self, bug_title: str, bug_steps: str,
                             bug_type: str, assigned_to: str) -> Optional[str]:
        if not self._valid_categories:
            return None

        valid = self._valid_categories

        type_name = BUG_TYPE_NAMES.get(bug_type, bug_type)
        categories_text = self._build_categories_prompt(valid)

        is_english = not is_mostly_chinese(
            bug_title + " " + (bug_steps or "")[:200])
        if is_english:
            prompt = (
                "You are an expert classifier for robot vacuum cleaner software defects. "
                "Select the most appropriate category from the list below based on the bug info.\n"
                "The categories are in Chinese - output the exact Chinese category name.\n\n"
                f"Available categories:\n{categories_text}\n\n"
                f"Bug info:\n"
                f"- Title: {bug_title}\n"
                f"- Description: {(bug_steps or '')[:500]}\n"
                f"- Type: {type_name}\n"
                f"- Assignee: {assigned_to}\n\n"
                "Output ONLY the category name in Chinese, nothing else."
            )
        else:
            prompt = (
                "你是扫地机器人软件缺陷分类专家。根据Bug信息从分类列表中选择最合适的一个。\n\n"
                f"可用分类：\n{categories_text}\n\n"
                f"Bug信息：\n"
                f"- 标题：{bug_title}\n"
                f"- 描述：{(bug_steps or '')[:500]}\n"
                f"- 类型：{type_name}\n"
                f"- 指派：{assigned_to}\n\n"
                "只输出分类名称，不要输出其他内容。"
            )

        content = self._call_llm_api(prompt)
        if not content:
            return None
        return self._validate_category(content.strip())

    # ── LLM 审核 ─────────────────────────────────────────

    def _llm_review(self, bug_title: str, bug_steps: str,
                    bug_type: str, assigned_to: str) -> Optional[str]:
        """LLM 审核：TF-IDF 和首次 LLM 都未命中时的最后 AI 尝试"""
        if not self._valid_categories:
            return None

        valid = self._valid_categories
        candidates = [c for c in valid if "其他" not in c and c != "未分类缺陷"]
        if not candidates:
            return None

        type_name = BUG_TYPE_NAMES.get(bug_type, bug_type)
        categories_text = self._build_categories_prompt(candidates)

        is_english = not is_mostly_chinese(
            bug_title + " " + (bug_steps or "")[:200])
        if is_english:
            prompt = (
                "You are a strict defect classification reviewer for robot vacuum cleaner software. "
                "The following bug has failed similarity matching and AI classification, "
                "and is about to be put into a fallback \"other\" category. "
                "Please carefully review and select the most appropriate non-fallback category.\n"
                "The categories are in Chinese - output the exact Chinese category name.\n\n"
                f"Candidate categories:\n{categories_text}\n\n"
                f"Bug info:\n"
                f"- Title: {bug_title}\n"
                f"- Description: {(bug_steps or '')[:500]}\n"
                f"- Type: {type_name}\n"
                f"- Assignee: {assigned_to}\n\n"
                "If confident, output ONLY the category name in Chinese. "
                "If truly unclassifiable, output \"unable to classify\"."
            )
        else:
            prompt = (
                "你是一个严格的扫地机器人缺陷分类审核员。以下Bug信息已经过相似度匹配和AI分类均未能归类，"
                "即将被归入\"其他问题\"兜底分类。请仔细审核，从非兜底分类中选一个最合适的。\n\n"
                f"可选分类：\n{categories_text}\n\n"
                f"Bug信息：\n"
                f"- 标题：{bug_title}\n"
                f"- 描述：{(bug_steps or '')[:500]}\n"
                f"- 类型：{type_name}\n"
                f"- 指派：{assigned_to}\n\n"
                "如果有足够把握，只输出分类名称；如果确实无法归类，输出\"无法归类\"。"
            )

        content = self._call_llm_api(prompt)
        if not content:
            return None
        content = content.strip()
        first_line = content.splitlines()[0]
        if "无法归类" in first_line or "unable to classify" in first_line.lower():
            return None
        return self._validate_category(content, candidates)

    # ── LLM 批量分类 ────────────────────────────────────

    def _llm_classify_batch(self, bugs: list) -> Dict[int, str]:
        if not self._valid_categories:
            return {}
        results: Dict[int, str] = {}
        valid = self._valid_categories
        categories_text = self._build_categories_prompt(valid)

        for i in range(0, len(bugs), self._batch_size):
            batch = bugs[i:i + self._batch_size]
            bug_lines = []
            has_english = False
            for idx, bug in enumerate(batch, 1):
                type_name = BUG_TYPE_NAMES.get(
                    bug.get("type", ""), bug.get("type", ""))
                steps = (bug.get("steps", "") or "")[:300]
                title = bug.get("title", "")
                if not is_mostly_chinese(title):
                    has_english = True
                bug_lines.append(
                    f"{idx}. [标题: {title}, "
                    f"描述: {steps}, "
                    f"类型: {type_name}, "
                    f"指派: {bug.get('assigned_to', '')}]"
                )

            if has_english:
                prompt = (
                    "You are an expert classifier for robot vacuum cleaner software defects. "
                    "Select the most appropriate category from the list for each bug.\n"
                    "The categories are in Chinese - output the exact Chinese category name.\n\n"
                    f"Available categories:\n{categories_text}\n\n"
                    "Bug list:\n" + "\n".join(bug_lines) + "\n\n"
                    "Output by number, one per line, category name only:\n"
                    "1. CategoryName\n2. CategoryName"
                )
            else:
                prompt = (
                    "你是扫地机器人软件缺陷分类专家。根据Bug信息从分类列表中分别选择最合适的一个分类。\n\n"
                    f"可用分类：\n{categories_text}\n\n"
                    "Bug列表：\n" + "\n".join(bug_lines) + "\n\n"
                    "按序号输出分类，每行一个，只输出分类名称。格式：\n"
                    "1. 分类名称\n2. 分类名称"
                )

            content = self._call_llm_api(prompt)
            if not content:
                continue

            for line in content.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'(\d+)\s*[.、)]\s*(.+)', line)
                if m:
                    idx = int(m.group(1))
                    cat = self._validate_category(m.group(2).strip(), valid)
                    if cat and 1 <= idx <= len(batch):
                        bug = batch[idx - 1]
                        results[bug["id"]] = cat
                        ck = self._cache_key(
                            bug.get("title", ""),
                            bug.get("steps", "")[:200],
                            bug.get("assigned_to", ""))
                        with self._cache_lock:
                            self._cache[ck] = cat

        return results

    # ── LLM API 调用 ────────────────────────────────────

    def _call_llm_api(self, prompt: str, max_tokens: int = 400) -> Optional[str]:
        if not self._http:
            return None

        result = self._call_primary_llm(prompt, max_tokens)
        if result is not None:
            return result

        # 主 LLM 全部重试失败，尝试兜底 LLM
        if self._fb_enabled and self._fb_api_key:
            logger.info("主 LLM 失败，切换到兜底模型 %s", self._fb_model)
            return self._call_fallback_llm(prompt, max_tokens)

        return None

    def _call_primary_llm(self, prompt: str, max_tokens: int) -> Optional[str]:
        url = f"{self._base_url.rstrip('/')}/chat/completions"
        messages = [
            {"role": "system", "content": (
                "You are a helpful assistant for robot vacuum defect classification. "
                "Follow the user's format exactly. "
                "When categories are in Chinese, output the exact Chinese category name."
            )},
            {"role": "user", "content": prompt},
        ]
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._http.post(
                    url, headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self._timeout,
                )
                if resp.status_code != 200:
                    logger.warning("LLM API 返回 HTTP %d: %s",
                                   resp.status_code, resp.text[:200])
                    continue
                data = resp.json()
                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {})
                content = msg.get("content", "")
                finish_reason = choice.get("finish_reason", "")
                # 推理模型（如 MiniMax-M2.7）: content 为空时检查 reasoning_content
                if not content:
                    reasoning = msg.get("reasoning_content", "")
                    if reasoning and finish_reason == "length":
                        logger.debug("LLM 推理耗尽 token (reasoning %d 字符), "
                                     "finish_reason=length", len(reasoning))
                        payload["max_tokens"] = min(
                            payload.get("max_tokens", 400) * 2, 8000)
                        continue
                if content:
                    if finish_reason == "length":
                        logger.debug("LLM 输出被截断 (finish_reason=length), "
                                     "内容长度 %d", len(content))
                    return content
                logger.warning("LLM 返回空内容, finish_reason=%s, "
                               "响应: %s", finish_reason,
                               json.dumps(data, ensure_ascii=False)[:300])
            except requests.exceptions.Timeout:
                logger.warning("LLM API 超时 (%ds), 第 %d 次", self._timeout, attempt)
            except Exception as e:
                logger.warning("LLM API 调用失败: %s", e)
            if attempt < self._max_retries:
                time.sleep(2 ** attempt)

        return None

    def _call_fallback_llm(self, prompt: str, max_tokens: int) -> Optional[str]:
        url = f"{self._fb_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._fb_api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "model": self._fb_model,
            "messages": [
                {"role": "system", "content": (
                    "You are a helpful assistant for robot vacuum defect classification. "
                    "Follow the user's format exactly. "
                    "When categories are in Chinese, output the exact Chinese category name."
                )},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        try:
            resp = self._http.post(
                url, headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self._fb_timeout,
            )
            if resp.status_code != 200:
                logger.warning("兜底 LLM 返回 HTTP %d: %s",
                               resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                logger.info("兜底 LLM (%s) 调用成功", self._fb_model)
                return content
            logger.warning("兜底 LLM 返回空内容")
        except requests.exceptions.Timeout:
            logger.warning("兜底 LLM 也超时 (%ds)", self._fb_timeout)
        except Exception as e:
            logger.warning("兜底 LLM 调用失败: %s", e)
        return None

    # ── 工具方法 ──────────────────────────────────────────

    def _validate_category(self, raw: str,
                           valid: List[str] = None) -> Optional[str]:
        """校验 LLM 输出是否为有效分类名"""
        if not raw:
            return None
        raw = raw.strip().strip('"').strip("'")
        check_list = valid or self._valid_categories

        if check_list:
            if raw in check_list:
                return raw
            for cat in check_list:
                ratio = difflib.SequenceMatcher(None, raw, cat).ratio()
                if ratio >= 0.8:
                    return cat
            return None

        # 没有有效列表时，检查分类描述
        if raw in self._category_desc:
            return raw
        return None

    @staticmethod
    def _cache_key(title: str, steps_prefix: str,
                   assigned_to: str = "") -> str:
        text = (title + "||" + steps_prefix + "||" + assigned_to).lower()
        return hashlib.md5(text.encode("utf-8")).hexdigest()
