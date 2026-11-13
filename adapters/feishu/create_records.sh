#!/usr/bin/env bash
# 新投递批量创建:把 build_payloads 产出的 creates(new_<aid>.json)写成新行。
# 用法: create_records.sh <tag>   (先跑 build_payloads.py --tag <tag>)
# 需要环境变量: HSK_BASE_TOKEN, HSK_TABLE_ID, HSK_WORKSPACE, HSK_PYTHON(可选)
#
# 为什么与 sync_bitable.sh 分开:updates 是 per-row 不同字段的 patch(只能逐条 upsert),
# creates 是同构全字段行(可 batch)。两者混在一条流水线里会让失败重试语义打架。
#
# ⚠️ 纪律(实测踩过,详见 PITFALLS.md):
#   - 批量创建 ≤200 行/次;本脚本自动分批。
#   - 创建**没有幂等键**——重跑会产生重复行。脚本先按 application_id 查现表拦截已存在者;
#     仍失败时不要盲目重跑,先回读查重。
#   - 写完必须回读核验:条数、查重、逐字段 diff(调用方负责,见 QUICKSTART 一轮动线)。
set -uo pipefail
: "${HSK_BASE_TOKEN:?}"; : "${HSK_TABLE_ID:?}"; : "${HSK_WORKSPACE:?}"
TAG="${1:?用法: create_records.sh <tag>}"
PY="${HSK_PYTHON:-python3}"
cd "$HSK_WORKSPACE"
PL="out/payloads_$TAG"
LOG="out/fb_create_$TAG.log"
: > "$LOG"

# 1) 幂等拦截:剔除 application_id 已在表内的 payload(防重跑造重复行)
$PY - "$TAG" <<'PYEOF' > "out/fb_create_${TAG}_plan.json"
import json, os, sys
tag = sys.argv[1]
man = json.load(open(f"out/fb_manifest_{tag}.json"))
have = set(json.load(open("out/fb_current.json")).keys())   # 现表快照的 application_id
todo, skipped = [], []
for c in man["creates"]:
    (skipped if c["appid"] in have else todo).append(c)
if skipped:
    print(f"⏭  已在表内，跳过 {len(skipped)} 条: "
          + ", ".join(f"{c['name']}({c['appid']})" for c in skipped[:8]), file=sys.stderr)
rows = []
for c in todo:
    rows.append(json.load(open(os.path.join(f"out/payloads_{tag}", c["payload"]))))
json.dump({"todo": [c["appid"] for c in todo], "rows": rows}, open("/dev/stdout", "w"), ensure_ascii=False)
PYEOF
N=$($PY -c "import json;print(len(json.load(open('out/fb_create_${TAG}_plan.json'))['todo']))")
if [ "$N" -eq 0 ]; then echo "无待创建记录"; echo "CREATEDONE" > "out/fb_create_$TAG.marker"; exit 0; fi
echo "待创建 $N 条"

# 2) 分批 batch-create(≤200/次);逐批重试,任一批彻底失败即中止,避免半截状态被误判为完成
OK=0
BATCH=0
while :; do
  REQ=$($PY - "$TAG" "$BATCH" <<'PYEOF'
import json, sys
tag, off = sys.argv[1], int(sys.argv[2])
plan = json.load(open(f"out/fb_create_{tag}_plan.json"))
chunk = plan["rows"][off:off+200]
if not chunk:
    print(""); raise SystemExit
cols = sorted({k for r in chunk for k in r})
print(json.dumps({"fields": cols,
                  "rows": [[r.get(c) for c in cols] for r in chunk]}, ensure_ascii=False))
PYEOF
)
  [ -z "$REQ" ] && break
  tries=0; done_ok=""
  while [ "$tries" -lt 5 ]; do
    OUT=$(lark-cli base +record-batch-create --base-token "$HSK_BASE_TOKEN" \
          --table-id "$HSK_TABLE_ID" --as user --json "$REQ" 2>>"$LOG")
    if [ "$(jq -r '.ok' <<<"$OUT" 2>/dev/null)" = "true" ]; then done_ok=1; break; fi
    tries=$((tries+1))
    echo "retry $tries batch@$BATCH $(jq -rc '.error.code//.error.subtype//"?"' <<<"$OUT" 2>/dev/null)" >>"$LOG"
    sleep 3
  done
  if [ -z "$done_ok" ]; then
    echo "❌ batch@$BATCH 重试 5 次仍失败,中止(已成功 $OK 条;回读查重后再决定是否续跑)" | tee -a "$LOG"
    exit 1
  fi
  n=$($PY -c "import json,sys;print(len(json.loads(sys.argv[1])['rows']))" "$REQ")
  OK=$((OK+n)); BATCH=$((BATCH+200))
  echo "  batch@$((BATCH-200)) 成功 $n 条(累计 $OK/$N)"
done

echo "DONE created=$OK/$N" | tee -a "$LOG"
echo "CREATEDONE" > "out/fb_create_$TAG.marker"
