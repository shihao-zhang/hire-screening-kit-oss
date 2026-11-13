#!/usr/bin/env bash
# 拉取面试记录(结论/面评原文/面试官/轮次)——hire API 只读,bot token。
# 对象 = report.csv 里已越过筛选阶段的投递(含已终止;没面试的返回空 items,无害)。
# ⚠️ 串行拉取,勿加并发:面评行体积常超 PIPE_BUF,并发 `>>` 会字节交错损坏(实测血坑,见 PITFALLS)。
# 用法: pull_interviews.sh   需要环境变量: HSK_WORKSPACE, HSK_CONFIG
set -uo pipefail
: "${HSK_WORKSPACE:?}"; : "${HSK_CONFIG:?}"
cd "$HSK_WORKSPACE"
OUT=out/interviews.ndjson
LOG=out/pull_interviews.log
# 选人:阶段不在 pending_stages(配置)的投递
${HSK_PYTHON:-python3} - "$HSK_CONFIG" > out/iv_app_ids.txt <<'PY'
import csv, sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
pending = set(cfg["report"]["pending_stages"])
for r in csv.DictReader(open("out/report.csv")):
    if (r.get("stage") or "") and r["stage"] not in pending:
        print(r["application_id"])
PY
: > "$OUT"; : > "$LOG"
n=0
while IFS= read -r AID; do
  [ -z "$AID" ] && continue
  tries=0
  while [ "$tries" -lt 4 ]; do
    out=$(lark-cli api GET "/open-apis/hire/v1/interviews" --as bot \
          --params "{\"application_id\":\"$AID\",\"page_size\":\"50\"}" 2>>"$LOG")
    if [ -n "$out" ] && [ "$(jq -r '.ok' <<<"$out" 2>/dev/null)" = "true" ]; then
      jq -c --arg aid "$AID" '{application_id:$aid, items:(.data.items // [])}' <<<"$out" >> "$OUT"
      break
    fi
    tries=$((tries+1)); echo "[retry $tries] $AID" >>"$LOG"; sleep 2
  done
  [ "$tries" -ge 4 ] && echo "FAILED $AID" >>"$LOG"
  n=$((n+1))
done < out/iv_app_ids.txt
echo "interviews pulled: $n apps → $OUT (fail=$(grep -c FAILED $LOG || true))"
