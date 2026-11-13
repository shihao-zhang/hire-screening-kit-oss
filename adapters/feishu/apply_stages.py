#!/usr/bin/env python3
"""把 refresh_stages.sh 拉到的新 stage/active_status patch 进 applications_raw,
并产出飞书「当前阶段」/「状态」的更新指令。

输入: out/app_stages_fresh.ndjson, applications_raw.ndjson, out/aid2rid.json
输出: out/stage_changes.json(diff), out/stage_upd_cmds.json(rid+fields)
保护: API 偶发返回空 application(stage=None 且 active_status=None) → 跳过不 patch、
      不清空旧值——源侧偶发空数据会在每轮刷新反复出现,靠此保护不污染库。

用法: python3 adapters/feishu/apply_stages.py --workspace <dir>
"""
import argparse
import json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--workspace", required=True)
args = ap.parse_args()
ROOT = Path(args.workspace)
OUT = ROOT / "out"


def stage_of(o):
    s = o.get("stage")
    return s.get("zh_name") if isinstance(s, dict) else s


def main():
    fresh = {}
    skipped = []
    for line in open(OUT / "app_stages_fresh.ndjson"):
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if o.get("fail"):
            continue
        # 空对象保护:stage 与 active_status 同时为 None 视为 API 抖动,跳过
        if o.get("stage") is None and o.get("active_status") is None:
            skipped.append(o["aid"])
            continue
        fresh[o["aid"]] = {"stage": o["stage"], "active_status": o["active_status"]}

    apps = []
    changed = []
    for line in open(ROOT / "applications_raw.ndjson"):
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        aid = o["id"]
        f = fresh.get(aid)
        if f:
            old_stage, old_as = stage_of(o), o.get("active_status")
            if old_stage != f["stage"] or old_as != f["active_status"]:
                changed.append({"aid": aid, "old_stage": old_stage, "new_stage": f["stage"],
                                "old_as": old_as, "new_as": f["active_status"]})
            if isinstance(o.get("stage"), dict):
                o["stage"]["zh_name"] = f["stage"]
            else:
                o["stage"] = {"zh_name": f["stage"]}
            o["active_status"] = f["active_status"]
        apps.append(o)

    with open(ROOT / "applications_raw.ndjson", "w") as fo:
        for o in apps:
            fo.write(json.dumps(o, ensure_ascii=False) + "\n")
    json.dump(changed, open(OUT / "stage_changes.json", "w"), ensure_ascii=False)

    # 构建飞书更新指令(需 aid2rid)
    rid = json.loads((OUT / "aid2rid.json").read_text()) if (OUT / "aid2rid.json").exists() else {}
    cmds, norid = [], []
    for c in changed:
        r = rid.get(c["aid"])
        if not r:
            norid.append(c["aid"])
            continue
        fields = {"当前阶段": c["new_stage"]}
        if c["new_as"] == 2:
            fields["状态"] = "已终止"
        elif c["new_as"] == 1:
            fields["状态"] = "在招"
        cmds.append({"rid": r, "fields": fields, "aid": c["aid"]})
    json.dump(cmds, open(OUT / "stage_upd_cmds.json", "w"), ensure_ascii=False)

    print(f"阶段变化 {len(changed)} 条｜跳过空对象 {len(skipped)}｜待写飞书 {len(cmds)}"
          + (f"｜无rid {len(norid)}" if norid else ""))
    if skipped:
        print("  跳过(API空对象):", skipped)
    for c in changed:
        print(f"  {c['aid'][-6:]} {c['old_stage']} → {c['new_stage']} (as {c['old_as']}→{c['new_as']})")


if __name__ == "__main__":
    main()
