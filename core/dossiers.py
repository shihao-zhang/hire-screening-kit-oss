#!/usr/bin/env python3
"""为精读网候选人构建 LLM 精读档案(dossier):规则分项 + 标志位 + 教育/职业原文。

档案是精读的唯一输入——LLM 不直接接触全量简历库,只读进网者的结构化档案
(subagent 隔离简历文本护主 context 的配套件)。

用法: python3 core/dossiers.py --config <job.toml> --workspace <dir> [aid1 aid2 ...]
      不传 aid 时按 [dossier].scope_source 取范围:
        "need_review_ids"(缺省,旧行为)读 workspace/out/need_review_ids.txt;
        "report" 直接取 out/report.csv 里 llm_scope=true 的人。
输出: workspace/llm_review/dossiers/scope_<name>_<aid>.json
"""
import argparse
import csv
import json
import re
import tomllib
from pathlib import Path


_YM = re.compile(r"^(\d{4})-(\d{2})")


def _ym(x):
    m = _YM.match(str(x or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def is_track_peer(company, cfg):
    """公司主业是否与本岗同赛道。裸子串匹配,与 company_hit 同款——名单里只放足够长、
    不会撞车的键(短键会误伤,见 v6.29 的子串撞车审计)。名单为空时恒 False。"""
    c = (company or "").lower()
    return any(k in c for k in ((cfg.get("companies") or {}).get("track_peer") or []))


OMIT_FLAGS = set()


def tenure_months(w, today):
    """单段任期月数;end_time 空按 today 算。算不出返回 None。"""
    a = _ym(w.get("start_time"))
    if not a:
        return None
    b = _ym(w.get("end_time")) or today
    return max((b[0] - a[0]) * 12 + (b[1] - a[1]), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("aids", nargs="*")
    args = ap.parse_args()
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    deg_label = {str(k): v for k, v in cfg["degrees"]["label"].items()}
    rank_key = cfg["merge"]["dossier_rank_key"]
    # 可从档案里撤掉的规则标志位(可选,缺省不撤 = 旧行为)。
    # 为什么需要:owner 词频标志位(exec_dom/owner_hits/exec_hits)在某实例上检验无判别力
    # (置换检验 p=1.000,对照组规则总分同样本 p=0.021 说明检验有效力),且 exec_hits 方向与设计
    # 意图相反、九次误导精读判断——喂给 LLM 的是假信号。评分引擎照常计算这些列,只是不进档案。
    global OMIT_FLAGS
    OMIT_FLAGS = set((cfg.get("dossier") or {}).get("omit_rule_flags") or [])
    # 复现性:任期算术的"今天"取配置里的评分基准日,不用 datetime.now()
    #(同一份档案今天跑和明天跑必须逐字节相同,见 METHODOLOGY §2.4)
    _run = cfg.get("run") or {}
    global TODAY
    TODAY = (_run.get("now_year"), _run.get("now_month"))
    if not all(TODAY):
        raise SystemExit("配置缺 [run].now_year/now_month:任期算术需要固定基准日,"
                         "拒绝回退到当前时间(同一份档案今天跑和明天跑必须逐字节相同)")
    ws = Path(args.workspace)
    dossier_dir = ws / "llm_review" / "dossiers"
    dossier_dir.mkdir(parents=True, exist_ok=True)

    tals = {}
    for l in open(ws / "talents_raw.ndjson"):
        l = l.strip()
        if l and l != "null":
            t = json.loads(l)
            tals[t["id"]] = t
    rows = {r["application_id"]: r for r in csv.DictReader(open(ws / "out/report.csv"))}
    # 建档范围来源。缺省读 need_review_ids.txt(旧行为);但那份清单由轮内辅助步骤产出,评分变动后
    # 若没刷新,就会按过期范围建档、静默少建(实测一轮少 3 份,只在 merge 时才暴露)。
    # "report" 直接以评分产物里的 llm_scope 为准,与评分永远同步。
    scope_source = (cfg.get("dossier") or {}).get("scope_source", "need_review_ids")
    if args.aids:
        ids = args.aids
    elif scope_source == "report":
        ids = [a for a, r in rows.items() if str(r.get("llm_scope", "")).lower() == "true"]
    elif scope_source == "need_review_ids":
        ids = [l.strip() for l in open(ws / "out/need_review_ids.txt") if l.strip()]
    else:
        raise SystemExit(f'[dossier].scope_source 只能是 "need_review_ids" 或 "report",收到 {scope_source!r}')
    n = 0
    for aid in ids:
        r = rows.get(aid)
        if not r:
            print("skip (not in report):", aid)
            continue
        t = tals.get(r["talent_id"])
        if not t:
            print("skip (no talent):", aid)
            continue
        d = {rank_key: int(r["rank"]), "name": r["name"], "application_id": aid,
             "rule_scores": {k: r[k] for k in ["total", "education", "major", "years",
                                               "skills", "company", "stability", "project"]},
             "role_fit_v6": r["role_fit"], "pedigree": r["pedigree"],
             "experience_years": r["years_text"],
             "rule_flags": {k: v for k, v in {
                            "exec_dom": r["flag_exec_dom"], "owner_hits": r["owner_hits"],
                            "exec_hits": r["exec_hits"], "shallow_env": r["flag_shallow_env"],
                            "ai_gap": r["ai_gap"], "short_stints": r["flag_short_stints"],
                            # RUBRIC #16 的条件①:整份简历是否已触发冗长档(0/1/2)。
                            # 与每段的 chars/months 配合使用——**别让模型自己数字数或算日期**,
                            # 那是 #9 反复踩过的坑(算术错了不报错,只是判错人)。
                            "verbose_lv": r["verbose_lv"]}.items() if k not in OMIT_FLAGS},
             "educations": [{"school": e.get("school", ""),
                             "degree": deg_label.get(str(e.get("degree", "")), str(e.get("degree", ""))),
                             "major": e.get("field_of_study", ""),
                             "period": f"{e.get('start_time','')}~{e.get('end_time','')}"}
                            for e in (t.get("education_list") or [])],
             "careers": [dict(company=w.get("company", ""), title=w.get("title", ""),
                              period=f"{w.get('start_time','')}~{w.get('end_time','')}",
                              # chars/months 预计算:供 RUBRIC #16 条件② 与 #9 的任期算术直接读,
                              # is_intern 标出实习段(#16/#9 都不适用于实习)
                              chars=len(w.get("desc") or ""),
                              months=tenure_months(w, TODAY),
                              is_intern=(w.get("career_type") == 1),
                              # 赛道同构标记:该公司主业是否与本岗所在赛道相同(配置 [companies].track_peer)。
                              # **只是给精读层的一个正向证据,不参与任何打分**;缺席不构成负向
                              # (名单必然不全)。由来见配置里那段注释。
                              track_peer=is_track_peer(w.get("company", ""), cfg),
                              desc=w.get("desc", ""))
                         for w in (t.get("career_list") or [])]}
        if t.get("project_list"):
            d["projects"] = [{"name": p.get("name", ""), "desc": p.get("desc", "")}
                             for p in t["project_list"]]
        safe = r["name"].replace("/", "_")
        json.dump(d, open(dossier_dir / f"scope_{safe}_{aid}.json", "w"),
                  ensure_ascii=False, indent=1)
        n += 1
    print(f"wrote {n} dossiers")


if __name__ == "__main__":
    main()
