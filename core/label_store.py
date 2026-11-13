#!/usr/bin/env python3
"""标签中心化(飞轮①):把四类标签源合并成单一 label store。

设计口径(校准得来,飞轮首要目标=运营提效):
  - PRIMARY(强基准)   = 人工评估(通过/待定/不通过) ∪ 终点真阳性(走到漏斗末端)
  - REFERENCE(参考)   = 面试结论(iv_pass/iv_fail) —— 因面试官宽严方差大,降为参考信号,
                         不进 harness 的核心一致率,只做"分离度"参考 + 附每面试官通过率
  - WEAK(弱)          = 阶段流转 / 撤回终止状态
label store 落本地 JSON(纯分析产物,不回写招聘系统,保持"招聘系统零写入"纪律)。

staff 排除:内部记录混入候选池时,其【候选人】身份不计任何标签(终点/强基准会被污染);
但其作为【面试官】的判定仍计入 interviewer_stats(那是真数据)。名单在配置 [labels].staff_ids。

终点 HM 参与门槛:[labels].endpoint_require_hm_interview(缺省 false)——终点正样本默认要求
HM 本人(配置 [labels].hm_interviewer)至少参与过一轮该候选人的面试。同事代面的面试推进
(如产品同事分流操作)反映的是他人的 bar,不是本岗位的终点真阳性。例外按人放行:
[labels].endpoint_hm_exemption_ids(缺省空)是「特别标注样本」清单,由 HM 逐人确认,
不建议自动推断;被门槛排除的人会带 endpoint_excluded_hm=true 留痕,发现新排除项时
harness 输出会列出,交 HM 判断是否豁免(发现→提报→HM 定夺,不做静默取舍)。

终点排除:[labels].endpoint_exclude_ids(缺省空)——走到终点阶段、但**并非经本管道筛选流入**的
记录(例如由更高层直接召回的历史候选人),其终点状态反映不了本管道的筛选效果,不计终点正样本;
其余标签(人工评估/面试结论)照常计入。这与「终点集合不因主观判否而剔除」不冲突:那条禁止的是
因结论不合意而剔人,这条依据的是样本来源不属于本管道。名单由 HM 逐条确认,不得自动推断。

用法: python3 core/label_store.py --config <job.toml> --workspace <dir> [--out-dir <dir>]
输入: out/report.csv, out/manual_evals_v2.json, out/interview_labels.json,
      out/labels_v2.json, out/withdrawn_ids.txt, llm_review/merged.json
输出: out/label_store.json  { generated_at, sources, interviewer_stats, candidates{appid:{...}} }
"""
import argparse
import csv
import json
import re
import tomllib
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    lc = cfg["labels"]
    ws = Path(args.workspace)
    src = ws / "out"                      # 输入所在(既有产物目录)
    out = Path(args.out_dir) if args.out_dir else src
    out.mkdir(parents=True, exist_ok=True)

    endpoint_stages = set(lc["endpoint_stages"])
    staff_ids = set(lc["staff_ids"])
    endpoint_exclude = set(lc.get("endpoint_exclude_ids") or [])
    endpoint_require_hm = bool(lc.get("endpoint_require_hm_interview", False))
    endpoint_hm_exempt = set(lc.get("endpoint_hm_exemption_ids") or [])
    verdict_re = re.compile(r"【\s*(" + "|".join(lc["manual_verdicts"]) + r")\s*】")
    rankhint_re = re.compile(lc["rankhint_pattern"])
    tz = timezone(timedelta(hours=lc["tz_hours"]))

    def parse_manual_eval(eval_text: str):
        """从人工评估首段解析 verdict + 排名提示。"""
        head = eval_text.strip()
        vm = verdict_re.search(head)
        rm = rankhint_re.search(head.split("\n", 1)[0])  # 排名提示只认首行
        return {
            "verdict": vm.group(1) if vm else None,
            "rank_hint": rm.group(1) if rm else None,
            "raw": eval_text.strip(),
        }

    # ---- report.csv: 候选主表(name/stage/active/分项) ----
    rows = {r["application_id"]: r for r in csv.DictReader(open(src / "report.csv"))}

    # ---- 人工评估(PRIMARY) ----
    manual = json.load(open(src / "manual_evals_v2.json"))

    # ---- 面试结论(REFERENCE,可选——由 hire 面试 API 回收,见移植地图缺口标注) ----
    # interview_labels.json schema: [{aid, interviewer, conclusion(1过/2挂/其他待定), round}]
    _iv = src / "interview_labels.json"
    interviews = json.load(open(_iv)) if _iv.exists() else []
    iv_by_aid = defaultdict(list)
    # HM 本人姓名(面试判定权重高于其他面试官;缺省 None = 退回旧的全员等权)
    HM_NAME = lc.get("hm_interviewer")
    # 跨岗 HM 面评(pull_xjob.py 产物,只含 HM 本人;缺省空 = 未跑该步骤)
    _xj = src / "xjob_hm_labels.json"
    # 跨岗 HM 面评的本岗语义**以人工标注表为准,不读 conclusion 码**。
    # 实测两条正好读反:「产品岗2/5,不走产品线;通过—转X岗」结论码是"通过",语义是本岗拒绝——
    # 转岗是操作动作而非本岗认可。未标注的记录**直接跳过**(宁可少一条标签,不猜 HM 的意思)。
    _ann = lc.get("xjob_hm_verdict", {})
    _MAP = {"positive": 1, "negative": 2}
    xjob_hm, xjob_unannotated = {}, []
    if _xj.exists():
        for _x in json.load(open(_xj)):
            v = _ann.get(str(_x.get("other_aid"))) or _ann.get(str(_x["aid"]))
            if v is None:
                xjob_unannotated.append(_x); continue
            if v not in _MAP:            # na = 不适用,明确排除
                continue
            xjob_hm.setdefault(_x["aid"], []).append({**_x, "conclusion": _MAP[v]})
    if xjob_unannotated:
        print(f"⚠️ 跨岗 HM 面评有 {len(xjob_unannotated)} 条未标注,已跳过(不猜结论码)："
              + "、".join(x["name"] for x in xjob_unannotated))
    for x in interviews:
        iv_by_aid[str(x["aid"])].append(x)

    # 面试官宽严统计(降为参考的证据)
    interviewer = defaultdict(lambda: {"pass": 0, "fail": 0, "pending": 0})
    for x in interviews:
        who = x.get("interviewer") or "?"
        c = x.get("conclusion")
        if c == 1:
            interviewer[who]["pass"] += 1
        elif c == 2:
            interviewer[who]["fail"] += 1
        else:
            interviewer[who]["pending"] += 1
    interviewer_stats = {}
    for who, s in sorted(interviewer.items(), key=lambda kv: -(kv[1]["pass"] + kv[1]["fail"])):
        decided = s["pass"] + s["fail"]
        interviewer_stats[who] = {
            **s,
            "decided": decided,
            "pass_rate": round(s["pass"] / decided, 3) if decided else None,
        }

    # ---- app 级面试桶(可选) labels_v2.json schema: {iv_pass:[aid], iv_fail:[aid], screen_pass:[aid]} ----
    _lv = src / "labels_v2.json"
    lv2 = json.load(open(_lv)) if _lv.exists() else {}
    iv_pass = set(lv2.get("iv_pass", []))
    iv_fail = set(lv2.get("iv_fail", []))
    screen_pass = set(lv2.get("screen_pass", []))

    # ---- 撤回(WEAK 负边界) ----
    wd_path = src / "withdrawn_ids.txt"
    withdrawn = {l.strip() for l in open(wd_path) if l.strip()} if wd_path.exists() else set()

    # ---- LLM 精读结果(附着,非标签源,但入 store 便于 harness join) ----
    merged = json.load(open(ws / "llm_review/merged.json"))

    candidates = {}
    for aid, r in rows.items():
        name = r["name"]
        stage = r.get("stage") or ""
        active = r.get("active") or ""

        rec = {"name": name, "stage": stage, "status": active, "tiers": []}

        # staff:作为候选人不计任何标签 tier(避免污染终点集/强基准)
        if aid in staff_ids:
            rec["staff"] = True
            rec["endpoint_positive"] = False
            rec["tier"] = "staff"
            candidates[aid] = rec
            continue

        # PRIMARY-1: 人工评估
        if aid in manual:
            rec["manual_eval"] = parse_manual_eval(manual[aid]["eval"])
            rec["tiers"].append("primary:manual")

        # PRIMARY-2: 终点真阳性
        rec["endpoint_positive"] = stage in endpoint_stages
        if rec["endpoint_positive"] and aid in endpoint_exclude:
            rec["endpoint_positive"] = False
            rec["endpoint_excluded"] = True   # 来源不属于本管道(见模块说明),不计终点正样本

        # 终点 HM 参与门槛(HM 2026-11-04 口径):同事代面推进到终点的,不算本岗位正样本;
        # 特别标注样本按人豁免。先攒面试轮(面试标签在后面才读,这里只初始化)。
        # 仅在开启门槛时写标记,缺省不留痕(输出与旧版逐字段一致)。
        if endpoint_require_hm:
            rec["_ep_hm_pending"] = rec["endpoint_positive"]
        if rec["endpoint_positive"]:
            rec["tiers"].append("primary:endpoint")

        # 面试结论。分两档记:
        #   hm   —— **HM 本人的判定**(含跨岗记录),权重高于其他面试官(HM 拍板口径)。
        #           理由:面试结论降级为 REFERENCE 的依据是"面试官宽严方差大"(实测 33%~63%),
        #           但 HM 自己的宽严**就是这个岗位的 bar**,不该被这条理由折价 → 升 PRIMARY。
        #   final —— 流程结论,给 §3 分离度用,仍是 REFERENCE。
        #
        # ⚠️ final 的两处修正(实测踩过):
        #   ① 旧实现取"最后一轮已判定结论"却**跳过其后未判定的轮次**——一面通过、二面 HM 未写
        #      面评(因为判待定),会被算成 pass,把在途的人当成已通过统计,污染分离度。
        #      现在:存在比最后判定更晚的未判定轮 → pending。
        #   ② HM 已判定时 final 以 HM 为准(权重更高,不是覆盖:其他轮仍完整记在 rounds 与
        #      n_pass/n_fail 里,分歧用 hm_conflict 显式标出,便于人查而不是被抹掉)。
        ivs = list(iv_by_aid.get(aid, []))
        ivs += xjob_hm.get(aid, [])          # 跨岗只取 HM 本人(见 pull_xjob.py 口径)
        if ivs or aid in iv_pass or aid in iv_fail:
            n_pass = sum(1 for x in ivs if x.get("conclusion") == 1)
            n_fail = sum(1 for x in ivs if x.get("conclusion") == 2)
            decided = [x for x in ivs if x.get("conclusion") in (1, 2)]
            undecided = [x for x in ivs if x.get("conclusion") not in (1, 2)]
            hm_dec = [x for x in decided if x.get("interviewer") == HM_NAME]
            hm_last = max(hm_dec, key=lambda x: (x.get("t") or 0, x.get("round") or 0)) if hm_dec else None

            if aid in iv_pass:
                final = "pass"
            elif aid in iv_fail:
                final = "fail"
            elif hm_last:
                final = "pass" if hm_last["conclusion"] == 1 else "fail"
            elif decided:
                last = max(decided, key=lambda x: (x.get("t") or 0, x.get("round") or 0))
                later_undecided = [u for u in undecided
                                   if (u.get("t") or 0, u.get("round") or 0) > (last.get("t") or 0, last.get("round") or 0)]
                final = "pending" if later_undecided else ("pass" if last["conclusion"] == 1 else "fail")
            else:
                final = "pending"

            others_dec = [x for x in decided if x.get("interviewer") != HM_NAME]
            conflict = bool(hm_last and others_dec and
                            any(x["conclusion"] != hm_last["conclusion"] for x in others_dec))
            rec["interview"] = {
                "final": final,
                "hm": ("pass" if hm_last["conclusion"] == 1 else "fail") if hm_last else None,
                "hm_conflict": conflict,
                "n_pass": n_pass,
                "n_fail": n_fail,
                "rounds": [
                    {"interviewer": x.get("interviewer"), "conclusion": x.get("conclusion"),
                     "round": x.get("round"), "xjob": x.get("other_job")}
                    for x in ivs
                ],
            }
            rec["tiers"].append("reference:interview")
            if hm_last:
                rec["tiers"].append("primary:hm_interview")

        # 面试轮读完后执行 HM 参与门槛(与上面 primary:hm_interview 同判据:HM 是否有已判定的轮)
        if endpoint_require_hm and rec.pop("_ep_hm_pending", False) and aid not in endpoint_hm_exempt:
            ivs_all = list(iv_by_aid.get(aid, []))
            ivs_all += xjob_hm.get(aid, [])      # 跨岗只取 HM 本人(见 pull_xjob.py 口径)
            hm_round = any(x.get("interviewer") == HM_NAME for x in ivs_all)
            if not hm_round:
                rec["endpoint_positive"] = False
                rec["endpoint_excluded_hm"] = True   # 同事代面推进,非 HM 亲面;豁免名单由 HM 逐人定

        # WEAK: 撤回/筛选通过
        if aid in withdrawn:
            rec["withdrawn"] = True
            rec["tiers"].append("weak:withdrawn")
        if aid in screen_pass:
            rec["screen_pass"] = True

        # 附 LLM 精读(非标签)
        if aid in merged:
            m = merged[aid]
            rec["llm"] = {"score": m.get("llm_score"), "verdict": m.get("verdict"),
                          "direction": m.get("direction")}

        # 顶层 tier(取最强)
        if any(t.startswith("primary") for t in rec["tiers"]):
            rec["tier"] = "primary"
        elif any(t.startswith("reference") for t in rec["tiers"]):
            rec["tier"] = "reference"
        elif rec["tiers"]:
            rec["tier"] = "weak"
        else:
            rec["tier"] = "none"

        candidates[aid] = rec

    tier_counts = Counter(c["tier"] for c in candidates.values())
    primary_appids = [a for a, c in candidates.items() if c["tier"] == "primary"]

    store = {
        "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
        "note": "四源合一标签库；tier=primary(人工评估∪终点真阳性)为回归强基准，reference(面试结论)因面试官方差降为参考",
        "sources": {
            "report_rows": len(rows),
            "manual_eval": len(manual),
            "interview_records": len(interviews),
            "iv_pass": len(iv_pass), "iv_fail": len(iv_fail),
            "screen_pass": len(screen_pass), "withdrawn": len(withdrawn),
        },
        "tier_counts": dict(tier_counts),
        "primary_count": len(primary_appids),
        "interviewer_stats": interviewer_stats,
        "candidates": candidates,
    }
    (out / "label_store.json").write_text(json.dumps(store, ensure_ascii=False, indent=1))

    # 控制台摘要
    print(f"label_store.json 写出：{len(candidates)} 候选")
    print(f"  tier: {dict(tier_counts)}")
    print(f"  强基准(primary): {len(primary_appids)} 人")
    print(f"  面试官宽严(通过率):")
    for who, s in interviewer_stats.items():
        if s["decided"]:
            print(f"    {who:6s} 判定{s['decided']:2d} 通过率 {s['pass_rate']}")


if __name__ == "__main__":
    main()
