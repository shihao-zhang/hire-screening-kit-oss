#!/usr/bin/env python3
"""跨岗位面评拉取(飞轮⑥):把本池候选在**其它职位**上的 HM 面评捞回来。

## 为什么需要

管道按 job_id 划界,`detect_new` 见到 `job_id != 本岗` 就丢弃。候选人一旦转岗或同时投了
别的岗,他在那条投递上的面试与结论就完全不可见。实测某池 481 人中 **26 人有本岗以外的投递、
8 人在那条投递上有面评**,其中 **4 条是 HM 本人的判定**——恰恰是标签体系里最贵的一档,全漏了。
代价具体到人:一位 LLM 87/推荐面试、rank16 的候选人,HM 在跨岗一面写的是「产品岗 2/5」;
另一位 LLM 78/推荐,HM 判不通过;反向还有一位规则 rank256 被埋、HM 却判了通过。

## 口径(HM 拍板,不要自行放宽)

- **只采纳 HM 本人的记录**。其它岗位、其它面试官的轮次**不进标签库**——那是按别的岗位 bar
  做的判断,拿来当本岗标签等于用别人的尺子量自己的池子。它们只落 advisory 文件供人查阅。
- HM 的跨岗面评之所以可采纳,是因为它评的往往正是"此人适不适合本岗"(实测原文形如
  「产品岗 2/5,通过—转 X 岗」)。采纳前**人应当读一眼原文**确认它确实在评本岗。

## 只读纪律

全程 hire API 只读(`--as bot`),不写招聘系统。

用法: python3 adapters/feishu/pull_xjob.py --config <job.toml> --workspace <dir>
产物: out/xjob_hm_labels.json   (进标签库)  ——只含 HM 记录
      out/xjob_other_advisory.json (仅供人查阅,不进标签库)
"""
import argparse
import csv
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path


def api(path, params=None, tries=3):
    cmd = ["lark-cli", "api", "GET", path, "--as", "bot"]
    if params:
        cmd += ["--params", json.dumps(params)]
    for _ in range(tries):
        try:
            o = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            d = json.loads(o.stdout)
            if d.get("ok"):
                return d
        except Exception:
            pass
        time.sleep(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workspace", required=True)
    args = ap.parse_args()
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    HM = cfg["labels"].get("hm_interviewer")
    JOB = str(cfg["feishu"]["job_id"])
    if not HM:
        print("⚠️ [labels] hm_interviewer 未配置,跳过跨岗拉取", file=sys.stderr)
        return 0
    ws = Path(args.workspace)
    rows = list(csv.DictReader(open(ws / "out/report.csv")))

    hm_labels, advisory = [], []
    n_other = 0
    for i, r in enumerate(rows):
        d = api("/open-apis/hire/v1/applications",
                {"talent_id": r["talent_id"], "page_size": 50})
        ids = (d or {}).get("data", {}).get("items", []) or []
        others = [a for a in ids if a != r["application_id"]]
        if not others:
            continue
        n_other += 1
        for oa in others:
            app = api(f"/open-apis/hire/v1/applications/{oa}")
            ojob = str(((app or {}).get("data", {}).get("application") or {}).get("job_id") or "")
            if ojob == JOB:
                continue                      # 同岗重复投递,已在本池
            iv = api("/open-apis/hire/v1/interviews",
                     {"application_id": oa, "page_size": 20})
            for it in (iv or {}).get("data", {}).get("items", []) or []:
                for rec in it.get("interview_record_list") or []:
                    who = ((rec.get("interviewer") or {}).get("name") or {}).get("zh_cn")
                    item = {"aid": r["application_id"], "name": r["name"],
                            "other_aid": oa, "other_job": ojob,
                            "interviewer": who, "conclusion": rec.get("conclusion"),
                            "content": rec.get("content"), "round": it.get("round"),
                            "t": it.get("begin_time")}
                    (hm_labels if who == HM else advisory).append(item)
        if (i + 1) % 100 == 0:
            print(f"  进度 {i+1}/{len(rows)}", flush=True)

    (ws / "out/xjob_hm_labels.json").write_text(
        json.dumps(hm_labels, ensure_ascii=False, indent=1))
    (ws / "out/xjob_other_advisory.json").write_text(
        json.dumps(advisory, ensure_ascii=False, indent=1))
    dec = [x for x in hm_labels if x["conclusion"] in (1, 2)]
    print(f"池 {len(rows)} 人｜有跨岗投递 {n_other} 人｜"
          f"HM 跨岗面评 {len(hm_labels)} 条(已判定 {len(dec)})｜"
          f"其它面试官 {len(advisory)} 条(仅 advisory,不进标签库)")
    for x in dec:
        v = "通过" if x["conclusion"] == 1 else "不通过"
        print(f"   [{v}] {x['name']}  job={x['other_job']}  {(x['content'] or '')[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
