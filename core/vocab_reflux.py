#!/usr/bin/env python3
"""词表回流通道(飞轮⑤):把"名单没覆盖到的实体"每轮捞出来,交给 LLM 归类 → 人核定 → 补进配置。

## 为什么需要它

公司/学校/专业这三个维度都是**手工名单 + 字符串匹配**。名单一旦冻结就必然落后于长尾:
实测某岗位池 1123 家去重公司里 **69% 任何名单都不认识**,865 段经历吃兜底分;
新一轮投递还会持续带来新实体。靠人肉盯是不可能的。

## 定位:名单不冻结,但引擎仍然确定性

LLM **不进评分路径**——它只在本工具里对"未识别实体"做归类提议,产物是给人看的候选清单。
人核定后写进配置 TOML,引擎照旧只读配置。这样同时保住:
  - 复现性(同输入同输出,评分不依赖当次 LLM 输出)
  - 人在环(档位是招聘 bar,由人拍板;LLM 只提供事实归类)
  - 可审计(每条新增在 git 里有 diff 与理由)

## 实测的 LLM 可信边界(写死在这里,免得后人高估)

  - **高置信可用、中低置信必须人过**:某轮 850 条公司归类里 high 只有 30 条(3.5%),
    medium 档混进大量错判(教培/车企/金融/医疗被标成"知名科技公司")。
  - **需要精确世界知识的事实不可信**:让它标 QS 排名档,8 所里错 4 所。
  - **结构性事实可信**:中外合办 / 独立学院 / 校名易混 / 这个专业属于商科——这类判得准。
  - 结论:**LLM 做分诊不做决策**。它的价值是把 1123 收敛到 107 让人能看完,不是替人定档。

## 用法

  # 1) 捞出本轮新出现且未被名单识别的实体(已核定过的不再重复出现)
  python3 core/vocab_reflux.py --config <job.toml> --workspace <dir> --mode extract
  # 2) 人/subagent 对 out/vocab/pending_*.tsv 做归类 → 复核 → 手工补进配置 TOML
  # 3) 标记本轮已处理(无论是否采纳,都记账,避免下轮重复提议)
  python3 core/vocab_reflux.py --config <job.toml> --workspace <dir> --mode mark
"""
import argparse
import collections
import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import score_engine as E  # noqa: E402


def top_edu(t: dict) -> dict:
    edus = t.get("education_list") or []
    if not edus:
        return {}
    def rank(e):
        try:
            return E.DEGREE_SCORE.get(int(e.get("degree")), 30)
        except (TypeError, ValueError):
            return 30
    return max(edus, key=rank)


def collect(ws: Path):
    """返回 {维度: Counter(实体名 → 出现次数)}。"""
    comps, schools, majors = collections.Counter(), collections.Counter(), collections.Counter()
    with open(ws / "talents_raw.ndjson") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            for c in t.get("career_list") or []:
                co = (c.get("company") or "").strip()
                if co:
                    comps[co] += 1
            for e in t.get("education_list") or []:
                s = (e.get("school") or "").strip()
                if s:
                    schools[s] += 1
            m = (top_edu(t).get("field_of_study") or "").strip()
            if m:
                majors[m] += 1
    return {"companies": comps, "schools": schools, "majors": majors}


def unknown_company(co: str, cfg: dict) -> bool:
    """任何名单/正则都不认识 → 该段只能吃兜底分。"""
    cl = co.lower()
    C = cfg["companies"]
    for nm in ("tier1", "tier2", "ai_native", "tech_extra", "pedigree"):
        if any(k.lower() in cl or k in co for k in C[nm]):
            return False
    return not E.TRAD_COMPANY_RE.search(co)


def unknown_school(s: str) -> bool:
    return E.school_tier_one(s) == 0


def unknown_major(m: str) -> bool:
    # 35 = 词表全不命中的兜底分。注意:兜底里混着"真不相关"和"词表没覆盖"两类,
    # 所以这里捞出来的是**待判**不是**待补**——归类结果可能就是"确实不相关"。
    return E.major_score(m) == 35


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--mode", choices=["extract", "mark"], default="extract")
    ap.add_argument("--min-freq", type=int, default=1, help="只报出现次数≥此值的实体")
    args = ap.parse_args()

    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    E.bind_config(cfg)
    ws = Path(args.workspace)
    vd = ws / "out/vocab"
    vd.mkdir(parents=True, exist_ok=True)
    ledger_p = vd / "reviewed.json"
    ledger = json.load(open(ledger_p)) if ledger_p.exists() else {}

    counts = collect(ws)
    checks = {"companies": lambda x: unknown_company(x, cfg),
              "schools": unknown_school, "majors": unknown_major}

    if args.mode == "mark":
        n = 0
        for dim in counts:
            p = vd / f"pending_{dim}.tsv"
            if not p.exists():
                continue
            done = set(ledger.get(dim, []))
            for line in open(p):
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[0].isdigit():
                    done.add(parts[1]); n += 1
            ledger[dim] = sorted(done)
        json.dump(ledger, open(ledger_p, "w"), ensure_ascii=False, indent=0)
        print(f"已记账 {n} 个实体 → {ledger_p}（下轮不再重复提议）")
        return 0

    total_new = 0
    for dim, cnt in counts.items():
        done = set(ledger.get(dim, []))
        pend = [(v, k) for k, v in cnt.items()
                if checks[dim](k) and k not in done and v >= args.min_freq]
        pend.sort(reverse=True)
        p = vd / f"pending_{dim}.tsv"
        with open(p, "w") as fh:
            fh.write("频次\t实体名\n")
            for v, k in pend:
                fh.write(f"{v}\t{k}\n")
        cov = sum(v for v, _ in pend)
        n_unk = sum(1 for k in cnt if checks[dim](k))
        print(f"{dim:<10} 去重 {len(cnt):>4} 个｜名单未识别 {n_unk:>4} 个｜"
              f"**本轮待判 {len(pend):>4} 个**(覆盖 {cov} 处) → {p.name}")
        total_new += len(pend)

    if total_new == 0:
        print("\n✅ 无新增待判实体——名单对当前池是完备的。")
    else:
        print(f"\n待判合计 {total_new} 个。下一步:对 pending_*.tsv 做归类(LLM 只做事实归类、"
              f"不给档位建议)→ **逐条人工复核**(高置信可用,中低置信必查)→ 手工补进配置 → "
              f"跑 --mode mark 记账。")
        print("⚠️ 纪律:档位是招聘 bar 不是事实,LLM 不得替人定档;"
              "补完配置必须走「模拟不落盘 + 血范围 + harness」再落。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
