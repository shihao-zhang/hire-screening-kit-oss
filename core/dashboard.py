#!/usr/bin/env python3
"""常驻看板生成器(飞轮⑤)。

汇总四块:漏斗快照 / 护栏闸门(harness §0 摘录) / 待归因队列(③输出) / 数据水位。
输出 out/dashboard.md;由 adapters 的 dashboard_sync 写入飞书常驻文档。
每轮管道(增量/阶段刷新/标签回流)跑完后重新生成。

用法: python3 core/dashboard.py --config <job.toml> --workspace <dir> [--out-dir <dir>]
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
    dc = cfg["dashboard"]
    ltr_th = cfg["harness"]["ltr_threshold"]
    stage_order = dc["stage_order"]
    late_stages = set(dc["late_stages"])
    ws = Path(args.workspace)
    src = ws / "out"
    out = Path(args.out_dir) if args.out_dir else src
    out.mkdir(parents=True, exist_ok=True)

    def artifact(name):
        """优先读本轮 out-dir 的新产物,回退到已有产物目录。"""
        p = out / name
        return p if p.exists() else src / name

    now = datetime.datetime.now().isoformat(timespec="minutes")

    # 漏斗快照(在招 vs 终止分开计数)
    funnel = {s: [0, 0] for s in stage_order}   # stage -> [在招, 已终止]
    total = term = 0
    names_late = []                              # 后期阶段的名单
    apps = {}
    for line in open(ws / "applications_raw.ndjson"):
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        apps[o["id"]] = o
        st = (o.get("stage") or {}).get("zh_name") if isinstance(o.get("stage"), dict) else o.get("stage")
        act = o.get("active_status")
        total += 1
        if act == 2:
            term += 1
        if st in funnel:
            funnel[st][1 if act == 2 else 0] += 1
    rows = {r["application_id"]: r for r in csv.DictReader(open(src / "report.csv"))}
    for aid, o in apps.items():
        st = (o.get("stage") or {}).get("zh_name") if isinstance(o.get("stage"), dict) else o.get("stage")
        if st in late_stages:
            r = rows.get(aid, {})
            tag = "终止" if o.get("active_status") == 2 else "在招"
            names_late.append(f"{r.get('name','?')}({st}/{tag})")

    # 护栏闸门摘录(harness_report.md §0 表格)
    gates = []
    hr = artifact("harness_report.md").read_text().splitlines()
    in_gate = False
    for l in hr:
        if l.startswith("## §0"):
            in_gate = True
            continue
        if in_gate and l.startswith("## "):
            break
        if in_gate and l.startswith("|") and "---" not in l and "检查" not in l:
            gates.append(l)

    # 待归因队列(③)
    aq = json.loads(artifact("attribution_queue.json").read_text())

    # 数据水位
    store = json.loads(artifact("label_store.json").read_text())
    n_eval = store["sources"]["manual_eval"]
    scored = len(rows)
    in_net = sum(1 for r in rows.values() if str(r.get("llm_scope", "")).lower() == "true")

    md = [
        dc["title"],
        "",
        f"> 更新：{now}｜投递 **{total}**（在招 {total-term} / 终止 {term}）｜评分 {scored}｜精读网 {in_net}",
        dc["overwrite_warning"],
        "",
        "## 漏斗快照",
        "",
        "| 阶段 | 在招 | 终止 |",
        "|---|---|---|",
    ]
    for s in stage_order:
        a, t = funnel[s]
        if a or t:
            md.append(f"| {s} | {a} | {t} |")
    if names_late:
        md += ["", f"**三面及以后**：{'、'.join(sorted(set(names_late)))}"]

    md += ["", "## 护栏闸门（harness）", "", "| 检查 | 状态 | 说明 |", "|---|---|---|"] + gates

    md += ["", f"## 待归因队列（{aq['pending']} 条待人工）", ""]
    if aq["pending"] == 0:
        md.append("✅ 无未消化标签。")
    else:
        md += ["| 类别 | 姓名 | 系统 | 人工 | 评语摘要 |", "|---|---|---|---|---|"]
        for q in aq["queue"]:
            ev = q["eval_text"].replace("\n", " ").replace("|", "/")[:60]
            md.append(f"| {q['category']} | {q['name']}(r{q['rank']}) | {q['llm']} | {q['manual']} | {ev} |")
        md += ["", "> 归因与规则改动由人拍板（人在环不可让渡）；消化后在 attribution_state.json 登记。"]

    md += ["", "## 数据水位", "",
           f"- 人工评估标签：{n_eval} 条（primary {store['primary_count']}/{ltr_th} LTR 阈值）",
           f"- 精读网覆盖：{in_net}/{scored}（{in_net/scored*100:.1f}%）",
           f"- 阶段数据最后刷新：{now}",
           ""]

    (out / "dashboard.md").write_text("\n".join(md))
    print(f"dashboard.md 写出（漏斗 {total} 人，待归因 {aq['pending']}，闸门 {len(gates)} 行）")


if __name__ == "__main__":
    main()
