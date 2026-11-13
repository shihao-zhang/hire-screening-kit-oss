#!/usr/bin/env python3
"""半自动归因队列(飞轮③,人在环不可让渡)。

职责边界(设计约束,不可放松):
  - 只提醒「哪些新标签未消化」+ 按事实分类 + 指出涉及的维度/规则族;
  - **不提出**"该改哪条规则/rubric"——归因结论与体系改动由人拍板;
  - 人工消化完成后,把 aid 追加进 out/attribution_state.json 的 digested,队列自动清空;
  - 终点真阳性漏网经人拍板"接受"后登记 accepted_endpoint_miss,harness 降 FAIL→WARN。

用法: python3 core/attribution.py --config <job.toml> --workspace <dir> [--out-dir <dir>]
输入: out/manual_evals_v2.json, out/label_store.json, out/report.csv,
      out/attribution_state.json, out/endpoint_missed.json(harness 产出)
输出: out/attribution_queue.json + out/attribution_queue.md
"""
import argparse
import csv
import datetime
import json
import tomllib
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    ac = cfg["attribution"]
    verdicts = tuple(cfg["labels"]["manual_verdicts"])   # 如 (通过, 待定, 不通过)
    high_rank = ac["high_rank_th"]
    ws = Path(args.workspace)
    src = ws / "out"
    out = Path(args.out_dir) if args.out_dir else src
    out.mkdir(parents=True, exist_ok=True)

    def load_state():
        p = out / "attribution_state.json"
        if not p.exists():
            p = src / "attribution_state.json"
        if p.exists():
            return json.loads(p.read_text())
        return {"digested": [], "note": "已走完人工归因的评估 aid；校准轮结束经用户确认后追加"}

    evals = json.loads((src / "manual_evals_v2.json").read_text())
    store_p = out / "label_store.json"
    store = json.loads((store_p if store_p.exists() else src / "label_store.json").read_text())
    cands = store["candidates"]
    rows = {r["application_id"]: r for r in csv.DictReader(open(src / "report.csv"))}
    state = load_state()
    digested = set(state["digested"])

    queue = []
    seen_aids = set()
    for aid, ev in evals.items():
        if aid in digested:
            continue
        # verdict 取自评语首个【X】;不守格式则标记待人工确认,不静默误分类
        raw = ev["eval"]
        head = raw.split("】")[0].lstrip("【") if raw.startswith("【") else ""
        verdict = head if head in verdicts else "格式待确认"
        llm_v = ev.get("llm_sugg")
        # 在网口径:进精读网 or 有 LLM 分(召回批/定向复审也算已精读)
        in_net = str(rows.get(aid, {}).get("llm_scope", "")).lower() == "true" or bool(ev.get("llm_score"))
        c = cands.get(aid, {})
        iv = (c.get("interview") or {}).get("final")
        # 面试挂兜底:interviews 数据源有滞后,评语文本里点明的面试结果也算
        txt = ev["eval"]
        iv_fail_hint = iv == "fail" or (("一面" in txt or "二面" in txt or "面试" in txt) and "不通过" in txt and verdict == "通过")

        # 分类(事实层,不做归因结论);顺序:硬分歧 > 面试下游 > 网外 > 系统压 > 一致
        if verdict == "不通过" and (llm_v == "推荐面试" or (ev.get("rank") or 999) <= high_rank):
            cat, hint = "A-系统推人工否", "规则高位+LLM推荐 vs 人工否——涉及维度看评语关键词（岗位实质/方向/深度族）"
        elif verdict == "通过" and iv_fail_hint:
            cat, hint = "C-面试下游信号", "筛选层人工与系统一致，面试挂——非筛选层分歧，供 rubric 面试前置验证问题参考"
        elif verdict == "通过" and not in_net:
            cat, hint = "B-网外漏召", "人工通过但未进精读网——精读网口径盲区；看规则哪个维度把分压到网外"
        elif verdict == "通过" and llm_v in ("待定", "不推荐"):
            cat, hint = "A-系统压人工通过", "LLM 压分 vs 人工通过——看 rubric 哪条压的、是否误伤"
        elif verdict == "格式待确认":
            cat, hint = "E-评语格式待确认", f"评语未按【{'/'.join(verdicts)}】开头，无法自动判向——请人工读原文归类"
        else:
            cat, hint = "D-方向一致", "系统与人工同向——无分歧，确认后直接消化"

        queue.append({
            "aid": aid, "name": ev["name"], "rank": ev.get("rank"), "total": ev.get("total"),
            "llm": f"{ev.get('llm_score')}/{llm_v}" if ev.get("llm_score") else "网外",
            "manual": verdict, "category": cat, "eval_text": ev["eval"],
            "dim_hint": hint, "status": "待人工归因",
        })
        seen_aids.add(aid)

    # F-终点真阳性漏网:读 harness §2b 输出,纳入统一归因入口
    # (终点漏网若只躺在 harness 报告里,归因队列就看不到——两信号源必须打通)。
    emp = out / "endpoint_missed.json"
    if not emp.exists():
        emp = src / "endpoint_missed.json"
    if emp.exists():
        for m in json.loads(emp.read_text()).get("missed", []):
            if m["aid"] in digested or m["aid"] in seen_aids:
                continue
            queue.append({
                "aid": m["aid"], "name": m["name"], "rank": m.get("rank"), "total": m.get("total"),
                "llm": m.get("role_fit", "—"), "manual": "终点真阳性",
                "category": "F-终点真阳性漏网", "eval_text": f"{m.get('stage')}｜{m.get('reason')}",
                "dim_hint": "走到漏斗末端却漏网(§2b 最无污染信号)——查召回口径为何没捞到;与人工评估分歧不同源",
                "status": "待人工归因"})
            seen_aids.add(m["aid"])

    queue.sort(key=lambda q: q["category"])
    result = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "pending": len(queue), "digested_total": len(digested), "queue": queue}
    (out / "attribution_queue.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))

    md = [ac["title"], "",
          f"> 生成 {result['generated_at']}｜待归因 **{len(queue)}** 条｜已消化 {len(digested)} 条",
          ac["hitl_note"], ""]
    if not queue:
        md.append("✅ 无未消化标签。")
    cur = None
    for q in queue:
        if q["category"] != cur:
            cur = q["category"]; md += [f"## {cur}", ""]
        md += [f"- **{q['name']}**（rank{q['rank']}/{q['total']}，LLM {q['llm']}，人工【{q['manual']}】）",
               f"  - 评语：{q['eval_text'].replace(chr(10), ' / ')}",
               f"  - 定位提示：{q['dim_hint']}", ""]
    (out / "attribution_queue.md").write_text("\n".join(md))
    if not (out / "attribution_state.json").exists():
        (out / "attribution_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print(f"待归因 {len(queue)} 条 → out/attribution_queue.md")
    for q in queue:
        print(f"  [{q['category']}] {q['name']} 人工{q['manual']} vs 系统{q['llm']}")


if __name__ == "__main__":
    main()
