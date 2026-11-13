#!/usr/bin/env bash
# 更新同步:按 manifest 对存量记录逐条 upsert 部分字段(per-row 不同值只能逐条,批量 API 不支持)。
# 用法: sync_bitable.sh <tag>   (先跑 build_payloads.py --tag <tag>)
# 需要环境变量: HSK_BASE_TOKEN, HSK_TABLE_ID, HSK_WORKSPACE
#
# ⚠️ 纪律(实测踩过):
#   - 同步启动后到 marker 出现前,不碰 payload 目录/manifest/快照;看进度只读 done/fail 计数。
#   - 重跑同步是幂等的(upsert 相同值无害);诊断不清时「重拉快照→重算 diff→重跑」即可收敛。
#   - 同步完成后必须全量回读核验(pull_records + 与本地基准 diff),零误差才算完成。
set -uo pipefail
: "${HSK_BASE_TOKEN:?}"; : "${HSK_TABLE_ID:?}"; : "${HSK_WORKSPACE:?}"
TAG="${1:?用法: sync_bitable.sh <tag>}"
cd "$HSK_WORKSPACE"
PL=out/payloads_$TAG
LOG=out/fb_sync_$TAG.log
DONE=out/fb_sync_${TAG}_done.txt
FAIL=out/fb_sync_${TAG}_fail.txt
: > "$LOG"; : > "$DONE"; : > "$FAIL"

upsert() {
  local pf="$1" rid="$2" label="$3" tries=0 out ok
  while [ "$tries" -lt 5 ]; do
    out=$(lark-cli base +record-upsert --base-token "$HSK_BASE_TOKEN" --table-id "$HSK_TABLE_ID" --as user \
          --record-id "$rid" --json "$(cat "$pf")" 2>>"$LOG")
    ok=$(jq -r '.ok' <<<"$out" 2>/dev/null)
    if [ "$ok" = "true" ]; then echo "$label" >> "$DONE"; return 0; fi
    tries=$((tries+1)); echo "retry $tries $label $(jq -rc '.error.code//.error.subtype//"?"' <<<"$out" 2>/dev/null)" >>"$LOG"; sleep 2
  done
  echo "$label" >> "$FAIL"; echo "FAILED $label last=$out" >>"$LOG"; return 1
}

# 注意:必须用进程替换喂 while,不能 `python3 ... | while`——管道会让循环跑在子 shell,
# 循环内 `&` 的最后一批任务变孤儿,父 shell 的 wait 等不到 → marker 提前落盘(实测级并发坑)。
i=0
while read -r AID RID NAME; do
    upsert "$PL/upd_$AID.json" "$RID" "UPD:$NAME:$AID" &
    i=$((i+1)); if [ $((i % 8)) -eq 0 ]; then wait; fi
done < <(python3 -c "import json;[print(m['appid'],m['record_id'],m['name']) for m in json.load(open('out/fb_manifest_$TAG.json'))['updates']]")
wait
echo "DONE done=$(wc -l < $DONE) fail=$(wc -l < $FAIL)" | tee -a "$LOG"
echo "SYNCDONE" > "out/fb_sync_$TAG.marker"
