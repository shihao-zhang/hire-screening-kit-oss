#!/usr/bin/env python3
"""面试记录扁平化:interviews.ndjson → interview_labels.json(label_store 的 REFERENCE 源)。

每条 = 一位面试官对一轮的判定:
  {aid, name, interviewer, conclusion(1过/2挂/其他待定), content(面评原文), round, t, score}
面试结论在标签体系里是 REFERENCE 层(面试官宽严方差大,只做分离度参考)——
这是设计口径,不因数据自动化而升级。

用法: python3 parse_interviews.py --workspace <dir>
"""
import argparse
import csv
import json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--workspace", required=True)
args = ap.parse_args()
ws = Path(args.workspace)

names = {r["application_id"]: r["name"] for r in csv.DictReader(open(ws / "out/report.csv"))}
labels = []
for line in open(ws / "out/interviews.ndjson"):
    line = line.strip()
    if not line:
        continue
    o = json.loads(line)
    aid = str(o["application_id"])
    for it in o.get("items") or []:
        rnd = it.get("round")
        t = it.get("begin_time")
        for rec in it.get("interview_record_list") or []:
            iv = ((rec.get("interviewer") or {}).get("name") or {}).get("zh_cn")
            labels.append({
                "aid": aid, "name": names.get(aid, "?"),
                "interviewer": iv, "conclusion": rec.get("conclusion"),
                "content": rec.get("content"), "round": rnd, "t": t,
                "score": (rec.get("interview_score") or {}).get("level"),
            })
labels.sort(key=lambda x: (x["aid"], x["t"] or 0))
(ws / "out/interview_labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=1))
n_c = sum(1 for l in labels if l["conclusion"] in (1, 2))
print(f"interview_labels.json: {len(labels)} 条记录(有结论 {n_c}),覆盖 {len({l['aid'] for l in labels})} 人")
