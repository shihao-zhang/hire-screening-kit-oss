#!/usr/bin/env python3
"""回归 harness(飞轮②):六检查一键化,每轮改评分体系后跑一次。

用法:
  python3 core/harness.py --config <job.toml> --workspace <dir> [--out-dir <dir>]
                          [--prev out/report_prev.csv] [--changed-dims 稳定性,pedigree]

输入: out/report.csv(当前) + out/label_store.json + out/attribution_state.json (+可选 prev report)
输出: out/harness_report.md + out/endpoint_missed.json(供归因队列消费) + 控制台闸门摘要

指标设计纪律(校准得来):
  - 核心一致率只用 PRIMARY 强基准(人工评估 ∪ 终点真阳性);
  - 面试结论(REFERENCE)隔离到 §3,只做分离度参考,强制附每面试官通过率(方差 caveat);
  - 循环引用防护:人工评估与规则可能共享盲区,一致率高≠预测有效;
    唯一无污染的"预测有效性"信号是终点真阳性召回(§2b)。
  - 实例特定的效度声明文案在配置([harness]),含召回子句设计史——防止后人把
    护栏指标误读成泛化证据。
"""
import argparse
import csv
import json
import random
import statistics
from collections import Counter
import tomllib
from pathlib import Path


def load_report(path):
    rows = {}
    for r in csv.DictReader(open(path)):
        rows[r["application_id"]] = r
    return rows


def f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pct(rank, n):
    """rank(1=最好) → 顶部百分位。返回'前X%'。"""
    if n <= 1:
        return 0.0
    return round((int(rank) - 1) / (n - 1) * 100, 1)


def quantiles(vals):
    vals = sorted(vals)
    if not vals:
        return {}
    def q(p):
        i = min(len(vals) - 1, int(p * (len(vals) - 1)))
        return round(vals[i], 1)
    return {"min": round(vals[0], 1), "p10": q(.1), "p25": q(.25), "p50": q(.5),
            "p75": q(.75), "p90": q(.9), "max": round(vals[-1], 1)}


