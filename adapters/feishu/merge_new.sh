#!/usr/bin/env bash
# 把 detect_new.py 的增量产物合并进工作库(评分前置)。
# 幂等保护:新增 id 已在库则中止(防重复合并);合并前自动备份。
# 用法: merge_new.sh   需要环境变量: HSK_WORKSPACE
set -euo pipefail
: "${HSK_WORKSPACE:?}"
cd "$HSK_WORKSPACE"
N=$(grep -c . out/new_app_ids.txt || true)
if [ "$N" -eq 0 ]; then echo "无新增,跳过"; exit 0; fi
dup=$(comm -12 <(sort out/new_app_ids.txt) <(sort out_application_ids.txt) | wc -l | tr -d ' ')
if [ "$dup" -gt 0 ]; then echo "❌ $dup 条新增 id 已在库(疑重复合并),中止"; exit 1; fi
ts=$(date +%s)
cp applications_raw.ndjson "applications_raw.bak_$ts"
cp talents_raw.ndjson "talents_raw.bak_$ts"
# 追加前确保文件以换行结尾,防止首条新记录与末行拼接
for f in applications_raw.ndjson talents_raw.ndjson out_application_ids.txt; do
  [ -s "$f" ] && [ "$(tail -c1 "$f" | wc -l)" -eq 0 ] && echo >> "$f"
done
cat out/new_apps.ndjson >> applications_raw.ndjson
cat out/new_talents.ndjson >> talents_raw.ndjson
cat out/new_app_ids.txt >> out_application_ids.txt
echo "合并 $N 条(备份 .bak_$ts);下一步重跑 core/score_engine.py"
