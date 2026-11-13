#!/usr/bin/env python3
"""bitable 拉取结果解析器(配合 pull_records.sh)。

关键硬事实(实测,勿改姿势):
  - record-list 返回**列式** JSON:.data.fields=列名数组,.data.data=行数组(按列序),
    .data.record_id_list 与行并行;
  - 多页输出是拼接的 pretty-JSON,必须 JSONDecoder.raw_decode 流式解析,逐行 loads 会崩;
  - 单元格值可能是 [{"text":...}] / [str] / dict / 标量,统一走 cell() 解包;
  - url 样式字段读回是 "[url](url)" markdown 包裹,比对前先剥壳(下游自理)。

模式(--mode):
  current  → fb_current.json        {aid: {列:值..., record_id}}   同步 diff 的基准快照
  manual   → fb_all_snapshot.json + manual_evals_v2.json           人工评估回流
  aid2rid  → aid2rid.json           {aid: record_id}               写表前置映射
  stages   → stages_readback.json   {aid: {当前阶段,状态}}          阶段写入回读核验
"""
import argparse
import json
from pathlib import Path


def stream_objs(text):
    dec = json.JSONDecoder()
    idx, n, objs = 0, len(text), []
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        o, end = dec.raw_decode(text, idx)
        objs.append(o)
        idx = end
    return objs


def cell(v):
    if v is None:
        return None
    if isinstance(v, list):
        if not v:
            return None
        # 富文本 text 字段回来的是 [{"text":"段1"},{"text":"段2"}] —— 必须空串拼接还原原文。
        if any(isinstance(x, dict) for x in v):
            return "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in v)
        # select 回来的是 ["在招"](单选,解包成标量,与 diff 侧的字符串比对一致);
        # 多选是 ["prompt","agent",...] —— 原样返回列表,空串拼接会糊成一坨(实测踩过)。
        return str(v[0]) if len(v) == 1 else [str(x) for x in v]
    if isinstance(v, dict):
        return str(v.get("text", v))
    return v


def iter_rows(objs):
    """yield (aid, {列:解包值}, record_id);要求拉取时包含 application_id 列。"""
    for o in objs:
        d = o["data"]
        fields = d["fields"]
        fi = {f: i for i, f in enumerate(fields)}
        rids = d.get("record_id_list") or []
        for j, rec in enumerate(d["data"]):
            aid = cell(rec[fi["application_id"]])
            row = {f: cell(rec[fi[f]]) for f in fields if f != "application_id"}
            yield str(aid), row, (rids[j] if j < len(rids) else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["current", "manual", "aid2rid", "stages"])
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    objs = stream_objs(open(args.raw).read())
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if args.mode == "current":
        rows = {}
        for aid, row, rid in iter_rows(objs):
            row["record_id"] = rid
            rows[aid] = row
        json.dump(rows, open(out / "fb_current.json", "w"), ensure_ascii=False)
        print("fb_current.json:", len(rows), "records")

    elif args.mode == "manual":
        rows, total = {}, 0
        for aid, row, rid in iter_rows(objs):
            total += 1
            rows[aid] = {
                "name": row.get("姓名"), "rank": row.get("粗排名"), "total": row.get("总分"),
                "pedigree": row.get("顶尖背书"), "stage": row.get("当前阶段"),
                "llm_score": row.get("LLM评分"), "llm_sugg": row.get("LLM建议"),
                "eval": row.get("简历评估（人工）"),
            }
        evals = {k: v for k, v in rows.items() if v["eval"]}
        json.dump(rows, open(out / "fb_all_snapshot.json", "w"), ensure_ascii=False, indent=1)
        json.dump(evals, open(out / "manual_evals_v2.json", "w"), ensure_ascii=False, indent=1)
        print(f"records: {total}, unique aid: {len(rows)}, with manual eval: {len(evals)}")

    elif args.mode == "aid2rid":
        rows = {aid: rid for aid, _, rid in iter_rows(objs) if aid and rid}
        json.dump(rows, open(out / "aid2rid.json", "w"), ensure_ascii=False)
        print("aid2rid.json:", len(rows), "条")

    elif args.mode == "stages":
        rows = {aid: {"当前阶段": row.get("当前阶段"), "状态": row.get("状态")}
                for aid, row, _ in iter_rows(objs)}
        json.dump(rows, open(out / "stages_readback.json", "w"), ensure_ascii=False)
        print("stages_readback.json:", len(rows), "条")


if __name__ == "__main__":
    main()