def hist_max_share(vals, lo, hi, step):
    """最大单档占比(防塌缩/饱和闸门)。"""
    bins = {}
    edges = []
    x = lo
    while x < hi:
        edges.append(x); x += step
    for v in vals:
        placed = False
        for e in edges:
            if e <= v < e + step:
                bins[e] = bins.get(e, 0) + 1; placed = True; break
        if not placed:
            bins["out"] = bins.get("out", 0) + 1
    if not vals:
        return 0.0, None
    top_edge = max(bins, key=bins.get)
    return round(max(bins.values()) / len(vals) * 100, 1), top_edge


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--prev", default=None, help="上一版 report.csv 路径(算 delta)")
    ap.add_argument("--changed-dims", default="", help="本轮改动的维度(逗号分隔，用于零变化校验说明)")
    ap.add_argument("--version", default="", help="本轮版本号(写入 metrics_ledger.jsonl，便于读趋势)")
    args = ap.parse_args()
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    hc = cfg["harness"]
    ws = Path(args.workspace)
    src = ws / "out"
    out = Path(args.out_dir) if args.out_dir else src
    out.mkdir(parents=True, exist_ok=True)

    # ⚠️ report.csv 也要优先取 --out-dir:否则"模拟不落盘"跑 harness 时会静默读到
    #    正式 out/report.csv,报出的护栏指标全是**旧版本**的,看起来"改动无影响"。
    #    (实测踩过:精读网数与 diff 对不上才发现。)
    cur = load_report(out / "report.csv" if (out / "report.csv").exists()
                      else src / "report.csv")
    store = json.load(open(out / "label_store.json" if (out / "label_store.json").exists()
                           else src / "label_store.json"))
    cand = store["candidates"]
    n = len(cur)
    LTR_TH = hc["ltr_threshold"]
    RULE_TOP = hc["rule_recall_top_pct"]
    SYS_PASS = hc["sys_rate_pass_pct"]
    COLLAPSE = hc["collapse_pct"]
    SATURATE = hc["saturate_pct"]

    gates = []  # (名称, 状态, 说明)  状态 ∈ PASS/WARN/FAIL/INFO
    md = []
    md.append(f"# 回归 harness 报告")
    md.append(f"\n生成基准：`label_store` @ {store['generated_at']}｜候选 {n} 人\n")

    # ========== §1 标签库存 & LTR 水位 ==========
    tc = store["tier_counts"]
    primary = store["primary_count"]
    md.append("## §1 标签库存 & LTR 水位\n")
    md.append(f"| tier | 数量 | 说明 |")
    md.append(f"|---|---|---|")
    md.append(f"| primary(强基准) | {tc.get('primary',0)} | 人工评估 ∪ 终点真阳性，回归核心基准 |")
    md.append(f"| reference(参考) | {tc.get('reference',0)} | 面试结论，因面试官方差降级 |")
    md.append(f"| weak/none | {tc.get('weak',0)+tc.get('none',0)} | 阶段/撤回/无标签 |")
    ltr_ok = primary >= LTR_TH
    gates.append(("LTR 水位", "INFO",
                  f"强标签 {primary}/{LTR_TH}，{'达阈值可评估 LTR' if ltr_ok else '仍不足，继续规则/rubric 校准'}"))
    md.append(f"\n**LTR 水位**：强标签 {primary} 条，阈值 {LTR_TH}——{'✅ 可开始评估' if ltr_ok else '❌ 未达阈值，本阶段维持规则校准（首要目标=运营提效）'}\n")

    # ========== §2 主基准对齐(强) ==========
    md.append("## §2 主基准对齐（强基准，核心指标）\n")
    md.append("> ⚠️ **循环引用防护**：人工评估(§2a)与规则可能共享盲区，一致率高≠预测有效。"
              "唯一无污染的预测有效性信号是**终点真阳性召回(§2b)**——他们走到漏斗末端不依赖单一面试官。\n")

    # --- §2a 人工评估 vs 规则/LLM ---
    # 两层系统度量：规则层(裸排名) vs 系统层(LLM 精读纠正后)。差值=LLM 层价值。
    # 消化 rank_hint：有排名提示时，提示是排名方向的权威信号，压过 verdict→位次的粗映射。
    md.append(hc["manual_header"])
    md.append("| 人工 | 提示 | 候选 | rank | 前% | total | LLM | 规则层 | 系统层 |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    manual_rows = [(a, c) for a, c in cand.items() if "manual_eval" in c]
    rule_ok = rule_tot = sys_ok = sys_tot = 0
    sys_conflicts = []
    for a, c in sorted(manual_rows, key=lambda x: f(cur.get(x[0], {}).get("rank"), 9999)):
        r = cur.get(a)
        if not r:
            continue
        me = c["manual_eval"]
        v = me["verdict"]; hint = me["rank_hint"]
        rk = r["rank"]; p = pct(rk, n); tot = f(r["total"])
        llm = c.get("llm", {}) or {}
        llm_v = llm.get("verdict")
        # LLM 信号三态
        sig = {"推荐面试": "pos", "待定": "neu", "不推荐": "neg"}.get(llm_v)
        if sig is None:  # 未精读 → 用规则位次代理
            sig = "pos" if p <= 50 else "neg"
        # 一致性判定
        rmark = smark = "—"
        if hint:  # 提示权威：偏低→应前50%，偏高→应后50%
            want_high = (hint == "偏低")
            r_c = (p <= 50) == want_high
            rule_tot += 1; rule_ok += r_c
            sys_tot += 1; sys_ok += r_c  # 排名类提示两层同判
            rmark = smark = "✓" if r_c else "✗"
            if not r_c:
                sys_conflicts.append((c["name"], f"排名{hint}", rk, p, llm_v))
        elif v in ("通过", "不通过"):
            r_c = (v == "通过" and p <= 50) or (v == "不通过" and p > 50)
            rule_tot += 1; rule_ok += r_c
            rmark = "✓" if r_c else "✗"
            s_c = (v == "通过" and sig in ("pos", "neu")) or (v == "不通过" and sig in ("neg", "neu"))
            sys_tot += 1; sys_ok += s_c
            smark = "✓" if s_c else "✗"
            if not s_c:
                sys_conflicts.append((c["name"], v, rk, p, llm_v))
        md.append(f"| {v} | {hint or '—'} | {c['name']} | {rk} | {p}% | {tot} | {llm.get('score') or '—'}/{llm_v or '—'} | {rmark} | {smark} |")
    rrate = round(rule_ok / rule_tot * 100, 1) if rule_tot else None
    srate = round(sys_ok / sys_tot * 100, 1) if sys_tot else None
    md.append(f"\n**规则层一致率**：{rule_ok}/{rule_tot} = {rrate}%（裸排名，反映规则层语义盲区所在）")
    md.append(f"**两层系统一致率**：{sys_ok}/{sys_tot} = {srate}%（LLM 精读纠正后）")
    if rrate is not None and srate is not None:
        md.append(f"**LLM 层价值 = {round(srate - rrate, 1)}pp**（规则层拦不住、LLM 层补回的语义判断）")
    md.append(f"\n> ⚠️ 系统层高一致率**可能含过拟合**：这些人工评估正是 5 轮 rubric 校准的调参样本，"
              f"与规则/rubric 可能共享盲区。真正无污染的有效性看 §2b。")
    if sys_conflicts:
        md.append("\n**系统层冲突(需归因，规则+LLM 都没对齐人工)**：")
        for nm, v, rk, p, llm_v in sys_conflicts:
            md.append(f"- {nm}：人工「{v}」但 rank{rk}(前{p}%) LLM{llm_v or '未精读'} —— 待人工归因(维度错/权重错/漏信号?)")
    gate_state = "PASS" if (srate is not None and srate >= SYS_PASS) else ("WARN" if srate is not None else "INFO")
    gates.append(("两层系统一致率", gate_state,
                  f"系统层 {srate}%（规则层 {rrate}%，LLM 补 {round((srate or 0)-(rrate or 0),1)}pp），冲突 {len(sys_conflicts)} 例"))

    # --- §2b 终点真阳性召回 ---
    md.append("\n### §2b 终点真阳性召回（无污染预测有效性）\n")
    md.append(hc["recall_preamble"])
    md.append("| 候选 | stage | rank | 前% | total | role_fit | 规则层 | 精读网 | LLM | 系统召回 |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    # 已决策接受的终点漏网(飞轮③ 人工归因后 accept 的,不再当"新警报",降 FAIL→WARN)
    astate_p = src / "attribution_state.json"
    astate = json.loads(astate_p.read_text()) if astate_p.exists() else {}
    accepted_miss = set((astate.get("accepted_endpoint_miss") or {}).keys())

    ep = [(a, c) for a, c in cand.items() if c.get("endpoint_positive")]
    ep_missed = []       # 规则层漏(粗排埋低位)
    sys_missed = []      # 系统漏(没进网 或 被误杀)——含已接受
    sys_missed_new = []  # 其中"未决策"的(=真警报,gate 用它判 FAIL)
    sys_missed_full = [] # 同上但带 aid+详情,供 attribution_queue(飞轮③)消费
    rescued = []         # 粗排埋了但精读网捞回——召回子句的价值
    for a, c in sorted(ep, key=lambda x: f(cur.get(x[0], {}).get("rank"), 9999)):
        r = cur.get(a)
        if not r:
            continue
        rk = r["rank"]; p = pct(rk, n); tot = f(r["total"]); rf = r.get("role_fit", "—")
        llm = c.get("llm", {}) or {}
        verdict = llm.get("verdict")
        in_net = str(r.get("llm_scope", "")).lower() == "true"

        rule_ok = p <= RULE_TOP                 # 规则层：终点真阳性理应进前N%
        if not rule_ok:
            ep_missed.append((c["name"], rk, p, tot, in_net))

        # 系统层：进网 且 未被误杀
        if not in_net:
            if verdict:
                # 精读过、只是现在不在网(近期子句到期、基准日对表或口径收紧后出网)。仍计系统漏网
                # (闸门口径不变),但原因如实写:不能说成「人根本没看到」——这段文本会进 HM 看的看板。
                sys_mark = "✗出网"
                sys_reason = f"已精读({llm.get('score')}/{verdict}),当前不在精读网(近期子句到期或口径收紧)"
            else:
                sys_mark, sys_reason = "✗漏网", "未进精读网，人根本没看到"
        elif verdict == "不推荐":
            sys_mark, sys_reason = "✗误杀", "进网但判「不推荐」"
        else:
            sys_mark, sys_reason = "✓", None
        if sys_reason:
            is_acc = a in accepted_miss
            sys_missed.append((c["name"], rk, p, sys_reason + ("（已决策接受）" if is_acc else "")))
            sys_missed_full.append({"aid": a, "name": c["name"], "stage": c.get("stage"),
                                    "rank": rk, "pct": p, "total": tot, "role_fit": rf,
                                    "reason": sys_reason, "accepted": is_acc})
            if not is_acc:
                sys_missed_new.append((c["name"], rk, p, sys_reason))
        elif not rule_ok:
            rescued.append((c["name"], rk, p, verdict))

        md.append(f"| {c['name']} | {c['stage']} | {rk} | {p}% | {tot} | {rf} | "
                  f"{'✓' if rule_ok else '✗埋'} | {'✓' if in_net else '✗'} | "
                  f"{llm.get('score') or '—'}/{verdict or '—'} | {sys_mark} |")

    n_ep = len(ep)
    rule_hit = n_ep - len(ep_missed)
    sys_hit = n_ep - len(sys_missed)
    # 落文件供飞轮③ attribution_queue 消费(终点真阳性漏网=最无污染信号,不该只躺在报告里)
    (out / "endpoint_missed.json").write_text(json.dumps(
        {"n_endpoint": n_ep, "n_missed": len(sys_missed_full), "missed": sys_missed_full},
        ensure_ascii=False, indent=1))
    md.append(f"\n**规则层召回**：{rule_hit}/{n_ep} 落在前{RULE_TOP}%")
    md.append(f"\n**系统召回**：{sys_hit}/{n_ep} 进了精读网且未被判不推荐 ← **这个才是交付给人的口径**")
    if rescued:
        md.append("\n**✅ 精读网捞回(粗排埋了但系统仍交付给人——两层架构的价值实证)**：")
        for nm, rk, p, v in rescued:
            md.append(f"- {nm}：rank{rk}(前{p}%) 被粗排埋在{RULE_TOP}%外，靠精读网捞回，LLM 判「{v}」")
    if sys_missed:
        md.append("\n**⚠️ 系统召回盲区(真阳性没被交付给人——最该关注的信号)**：")
        for nm, rk, p, why in sys_missed:
            md.append(f"- {nm}：走到漏斗末端却 {why}（rank{rk}，前{p}%）")
    if ep_missed and not sys_missed:
        md.append(f"\n> 注：规则层漏 {len(ep_missed)} 例但系统全召回——粗排权重仍值得查，"
                  f"但**优先级低于系统召回**（人已经看到这些人了）。")
    md.append(hc["validity_note"])
    ep_rate = round(sys_hit / n_ep * 100, 1) if n_ep else None
    n_acc = len(sys_missed) - len(sys_missed_new)
    sys_gate = "FAIL" if sys_missed_new else ("WARN" if sys_missed else "PASS")
    gates.append(("终点真阳性·系统召回", sys_gate,
                  f"{sys_hit}/{n_ep} 进网且未误杀"
                  + (f"；另 {n_acc} 例已决策接受(非新警报)" if n_acc else "")
                  + (f"；{len(sys_missed_new)} 例新漏网待查" if sys_missed_new else "")
                  + hc["gate_sys_suffix"]))
    gates.append(("终点真阳性·规则层召回", "WARN" if ep_missed else "PASS",
                  f"{rule_hit}/{n_ep} 前{RULE_TOP}%，埋 {len(ep_missed)} 例"
                  + (f"（其中 {len(rescued)} 例已被精读网捞回）" if rescued else "")))
    # 精读网成本面
    net_n = sum(1 for r in cur.values() if str(r.get("llm_scope", "")).lower() == "true")
    md.append(f"\n**精读网覆盖**：{net_n}/{n} 人（{round(net_n/n*100,1)}%）——召回与成本的平衡点，"
              f"扩网提召回但线性推高精读开销。")
    gates.append(("精读网覆盖", "INFO", f"{net_n}/{n}（{round(net_n/n*100,1)}%）"))

    # ========== §2c 终点漏网归因(只读) ==========
    # 飞轮复盘(METHODOLOGY §7.1):护栏过去只报「规则层召回 x/12」,被埋的真阳性连续多轮印在
    # 表里却没人算共性——直到 HM 提出头部加权才发现被埋者全是双背书、被乘数带宽困住。
    # 这里对每个排在前 RULE_TOP% 之外的终点真阳性,算拖分维度与背书构成,只报现象不改规则。
    DIMS = ["education", "major", "years", "skills", "company", "stability", "project"]
    pool_med = {d: statistics.median([f(r.get(d)) for r in cur.values()]) for d in DIMS}
    buried = []
    for a, c in ep:
        r = cur.get(a)
        if not r or pct(r["rank"], n) <= RULE_TOP:
            continue
        drag = [d for d in DIMS if f(r.get(d)) < pool_med[d] - 10]
        buried.append((c["name"], r["rank"], pct(r["rank"], n), r.get("pedigree", "—"), drag))
    md.append("\n### §2c 终点漏网归因(只读,不改规则)\n")
    if buried:
        md.append(f"> 全池各维度中位数:" + "、".join(f"{d} {pool_med[d]:.0f}" for d in DIMS))
        md.append("\n| 被埋真阳性 | rank | 前% | 背书 | 拖分维度(低于池中位 10 分以上) |")
        md.append("|---|---|---|---|---|")
        for nm, rk, p, ped, drag in buried:
            md.append(f"| {nm} | {rk} | {p}% | {ped} | {'、'.join(drag) or '—'} |")
        ped_cnt = Counter(b[3] for b in buried)
        drag_cnt = Counter(d for b in buried for d in b[4])
        md.append(f"\n**共性**:背书构成 {dict(ped_cnt)};最常见拖分维度 "
                  + ("、".join(f"{d}×{k}" for d, k in drag_cnt.most_common(3)) or "无"))
        md.append("\n> 读法:若被埋者**集中在某一背书档**(例:全是双背书),说明背书乘数带宽可能困住了他们;"
                  "若集中在某一拖分维度,说明该维度对这类人的打分口径值得查。**只作假设来源,改规则仍需人拍板**。")
    else:
        md.append("本轮无被埋在前 RULE_TOP% 之外的终点真阳性。")

    # ========== §3 参考基准(面试结论，带方差) ==========
    md.append("\n## §3 参考基准：面试结论（降级，仅参考）\n")
    md.append("> ⚠️ 面试结论受**面试官宽严方差**污染，不进核心一致率。下表仅看"
              "「粗排是否与面试结论同向」的分离度，且必须对照每面试官通过率读。\n")
    iv_pass_tot, iv_fail_tot = [], []
    iv_pass_llm, iv_fail_llm = [], []
    for a, c in cand.items():
        iv = c.get("interview")
        r = cur.get(a)
        if not iv or not r:
            continue
        tot = f(r["total"])
        llm = (c.get("llm") or {}).get("score")
        if iv["final"] == "pass":
            iv_pass_tot.append(tot)
            if llm is not None: iv_pass_llm.append(llm)
        elif iv["final"] == "fail":
            iv_fail_tot.append(tot)
            if llm is not None: iv_fail_llm.append(llm)
    def mean(x): return round(statistics.mean(x), 1) if x else None
    md.append(f"| 分离度 | 面试通过 | 面试不通过 | 差 |")
    md.append(f"|---|---|---|---|")
    sep_tot = (mean(iv_pass_tot) or 0) - (mean(iv_fail_tot) or 0)
    sep_llm = (mean(iv_pass_llm) or 0) - (mean(iv_fail_llm) or 0)
    md.append(f"| 规则 total 均分 | {mean(iv_pass_tot)}(n{len(iv_pass_tot)}) | {mean(iv_fail_tot)}(n{len(iv_fail_tot)}) | {round(sep_tot,1)} |")
    md.append(f"| LLM 均分 | {mean(iv_pass_llm)}(n{len(iv_pass_llm)}) | {mean(iv_fail_llm)}(n{len(iv_fail_llm)}) | {round(sep_llm,1)} |")
    md.append(f"\n**面试官宽严(为何降级的证据)**：")
    md.append(f"| 面试官 | 判定 | 通过率 |")
    md.append(f"|---|---|---|")
    for who, s in store["interviewer_stats"].items():
        if s["decided"] >= 3:
            md.append(f"| {who} | {s['decided']} | {s['pass_rate']} |")
    gates.append(("面试结论分离度(参考)", "INFO",
                  f"total 差 {round(sep_tot,1)}｜LLM 差 {round(sep_llm,1)}" + hc["iv_note_suffix"]))

    # ========== §4 分布健康度 ==========
    md.append("\n## §4 分布健康度（防塌缩/饱和）\n")
    totals = [f(r["total"]) for r in cur.values()]
    skills = [f(r.get("skills")) for r in cur.values()]
    qt = quantiles(totals)
    md.append(f"- total 分位：{qt}")
    md.append(f"- skills 分位：{quantiles(skills)}")
    share, top_edge = hist_max_share(totals, 10, 100, 10)
    # 真塌缩=单档占多数(判别力丢失)；真饱和=顶档[80+]堆积(天花板打满)。
    # 中位档出现峰(近正态)是健康的，不触发。
    top_share = round(sum(1 for t in totals if t >= 80) / len(totals) * 100, 1)
    collapse = share > COLLAPSE
    saturate = top_share > SATURATE
    dist_ok = not (collapse or saturate)
    md.append(f"- 峰值档占比：{share}%（档 {top_edge}）— 塌缩闸门 <{COLLAPSE}% {'✅' if not collapse else '❌ 判别力丢失'}")
    md.append(f"- 顶档[80+]占比：{top_share}% — 饱和闸门 <{SATURATE}% {'✅' if not saturate else '❌ 天花板打满'}")
    md.append(f"- （峰在中位档属近正态健康形态，不触发闸门）")
    gates.append(("分布健康度", "PASS" if dist_ok else "FAIL",
                  f"峰档 {share}%(<{COLLAPSE}%)｜顶档[80+] {top_share}%(<{SATURATE}%)"))

    # ── 区分度(正向指标,补护栏的单向性)──────────────────────────────
    # 分布闸门只抓塌缩/饱和,不会告诉你「分数还可以分得更开」(METHODOLOGY §6-10)。
    # 这里算总分对 PRIMARY 标签的标准化效应量 (正均值-负均值)/总体标准差,跨版本追踪。
    # 置换检验固定种子,保证同一份数据跑两次结果逐字节相同。⚠️ 仍在圈选网内测,非泛化证据。
    lab = {}
    for a, c in cand.items():
        if a not in cur or not any(t.startswith("primary") for t in (c.get("tiers") or [])):
            continue
        y = None
        if c.get("endpoint_positive"):
            y = 1
        else:
            hm = (c.get("interview") or {}).get("hm")
            if hm == "pass":
                y = 1
            elif hm == "fail":
                y = 0
            mv = str((c.get("manual_eval") or {}).get("verdict") or "")
            if "不通过" in mv:
                y = 0
            elif "通过" in mv:
                y = 1
        if y is not None:
            lab[a] = y
    pos = [f(cur[a]["total"]) for a, y in lab.items() if y == 1]
    neg = [f(cur[a]["total"]) for a, y in lab.items() if y == 0]
    if len(pos) >= 5 and len(neg) >= 5:
        allv = pos + neg
        sd = statistics.pstdev(allv) or 1.0
        diff = statistics.mean(pos) - statistics.mean(neg)
        es = diff / sd
        rnd = random.Random(20260914); k = len(pos); hit = 0; N_PERM = 5000
        pool = allv[:]
        for _ in range(N_PERM):
            rnd.shuffle(pool)
            if abs(sum(pool[:k]) / k - sum(pool[k:]) / (len(pool) - k)) >= abs(diff):
                hit += 1
        pval = hit / N_PERM
        md.append(f"- **区分度**(总分对 PRIMARY 标签,正 {len(pos)}/负 {len(neg)}):"
                  f"均值差 {diff:+.2f} 分、标准化效应量 **{es:.3f}**、置换 p={pval:.3f}")
        gates.append(("区分度(参考)", "INFO",
                      f"效应量 {es:.3f}(正{len(pos)}/负{len(neg)}),p={pval:.3f}｜圈选网内测,非泛化证据"))

    # ========== §5 版本 delta ==========
    if args.prev and Path(args.prev).exists():
        prev = load_report(args.prev)
        md.append(f"\n## §5 版本 delta（vs `{Path(args.prev).name}`）\n")
        deltas = []
        for a, r in cur.items():
            p = prev.get(a)
            if not p:
                continue
            dt = f(r["total"]) - f(p["total"])
            drk = int(p["rank"]) - int(r["rank"])  # 正=上升
            if abs(dt) > 1e-6:
                deltas.append((r["name"], a, dt, drk, f(p["total"]), f(r["total"])))
        deltas.sort(key=lambda x: -abs(x[2]))
        newcomers = [a for a in cur if a not in prev]
        md.append(f"- 新增候选：{len(newcomers)} 人；总分有变动：{len(deltas)} 人")
        cd = args.changed_dims or "(未声明)"
        md.append(f"- 本轮声明改动维度：**{cd}** —— 未涉及维度理论上零变化，下表变动最大者请逐个确认可解释")
        md.append(f"\n| 候选 | Δtotal | Δrank | 旧→新 | 归因(人填) |")
        md.append(f"|---|---|---|---|---|")
        for nm, a, dt, drk, ot, nt in deltas[:15]:
            md.append(f"| {nm} | {dt:+.1f} | {drk:+d} | {ot}→{nt} |  |")
        gates.append(("版本 delta", "INFO", f"{len(deltas)} 人变动，top15 待归因；新增 {len(newcomers)} 人"))

        # ---------- §5b 规则-LLM 边界方向一致率(先行指标) ----------
        # 规则改动把某人推进/推出精读网时，看该人**已有的独立 LLM 判定**是否与移动方向同向：
        # 进网者不该是「不推荐」，出网者不该是「推荐」。
        # 为什么用它当日常拍板的先行指标：终点真阳性只有个位数且已饱和(连续多版 9/10 纹丝不动)，
        # 而边界样本**每次改动必然产生**，分辨率高得多。
        # ⚠️ 效度前提(必须守)：只有当规则改动是**独立于 LLM 输出**推导出来的，它才算效度证据。
        #    一旦拿它当调参目标去拟合，就变成自证，指标立刻失去意义。
        # ⚠️ **判定必须早于改动**:读当前 verdict 会把「本轮进网、本轮才精读」的人算进来,
        #    那是改动之后产生的判定,构成循环自证(实测踩过:词表改动把 2 人推进网,同轮精读后
        #    §5b 报 2/2 全同向,而那两条判定正是改动之后写的)。故用**上一轮结束时的快照**
        #    out/verdicts_prev.json 作「已有判定」的唯一来源,本轮新产生的判定一律不计入。
        vp = out / "verdicts_prev.json"
        prior_v = json.load(open(vp)) if vp.exists() else {}

        def in_net(r):
            return str(r.get("llm_scope", "")).lower() == "true"
        entered = [a for a, r in cur.items() if in_net(r) and a in prev and not in_net(prev[a])]
        left = [a for a, r in prev.items() if in_net(r) and a in cur and not in_net(cur[a])]
        agree, disagree, nolabel = [], [], []
        for a, direction in [(x, "进网") for x in entered] + [(x, "出网") for x in left]:
            v = prior_v.get(a)          # 只认改动前就存在的判定
            nm = (cur.get(a) or prev.get(a) or {}).get("name", a)
            if not v:
                nolabel.append((nm, direction)); continue
            ok = (v != "不推荐") if direction == "进网" else (v != "推荐面试")
            (agree if ok else disagree).append((nm, direction, v))
        n_lab = len(agree) + len(disagree)
        md.append("\n## §5b 规则-LLM 边界方向一致率（先行指标）\n")
        md.append("> 规则改动导致进/出精读网的人里，**已有独立 LLM 判定**的那些，移动方向是否与判定同向"
                  "（进网者非「不推荐」、出网者非「推荐面试」）。\n>\n"
                  "> ⚠️ **仅当本轮规则改动独立于 LLM 输出推导时**，此项才构成效度证据；"
                  "拿它当调参目标即自证失效。\n")
        md.append(f"- 边界样本 {len(entered) + len(left)} 人（进 {len(entered)} / 出 {len(left)}），"
                  f"其中 {n_lab} 人**在本轮改动前**已有 LLM 判定、{len(nolabel)} 人当时未精读（不计入）")
        if n_lab:
            md.append(f"- **方向一致 {len(agree)}/{n_lab}**")
            for nm, d, v in disagree:
                md.append(f"  - ❗反向：{nm} {d} 但 LLM 判「{v}」")
        if nolabel:
            md.append(f"- 未精读(不计入)：{'、'.join(nm + d for nm, d in nolabel)}")
        if n_lab == 0:
            gates.append(("规则-LLM 边界一致率", "INFO",
                          f"边界 {len(entered)+len(left)} 人但无一有 LLM 判定，本轮无信号"))
        else:
            rate = round(len(agree) / n_lab * 100, 1)
            # 小样本保护:n<5 时比率没有意义(1 例反向就掉到 50%~80%)，只列出反向个案供人查，
            # 不设闸门。**本指标是调查触发器，不是判决**——实测反向例查下来是正确修复
            # (「商业与数据分析」正确归入商科而上分，此人只是恰好被 LLM 判不推荐)。
            if n_lab < 5:
                st = "INFO"
                extra = "（样本<5，比率不作判据，只看反向个案）"
            else:
                st = "PASS" if rate >= 80 else ("WARN" if rate >= 60 else "FAIL")
                extra = ""
            gates.append(("规则-LLM 边界一致率", st,
                          f"{len(agree)}/{n_lab}（{rate}%）方向同向"
                          + (f"；反向待查：{'、'.join(nm for nm, _, _ in disagree)}" if disagree else "")
                          + extra))
    else:
        md.append(f"\n## §5 版本 delta\n\n(未提供 --prev，跳过)")
        gates.append(("版本 delta", "INFO", "未提供 prev"))

    # ========== §6 闸门汇总 ==========
    md.insert(2, _gate_summary(gates))  # 置顶到报告开头(标题+基准行之后)

    (out / "harness_report.md").write_text("\n".join(md))

    # ---------- 指标台账:每轮追加一行,让趋势可见 ----------
    # 单次快照看不出"这个指标有没有分辨力"。实测教训:终点召回连续 5 个版本都是 9/10、
    # 一致率一直 85.0%,而面试分离度 1.2→1.8→2.0 一直在动——没有台账就看不出来谁在提供信号。
    # 只记不判:台账不设闸门,是给人读趋势用的。
    ledger = out / "metrics_ledger.jsonl"
    row = {
        "version": args.version or "(未标注)",
        "n": n,
        "endpoint_sys_recall": f"{sys_hit}/{n_ep}",       # ★ 北极星(与覆盖率成对读)
        "net_coverage_pct": round(net_n / n * 100, 1),    # ★ 北极星的成本侧
        "boundary_agree": next((d for nm, _, d in gates if nm == "规则-LLM 边界一致率"), None),
        "endpoint_rule_recall": f"{rule_hit}/{n_ep}",
        "rule_vs_sys_gap": sys_hit - rule_hit,            # 差值变大=规则层在退化
        "sys_consistency_pct": srate,
        "rule_consistency_pct": rrate,
        "iv_sep_total": round(sep_tot, 1),
        "peak_pct": share,
        "top80_pct": top_share,
        "strong_labels": primary,
    }
    with open(ledger, "a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 落盘本轮 verdict 快照,供**下一轮** §5b 当「改动前已有判定」的唯一来源。
    # 必须写在 §5b 读取之后:同一轮内先读旧快照、后写新快照,才能保证"判定早于改动"。
    # 注意只在正式跑时更新(模拟不落盘用 --out-dir,写到 preview 目录不污染正式快照)。
    json.dump({a: (c.get("llm") or {}).get("verdict")
               for a, c in cand.items() if (c.get("llm") or {}).get("verdict")},
              open(out / "verdicts_prev.json", "w"), ensure_ascii=False)

    # 控制台
    print("=== 回归 harness 闸门摘要 ===")
    for name, st, desc in gates:
        icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "INFO": "ℹ️ "}[st]
        print(f"{icon} {name}: {desc}")
    # 打真实路径:模拟不落盘时 --out-dir 指向 preview 目录,写死 "out/" 会让人误以为
    # 正式产物被覆盖(实测差点据此误判污染)。
    print(f"\n报告写出：{out / 'harness_report.md'}")


def _gate_summary(gates):
    lines = ["## §0 闸门摘要\n", "| 检查 | 状态 | 说明 |", "|---|---|---|"]
    for name, st, desc in gates:
        icon = {"PASS": "✅ PASS", "WARN": "⚠️ WARN", "FAIL": "❌ FAIL", "INFO": "ℹ️ INFO"}[st]
        lines.append(f"| {name} | {icon} | {desc} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
