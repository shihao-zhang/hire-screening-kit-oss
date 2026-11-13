#!/usr/bin/env python3
"""同步构建器(增量新投递 creates + 存量 updates 的字段级 diff)。

- creates: 新 application_id 的全字段记录(无 record_id)
- updates: 存量记录 vs out/fb_current.json 快照的字段 diff(只写有变化的字段——
  防意外污染:非目标字段零触碰,人工列(简历评估等)永不出现在 patch 里)
写 out/payloads_<tag>/{new_,upd_}<aid>.json + out/fb_manifest_<tag>.json {creates, updates}。

⚠️ 竞态纪律:同步(sync_bitable.sh)启动后到 marker 出现前,不要重跑本脚本——
   它会清空重写 payload 与 manifest,同步后半程读到不一致状态(实测最难诊断的坑)。

列名契约:字段名 = kit 自带 bitable 模板的列名;改列名须同步改本文件。
  「粗排名」= 规则层粗排位次(与 LLM 精排相对);人工评语里的"排名偏高/偏低"提示是另一回事,
  由 [merge].rankhint_pattern 解析,勿一起改名(现 pattern 对"粗排名偏高"仍匹配)。

用法: python3 adapters/feishu/build_payloads.py --config <job.toml> --workspace <dir> --tag <轮次tag>
"""
import argparse
import csv
import datetime
import json
import tomllib
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--workspace", required=True)
ap.add_argument("--tag", required=True, help="轮次标签,如 v613——payload 目录与 manifest 后缀")
args = ap.parse_args()
with open(args.config, "rb") as fh:
    cfg = tomllib.load(fh)
JOB = cfg["feishu"]["job_id"]
URL_T = cfg["feishu"]["detail_url_template"]

BASE = Path(args.workspace)
OUT = BASE / "out"
PL = OUT / f"payloads_{args.tag}"
PL.mkdir(exist_ok=True)

cur = {r["application_id"]: r for r in csv.DictReader(open(OUT / "report.csv"))}
# reviews_by_appid.json = 规则评语生成器产物(可选,缺省空——评语列留空);
# fb_kw_opts.json = 「命中关键词」多选列的现有 options(field-get 拉取,可选,缺省不写该列——
#   bitable 写入未知选项会报 800030005,故只写在 options 内的词)。
_rv = OUT / "reviews_by_appid.json"
reviews = json.load(open(_rv)) if _rv.exists() else {}
merged = json.load(open(BASE / "llm_review/merged.json"))
fb = json.load(open(OUT / "fb_current.json"))
_kw = OUT / "fb_kw_opts.json"
kw_opts = set(json.load(open(_kw))) if _kw.exists() else set()
# creates 以**飞书快照**为准,不是以探测名单为准:同一轮里若已 create_records 过一次
# (例如后来又改了规则要重同步),这些 aid 已在表内——仍当 creates 会让它们既不重建、
# 又被 updates 分支 `continue` 跳过,变成"两不管"的过期行(实测漏掉 5 条粗排名)。
_detected = [l.strip() for l in open(OUT / "new_app_ids.txt") if l.strip()]
apps = {}
for line in open(OUT / "new_apps.ndjson"):
    line = line.strip()
    if line:
        a = json.loads(line)
        apps[a["id"]] = a
# 多岗位实例([feishu].job_ids 含其它岗位)下,探测会把别的岗位的投递也并入——
# 本表只接收主岗(job_id)。其它岗位的投递由各自表的同步流程处理(实测缺失会把
# 实习岗投递建成全职表的行,两张表都出现错位)。
_other_job = [a for a in _detected if (apps.get(a) or {}).get("job_id") not in (None, JOB)]
if _other_job:
    print(f"  跳过非主岗投递 {len(_other_job)} 条(交由对应岗位的表同步)")
_detected = [a for a in _detected if a not in _other_job]
new_ids = [a for a in _detected if a not in fb]
if len(new_ids) != len(_detected):
    print(f"  ({len(_detected) - len(new_ids)} 条探测新投递已在表内，转入 updates 分支)")


def detail_url(tid, aid):
    return URL_T.format(tid=tid, aid=aid, job=JOB)


