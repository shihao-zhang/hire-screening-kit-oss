#!/usr/bin/env bash
# 通用 bitable 拉取器:自动翻页直到页不满,任意表规模安全。
# (取代旧 pull_fb_current/pull_manual_evals/build_aid2rid 里硬编码 0/200/400 三页的写法——
#  那会在表超 600 条时静默截断,是最危险的一类 bug。)
#
# 用法: pull_records.sh <输出raw文件> [<field-id>...]   不传 field-id = 拉全列(同步 diff 的基准快照)
# 需要环境变量: HSK_BASE_TOKEN, HSK_TABLE_ID (source 你的私有 feishu.env)
# 输出: raw 文件为多页拼接的 pretty-JSON —— 必须用 parse_records.py 流式解析,勿逐行 json.loads。
set -uo pipefail
: "${HSK_BASE_TOKEN:?source 私有 feishu.env 后再跑}"
: "${HSK_TABLE_ID:?source 私有 feishu.env 后再跑}"
RAW="$1"; shift
FIELDS=()
for f in "$@"; do FIELDS+=(--field-id "$f"); done
: > "$RAW"
off=0
while :; do
  tries=0; got=""
  while [ "$tries" -lt 5 ]; do
    out=$(lark-cli base +record-list --base-token "$HSK_BASE_TOKEN" --table-id "$HSK_TABLE_ID" \
          --as user --format json --limit 200 --offset "$off" \
          "${FIELDS[@]+"${FIELDS[@]}"}" 2>/dev/null)   # 空数组展开:bash 3.2 下 "${A[@]}" 在 set -u 会崩
    if [ -n "$out" ] && [ "$(jq -r '.ok' <<<"$out" 2>/dev/null)" = "true" ]; then got=1; break; fi
    tries=$((tries+1)); sleep 2
  done
  if [ -z "$got" ]; then echo "offset $off FAILED(重试5次)" >&2; exit 1; fi
  echo "$out" >> "$RAW"
  n=$(jq -r '.data.data | length' <<<"$out")
  if [ "$n" -lt 200 ]; then break; fi
  off=$((off+200))
done
echo "pulled offsets 0..$off (last page $n rows)"
