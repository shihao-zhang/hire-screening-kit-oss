#!/usr/bin/env bash
# 重拉全部 application 详情,刷新 stage + active_status(保持"当前阶段"/"状态"不滞后)。
# 用法: refresh_stages.sh   需要环境变量: HSK_WORKSPACE
# 输出: out/app_stages_fresh.ndjson(小行,并发追加安全;大体积行严禁并发 >>,见 PITFALLS)
set -uo pipefail
: "${HSK_WORKSPACE:?}"
cd "$HSK_WORKSPACE"
LOG=out/refresh_stages.log
OUT=out/app_stages_fresh.ndjson
: > "$LOG"; : > "$OUT"
get() {
  local aid="$1" tries=0 out
  while [ "$tries" -lt 3 ]; do
    out=$(lark-cli api GET "/open-apis/hire/v1/applications/$aid" --as bot 2>>"$LOG")
    if [ -n "$out" ] && [ "$(jq -r '.ok' <<<"$out" 2>/dev/null)" = "true" ]; then
      jq -c '{id: .data.application.id, aid: "'"$aid"'", stage: (.data.application.stage.zh_name // null), active_status: .data.application.active_status}' <<<"$out" >> "$OUT"
      return 0
    fi
    tries=$((tries+1)); sleep 1
  done
  echo "FAIL $aid" >>"$LOG"; echo "{\"aid\":\"$aid\",\"stage\":null,\"active_status\":null,\"fail\":true}" >> "$OUT"; return 1
}
n=0
while IFS= read -r AID; do
  [ -z "$AID" ] && continue
  get "$AID" &
  n=$((n+1)); if [ $((n % 6)) -eq 0 ]; then wait; fi
done < out_application_ids.txt
wait
echo "DONE refreshed=$(grep -c . $OUT)" | tee -a "$LOG"