def bj_time(ms):
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.fromtimestamp(int(ms) / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def llm_text(m):
    """评审意见排版:头部速查(结论/匹配点)+ 分块正文 + 尾部面试考察点。

    面向"HM 面试前 30 秒扫一眼"的读法:头尾两块是可直接复制进面试记录的速查语,
    中间是详述。bitable 多行文本保留 \n(实测),故用换行+项目符号分块。
    考察点由 red_flags 派生——rubric 要求 red_flag 写成"面试要验证什么",天然是问题清单。
    """
    verdict = m.get("verdict", "")
    direction = m.get("direction", "")
    score = m.get("llm_score")
    strengths = m.get("strengths") or []
    flags = m.get("red_flags") or []
    reasoning = (m.get("reasoning") or "").strip()

    L = [f"🎯 {verdict} · {direction} · {score}分"]
    # ── 速查块(面试前 30 秒扫读、可整块复制):每条独立成行,不折行堆叠
    if strengths:
        L += ["", "📌 匹配点"] + [f"{i}. {s}" for i, s in enumerate(strengths, 1)]
    if flags:
        L += ["", "🔍 面试重点考察"] + [f"{i}. {f}" for i, f in enumerate(flags, 1)]
    # ── 详述(速查块已含要点,此处不再重复列举,避免同内容三遍)
    if reasoning:
        L += ["", "─────────", "", "📋 评审详述", reasoning]
    return "\n".join(L)


# 同一人重复投递:飞书会给重投的人**新建 talent_id**,系统视作两个人(分数/排名/标签各算一份),
# `talent_id` 层面查重抓不到(见 PITFALLS)。人工确认后登记在配置 [duplicates].same_person
# {新aid = 旧aid},此处每轮**重新生成**标注——旧行的粗排名与阶段每轮都会变,写死文本会过期,
# 而评审意见是生成字段,手工补的标注下一轮就被覆盖(实测踩过)。
DUP = (cfg.get("duplicates") or {}).get("same_person") or {}


def dup_note(aid):
    old = DUP.get(aid)
    o = cur.get(old) if old else None
    if not o:
        return ""
    return (f"⚠️ 同一人重复投递:本表另有旧记录(粗排名 {o['rank']}、投递更早、"
            f"阶段「{o['stage']}」)。两条 talent_id 不同故系统未自动合并;"
            f"本行是最新简历,建议以本行为准,旧行勿重复评审。")


def numeq(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


# ---------- creates (全字段) ----------
creates = []
new_set = set(new_ids)
for aid in new_ids:
    r = cur[aid]
    hits = [h for h in (r["skills_hit"].split(",") if r["skills_hit"] else []) if h in kw_opts]
    fm = {
        "粗排名": int(r["rank"]), "姓名": r["name"], "总分": num(r["total"]),
        "学历分": num(r["education"]), "专业分": num(r["major"]), "年限分": num(r["years"]),
        "技能分": num(r["skills"]), "公司分": num(r["company"]),
        "稳定性分": num(r["stability"]), "项目分": num(r["project"]),
        "方向匹配": r["role_fit"], "状态": r["active"], "当前阶段": r["stage"],
        "顶尖背书": r["pedigree"], "学位": r["degree"], "专业": r["major_text"],
        "当前公司": r["current_company"], "院校": r["school"], "简历来源": r["resume_source"],
        "详情页链接": detail_url(r["talent_id"], aid),
        "application_id": aid, "talent_id": r["talent_id"],
        "简历评价": reviews.get(aid, ""),
    }
    _dn = dup_note(aid)
    if _dn and not merged.get(aid):
        fm["简历评价"] = (_dn + "\n\n" + fm["简历评价"]).strip()
    if hits:
        fm["命中关键词"] = hits
    ct = apps.get(aid, {}).get("create_time")
    if ct:
        fm["投递时间"] = bj_time(ct)
    m = merged.get(aid)
    if m:
        fm["LLM评分"] = m["llm_score"]
        fm["LLM建议"] = m["verdict"]
        fm["LLM方向"] = m["direction"]
        fm["LLM评审意见"] = (_dn + "\n\n" if _dn else "") + llm_text(m)
    (PL / f"new_{aid}.json").write_text(json.dumps(fm, ensure_ascii=False))
    creates.append({"appid": aid, "name": r["name"], "payload": f"new_{aid}.json"})

# ---------- updates (diff vs fb 快照，含 状态/当前阶段) ----------
updates = []
field_counts = {}
for aid, r in cur.items():
    if aid in new_set:
        continue
    f = fb.get(aid)
    if not f:
        print(f"!! {r['name']} {aid} 不在飞书快照，跳过")
        continue
    patch = {}
    if not numeq(r["rank"], f.get("粗排名")):
        patch["粗排名"] = int(r["rank"])
    if not numeq(r["total"], f.get("总分")):
        patch["总分"] = float(r["total"])
    if r["pedigree"] != (f.get("顶尖背书") or ""):
        patch["顶尖背书"] = r["pedigree"]
    _dn = dup_note(aid)
    _rv = reviews.get(aid, "")
    if _dn and not merged.get(aid):
        _rv = (_dn + "\n\n" + _rv).strip()
    if _rv != (f.get("简历评价") or ""):
        patch["简历评价"] = _rv
    # 七个分项全部纳入 diff。此前只 diff 稳定性分——因为早期几轮恰好只有它变过;
    # v6.20 改学历口径时 100 人的学历分变了却同步不出去,表里与 report 静默漂移。
    # 分项列是规则层产物(非人工列),按"表 = report 当前态"的契约就该全量对齐。
    for col, key in (("学历分", "education"), ("专业分", "major"), ("年限分", "years"),
                     ("技能分", "skills"), ("公司分", "company"),
                     ("稳定性分", "stability"), ("项目分", "project")):
        if not numeq(r[key], f.get(col)):
            patch[col] = float(r[key])
    if r["active"] != (f.get("状态") or ""):
        patch["状态"] = r["active"]
    if r["stage"] != (f.get("当前阶段") or ""):
        patch["当前阶段"] = r["stage"]
    m = merged.get(aid)
    if m:
        if not numeq(m["llm_score"], f.get("LLM评分")):
            patch["LLM评分"] = m["llm_score"]
        if m["verdict"] != (f.get("LLM建议") or ""):
            patch["LLM建议"] = m["verdict"]
        if m["direction"] != (f.get("LLM方向") or ""):
            patch["LLM方向"] = m["direction"]
        _txt = (_dn + "\n\n" if _dn else "") + llm_text(m)
        if _txt != (f.get("LLM评审意见") or ""):
            patch["LLM评审意见"] = _txt
    if not patch:
        continue
    for k in patch:
        field_counts[k] = field_counts.get(k, 0) + 1
    (PL / f"upd_{aid}.json").write_text(json.dumps(patch, ensure_ascii=False))
    updates.append({"appid": aid, "name": r["name"], "record_id": f["record_id"],
                    "payload": f"upd_{aid}.json", "fields": sorted(patch)})

json.dump({"creates": creates, "updates": updates},
          open(OUT / f"fb_manifest_{args.tag}.json", "w"), ensure_ascii=False, indent=0)
print(f"creates: {len(creates)} | updates: {len(updates)}")
for k, v in sorted(field_counts.items(), key=lambda kv: -kv[1]):
    print(f"  upd {k}: {v}")
