#!/usr/bin/env python3
"""LLM 精读批次合并器:多批次 first-wins 合并 + schema 校验 + 劣化哨兵 + 人工对齐检查。

机制要点:
  - 批次注册表在配置([merge]):override_globs(高优先,按序)+ main_globs(常规,按序);
    同一候选出现在多个批次时 first-wins——重评/修正批放 override 前列即可覆盖旧结论。
  - 合并范围([merge].scope):
      "dossiers"(缺省,旧行为)= dossiers 目录里现存档案的人。注意这是由删除历史决定的偶然集合:
          dossiers.py 只写不删,出网者的档案会一直留着;一旦有人清空过目录,之前出网者的结论
          就再也进不了 merged——既不是「全部精读过的人」,也不是「当前精读网」。
      "net"      = 当前精读网(report.csv 里 llm_scope=true)。
      "reviewed" = 候选池里所有出现在结果批次中的人(精读过就算,无论现在是否在网)。
      后两种的缺漏检查都只针对当前精读网(网内的人必须有结论)。
  - 劣化哨兵:终版为推荐且 red_flags 为空、但任一历史版本(含已归档批次)曾有 red_flag
    → 疑似重跑把扣分理由抹掉,列入观察名单供人工复核。只提示、不改判分(人在环)。
    归档批次(archived_globs)不参与合并判分,但其 red_flag 是哨兵的关键历史证据。

用法:
  python3 core/merge_reviews.py --config <job.toml> --workspace <dir> [--out-dir <dir>]
输入: workspace/llm_review/{dossiers,results}/;输出: workspace/llm_review/merged.json
     + out_dir/rerun_loosen_watch.json
"""
import argparse
import csv
import json
import sys
import tomllib
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    mc = cfg["merge"]
    ws = Path(args.workspace)
    out_dir = Path(args.out_dir) if args.out_dir else ws / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = ws / "llm_review" / "results"
    dossiers = ws / "llm_review" / "dossiers"

    valid_direction = set(mc["valid_direction"])
    valid_verdict = set(mc["valid_verdict"])
    rank_keys = mc["rank_keys"]

    scope = mc.get("scope", "dossiers")
    expected_appids = set()      # 必须有结论的人(缺漏检查)
    appid_to_rank = {}
    appid_to_name = {}
    if scope == "dossiers":
        for f in dossiers.glob("*.json"):
            d = json.loads(f.read_text())
            expected_appids.add(d["application_id"])
            rk = 9999
            for k in rank_keys:
                if d.get(k):
                    rk = d[k]
                    break
            appid_to_rank[d["application_id"]] = rk
            appid_to_name[d["application_id"]] = d["name"]
        valid_appids = expected_appids                      # 允许进 merged 的人
        out_of_scope_msg = "application_id不在精读网(dossiers)范围"
    elif scope in ("net", "reviewed"):
        report = {r["application_id"]: r for r in csv.DictReader(open(ws / "out/report.csv"))}
        for a, r in report.items():
            appid_to_rank[a] = int(r["rank"]) if str(r.get("rank", "")).isdigit() else 9999
            appid_to_name[a] = r["name"]
            if str(r.get("llm_scope", "")).lower() == "true":
                expected_appids.add(a)
        if scope == "net":
            valid_appids = expected_appids
            out_of_scope_msg = "application_id不在当前精读网"
        else:
            valid_appids = set(report)
            out_of_scope_msg = "application_id不在候选池(report.csv)"
    else:
        raise SystemExit(f'[merge].scope 只能是 "dossiers"/"net"/"reviewed",收到 {scope!r}')

    merged = {}
    problems = []
    override_files = [p for g in mc["override_globs"] for p in sorted(results.glob(g))]
    all_files = override_files + [p for g in mc["main_globs"] for p in sorted(results.glob(g))]
    override_paths = set(override_files)

    override_appids = set()
    flag_hist = {}   # appid -> [{file,nflag,verdict,score}]  收集全部合法版本,供劣化哨兵
    for bf in all_files:
        try:
            text = bf.read_text().strip()
            # 容错: 剥掉可能的 markdown 代码围栏
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            items = json.loads(text)
        except Exception as e:
            problems.append(f"{bf.name}: 解析失败 {e}")
            continue
        for it in items:
            appid = str(it.get("application_id", ""))
            errs = []
            if appid not in valid_appids:
                errs.append(out_of_scope_msg)
            score = it.get("llm_score")
            if not isinstance(score, int) or not (0 <= score <= 100):
                errs.append(f"llm_score非法:{score}")
            if it.get("direction") not in valid_direction:
                errs.append(f"direction非法:{it.get('direction')}")
            if it.get("verdict") not in valid_verdict:
                errs.append(f"verdict非法:{it.get('verdict')}")
            if errs:
                problems.append(f"{bf.name} {it.get('name','?')}: {'; '.join(errs)}")
                continue
            flag_hist.setdefault(appid, []).append({
                "file": bf.name, "nflag": len(it.get("red_flags") or []),
                "verdict": it.get("verdict"), "score": score, "name": it.get("name")})
            if appid in merged:
                if appid not in override_appids:
                    problems.append(f"{appid} 重复出现")
                continue
            merged[appid] = it
            if bf in override_paths:
                override_appids.add(appid)

    missing = expected_appids - set(merged)
    if scope == "dossiers":
        print(f"合并成功: {len(merged)}/{len(expected_appids)}")
    else:
        n_in = len(expected_appids & set(merged))
        print(f"合并成功: {len(merged)} 人(scope={scope});当前精读网 {n_in}/{len(expected_appids)} 有结论")
    if missing:
        print(f"缺失 {len(missing)} 人:")
        for a in sorted(missing, key=lambda x: appid_to_rank[x]):
            print(f"  rank{appid_to_rank[a]} {appid_to_name[a]} {a}")
    if problems:
        print("问题:")
        for p in problems:
            print(" ", p)

    (ws / "llm_review" / "merged.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1))

    # 归档批次不参与合并判分,但其 red_flag 是劣化哨兵的关键历史证据;
    # 单独补扫进 flag_hist,只作历史、不进 merged。
    for bf in [p for g in mc["archived_globs"] for p in sorted(results.glob(g))]:
        try:
            arr = json.loads(bf.read_text())
        except Exception:
            continue
        for it in (arr if isinstance(arr, list) else [arr]):
            if not isinstance(it, dict):
                continue
            appid = str(it.get("application_id", ""))
            if appid:
                flag_hist.setdefault(appid, []).append({
                    "file": bf.name, "nflag": len(it.get("red_flags") or []),
                    "verdict": it.get("verdict"), "score": it.get("llm_score"),
                    "name": it.get("name"), "archived": True})

    # 劣化哨兵:终版 verdict=推荐 且 red_flags 空,但历史某版本曾有 red_flag
    # → 疑似重跑把扣分理由抹掉了,列入观察名单供人工复核。只提示、不改判分(人在环)。
    watch_verdict = mc["watch_verdict"]
    watch = []
    for appid, it in merged.items():
        if it.get("verdict") == watch_verdict and not (it.get("red_flags") or []):
            had = [h for h in flag_hist.get(appid, []) if h["nflag"] > 0]
            if had:
                watch.append({
                    "application_id": appid, "name": it.get("name"),
                    "final": {"file": "(选中版)", "score": it.get("llm_score"), "nflag": 0},
                    "flagged_versions": had})
    (out_dir / "rerun_loosen_watch.json").write_text(
        json.dumps({"count": len(watch),
                    "note": "终版推荐+red_flag空但历史有flag,疑重跑劣化,人工复核用,不自动改判。"
                            "多数放松可能是rubric-v2正确校准(如走到漏斗后期者),重点核无漏斗进展的",
                    "watch": watch}, ensure_ascii=False, indent=1))
    if watch:
        print(f"\n⚠️  劣化哨兵：{len(watch)} 人终版推荐且 red_flag 空、但历史曾有 red_flag（→ out/rerun_loosen_watch.json 复核）")
        for w in sorted(watch, key=lambda x: -max(h["nflag"] for h in x["flagged_versions"])):
            vs = ",".join(f"{h['file'].replace('.json','')}={h['nflag']}flag/{h['score']}" for h in w["flagged_versions"])
            print(f"    {w['name']}: 历史 {vs}")

    # 与人工评估对齐检查(仅打印,不产出文件)
    manual_path = ws / mc["manual_evals"]
    if manual_path.exists():
        manual = json.loads(manual_path.read_text())
        print("\n=== 与人工评估对齐(精读网内) ===")
        for appid, m in manual.items():
            verdict_line = m["eval"].splitlines()[0]
            r = merged.get(appid)
            if r:
                print(f"{m['name']:8s} 人工:{verdict_line:24s} LLM:{r['verdict']} {r['llm_score']}分 {r['direction']}")
            else:
                print(f"{m['name']:8s} 人工:{verdict_line:24s} LLM:(不在精读网)")

    return 1 if (missing or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
