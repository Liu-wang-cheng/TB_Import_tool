"""同步引擎：核心同步逻辑"""

import difflib
import html
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from src.log_analysis_integration import LogAnalysisIntegration
from src.models import (BUG_TYPE_NAMES, SEVERITY_NAMES, SyncAction, SyncResult,
                        SyncStats, ZentaoBug)
from src.source_client import SourceClient
from src.teambition_client import TeambitionClient
from src.utils import extract_department_prefix, resolve_assigned_to

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, config: dict, source: SourceClient,
                 teambition: TeambitionClient, dingtalk_bot=None):
        self.config = config
        self.source = source
        self.teambition = teambition
        self.dingtalk_bot = dingtalk_bot

        sync_cfg = config.get("sync", {})
        self.source_tag_in_tb = sync_cfg.get("zentao_tag_in_tb", "【禅道{bug_id}】")
        self.tb_tag_in_zentao = sync_cfg.get("teambition_tag_in_zentao", "【TB-{task_id}】")
        self.dedup_threshold = sync_cfg.get("dedup_threshold", 0.8)
        self.batch_size = sync_cfg.get("batch_size", 20)
        self.sync_attachments = sync_cfg.get("sync_attachments", True)
        self.max_attachment_size_mb = sync_cfg.get("max_attachment_size_mb", 50)
        self.dry_run = sync_cfg.get("dry_run", False)
        self.attachment_retries = sync_cfg.get("attachment_retries", 3)
        self.reactivate_closed = sync_cfg.get("reactivate_closed", False)
        self.extraction_enabled = sync_cfg.get("extraction", {}).get("enabled", True)

        tb_cfg = config.get("teambition", {})
        self.user_mapping: Dict[str, str] = tb_cfg.get("user_mapping", {})
        self.severity_map: Dict[str, str] = tb_cfg.get("severity_map", {
            "1": "S", "2": "A", "3": "B", "4": "C",
        })
        self.type_category_map: Dict[str, str] = tb_cfg.get("type_category_map", {})
        self.assignee_category_map: Dict[str, str] = tb_cfg.get("assignee_category_map", {})
        self.default_reproduction = tb_cfg.get("default_reproduction", "中概率")
        self.cf_ids: dict = tb_cfg.get("customfield_ids", {})

        # project_name: 兼容 configs/ 多文件和旧 config.yaml 两种格式
        # 优先使用 belong_project_value（TB所属项目字段值），其次 project_name，最后 project.name
        project_cfg = tb_cfg.get("project", {})
        self.project_name: str = (
            tb_cfg.get("belong_project_value", "")
            or tb_cfg.get("project_name", "")
            or project_cfg.get("name", "")
        )

        zt_cfg = config.get("zentao", {})
        self.module_filter: str = zt_cfg.get("filters", {}).get("module_filter", "")

        # AI 缺陷分类器（可选）
        classifier_cfg = config.get("classifier", {})
        # 兼容双层嵌套：config_loader 用文件名作 key
        if "classifier" in classifier_cfg and "enabled" not in classifier_cfg:
            classifier_cfg = classifier_cfg["classifier"]
        if classifier_cfg.get("enabled", False):
            from src.classifier import BugClassifier
            self.classifier = BugClassifier(config)
            logger.info("AI 缺陷分类器已启用")
        else:
            self.classifier = None

        # 从 zentao.assigned_to 配置构建 "姓名→缺陷分类" 映射
        # "IOT-陈斌" → {"陈斌": "IOT-其他问题", "IOT-陈斌": "IOT-其他问题"}
        self._assignee_name_category: Dict[str, str] = {}
        assigned_to_list = zt_cfg.get("filters", {}).get("assigned_to", [])
        if assigned_to_list and self.assignee_category_map:
            if isinstance(assigned_to_list, str):
                assigned_to_list = [assigned_to_list]
            for entry in assigned_to_list:
                if "-" in entry:
                    prefix, name = entry.split("-", 1)
                    category = self.assignee_category_map.get(prefix.strip())
                    if category:
                        self._assignee_name_category[entry] = category
                        self._assignee_name_category[name.strip()] = category

        # Jira 英文名映射：{"jiansen shi": {"department": "IOT", "tb_user": "陈斌"}}
        jira_cfg = config.get("jira", {})
        self._jira_user_map: Dict[str, dict] = jira_cfg.get("user_map") or {}
        # 预构建小写索引，支持大小写不敏感匹配
        self._jira_user_map_lower: Dict[str, dict] = {}
        for name, info in self._jira_user_map.items():
            self._jira_user_map_lower[name.lower().strip()] = info

        # TB 任务流状态缓存（run() 启动时初始化）
        self._closed_status_ids: set = set()
        self._reopen_status_id: str = ""

        # 拼音→成员ID 索引（preload_members 后构建）
        self._pinyin_member_index: Dict[str, str] = {}
        # 同步过程中动态学习：源平台经办人 → TB executorId
        self._learned_assignee_map: Dict[str, str] = {}

        # SN 模式学习缓存（按 TB 项目隔离，持久化到本地）
        self._tb_project_id: str = teambition.project_id or ""
        self._sn_patterns: list = None
        self._sn_patterns_loaded: bool = False
        if self._tb_project_id:
            self._load_sn_patterns_for_project(self._tb_project_id)

        # AI 日志分析集成（可选）
        ai_cfg = config.get("ai_analysis", {})
        self.ai_analysis_enabled = ai_cfg.get("enabled", False)
        self.log_analyzer: Optional[LogAnalysisIntegration] = None
        if self.ai_analysis_enabled:
            try:
                web_cookies = tb_cfg.get("web_cookies", {})
                self.log_analyzer = LogAnalysisIntegration(
                    tb_client=teambition,
                    drc_server=ai_cfg.get("drc_server"),
                    drc_username=ai_cfg.get("drc_username"),
                    drc_password=ai_cfg.get("drc_password"),
                    drc_model=ai_cfg.get("drc_model") or tb_cfg.get("project", {}).get("name"),
                    zentao_client=source,
                    web_cookies=web_cookies if web_cookies else None,
                )
                logger.info("AI 日志分析集成已启用")
            except Exception as e:
                logger.warning("AI 日志分析初始化失败: %s", e)
                self.ai_analysis_enabled = False

    def run(self, dry_run: bool = False, progress_callback=None) -> SyncStats:
        """执行同步

        progress_callback: 可选回调 (current, total, message)，用于 GUI 进度条
        """
        start_time = time.time()
        stats = SyncStats()
        is_dry_run = dry_run or self.dry_run
        if is_dry_run:
            logger.info("===== 试运行模式（不会实际创建/修改）=====")

        # 获取禅道 Bug 列表
        if progress_callback:
            progress_callback(0, 0, "正在获取禅道 Bug 列表...")
        filters = self.config.get("zentao", {}).get("filters", {})
        assigned_to = resolve_assigned_to(filters, self.source.account)
        bugs = self.source.fetch_all_bugs(
            product_id=filters.get("product_id"),
            project_id=filters.get("project_id"),
            statuses=filters.get("statuses"),
            date_from=filters.get("date_from"),
            date_to=filters.get("date_to"),
            assigned_to=assigned_to,
        )

        # 模块过滤预解析：通过模块API将名称解析为ID集合，
        # 避免在 _sync_single_bug 中为不匹配的 Bug 浪费一次详情请求
        self._module_id_set: Optional[set] = None
        if self.module_filter and bugs:
            mf = self.module_filter.strip()
            if mf.isdigit():
                bugs = [b for b in bugs if str(b.module) == mf]
                logger.info("模块ID '%s' 预过滤后剩余 %d 条", mf, len(bugs))
            elif filters.get("product_id"):
                t0 = time.time()
                self._module_id_set = self.source.resolve_module_ids_by_name(
                    int(filters["product_id"]), mf)
                # 区分空集合与 None：
                #   set (含空) = API成功 → 用此集合过滤
                #   None       = API不可用 → 留待 _sync_single_bug 逐条回退
                if self._module_id_set is not None:
                    before = len(bugs)
                    bugs = [b for b in bugs if str(b.module) in self._module_id_set]
                    logger.info(
                        "模块名称 '%s' 命中 %d 个ID，预过滤 %d→%d 条 (耗时 %.2fs)",
                        mf, len(self._module_id_set), before, len(bugs),
                        time.time() - t0)
                else:
                    logger.warning(
                        "模块API不可用，回退到 _sync_single_bug 中逐条 fetch_detail "
                        "比对 moduleName（每条约 0.6s 延迟）")

        stats.total = len(bugs)
        logger.info("待处理 Bug: %d 条", stats.total)

        # 打印各指派人的 Bug 数量统计
        if bugs:
            assignee_counter = Counter(b.assignedTo for b in bugs)
            summary = ", ".join(
                f"{name or '(未指派)'}({count})"
                for name, count in assignee_counter.most_common()
            )
            logger.info("指派人统计: %s", summary)

        if progress_callback:
            progress_callback(0, max(stats.total, 1),
                              f"待处理 {stats.total} 条 Bug")

        # 检测缺陷类型和自定义字段（试运行也执行，用于验证）
        self.teambition.get_defect_scenariofieldconfig_id()
        self._detect_custom_fields()

        # 一次性预加载 TB 成员索引，避免每条 Bug 都翻页搜索
        if progress_callback:
            progress_callback(0, max(stats.total, 1), "正在加载 Teambition 成员列表...")
        self.teambition.preload_members()

        # 构建拼音索引（英文名自动匹配 TB 中文成员）
        self._build_pinyin_member_index()

        # 从当前 Bug 列表 + 已有 TB 任务中预学习经办人映射
        self._preload_assignee_mapping(bugs)

        # 初始化 TB 任务流状态映射（用于状态对比日志和重新激活）
        self._init_taskflow_status_map()

        # 初始化 AI 分类器
        if self.classifier:
            categories = self.classifier.get_category_names()
            if categories:
                self.classifier.set_valid_categories(categories)
                logger.info("分类器已加载 %d 个分类", len(categories))
            else:
                logger.warning("分类器无有效分类，已禁用")
                self.classifier = None

        # 学习 SN 格式模式（从 TB 已有缺陷任务中）
        if self.extraction_enabled:
            self._learn_sn_patterns(progress_callback)

        # 从 TB 已分类任务中学习训练 TF-IDF 模型
        if self.classifier:
            self._train_similarity_classifier(progress_callback)

        for i, bug in enumerate(bugs):
            if progress_callback:
                progress_callback(i, max(stats.total, 1),
                                  f"处理 [{i + 1}/{stats.total}] Bug#{bug.id}")
            if i > 0 and i % self.batch_size == 0:
                logger.info("已处理 %d/%d，暂停3秒...", i, stats.total)
                time.sleep(3)

            result = self._sync_single_bug(bug, is_dry_run)
            if result.action == SyncAction.CREATED:
                stats.created += 1
            elif result.action == SyncAction.REACTIVATED:
                stats.reactivated += 1
            elif result.action == SyncAction.SKIPPED_DEDUP:
                stats.skipped_dedup += 1
            elif result.action == SyncAction.SKIPPED_FILTERED:
                stats.skipped_filtered += 1
            elif result.action == SyncAction.ERROR:
                stats.errors += 1
            if is_dry_run:
                # dry-run 不需要暂停
                continue

        elapsed = time.time() - start_time
        logger.info(str(stats))
        if is_dry_run and (stats.created > 0 or stats.reactivated > 0):
            logger.info("试运行汇总: 新建 %d 条, 重新激活 %d 条, 去重跳过 %d 条, 过滤跳过 %d 条, 错误 %d 条 (共 %d 条, 耗时 %.1fs)",
                         stats.created, stats.reactivated, stats.skipped_dedup,
                         stats.skipped_filtered, stats.errors, stats.total, elapsed)
        if progress_callback:
            progress_callback(max(stats.total, 1), max(stats.total, 1),
                              "同步完成")

        # 钉钉通知
        if self.dingtalk_bot:
            try:
                self.dingtalk_bot.send_sync_result(
                    stats, elapsed, dry_run=is_dry_run,
                    project_name=self.project_name,
                )
            except Exception as e:
                logger.warning("钉钉通知发送失败: %s", e)

        return stats

    def _detect_custom_fields(self):
        required = ("severity", "reproduction", "category", "version", "found_time", "sn_code", "belong_project")
        if all(self.cf_ids.get(k) for k in required):
            return
        try:
            # 获取自定义字段列表
            all_fields = []
            page_token = ""
            while True:
                params = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                data = self.teambition._request(
                    "GET",
                    f"/v3/project/{self.teambition.project_id}/customfield/search",
                    params=params,
                )
                fields = data.get("result", [])
                all_fields.extend(fields)
                page_token = data.get("nextPageToken", "")
                if not page_token or not fields:
                    break

            for f in all_fields:
                fname = f.get("name", "")
                fid = f.get("id", "")
                if "严重程度" in fname and not self.cf_ids.get("severity"):
                    self.cf_ids["severity"] = fid
                elif "复现概率" in fname and not self.cf_ids.get("reproduction"):
                    self.cf_ids["reproduction"] = fid
                elif "缺陷分类" in fname and not self.cf_ids.get("category"):
                    self.cf_ids["category"] = fid
                elif fname == "所属版本" and not self.cf_ids.get("version"):
                    self.cf_ids["version"] = fid
                elif "产生时间" in fname and not self.cf_ids.get("found_time"):
                    self.cf_ids["found_time"] = fid
                elif "SN编码" in fname and not self.cf_ids.get("sn_code"):
                    self.cf_ids["sn_code"] = fid
                elif "所属项目" in fname and not self.cf_ids.get("belong_project"):
                    self.cf_ids["belong_project"] = fid
                elif "日志附件" in fname and not self.cf_ids.get("attachment"):
                    self.cf_ids["attachment"] = fid
            logger.info("自定义字段ID: %s", self.cf_ids)
        except Exception as e:
            logger.warning("检测自定义字段失败: %s", e)

    def _train_similarity_classifier(self, progress_callback=None):
        """扫描 TB 已分类缺陷任务，训练 TF-IDF 相似度分类器。

        首次运行：全量训练（最新 N 条，N 由 max_fetch 配置）。
        后续运行：加载缓存，若超过 7 天则增量学习最新 500 条。
        """
        category_cf_id = self.cf_ids.get("category", "")
        if not category_cf_id:
            logger.info("未检测到缺陷分类字段，跳过 TF-IDF 训练")
            return

        # 获取缺陷类型 ID
        defect_sfc_id = self.teambition.scenariofieldconfig_id
        if not defect_sfc_id:
            defect_sfc_id = self.teambition.get_defect_scenariofieldconfig_id()
        if not defect_sfc_id:
            logger.warning("未检测到缺陷类型 ID，TF-IDF 训练跳过")
            return

        classifier_cfg = self.config.get("classifier", {})
        if "classifier" in classifier_cfg and "similarity" not in classifier_cfg:
            classifier_cfg = classifier_cfg["classifier"]

        # ── 尝试加载已有缓存 ──
        loaded = self.classifier.load_similarity_model()

        if loaded:
            sim = self.classifier._sim_classifier
            inc_days = classifier_cfg.get("similarity", {}).get(
                "incremental_days", 7)
            if inc_days > 0 and not sim.is_stale(days=inc_days):
                logger.info("TF-IDF 模型已从缓存加载（%d 条样本，未过期），跳过增量学习",
                             sim.sample_count)
                return

            # 增量学习最新 500 条
            logger.info("TF-IDF 缓存已超过 7 天（%d 条样本），开始增量学习最新缺陷...",
                         sim.sample_count)
            if progress_callback:
                progress_callback(0, 0, "TF-IDF 增量学习最新缺陷...")
            samples = self._fetch_defect_samples(
                defect_sfc_id, category_cf_id, limit=500,
                progress_callback=progress_callback)
            if samples:
                logger.info("增量学习: 新增 %d 条样本，重新训练并保存", len(samples))
                if progress_callback:
                    progress_callback(0, 0,
                                      f"增量训练 TF-IDF（新增 {len(samples)} 条）...")
                old_count = self.classifier._sim_classifier.sample_count
                self.classifier._sim_classifier.add_samples(samples)
                self.classifier._sim_classifier.save()
                new_count = self.classifier._sim_classifier.sample_count
                logger.info("TF-IDF 增量学习完成，模型已更新（共 %d 条样本）",
                             new_count)
                # AI 审核新增训练数据（只审核新增部分）
                if progress_callback:
                    progress_callback(0, 0, "AI 审核新增训练数据...")
                self.classifier.review_training_data(
                    target_indices=range(old_count, new_count))
            else:
                logger.info("增量学习: 未获取到新样本，保持现有模型")
            return

        # ── 无缓存，全量训练 ──
        logger.info("未找到 TF-IDF 缓存，开始扫描 TB 缺陷任务作为训练数据...")
        if progress_callback:
            progress_callback(0, 0, "正在扫描 Teambition 缺陷任务...")

        max_fetch = classifier_cfg.get("similarity", {}).get("max_fetch", 5000)
        samples = self._fetch_defect_samples(
            defect_sfc_id, category_cf_id, limit=max_fetch,
            progress_callback=progress_callback)

        if samples:
            cat_counts: Dict[str, int] = {}
            for _, c in samples:
                cat_counts[c] = cat_counts.get(c, 0) + 1
            top_cats = sorted(cat_counts.items(), key=lambda x: -x[1])[:10]
            top_desc = ", ".join(f"{c}({n})" for c, n in top_cats)
            logger.info("扫描完成: %d 条带分类样本, %d 个分类\n"
                         "  TOP10: %s\n"
                         "  开始训练 TF-IDF 模型...",
                         len(samples), len(cat_counts), top_desc)
            if progress_callback:
                progress_callback(0, 0,
                                  f"训练 TF-IDF 模型（{len(samples)} 条样本）...")
            self.classifier.train_similarity(samples, save=True)
            logger.info("TF-IDF 模型训练并保存完成，分类器就绪")
            # AI 审核训练数据
            if progress_callback:
                progress_callback(0, 0, "AI 审核训练数据...")
            self.classifier.review_training_data()
        else:
            logger.info("TB 缺陷任务中未找到带分类的样本，"
                         "TF-IDF 不可用，将使用 LLM 和规则兜底分类")

    def _get_sn_patterns_path(self, project_id: str) -> str:
        """返回指定项目的 SN patterns 持久化文件路径。"""
        from src.utils import get_app_data_dir
        safe_id = project_id.replace("-", "_") if project_id else "default"
        return os.path.join(get_app_data_dir(), f"sn_patterns_{safe_id}.json")

    def _load_sn_patterns_for_project(self, project_id: str) -> bool:
        """加载指定项目的已保存 SN patterns，返回是否成功。"""
        path = self._get_sn_patterns_path(project_id)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    patterns = json.load(f)
                if isinstance(patterns, list) and patterns:
                    self._sn_patterns = patterns
                    self._sn_patterns_loaded = True
                    logger.info("SN 格式已加载（项目 %s...）: %d 个模式",
                                project_id[:8], len(patterns))
                    return True
            except Exception as e:
                logger.warning("SN 格式文件加载失败 %s: %s", path, e)
        self._sn_patterns = None
        self._sn_patterns_loaded = False
        return False

    def _save_sn_patterns(self, project_id: str, patterns: list):
        """将 SN patterns 持久化到本地文件。"""
        path = self._get_sn_patterns_path(project_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(patterns, f, ensure_ascii=False, indent=2)
            logger.info("SN 格式已保存（项目 %s...）: %s", project_id[:8], path)
        except Exception as e:
            logger.warning("SN 格式保存失败: %s", e)

    def _learn_sn_patterns(self, progress_callback=None):
        """从 TB 已有缺陷任务的 SN 字段中学习格式模式（按项目隔离）。"""
        if self._sn_patterns_loaded:
            return

        # 无 project_id 时无法按项目隔离，退回到默认模式
        project_id = self._tb_project_id
        if not project_id:
            from src.extractor import DEFAULT_SN_PATTERNS
            self._sn_patterns = DEFAULT_SN_PATTERNS.copy()
            self._sn_patterns_loaded = True
            return

        sn_cf_id = self.cf_ids.get("sn_code", "")
        if not sn_cf_id:
            from src.extractor import DEFAULT_SN_PATTERNS
            self._sn_patterns = DEFAULT_SN_PATTERNS.copy()
            self._sn_patterns_loaded = True
            self._save_sn_patterns(project_id, self._sn_patterns)
            return

        try:
            if progress_callback:
                progress_callback(0, 0, "正在学习 SN 格式...")

            # 获取缺陷类型 ID
            defect_sfc_id = self.teambition.scenariofieldconfig_id
            if not defect_sfc_id:
                defect_sfc_id = self.teambition.get_defect_scenariofieldconfig_id()
            if not defect_sfc_id:
                logger.warning("未检测到缺陷类型 ID，SN 学习跳过")
                from src.extractor import DEFAULT_SN_PATTERNS
                self._sn_patterns = DEFAULT_SN_PATTERNS.copy()
                self._sn_patterns_loaded = True
                self._save_sn_patterns(project_id, self._sn_patterns)
                return

            # 拉取已有缺陷任务列表（query_project_tasks 已按 self.project_id 过滤）
            sn_values = []
            page_token = ""
            task_ids = []
            while True:
                tasks, page_token = self.teambition.query_project_tasks(
                    page_size=200, page_token=page_token,
                    sfc_id=defect_sfc_id)
                if not tasks:
                    break
                for t in tasks:
                    if t.taskId:
                        task_ids.append(t.taskId)
                if not page_token or len(task_ids) >= 500:
                    break

            # 并行拉取详情，提取 SN 字段值
            if task_ids:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _fetch_sn(tid):
                    try:
                        data = self.teambition._request(
                            "GET", "/v3/task/query", params={"taskId": tid})
                        result = data.get("result", [])
                        raw = result[0] if isinstance(result, list) and result else (
                            result if isinstance(result, dict) else None)
                        if raw and raw.get("customfields"):
                            for cf in raw["customfields"]:
                                if cf.get("cfId") == sn_cf_id:
                                    val = cf.get("value", [])
                                    if isinstance(val, list) and val:
                                        first = val[0]
                                        sn = first.get("title", "") if isinstance(first, dict) else str(first)
                                        if sn and sn not in ("/", "-", "", "."):
                                            return sn
                                    break
                    except Exception:
                        pass
                    return None

                with ThreadPoolExecutor(max_workers=10) as pool:
                    futures = {pool.submit(_fetch_sn, tid): tid for tid in task_ids}
                    for future in as_completed(futures):
                        sn = future.result()
                        if sn:
                            sn_values.append(sn)

            from src.extractor import learn_sn_patterns
            self._sn_patterns = learn_sn_patterns(sn_values)
            self._sn_patterns_loaded = True
            self._save_sn_patterns(project_id, self._sn_patterns)
            logger.info("SN 格式学习完成: 从 %d 条样本中学到 %d 个模式: %s",
                        len(sn_values), len(self._sn_patterns), self._sn_patterns)
            if progress_callback:
                progress_callback(0, 0, f"SN 格式学习完成 ({len(sn_values)} 条样本)")
        except Exception as e:
            logger.warning("SN 格式学习失败: %s，使用默认模式", e)
            from src.extractor import DEFAULT_SN_PATTERNS
            self._sn_patterns = DEFAULT_SN_PATTERNS.copy()
            self._sn_patterns_loaded = True
            self._save_sn_patterns(project_id, self._sn_patterns)

    def _fetch_defect_samples(self, defect_sfc_id: str,
                               category_cf_id: str, limit: int = 500,
                               progress_callback=None) -> list:
        """从 TB 拉取缺陷任务详情，返回 [(标题, 分类名), ...] 列表"""
        # 第一步：列表扫描收集任务 ID
        task_ids = []
        page_token = ""
        page_num = 0
        try:
            while True:
                page_num += 1
                tasks, page_token = self.teambition.query_project_tasks(
                    page_size=200, page_token=page_token,
                    sfc_id=defect_sfc_id)
                if not tasks:
                    if not page_token:
                        break
                    logger.warning("列表扫描第 %d 页返回空（有 page_token），继续翻页",
                                    page_num)
                    continue
                for t in tasks:
                    if t.taskId:
                        task_ids.append(t.taskId)
                logger.info("列表扫描第 %d 页: 本页 %d 条, 累计 %d 个缺陷任务 ID",
                             page_num, len(tasks), len(task_ids))
                if not page_token:
                    break
                if len(task_ids) >= limit:
                    break
        except Exception as e:
            logger.warning("列表扫描失败: %s", e)

        task_ids = task_ids[:limit]
        if not task_ids:
            return []

        logger.info("开始并行拉取 %d 条任务详情（10 并发）...", len(task_ids))

        # 第二步：并行拉取详情，提取分类
        samples = []
        fetched = 0
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_raw(tid):
                try:
                    data = self.teambition._request(
                        "GET", "/v3/task/query", params={"taskId": tid})
                    result = data.get("result", [])
                    raw = result[0] if isinstance(result, list) and result else (
                        result if isinstance(result, dict) else None)
                    task = self.teambition._parse_task(raw) if raw else None
                    return task
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_fetch_raw, tid): tid
                           for tid in task_ids}
                for future in as_completed(futures):
                    fetched += 1
                    task = future.result()
                    if not task or not task.customfields:
                        continue
                    for cf in task.customfields:
                        if not isinstance(cf, dict):
                            continue
                        if cf.get("cfId") != category_cf_id:
                            continue
                        val = cf.get("value", [])
                        if isinstance(val, list) and val:
                            first = val[0]
                            cat = first.get("title", "") if isinstance(first, dict) else (
                                str(first) if isinstance(first, str) else "")
                            if cat and task.content:
                                clean_title = re.sub(
                                    r'【[^】]*】', '', task.content).strip()
                                if clean_title:
                                    samples.append((clean_title, cat))
                        break

                    if fetched % 200 == 0:
                        logger.info("拉取详情进度: %d/%d, 已收集 %d 条训练样本",
                                     fetched, len(task_ids), len(samples))
                        if progress_callback:
                            progress_callback(
                                fetched, len(task_ids),
                                f"拉取任务详情 {fetched}/{len(task_ids)}，"
                                f"已收集 {len(samples)} 条样本")

        except Exception as e:
            logger.warning("并行拉取详情失败: %s", e)

        return samples

    def _sync_single_bug(self, bug: ZentaoBug,
                         dry_run: bool) -> SyncResult:
        try:
            # VLNS / CPAX 标记去重：标题中包含说明已导入过
            # 非激活状态直接跳过；激活状态走下面去重+状态校验流程（避免重复 API 调用）
            if re.search(r'(?:VLNS|CPAX)-\d+', bug.title) and bug.status != "active":
                logger.info("[跳过-已导入] Bug#%d 标题含 VLNS/CPAX 标记: %s",
                            bug.id, bug.title)
                return SyncResult(bug.id, SyncAction.SKIPPED_DEDUP,
                                  "", "标题含VLNS/CPAX，已导入过")

            # 去重检查：TB 搜索（一次 API 调用）
            existing = self._find_existing_task(bug)
            if existing:
                # 动态学习：记录源平台经办人 → TB 执行人映射
                if bug.assignedTo and existing.executorId:
                    self._learned_assignee_map.setdefault(
                        bug.assignedTo, existing.executorId)
                # 检查是否需要重新激活：禅道 Bug 激活但 TB 任务已关闭
                if self.reactivate_closed and self._should_reactivate(bug, existing):
                    return self._reactivate_task(bug, existing, dry_run)
                # 打印双方状态对比
                tb_status_name = self._get_taskflow_status_name(existing.status)
                logger.info("[跳过-重复] Bug#%d (禅道=%s, TB=%s) 已存在: %s",
                            bug.id, bug.status, tb_status_name,
                            existing.content)
                return SyncResult(bug.id, SyncAction.SKIPPED_DEDUP,
                                  existing.taskId, "已存在")

            # 检查禅道备注/历史记录中是否含 VLNS 或 CPAX
            # 仅当 TB 仍存在对应任务时才跳过（避免 TB 任务已删除后无法重新导入）
            if self.source.check_bug_has_vlns(bug.id):
                logger.warning("[提醒] Bug#%d 备注中含 VLNS/CPAX 历史标记，"
                             "但 TB 未找到对应任务，将继续导入", bug.id)

            # 获取完整详情
            full_bug = self.source.fetch_bug_detail(bug.id)

            # module_filter 检查：批量API不返回moduleName。
            # 若 run() 已用模块API预过滤（_module_id_set 是 set），此处可跳过。
            # 仅当 _module_id_set is None（API不可用）时回退到 moduleName 子串匹配。
            if self.module_filter and getattr(self, "_module_id_set", None) is None:
                id_match = self.module_filter.isdigit() and str(full_bug.module) == self.module_filter
                name_match = self.module_filter in full_bug.moduleName
                if not id_match and not name_match:
                    logger.info("[跳过-模块过滤] Bug#%d 模块 '%s'(ID=%s) 不匹配 '%s'",
                                bug.id, full_bug.moduleName, full_bug.module,
                                self.module_filter)
                    return SyncResult(bug.id, SyncAction.SKIPPED_FILTERED, "",
                                      f"模块过滤: {full_bug.moduleName}")

            # 构建字段
            title = self._build_teambition_title(full_bug)
            note = self._build_note(full_bug)
            tb_severity = self._map_severity(full_bug.severity)
            executor = self._map_assignee(full_bug.assignedTo)
            if self.classifier:
                category = self.classifier.classify(
                    bug_title=full_bug.title,
                    bug_steps=full_bug.steps[:500] if full_bug.steps else "",
                    bug_type=full_bug.type,
                    assigned_to=full_bug.assignedTo,
                    rule_fallback_fn=self._map_type_to_category,
                )
            else:
                category = self._map_type_to_category(full_bug.type, full_bug.assignedTo)
            customfields = self._build_customfields(full_bug, tb_severity, category)

            if dry_run:
                logger.info("[试运行] 将创建: %s (严重程度=%s, 分类=%s)",
                            title, tb_severity, category)
                return SyncResult(bug.id, SyncAction.CREATED, "",
                                  f"试运行: {title}")

            # 创建 Teambition 任务
            task_id, task_identifier = self.teambition.create_task(
                content=title,
                note=note,
                executor_id=executor,
                customfields=customfields,
            )

            # 双向标题同步：回写禅道标题（优先使用 taskIdentifier 如 VLNS-62575）
            display_id = task_identifier or task_id
            new_zentao_title = self._build_zentao_title(full_bug.title, display_id)
            try:
                self.source.update_bug_title(full_bug.id, new_zentao_title)
            except Exception as e:
                logger.warning("回写禅道标题失败: Bug#%d - %s", full_bug.id, e)

            # 同步评论（禅道备注/评论 → Teambition 任务评论）
            try:
                self._sync_bug_comments(full_bug, task_id)
            except Exception as e:
                logger.warning("同步评论失败: Bug#%d - %s", full_bug.id, e)

            # 同步附件
            if self.sync_attachments:
                self._sync_attachments(full_bug, task_id)

            # AI 日志分析（可选，失败不影响同步）
            if self.ai_analysis_enabled and self.log_analyzer:
                try:
                    data = self.teambition._request(
                        "GET", "/v3/task/query", params={"taskId": task_id}
                    )
                    result = data.get("result", [])
                    raw = result[0] if isinstance(result, list) and result else (
                        result if isinstance(result, dict) else None)
                    if raw:
                        task_obj = self.teambition._parse_task(raw)
                        self.log_analyzer.analyze_and_comment(
                            task_obj, task_raw=raw,
                            fw_hint=full_bug.openedBuild or "",
                        )
                except Exception as e:
                    logger.warning("AI 分析失败: Bug#%d → TB %s - %s", full_bug.id, task_id, e)

            logger.info("[已同步] Bug#%d → TB %s (%s)", full_bug.id, task_identifier or task_id, task_id)
            return SyncResult(bug.id, SyncAction.CREATED, task_id,
                              f"同步成功 → {task_identifier or task_id}")

        except Exception as e:
            logger.error("[错误] 同步 Bug#%d 失败: %s", bug.id, e,
                         exc_info=True)
            return SyncResult(bug.id, SyncAction.ERROR, "", str(e))

    # ── 状态同步 ──────────────────────────────────────

    def _init_taskflow_status_map(self):
        """初始化 TB 任务流状态映射，识别关闭状态和重新打开状态"""
        status_map = self.teambition.get_taskflow_status_map()
        if not status_map:
            return
        # 识别关闭类状态：仅真正终结的状态（关闭、已完成、已取消）
        # 注意：已解决、开发完成、待处理等在 TB 中仍属打开状态，不需要重新激活
        closed_keywords = ("关闭", "已完成", "已取消", "待回归", "待复现", "closed")
        for sid, sname in status_map.items():
            name_lower = sname.lower()
            if any(kw in name_lower for kw in closed_keywords):
                self._closed_status_ids.add(sid)
        # 识别重新打开状态
        reopen_keywords = ("重新打开", "reopen")
        pending_keywords = ("待处理", "进行中", "未开始", "pending", "in_progress", "todo")
        for sid, sname in status_map.items():
            name_lower = sname.lower()
            if any(kw in name_lower for kw in reopen_keywords):
                self._reopen_status_id = sid
                break
        if not self._reopen_status_id:
            for sid, sname in status_map.items():
                name_lower = sname.lower()
                if any(kw in name_lower for kw in pending_keywords):
                    self._reopen_status_id = sid
                    break
        if self._closed_status_ids:
            closed_names = [status_map[sid] for sid in self._closed_status_ids
                            if sid in status_map]
            logger.info("关闭类状态: %s", ", ".join(closed_names))
        if self._reopen_status_id:
            logger.info("重新打开目标状态: %s",
                        status_map.get(self._reopen_status_id, self._reopen_status_id))

    def _get_taskflow_status_name(self, status_id: str) -> str:
        """根据 taskflowstatusId 获取状态名称，用于日志"""
        if not status_id:
            return "无状态"
        status_map = getattr(self.teambition, '_taskflow_status_map', None)
        if status_map and status_id in status_map:
            return status_map[status_id]
        return status_id[:8]

    def _should_reactivate(self, bug: ZentaoBug, existing_task) -> bool:
        """判断是否需要重新激活：禅道 Bug 激活且 TB 任务处于关闭状态"""
        # 禅道状态不是 active 则无需重新激活
        if bug.status != "active":
            return False
        # 没有识别到关闭状态列表，无法判断
        if not self._closed_status_ids:
            return False
        return existing_task.status in self._closed_status_ids

    def _reactivate_task(self, bug: ZentaoBug, existing_task,
                         dry_run: bool) -> SyncResult:
        """重新打开 TB 任务，同步最新评论和附件"""
        task_id = existing_task.taskId
        if dry_run:
            logger.info("[试运行] 将重新激活: Bug#%d → TB %s",
                        bug.id, task_id)
            return SyncResult(bug.id, SyncAction.REACTIVATED, task_id,
                              "试运行: 重新激活")

        # 1. 重新打开 TB 任务
        if self._reopen_status_id:
            try:
                self.teambition.update_task_status(task_id, self._reopen_status_id)
                logger.info("[重新激活] Bug#%d → TB 任务 %s 已重新打开",
                            bug.id, task_id)
            except Exception as e:
                logger.warning("重新打开 TB 任务 %s 失败: %s", task_id, e)
        else:
            logger.warning("未找到重新打开状态，仅同步评论和附件")

        # 2. 添加重新激活评论
        reactivation_comment = (
            f"【禅道重新激活】Bug#{bug.id} 在禅道中已被重新激活，"
            f"当前指派: {bug.assignedTo or '未知'}"
        )
        try:
            self.teambition.add_task_comment(task_id, reactivation_comment)
        except Exception as e:
            logger.warning("添加重新激活评论失败: %s", e)

        # 3. 获取完整详情，同步最新的评论和附件
        full_bug = self.source.fetch_bug_detail(bug.id)

        # 4. 更新执行人为禅道当前指派人
        if full_bug.assignedTo:
            try:
                executor = self._map_assignee(full_bug.assignedTo)
                if executor:
                    self.teambition.update_task_executor(task_id, executor)
                    logger.info("[重新激活] Bug#%d 执行人已更新为 %s (%s)",
                                full_bug.id, full_bug.assignedTo, executor[:8])
            except Exception as e:
                logger.warning("更新执行人失败: Bug#%d - %s", full_bug.id, e)

        # 同步评论（只同步 TB 任务最后更新时间之后的新评论）
        try:
            self._sync_bug_comments(full_bug, task_id,
                                    cutoff_time=existing_task.updated)
        except Exception as e:
            logger.warning("同步评论失败: Bug#%d - %s", full_bug.id, e)

        # 同步附件（只上传新增的，跳过已有的）
        if self.sync_attachments:
            existing_filenames = self._get_existing_task_filenames(task_id)
            self._sync_attachments(full_bug, task_id,
                                   existing_filenames=existing_filenames)

        # AI 日志分析（可选，失败不影响同步）
        if self.ai_analysis_enabled and self.log_analyzer:
            try:
                data = self.teambition._request(
                    "GET", "/v3/task/query", params={"taskId": task_id}
                )
                result = data.get("result", [])
                raw = result[0] if isinstance(result, list) and result else (
                    result if isinstance(result, dict) else None)
                if raw:
                    task_obj = self.teambition._parse_task(raw)
                    self.log_analyzer.analyze_and_comment(
                        task_obj, task_raw=raw,
                        fw_hint=full_bug.openedBuild or "",
                    )
            except Exception as e:
                logger.warning("AI 分析失败: Bug#%d → TB %s - %s", full_bug.id, task_id, e)

        logger.info("[已重新激活] Bug#%d → TB %s", full_bug.id, task_id)
        return SyncResult(bug.id, SyncAction.REACTIVATED, task_id,
                          f"重新激活成功 → {task_id}")

    # ── 去重 ──────────────────────────────────────────

    def _find_existing_task(self, bug: ZentaoBug):
        # Tier 1: 精确匹配 【禅道{id}】
        tag = f"【禅道{bug.id}】"
        results = self.teambition.search_tasks(tag)
        # 过滤已归档任务，且所属项目必须匹配
        active = [t for t in results
                  if not getattr(t, 'isArchived', False)
                  and self._match_project(t, self.project_name)]
        if active:
            return active[0]

        # Tier 2: 标题模糊匹配
        base_title = bug.get_base_title()
        if not base_title:
            return None
        results = self.teambition.search_tasks(base_title)
        for task in results:
            if getattr(task, 'isArchived', False):
                continue
            if not self._match_project(task, self.project_name):
                continue
            task_base = task.get_base_title()
            ratio = difflib.SequenceMatcher(
                None, self._normalize(base_title),
                self._normalize(task_base),
            ).ratio()
            if ratio >= self.dedup_threshold:
                logger.info("模糊匹配命中 (%.2f): %s", ratio, task_base[:60])
                return task
        return None

    def _match_project(self, task, project_name: str) -> bool:
        """检查任务的'所属项目'字段是否匹配当前同步项目"""
        if not project_name:
            return True
        belong_cf_id = self.cf_ids.get("belong_project", "")
        if not belong_cf_id:
            return True
        for cf in (task.customfields or []):
            if cf.get("cfId") == belong_cf_id:
                val = cf.get("value", [])
                if isinstance(val, list):
                    for v in val:
                        title = v.get("title", "") if isinstance(v, dict) else str(v)
                        if project_name == title:
                            return True
                return False
        # 没有"所属项目"字段的任务视为不匹配（避免误判）
        return False

    @staticmethod
    def _convert_to_cst(dt_str: str) -> str:
        """将 ISO 8601 时间字符串转为北京时间 YYYY-MM-DD HH:mm"""
        if not dt_str:
            return ""
        try:
            # 处理带 Z 后缀的 UTC 格式
            dt_str = dt_str.strip()
            if dt_str.endswith("Z"):
                dt_str = dt_str[:-1] + "+00:00"
            # Python 3.7+ 支持 fromisoformat
            dt = datetime.fromisoformat(dt_str)
            # 如果没有时区信息，假设是 UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # 转为 UTC+8
            cst = dt.astimezone(timezone(timedelta(hours=8)))
            return cst.strftime("%Y-%m-%d %H:%M")
        except Exception:
            # 降级：直接截断字符串
            return dt_str[:16].replace("T", " ") if len(dt_str) >= 16 else dt_str

    @staticmethod
    def _normalize_dt(dt_str: str) -> str:
        """将各种时间格式统一为 CST 的 YYYY-MM-DD HH:MM:SS，用于比较"""
        if not dt_str:
            return ""
        dt_str = dt_str.strip()
        try:
            if dt_str.endswith("Z"):
                dt_str = dt_str[:-1] + "+00:00"
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                # 禅道日期无时区后缀，视为 CST（UTC+8）
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            cst = dt.astimezone(timezone(timedelta(hours=8)))
            return cst.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return dt_str[:19].replace("T", " ")

    @staticmethod
    def _normalize(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'[#【】\[\]（）()]', '', text)
        return text

    # ── 字段映射 ──────────────────────────────────────

    def _map_severity(self, severity: str) -> str:
        """禅道严重程度(1-4) → Teambition严重程度(S/A/B/C)"""
        return self.severity_map.get(str(severity), "B")

    def _map_assignee(self, assigned_to: str) -> Optional[str]:
        creator_id = self.teambition.operator_id
        if not assigned_to:
            return creator_id

        # 0. 动态学习缓存：同步过程中已确认的映射
        learned = self._learned_assignee_map.get(assigned_to)
        if learned:
            return learned

        # 1. Jira user_map 配置映射（大小写不敏感 + 姓名反转）
        jira_match = self._lookup_jira_user(assigned_to)
        if jira_match:
            tb_user = jira_match.get("tb_user", "")
            if tb_user:
                mapped = self.user_mapping.get(tb_user)
                if mapped:
                    self._learned_assignee_map[assigned_to] = mapped
                    return mapped
                auto_id = self.teambition.search_member(tb_user)
                if auto_id:
                    self.user_mapping[tb_user] = auto_id
                    self.user_mapping[assigned_to] = auto_id
                    self._learned_assignee_map[assigned_to] = auto_id
                    return auto_id

        # 2. 拼音自动匹配：英文名 → 中文成员（pypinyin 轮转索引）
        pinyin_id = self._lookup_member_by_pinyin(assigned_to)
        if pinyin_id:
            self._learned_assignee_map[assigned_to] = pinyin_id
            logger.info("拼音匹配命中: %s → %s", assigned_to, pinyin_id[:8])
            return pinyin_id

        # 3. 候选名称：原始值 + 去掉首段部门前缀后的值
        # 例如 "项目-乐动开发-343" → 也尝试 "乐动开发-343"
        candidates = [assigned_to]
        if "-" in assigned_to:
            stripped = assigned_to.split("-", 1)[1].strip()
            if stripped and stripped != assigned_to:
                candidates.append(stripped)

        for candidate in candidates:
            mapped = self.user_mapping.get(candidate)
            if mapped:
                self._learned_assignee_map[assigned_to] = mapped
                return mapped
            auto_id = self.teambition.search_member(candidate)
            if auto_id:
                self.user_mapping[candidate] = auto_id
                if candidate != assigned_to:
                    self.user_mapping[assigned_to] = auto_id
                self._learned_assignee_map[assigned_to] = auto_id
                return auto_id

        # 4. 无匹配时回退为创建人
        logger.info("未找到用户映射: %s，回退为创建人", assigned_to)
        return creator_id

    def _lookup_jira_user(self, assigned_to: str) -> Optional[dict]:
        """从 jira_user_map 中查找英文名对应的信息。

        支持大小写不敏感匹配和姓名反转（first last ↔ last first）。
        """
        if not self._jira_user_map_lower or not assigned_to:
            return None

        name_lower = assigned_to.strip().lower()

        # 精确匹配
        if name_lower in self._jira_user_map_lower:
            return self._jira_user_map_lower[name_lower]

        # 姓名反转：jiansen shi → shi jiansen
        parts = name_lower.split()
        if len(parts) == 2:
            reversed_name = f"{parts[1]} {parts[0]}"
            if reversed_name in self._jira_user_map_lower:
                return self._jira_user_map_lower[reversed_name]

        return None

    def _preload_assignee_mapping(self, bugs: list):
        """预学习源平台经办人 → TB 执行人映射。

        从当前 Bug 列表和已有 TB 缺陷任务中学习：
        1. 对当前 Bug 列表中的每条，通过拼音索引预先解析 assignedTo → TB userId
        2. 扫描 TB 最近缺陷任务，用【禅道{id}】标签关联已有 bug 的 assignedTo
        """
        # 1. 拼音预匹配：对当前 Bug 列表中每个唯一的 assignedTo 尝试拼音匹配
        unique_assignees = {b.assignedTo for b in bugs if b.assignedTo}
        for name in unique_assignees:
            if name in self._learned_assignee_map:
                continue
            pinyin_id = self._lookup_member_by_pinyin(name)
            if pinyin_id:
                self._learned_assignee_map[name] = pinyin_id

        # 2. 从已有 TB 任务学习：利用 bug_id 关联
        if not bugs or not self.teambition.project_id:
            return
        defect_sfc_id = self.teambition.scenariofieldconfig_id
        if not defect_sfc_id:
            return

        try:
            # 构建 bug_id → assignedTo 快速查找
            bug_assignee = {b.id: b.assignedTo for b in bugs if b.assignedTo}
            if not bug_assignee:
                return

            # 拉最近 50 条 TB 任务
            tag_re = re.compile(r'【禅道(\d+)】')
            tasks = []
            page_token = ""
            while True:
                batch, page_token = self.teambition.query_project_tasks(
                    page_size=50, page_token=page_token, sfc_id=defect_sfc_id)
                if not batch:
                    break
                tasks.extend(batch)
                if not page_token or len(tasks) >= 50:
                    break

            learned = 0
            for task in tasks:
                if not task.executorId:
                    continue
                m = tag_re.search(task.content or "")
                if not m:
                    continue
                bug_id = int(m.group(1))
                assigned = bug_assignee.get(bug_id)
                if assigned and assigned not in self._learned_assignee_map:
                    self._learned_assignee_map[assigned] = task.executorId
                    learned += 1

            if learned:
                logger.info("从已有 TB 任务学习到 %d 条经办人→执行人映射", learned)
        except Exception as e:
            logger.debug("预学习经办人映射失败（非致命）: %s", e)

    def _build_pinyin_member_index(self):
        """从 TB 成员的中文名构建拼音轮转索引。

        对每个中文名，用 pypinyin 生成拼音音节列表，然后将所有轮转拼接作为索引 key。
        例如 "张伟嘉" → ['zhang', 'wei', 'jia'] → 索引: 'zhangweijia', 'weijiazhang', 'jiaweizhang'
        匹配时 Jira 英文名 "Weijia Zhang" → 归一化 'weijiazhang' 即可命中。
        """
        try:
            import pypinyin
        except ImportError:
            logger.info("pypinyin 未安装，Jira 英文名拼音自动匹配不可用，"
                        "可通过 'pip install pypinyin' 启用")
            return

        member_index = self.teambition._member_index
        if not member_index:
            return

        index: Dict[str, str] = {}
        chinese_count = 0
        for name, uid in member_index.items():
            if not name or not any('一' <= c <= '鿿' for c in name):
                continue
            syllables = [s.lower() for s in pypinyin.lazy_pinyin(name)]
            if not syllables:
                continue
            chinese_count += 1
            n = len(syllables)
            for i in range(n):
                rotated = ''.join(syllables[i:] + syllables[:i])
                if rotated not in index:
                    index[rotated] = uid

        self._pinyin_member_index = index
        logger.info("拼音索引已构建: %d 项（从 %d 个中文名）",
                     len(index), chinese_count)

    def _lookup_member_by_pinyin(self, english_name: str) -> Optional[str]:
        """用拼音索引查找 TB 成员。

        将英文名去空格、小写后，在拼音轮转索引中精确匹配。
        例如 "Weijia Zhang" → "weijiazhang" → 匹配 "张伟嘉" 的轮转之一。
        """
        if not self._pinyin_member_index or not english_name:
            return None
        normalized = english_name.strip().lower().replace(' ', '')
        return self._pinyin_member_index.get(normalized)

    def _map_type_to_category(self, bug_type: str, assigned_to: str = "") -> str:
        """确定 Teambition 缺陷分类

        优先级：
          1. 指派人姓名匹配（从 assigned_to 配置的部门前缀派生）
          2. 指派人带部门前缀匹配（如 "IOT-陈斌"）
          3. Jira 英文名的部门映射（如 "jiansen shi" → "IOT"）
          4. Bug 类型映射（type_category_map）
          5. 默认值
        """
        if assigned_to and self._assignee_name_category:
            category = self._assignee_name_category.get(assigned_to)
            if category:
                return category
        # 兼容：assignedTo 本身带部门前缀的情况
        if assigned_to and self.assignee_category_map:
            prefix = extract_department_prefix(assigned_to)
            if prefix and prefix in self.assignee_category_map:
                return self.assignee_category_map[prefix]
        # Jira 英文名部门映射
        if assigned_to and self._jira_user_map_lower:
            jira_match = self._lookup_jira_user(assigned_to)
            if jira_match:
                dept = jira_match.get("department", "")
                if dept and dept in self.assignee_category_map:
                    return self.assignee_category_map[dept]
        return self.type_category_map.get(bug_type, "应用-其他问题")

    def _build_customfields(self, bug: ZentaoBug,
                            severity: str, category: str) -> list:
        fields = []
        if self.cf_ids.get("severity"):
            fields.append({
                "cfId": self.cf_ids["severity"],
                "value": [severity],
            })
        if self.cf_ids.get("reproduction"):
            fields.append({
                "cfId": self.cf_ids["reproduction"],
                "value": [self.default_reproduction],
            })
        if self.cf_ids.get("category"):
            fields.append({
                "cfId": self.cf_ids["category"],
                "value": [category],
            })
        if self.cf_ids.get("version") and bug.openedBuild:
            fields.append({
                "cfId": self.cf_ids["version"],
                "value": [bug.openedBuild],
            })
        if self.cf_ids.get("found_time"):
            found_time = None
            if self.extraction_enabled:
                from src.extractor import extract_datetime
                # 从 openedDate 解析参考日期，用于 M/D 和纯时间格式补全
                reference_date = None
                if bug.openedDate:
                    try:
                        dt_str = bug.openedDate.strip()
                        if dt_str.endswith("Z"):
                            dt_str = dt_str[:-1] + "+00:00"
                        reference_date = datetime.fromisoformat(dt_str)
                    except (ValueError, TypeError):
                        pass
                found_time = extract_datetime(bug.steps, reference_date)
            if not found_time and bug.openedDate:
                found_time = self._convert_to_cst(bug.openedDate)
            if found_time:
                fields.append({
                    "cfId": self.cf_ids["found_time"],
                    "value": [found_time],
                })
        if self.cf_ids.get("sn_code"):
            sn_value = bug.snCode or "/"
            if self.extraction_enabled and sn_value in ("/", "", "-"):
                from src.extractor import extract_sn
                patterns = getattr(self, '_sn_patterns', None)
                extracted = extract_sn(bug.steps, patterns)
                if extracted:
                    sn_value = extracted
            # LLM 兜底：正则未提取到时，调用 LLM 分析
            if self.extraction_enabled and sn_value in ("/", "", "-"):
                if self.classifier and hasattr(self.classifier, '_call_llm_api'):
                    from src.extractor import extract_with_llm
                    llm_result = extract_with_llm(
                        bug.steps, self.classifier._call_llm_api)
                    if llm_result.get("sn"):
                        sn_value = llm_result["sn"]
                        logger.info("LLM 提取 SN: Bug#%d → %s", bug.id, sn_value)
            fields.append({
                "cfId": self.cf_ids["sn_code"],
                "value": [sn_value],
            })
        # 所属项目：使用 Teambition 项目名（配置中 project.name）
        if self.cf_ids.get("belong_project"):
            if self.project_name:
                fields.append({
                    "cfId": self.cf_ids["belong_project"],
                    "value": [self.project_name],
                })
        return fields

    # ── 标题构建 ──────────────────────────────────────

    def _build_teambition_title(self, bug: ZentaoBug) -> str:
        """
        格式：【禅道{id}】{原始标题}
        如禅道标题已有其他【xxx】前缀标签，放在禅道标签后面
        """
        base = bug.get_base_title()
        tag = self.source_tag_in_tb.replace("{bug_id}", str(bug.id))
        return f"{tag}{base}"

    def _build_zentao_title(self, original_title: str,
                            task_id: str) -> str:
        """格式：【VLNS-xxxxx】{原始标题}"""
        base = re.sub(r'【[\w]+-[\d]+】', '', original_title).strip()
        tag = self.tb_tag_in_zentao.replace("{task_id}", str(task_id))
        return f"{tag}{base}"

    def _build_note(self, bug: ZentaoBug) -> str:
        """构建 Teambition 备注（对应禅道重现步骤），转为 TB 兼容格式"""
        parts = []
        steps = bug.steps.strip()
        if steps:
            # 将禅道 HTML 转换为 TB 兼容的纯文本格式
            text = self._html_to_text(steps)
            if text:
                # 用 <pre> 包裹保留原始换行和间距，TB 渲染更可靠
                parts.append(f"<pre style=\"white-space:pre-wrap;"
                            f"font-family:inherit;\">{text}</pre>")

        # 来源信息作为备注末尾的元数据
        severity_name = SEVERITY_NAMES.get(str(bug.severity), bug.severity)
        tb_severity = self._map_severity(bug.severity)
        meta_parts = [f"禅道 #{bug.id}"]
        if bug.openedBuild:
            meta_parts.append(f"版本: {bug.openedBuild}")
        meta_parts.append(f"SN: {bug.snCode or '/'}")
        meta_parts.append(f"严重程度: {severity_name} → {tb_severity}")
        meta_parts.append(f"创建: {bug.openedBy} {bug.openedDate[:10] if bug.openedDate else ''}")
        if bug.assignedTo:
            meta_parts.append(f"指派: {bug.assignedTo}")
        if bug.type:
            type_name = BUG_TYPE_NAMES.get(bug.type, bug.type)
            meta_parts.append(f"类型: {type_name}")

        meta = f"<hr><p><small>{' | '.join(meta_parts)}</small></p>"
        parts.append(meta)
        return "".join(parts)

    @staticmethod
    def _extract_inline_image_ids(steps_html: str) -> list:
        """从禅道重现步骤 HTML 中提取内联图片的 file_id 列表（去重）"""
        if not steps_html or "<img" not in steps_html:
            return []
        ids = []
        for match in re.finditer(r'file-read[_-](\d+)', steps_html):
            ids.append(match.group(1))
        for match in re.finditer(r'/file/download/(\d+)', steps_html):
            ids.append(match.group(1))
        # dict.fromkeys 去重并保持顺序
        return list(dict.fromkeys(ids))

    # ── 评论同步 ──────────────────────────────────────

    def _sync_bug_comments(self, bug: ZentaoBug, task_id: str,
                           cutoff_time: str = ""):
        """将禅道 Bug 的备注/评论同步到 Teambition 任务评论中（含图片/视频）

        评论中的图片/视频统一作为附件上传到 TB 任务，评论文本中用文件名引用。
        cutoff_time: ISO 时间字符串，只同步该时间之后的评论（用于重新激活场景）
        """
        comments = self.source.fetch_bug_comments(bug.id)
        if not comments:
            return
        synced = 0
        # 收集所有评论中需要上传的媒体文件
        # {file_id: assigned_filename}  e.g. {"123": "comment_01.png"}
        media_to_upload: Dict[str, str] = {}
        media_counter = 0
        processed_comments: List[tuple] = []  # (content, actor, date)

        for c in comments:
            actor = c.get("actor", "")
            date = c.get("date", "")
            comment = c.get("comment", "").strip()
            if not comment:
                continue
            # 按时间过滤：只同步 cutoff_time 之后的新评论
            if cutoff_time and date:
                norm_date = self._normalize_dt(date)
                norm_cutoff = self._normalize_dt(cutoff_time)
                if norm_date and norm_cutoff and norm_date <= norm_cutoff:
                    continue
            # 替换评论中的图片/视频标签为文件名引用
            processed, media_counter = self._replace_comment_media_with_filenames(
                comment, media_to_upload, media_counter
            )
            # HTML → 纯文本：TB 评论不支持 HTML 渲染
            processed = self._html_to_text(processed)
            processed_comments.append((processed, actor, date))

        # 批量上传评论中的媒体文件到 TB 附件
        if media_to_upload:
            self._upload_comment_media(
                bug.id, task_id, media_to_upload, cutoff_time
            )

        # 发送评论
        for processed, actor, date in processed_comments:
            content_parts = ["【禅道评论】"]
            if actor:
                content_parts.append(actor)
            if date:
                content_parts.append(date)
            header = " ".join(content_parts)
            content = f"{header}\n{processed}"
            try:
                self.teambition.add_task_comment(task_id, content)
                synced += 1
            except Exception as e:
                logger.warning("添加评论失败: %s", e)
        if cutoff_time and synced > 0:
            logger.info("同步 %d 条新评论（截止时间 %s 之后）", synced, cutoff_time)

    @staticmethod
    def _html_to_text(html: str) -> str:
        """将禅道 HTML 转为 TB 可读的纯文本，特别处理表格"""
        if not html or "<" not in html:
            return html
        try:
            soup = BeautifulSoup(html, "html.parser")
            # 处理表格：转为 Markdown 风格的表格文本
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if not rows:
                    table.replace_with("")
                    continue
                lines = []
                max_cols = 0
                all_cells = []
                for tr in rows:
                    cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                    all_cells.append(cells)
                    max_cols = max(max_cols, len(cells))
                if max_cols == 0:
                    table.replace_with("")
                    continue
                # 生成等宽文本表格（TB 用 <pre> 包裹）
                col_widths = [0] * max_cols
                for cells in all_cells:
                    for i, c in enumerate(cells):
                        col_widths[i] = max(col_widths[i], len(c))
                for i, cells in enumerate(all_cells):
                    padded = cells + [""] * (max_cols - len(cells))
                    row = " | ".join(
                        c.ljust(col_widths[j]) for j, c in enumerate(padded))
                    lines.append(row)
                    if i == 0:
                        sep = "-+-".join("-" * col_widths[j] for j in range(max_cols))
                        lines.append(sep)
                table.replace_with("\n" + "\n".join(lines) + "\n")

            # 处理未被替换的图片
            for img in soup.find_all("img"):
                img.replace_with("[图片]")
            # 处理视频标签
            for video in soup.find_all("video"):
                video.replace_with("[视频]")
            # 处理换行
            for tag in soup.find_all(["br", "p", "div"]):
                tag.insert_after("\n")
            # 有序列表：保留编号，每项后加换行
            for ol in soup.find_all("ol"):
                for i, li in enumerate(ol.find_all("li"), 1):
                    li.insert_before(f"\n{i}. ")
            # 无序列表：加项目符号
            for ul in soup.find_all("ul"):
                for li in ul.find_all("li"):
                    li.insert_before("\n- ")
            text = soup.get_text()
            # 清理多余空行
            lines = [l.strip() for l in text.splitlines()]
            return "\n".join(l for l in lines if l)
        except Exception:
            return html

    def _replace_comment_media_with_filenames(
            self, comment: str, media_map: Dict[str, str],
            counter: int) -> tuple:
        """将评论中的图片/视频标签替换为文件名引用，收集需要上传的媒体。

        Args:
            comment: 禅道评论 HTML
            media_map: {zentao_file_id: assigned_filename} 累积收集器
            counter: 当前媒体编号计数器

        Returns:
            (processed_comment, new_counter) 元组
        """
        if "<img" not in comment and "<video" not in comment:
            return comment, counter
        try:
            soup = BeautifulSoup(comment, "html.parser")
            modified = False

            # 处理 <img> 标签
            for img in soup.find_all("img"):
                src = img.get("src", "")
                file_id = self._extract_file_id_from_src(src)
                if not file_id:
                    continue
                if file_id not in media_map:
                    counter += 1
                    media_map[file_id] = f"comment_{counter:02d}.png"
                fname = media_map[file_id]
                img.replace_with(f"[图片: {fname}]")
                modified = True

            # 处理 <video> / <source> 标签
            for video in soup.find_all("video"):
                src = video.get("src", "")
                if not src:
                    source = video.find("source")
                    if source:
                        src = source.get("src", "")
                file_id = self._extract_file_id_from_src(src)
                if not file_id:
                    video.replace_with("[视频]")
                    modified = True
                    continue
                if file_id not in media_map:
                    counter += 1
                    media_map[file_id] = f"comment_{counter:02d}.mp4"
                fname = media_map[file_id]
                video.replace_with(f"[视频: {fname}]")
                modified = True

            if modified:
                return str(soup), counter
        except Exception as e:
            logger.warning("处理评论媒体失败: %s", e)
        return comment, counter

    @staticmethod
    def _extract_file_id_from_src(src: str) -> str:
        """从 src URL 中提取禅道 file_id"""
        if not src:
            return ""
        match = re.search(r'file-read[_-](\d+)', src)
        if not match:
            match = re.search(r'/file/download/(\d+)', src)
        return match.group(1) if match else ""

    def _upload_comment_media(self, bug_id: int, task_id: str,
                               media_map: Dict[str, str],
                               cutoff_time: str = ""):
        """批量上传评论中的媒体文件到 TB 任务附件。

        Args:
            media_map: {zentao_file_id: assigned_filename}
        """
        attachment_cf_id = self.cf_ids.get("attachment", "")
        existing_filenames = set()
        if attachment_cf_id:
            existing_filenames = self._get_existing_task_filenames(task_id)

        uploaded: Dict[str, tuple] = {}  # file_id → (work_id, filename, download_url)
        for file_id, assigned_name in media_map.items():
            # 跳过已上传的
            if assigned_name in existing_filenames:
                logger.info("跳过已上传评论媒体: %s", assigned_name)
                continue

            def _do_upload(fid=file_id, fname=assigned_name):
                # 视频文件用 download_attachment（支持大文件和更长超时）
                is_video = fname.lower().endswith(
                    (".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv"))
                if is_video:
                    att = self.source.download_attachment(int(fid), fname)
                else:
                    att = self.source.download_image(int(fid))
                    att.filename = fname
                result = self.teambition.upload_attachment(task_id, att)
                if result:
                    uploaded[fid] = (result[0], fname, result[1])
                    return True
                return False

            if not self._retry(f"评论媒体 {assigned_name}", _do_upload):
                logger.warning("评论媒体上传失败（已耗尽重试）: %s (file_id=%s)",
                               assigned_name, file_id)

        # 更新"日志附件"自定义字段
        if attachment_cf_id and uploaded:
            try:
                existing_values = self._get_existing_attachment_values(
                    task_id, attachment_cf_id
                ) if existing_filenames else []
                values = list(existing_values)
                for entry in uploaded.values():
                    values.append({"id": entry[0], "title": entry[1]})
                if values:
                    self.teambition._request(
                        "POST",
                        f"/v3/task/{task_id}/customfield/{attachment_cf_id}/update",
                        json={"value": values},
                    )
                    logger.info("评论媒体附件已更新: 新增 %d 个", len(uploaded))
            except Exception as e:
                logger.warning("更新评论媒体附件字段失败: %s", e)

    # ── 附件同步 ──────────────────────────────────────

    def _sync_attachments(self, bug: ZentaoBug, task_id: str,
                          existing_filenames: set = None):
        """同步附件：bug.files + 重现步骤内联图片，全部写入日志附件字段。

        Args:
            existing_filenames: 已上传的文件名集合（用于重新激活时跳过已有附件）
        """
        if existing_filenames is None:
            existing_filenames = set()
        uploaded: Dict[str, tuple] = {}  # file_id → (work_id, filename, download_url)
        skipped = 0

        # Step 1: 上传 bug.files 文件附件
        total_files = len(bug.files)
        for idx, f in enumerate(bug.files, 1):
            file_id = str(f.get("id", ""))
            filename = f.get("title", f.get("name", ""))
            size = f.get("size", 0)
            if not file_id:
                continue
            # 跳过已上传的附件（按文件名匹配）
            if filename and filename in existing_filenames:
                logger.info("跳过已上传附件 [%d/%d]: %s", idx, total_files, filename)
                skipped += 1
                continue
            size_mb = size / 1024 / 1024
            if size > self.max_attachment_size_mb * 1024 * 1024:
                logger.warning("附件过大跳过: %s (%.1f MB)", filename, size_mb)
                continue
            logger.info("下载附件 [%d/%d]: %s (%.1f MB)",
                        idx, total_files, filename, size_mb)

            def _do_upload(fid=file_id, fn=filename):
                att = self.source.download_attachment(int(fid), fn)
                result = self.teambition.upload_attachment(task_id, att)
                if result:
                    uploaded[fid] = (result[0], fn, result[1])
                    return True
                return False
            if not self._retry(f"附件同步 {filename}", _do_upload):
                logger.warning("附件同步失败（已耗尽重试）: %s (file_id=%s)", filename, file_id)

        # Step 2: 提取并上传重现步骤中的内联图片
        inline_ids = self._extract_inline_image_ids(bug.steps)
        for file_id in inline_ids:
            if file_id in uploaded:
                continue  # 避免与 bug.files 重复上传
            # 内联图片文件名固定为 image_{file_id}.png，也检查是否已上传
            inline_name = f"image_{file_id}.png"
            if inline_name in existing_filenames:
                logger.info("跳过已上传内联图片: file_id=%s", file_id)
                skipped += 1
                continue
            logger.info("下载内联图片: file_id=%s", file_id)

            def _do_inline(fid=file_id):
                att = self.source.download_image(int(fid))
                result = self.teambition.upload_attachment(task_id, att)
                if result:
                    uploaded[fid] = (result[0], att.filename, result[1])
                    return True
                return False
            if not self._retry(f"内联图片 file#{file_id}", _do_inline):
                logger.warning("内联图片上传失败（已耗尽重试）: file_id=%s", file_id)

        # Step 3: 批量更新"日志附件"自定义字段（包含已有 + 新上传）
        attachment_cf_id = self.cf_ids.get("attachment", "")
        if attachment_cf_id and (uploaded or existing_filenames):
            # 合并已有附件（从 existing_filenames 保留）和新上传的
            values = []
            # 先保留已有附件记录（从任务详情 customfields 中获取）
            if existing_filenames:
                existing_values = self._get_existing_attachment_values(task_id, attachment_cf_id)
                values.extend(existing_values)
            # 追加新上传的
            for entry in uploaded.values():
                values.append({"id": entry[0], "title": entry[1]})
            if values:
                try:
                    self.teambition._request(
                        "POST",
                        f"/v3/task/{task_id}/customfield/{attachment_cf_id}/update",
                        json={"value": values},
                    )
                    new_count = len(uploaded)
                    total = len(values)
                    logger.info("日志附件字段已更新: %d 个文件 (新增 %d, 跳过 %d, 总计 %d)",
                                total, new_count, skipped, total)
                except Exception as e:
                    logger.warning("更新日志附件字段失败: %s", e)

    def _get_existing_attachment_values(self, task_id: str, cf_id: str) -> list:
        """从任务详情中获取日志附件自定义字段的已有值列表。"""
        try:
            data = self.teambition._request(
                "GET", "/v3/task/query", params={"taskId": task_id}
            )
            result = data.get("result", [])
            raw = result[0] if isinstance(result, list) and result else (
                result if isinstance(result, dict) else None)
            if not raw:
                return []
            for cf in raw.get("customfields", []):
                if cf.get("customfieldId") == cf_id or cf.get("id") == cf_id:
                    val = cf.get("value", [])
                    if isinstance(val, list):
                        return val
            return []
        except Exception:
            return []

    def _get_existing_task_filenames(self, task_id: str) -> set:
        """获取 TB 任务已上传附件的文件名集合，用于去重。"""
        filenames = set()
        # 1. 从 works 列表获取
        try:
            works = self.teambition.list_task_works(task_id)
            for w in works:
                fn = w.get("fileName", "")
                if fn:
                    filenames.add(fn)
        except Exception:
            pass
        # 2. 从日志附件自定义字段获取
        attachment_cf_id = self.cf_ids.get("attachment", "")
        if attachment_cf_id:
            existing = self._get_existing_attachment_values(task_id, attachment_cf_id)
            for item in existing:
                title = item.get("title", "") if isinstance(item, dict) else ""
                if title:
                    filenames.add(title)
        if filenames:
            logger.info("任务 %s 已有 %d 个附件: %s", task_id, len(filenames),
                        ", ".join(list(filenames)[:5]))
        return filenames

    # ── 重试机制 ──────────────────────────────────────

    def _retry(self, label: str, fn, retries: int = 0):
        """执行 fn，失败时指数退避重试。成功返回 True，最终失败返回 False。"""
        max_attempts = retries or self.attachment_retries
        for attempt in range(1, max_attempts + 1):
            try:
                fn()
                return True
            except Exception as e:
                if attempt < max_attempts:
                    wait = 2 ** attempt
                    logger.warning("[%s] 第%d次失败，%d秒后重试: %s",
                                   label, attempt, wait, e)
                    time.sleep(wait)
                else:
                    logger.warning("[%s] 重试%d次后仍失败: %s",
                                   label, max_attempts, e)
        return False
