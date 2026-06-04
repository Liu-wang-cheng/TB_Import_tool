"""重新训练 TF-IDF 分类器模型并审核训练数据质量。

用法: python tools/retrain_classifier.py [--review]
  --review  训练后用 LLM 审核训练数据（需要配置 LLM API key）
"""

import os
import sys
import logging
import argparse

# 项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("retrain")


def main():
    parser = argparse.ArgumentParser(description="重新训练 TF-IDF 分类器")
    parser.add_argument("--review", action="store_true",
                        help="训练后用 LLM 审核训练数据")
    args = parser.parse_args()

    # 加载配置
    from src.config_loader import load_configs
    config = load_configs()
    logger.info("配置已加载")

    # 初始化 TB 客户端
    from src.teambition_client import TeambitionClient
    tb_cfg = config["teambition"]

    project_id = tb_cfg.get("project_id", "")
    if not project_id:
        project_cfg = tb_cfg.get("project", {})
        project_id = project_cfg.get("id", "") or project_cfg.get("project_id", "")

    teambition = TeambitionClient(
        app_id=tb_cfg["app_id"],
        app_secret=tb_cfg["app_secret"],
        project_id=project_id,
        org_id=tb_cfg.get("org_id", ""),
    )
    logger.info("TB 客户端已初始化 (project: %s)", project_id[:8] if project_id else "N/A")

    # 获取缺陷类型 ID
    defect_sfc_id = teambition.get_defect_scenariofieldconfig_id()
    if not defect_sfc_id:
        logger.error("未检测到缺陷类型 ID，无法训练")
        return
    logger.info("缺陷类型 ID: %s", defect_sfc_id)

    # 获取分类字段 ID（从 TB API 自动检测）
    cf_ids = config.get("teambition", {}).get("customfield_ids", {})
    category_cf_id = cf_ids.get("category", "")
    if not category_cf_id:
        logger.info("未配置分类字段 ID，自动从 TB 检测...")
        cf_name_map = {
            "严重程度": "severity", "复现概率": "reproduction",
            "缺陷分类": "category", "所属版本": "version",
            "产生时间": "found_time", "SN编码": "sn_code",
            "所属项目": "belong_project", "日志附件": "attachment",
        }
        all_fields = []
        page_token = ""
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            data = teambition._request(
                "GET",
                f"/v3/project/{teambition.project_id}/customfield/search",
                params=params,
            )
            fields = data.get("result", [])
            all_fields.extend(fields)
            page_token = data.get("nextPageToken", "")
            if not page_token or not fields:
                break
        for f in all_fields:
            fname = (f.get("name", "") or "").strip()
            fid = f.get("id", "")
            key = cf_name_map.get(fname)
            if key:
                cf_ids[key] = fid
        category_cf_id = cf_ids.get("category", "")
        logger.info("检测到自定义字段: %s", cf_ids)

    if not category_cf_id:
        logger.error("无法获取分类字段 ID")
        return
    logger.info("分类字段 ID: %s", category_cf_id)

    # 初始化分类器
    from src.classifier import BugClassifier
    classifier = BugClassifier(config)
    categories = classifier.get_category_names()
    classifier.set_valid_categories(categories)
    logger.info("分类器已加载 %d 个分类", len(categories))

    # 拉取训练数据（模拟 sync_engine._fetch_defect_samples）
    from src.classifier import SimilarityClassifier
    max_fetch = 5000
    logger.info("开始拉取 TB 缺陷任务 (最多 %d 条)...", max_fetch)

    task_ids = []
    page_token = ""
    while True:
        tasks, page_token = teambition.query_project_tasks(
            page_size=200, page_token=page_token, sfc_id=defect_sfc_id)
        if not tasks:
            if not page_token:
                break
            continue
        for t in tasks:
            if t.taskId:
                task_ids.append(t.taskId)
        logger.info("列表扫描: 累计 %d 个缺陷任务 ID", len(task_ids))
        if not page_token or len(task_ids) >= max_fetch:
            break

    task_ids = task_ids[:max_fetch]
    if not task_ids:
        logger.error("未找到缺陷任务")
        return

    logger.info("开始并行拉取 %d 条任务详情...", len(task_ids))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_raw(tid):
        try:
            data = teambition._request("GET", "/v3/task/query", params={"taskId": tid})
            result = data.get("result", [])
            raw = result[0] if isinstance(result, list) and result else (
                result if isinstance(result, dict) else None)
            task = teambition._parse_task(raw) if raw else None
            return task
        except Exception:
            return None

    samples = []
    fetched = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_raw, tid): tid for tid in task_ids}
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
                        clean_title = SimilarityClassifier._clean_title(task.content)
                        if len(clean_title) >= 2:
                            samples.append((clean_title, cat))
                break
            if fetched % 200 == 0:
                logger.info("进度: %d/%d, 已收集 %d 条样本", fetched, len(task_ids), len(samples))

    logger.info("拉取完成: %d 条有效训练样本", len(samples))

    if not samples:
        logger.error("无有效训练数据")
        return

    # ── 数据纠错 ──
    from _classifier_corrections import apply_corrections
    samples, fix_count = apply_corrections(samples)
    logger.info("数据纠错: 修正了 %d 条分类不合理的样本", fix_count)

    # 统计分类分布
    from collections import Counter
    cat_counts = Counter(c for _, c in samples)
    logger.info("纠错后分类分布 (共 %d 个分类):", len(cat_counts))
    for cat, count in cat_counts.most_common():
        logger.info("  %-40s %d", cat, count)

    # 训练模型
    logger.info("开始训练 TF-IDF 模型...")
    sim = classifier._sim_classifier
    sim.train(samples)
    sim.save()
    logger.info("模型已保存到 data/classifier_model.pkl")

    # 数据质量审核
    logger.info("=" * 60)
    logger.info("训练数据质量审核")
    logger.info("=" * 60)

    # 1. 检查样本过少的分类
    sparse = [(cat, count) for cat, count in cat_counts.items() if count < 5]
    if sparse:
        logger.warning("样本过少的分类 (< 5 条，建议合并或删除):")
        for cat, count in sorted(sparse, key=lambda x: x[1]):
            logger.warning("  %-40s %d 条", cat, count)
    else:
        logger.info("[OK] 所有分类均有 >= 5 条样本")

    # 2. 检查标题为空或过短的样本
    short_titles = [(t, c) for t, c in samples if len(t) < 3]
    if short_titles:
        logger.warning("标题过短的样本 (< 3 字符): %d 条", len(short_titles))
        for t, c in short_titles[:5]:
            logger.warning("  '%s' → %s", t, c)
    else:
        logger.info("[OK] 无标题过短的样本")

    # 3. 检查标题中残留的噪音
    import re
    noise_patterns = [
        (r'[A-Z0-9]{10,}', 'SN编码/长编号'),
        (r'\d{4}[-/.]\d{1,2}[-/.]\d{1,2}', '日期格式'),
        (r'\d{1,2}[：:]\d{2}', '时间格式'),
    ]
    for pat, desc in noise_patterns:
        noisy = [(t, c) for t, c in samples if re.search(pat, t)]
        if noisy:
            logger.warning("标题中残留 %s: %d 条", desc, len(noisy))
            for t, c in noisy[:3]:
                logger.warning("  '%s' → %s", t[:50], c)
        else:
            logger.info("[OK] 无残留 %s", desc)

    # 4. 抽样显示各分类的标题
    logger.info("")
    logger.info("各分类标题抽样:")
    import random
    by_cat = {}
    for t, c in samples:
        by_cat.setdefault(c, []).append(t)
    for cat in sorted(by_cat.keys()):
        titles = by_cat[cat]
        sample_titles = random.sample(titles, min(3, len(titles)))
        logger.info("  [%s] (%d 条):", cat, len(titles))
        for t in sample_titles:
            logger.info("    - %s", t[:60])

    # 5. 用几条测试标题验证分类效果
    logger.info("")
    logger.info("分类效果验证:")
    test_titles = [
        "机器开始充电语音错误",
        "清扫中突然停止不动",
        "回充找不到基站",
        "地图丢失无法建图",
        "WiFi连接断开",
        "APP显示设备离线",
        "清扫路线混乱重复",
        "按键无响应",
    ]
    for title in test_titles:
        result, score = sim.classify_with_score(title)
        logger.info("  '%s' → %s (score=%.3f)", title,
                     result or "无匹配", score)

    # 6. LLM 审核（可选）
    if args.review:
        logger.info("")
        logger.info("开始 LLM 审核训练数据...")
        removed = classifier.review_training_data(max_per_category=3)
        logger.info("LLM 审核完成: 剔除 %d 条不合理样本", removed)
    else:
        logger.info("")
        logger.info("提示: 使用 --review 参数可启用 LLM 审核训练数据")


if __name__ == "__main__":
    main()
