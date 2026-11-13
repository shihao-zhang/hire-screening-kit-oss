#!/usr/bin/env python3
"""增量探测:发现本职位的新投递(应对 hire API job_id 过滤失效)。

姿势(实测校准,详见 PITFALLS.md):
  游标拉全 org 投递 ID(降序单调雪花) → 基线阈值筛新建 → 逐条 GET 查 job_id 匹配本职位。
  基线 = 已入库最大 application_id,动态读取——硬编码会在入库后重复命中已有记录。
  并发只用于**读** API(ThreadPoolExecutor),结果 return 主线程,最后串行写盘——
  并发 `>>` 追加大体积行会字节交错损坏(PIPE_BUF 之外的写非原子,实测踩过)。

用法: python3 adapters/feishu/detect_new.py --config <job.toml> --workspace <dir>
      [feishu] job_ids(可选列表)可同时探测多岗位(如正式岗+实习岗);缺省只看单键 job_id。
输出: out/new_apps.ndjson / out/new_talents.ndjson / out/new_app_ids.txt;打印 HITS=N
"""
import argparse
import json
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--workspace", required=True)
args = ap.parse_args()
with open(args.config, "rb") as fh:
    _cfg = tomllib.load(fh)
JOB = _cfg["feishu"]["job_id"]
JOBS = set(_cfg["feishu"].get("job_ids") or [JOB])
ROOT = Path(args.workspace)


def _base():
    """基线=已入库最大 application_id,动态读取。"""
    with open(ROOT / "out_application_ids.txt") as f:
        return max(int(l.strip()) for l in f if l.strip())


BASE = _base()


def api(path, params=None):
    cmd = ["lark-cli", "api", "GET", path, "--as", "bot"]
    if params:
        cmd += ["--params", json.dumps(params)]
    for _ in range(4):
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        try:
            d = json.loads(out)
            if d.get("ok"):
                return d
        except Exception:
            pass
    return None


def collect_delta():
    """游标翻页拉降序 org ID,返回 > BASE 的新建 aid 列表。"""
    delta, token, pages = [], None, 0
    while True:
        params = {"page_size": 100}
        if token:
            params["page_token"] = token
        d = api("/open-apis/hire/v1/applications", params)
        if not d:
            print("!! 拉 org ID 失败", file=sys.stderr); break
        data = d["data"]; items = data.get("items", []); pages += 1
        stop = False
        for aid in items:
            if int(aid) > BASE:
                delta.append(aid)
            else:
                stop = True  # 降序:遇到 ≤基线,后面都更旧
                break
        if stop or not data.get("has_more") or not items:
            break
        token = data.get("page_token")
        if not token:
            break
    print(f"翻 {pages} 页，> 基线的新建 candidate: {len(delta)}")
    return delta


def check_one(aid):
    """查单条:匹配本 job 则返回 (aid, app, talent),否则标记原因。"""
    r = api(f"/open-apis/hire/v1/applications/{aid}")
    if not r:
        return (aid, None, None, "FAIL_APP")
    app = r["data"].get("application")
    if not app or app == {}:
        return (aid, None, None, "EMPTY")
    if app.get("job_id") not in JOBS:
        return (aid, None, None, f"OTHER_JOB:{app.get('job_id')}")
    tid = app.get("talent_id")
    tr = api(f"/open-apis/hire/v1/talents/{tid}")
    if not tr:
        return (aid, app, None, "FAIL_TALENT")
    talent = {**tr["data"]["talent"], "application_id": aid}
    return (aid, app, talent, "HIT")


def _write(apps, talents, hit_ids):
    (ROOT / "out").mkdir(exist_ok=True)
    with open(ROOT / "out/new_apps.ndjson", "w") as f:
        for a in apps:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    with open(ROOT / "out/new_talents.ndjson", "w") as f:
        for t in talents:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    with open(ROOT / "out/new_app_ids.txt", "w") as f:
        f.write("\n".join(hit_ids) + ("\n" if hit_ids else ""))


def main():
    delta = collect_delta()
    if not delta:
        print("HITS=0 (无新建)")
        _write([], [], [])
        return
    apps, talents, hit_ids, fails = [], [], [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for aid, app, talent, status in ex.map(check_one, delta):
            if status == "HIT":
                apps.append(app); hit_ids.append(aid)
                if talent:
                    talents.append(talent)
                else:
                    fails.append(f"{aid}:talent失败")
            elif status.startswith("FAIL"):
                fails.append(f"{aid}:{status}")
    _write(apps, talents, hit_ids)
    print(f"HITS={len(hit_ids)} apps={len(apps)} talents={len(talents)} candidates={len(delta)}")
    if fails:
        print("失败/异常:", fails)
    if len(talents) != len(hit_ids):
        print(f"⚠️ talent 数({len(talents)})≠hit数({len(hit_ids)})，需补拉")


if __name__ == "__main__":
    main()
