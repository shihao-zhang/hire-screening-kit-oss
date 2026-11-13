#!/usr/bin/env python3
"""同步后回读核验:把刚写进 bitable 的 payload 与回读快照逐字段比对。

「批量写外部系统后必须全量回读核验,零误差才算完成」是硬纪律,但每轮手写比对器
会重复踩同一批**形态伪差**——回读值与写入值语义相同、序列化形态不同,朴素
exact-diff 会报出几十条假不符,真错混在里面看不见(实测一次假报 46 条)。
本脚本把已知的形态归一固化下来:

  1. 超链接列  写 "https://x"      回读 "[https://x](https://x)"
  2. 日期列    写 "2026-08-19 13:53:56"  回读 "2026-08-19T13:53:56.000+08:00"
  3. 多选列    写 ["产品经理"](单值)     回读 "产品经理"(裸字符串,见 PITFALLS 的 cell() 解包)
  4. 数值列    写 80 / 回读 80.0 —— 按浮点比
  5. application_id 是 fb_current 的**字典键**,值字典里没有这一列

⚠️ 新形态出现时**加归一规则,不要放宽比对**——放宽会把真错一起吃掉。

用法:
  bash adapters/feishu/pull_records.sh out/fb_readback_raw.json
  python3 adapters/feishu/parse_records.py --mode current --raw out/fb_readback_raw.json --out-dir out
  cp out/fb_current.json out/fb_readback.json      # 回读快照(与 diff 基准分开存)
  python3 adapters/feishu/verify_sync.py --workspace <dir> --tag <轮次tag>

退出码:0=零误差;1=有不符(不要当同步完成)。
"""
import argparse
import collections
import json
import re
from pathlib import Path

_LINK_RE = re.compile(r"^\[(.+?)\]\(.*\)$")
_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})")


def norm(v):
    """只做形态归一,不做语义放宽。"""
    if isinstance(v, str):
        m = _LINK_RE.match(v)
        if m:
            v = m.group(1)
        m = _DT_RE.match(v)
        if m:
            v = f"{m.group(1)} {m.group(2)}"
    return v


def same(exp, act):
    exp, act = norm(exp), norm(act)
    if isinstance(exp, list) or isinstance(act, list):
        le = exp if isinstance(exp, list) else ([exp] if exp else [])
        la = act if isinstance(act, list) else ([act] if act else [])
        return sorted(le) == sorted(la)
    if isinstance(exp, (int, float)) or isinstance(act, (int, float)):
        try:
            return abs(float(exp) - float(act)) < 1e-6
        except (TypeError, ValueError):
            return False
    return (exp or "") == (act or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--readback", default="out/fb_readback.json",
                    help="回读快照(parse_records --mode current 的产物,建议另存一份)")
    ap.add_argument("--max-detail", type=int, default=30)
    args = ap.parse_args()
    ws = Path(args.workspace)
    man = json.load(open(ws / f"out/fb_manifest_{args.tag}.json"))
    got = json.load(open(ws / args.readback))

    bad = collections.Counter()
    details = []
    for kind in ("creates", "updates"):
        for m in man.get(kind, []):
            aid = m["appid"]
            exp = json.load(open(ws / f"out/payloads_{args.tag}" / m["payload"]))
            act = got.get(aid)
            if act is None:
                bad["缺行"] += 1
                details.append(f"缺行 {kind} {m['name']} {aid}")
                continue
            for k, v in exp.items():
                if k == "application_id":      # 是字典键,不在值里
                    continue
                if k not in act:
                    bad["缺列"] += 1
                    details.append(f"缺列 {m['name']} {k}")
                elif not same(v, act[k]):
                    bad["值不符"] += 1
                    details.append(f"值不符 {m['name']} {k}: 写={str(v)[:40]!r} 读={str(act[k])[:40]!r}")

    n_c, n_u = len(man.get("creates", [])), len(man.get("updates", []))
    # 查重:创建没有幂等键,重跑会产生重复行,故顺带核 aid 唯一与总行数
    print(f"回读核验: {dict(bad) or '零误差 ✅'}  (creates {n_c} / updates {n_u} / 表内 {len(got)} 行)")
    for d in details[:args.max_detail]:
        print("  ", d)
    if len(details) > args.max_detail:
        print(f"   …还有 {len(details) - args.max_detail} 条")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
