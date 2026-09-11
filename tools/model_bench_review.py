"""AI 审核模型对比：glm-5.3-flash / deepseek-v4-flash / deepseek-v4-pro

用与 src/classifier.py _review_batch 相同的审核 prompt（真实训练样本），
对每个模型重复 N 轮请求，统计成功率、延迟、输出可解析率。

用法:
  python tools/model_bench_review.py --models glm-5.3-flash deepseek-v4-flash deepseek-v4-pro \
      --batch-size 30 --reps 4
"""

import argparse
import json
import os
import pickle
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEM = ("You are a helpful assistant for robot vacuum defect classification. "
          "Follow the user's format exactly. "
          "When categories are in Chinese, output the exact Chinese category name.")

VERDICT_RE = re.compile(r'(\d+)\s*[.、)]\s*(.+)')


def load_config():
    with open(os.path.join(ROOT, "configs", "classifier.yaml"),
              encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("classifier", cfg)


def load_samples():
    """从本地 TF-IDF 模型读取真实训练样本"""
    with open(os.path.join(ROOT, "data", "classifier_model.pkl"), "rb") as f:
        data = pickle.load(f)
    return [(t, c) for t, c in data["samples"]]


def build_prompt(batch, categories):
    cat_lines = []
    for cat, desc in categories.items():
        cat_lines.append(f"  - {cat}: {desc}" if desc else f"  - {cat}")
    lines = [f"{i}. 标题: {t}  当前分类: {c}"
             for i, (t, c) in enumerate(batch, 1)]
    return (
        "你是扫地机器人缺陷分类质检员。以下是训练数据中的样本，请检查每条的"
        "\"当前分类\"是否合理。\n\n"
        f"可用分类：\n" + "\n".join(cat_lines) + "\n\n"
        "样本列表：\n" + "\n".join(lines) + "\n\n"
        "对每条输出判定结果，格式为：\n"
        "序号. OK（分类正确）\n"
        "序号. 建议→正确分类名（分类不合理时给出建议）\n"
        "只输出有问题的条目也可以，没有问题的不用全部列出。"
    )


def call(session, base_url, key, model, prompt, timeout,
         max_tokens=4000, escalate=False):
    """单次调用；escalate=True 模拟代码路径：
    content 空 + finish=length + 有 reasoning → 翻倍 token 重试一次。"""
    total_elapsed = 0.0
    tries = 0
    while True:
        tries += 1
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        t0 = time.time()
        try:
            r = session.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout)
        except Exception as e:
            total_elapsed += time.time() - t0
            return {"ok": False, "elapsed": total_elapsed, "tries": tries,
                    "error": f"{type(e).__name__}: {str(e)[:120]}"}
        total_elapsed += time.time() - t0
        if r.status_code != 200:
            return {"ok": False, "elapsed": total_elapsed, "tries": tries,
                    "error": f"HTTP {r.status_code}: {r.text[:120]}"}
        data = r.json()
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        usage = data.get("usage") or {}
        if (not content and escalate and reasoning
                and ch.get("finish_reason") == "length"
                and max_tokens < 8000 and tries < 3):
            max_tokens = min(max_tokens * 2, 8000)
            continue
        return {
            "ok": bool(content),
            "elapsed": total_elapsed,
            "tries": tries,
            "max_tokens": max_tokens,
            "content_len": len(content),
            "reasoning_len": len(reasoning),
            "finish": ch.get("finish_reason", ""),
            "verdicts": len(VERDICT_RE.findall(content)),
            "tokens_in": usage.get("prompt_tokens"),
            "tokens_out": usage.get("completion_tokens"),
            "error": "" if content else
                     f"empty content (finish={ch.get('finish_reason')}, "
                     f"reasoning={len(reasoning)}字符)",
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+",
                    default=["glm-5.3-flash", "deepseek-v4-flash",
                             "deepseek-v4-pro"])
    ap.add_argument("--batch-size", type=int, default=30)
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--escalate", action="store_true",
                    help="content 空+length 时翻倍 token 重试一次（模拟代码路径）")
    ap.add_argument("--out", default=os.path.join(
        ROOT, "tools", "_model_bench_results.jsonl"))
    args = ap.parse_args()

    cfg = load_config()
    llm = cfg["llm"]
    base_url = llm["base_url"].rstrip("/")
    key = llm["api_key"]
    timeout = llm.get("timeout", 180)
    categories = cfg["category_descriptions"]
    samples = load_samples()

    rnd = random.Random(42)
    batches = [rnd.sample(samples, args.batch_size)
               for _ in range(args.warmup + args.reps)]

    session = requests.Session()
    results = {}
    for model in args.models:
        recs = []
        for i, batch in enumerate(batches):
            prompt = build_prompt(batch, categories)
            r = call(session, base_url, key, model, prompt, timeout,
                     max_tokens=args.max_tokens, escalate=args.escalate)
            r["model"] = model
            r["batch_size"] = args.batch_size
            r["warmup"] = i < args.warmup
            recs.append(r)
            tag = "warmup" if r["warmup"] else f"run{i - args.warmup + 1}"
            print(f"{model:22s} {tag:8s} {r['elapsed']:6.1f}s "
                  f"tries={r.get('tries', 1)} ok={r['ok']} "
                  f"verdicts={r.get('verdicts', '-')} "
                  f"finish={r.get('finish', '-')} {r.get('error', '')}",
                  flush=True)
            with open(args.out, "a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        results[model] = recs

    print(f"\n=== 汇总 (batch={args.batch_size}, 剔除 warmup) ===")
    print(f"{'模型':24s} {'成功':6s} {'耗时mean':9s} {'min':7s} {'max':7s} "
          f"{'判定行数':8s} {'输出tok':8s}")
    for model, recs in results.items():
        measured = [r for r in recs if not r["warmup"]]
        if not measured:
            continue
        ok = [r for r in measured if r["ok"]]
        times = [r["elapsed"] for r in measured]
        mean = sum(times) / len(times)
        vd = (sum(r.get("verdicts", 0) for r in ok) / len(ok)) if ok else 0
        toks = [r["tokens_out"] for r in ok if r.get("tokens_out")]
        tok_s = f"{sum(toks)/len(toks):.0f}" if toks else "-"
        print(f"{model:24s} {len(ok)}/{len(measured):<4d} {mean:8.1f}s "
              f"{min(times):6.1f}s {max(times):6.1f}s {vd:7.1f} {tok_s:>8s}")


if __name__ == "__main__":
    main()
