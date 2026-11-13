#!/usr/bin/env bash
# 飞轮④ 手动触发的一键编排器(运营维护刷新)。
# 设计约束:保持手动——只在操作者在场时触发,不挂 cron、不无人值守(人在环纪律)。
# 覆盖:阶段刷新(+写飞书+回读核验) → 标签回流 → 护栏 → 归因队列 → 看板更新。
# 不含:探测新投递 / 评分 / LLM 精读 / 新增同步——这些需要在场判断阈值与授权,留在主流程。
#
# 用法: source <你的私有 feishu.env> && ./flywheel.sh [prev_report.csv]
# 需要环境变量: HSK_WORKSPACE, HSK_CONFIG, HSK_KIT, HSK_BASE_TOKEN, HSK_TABLE_ID
set -uo pipefail
: "${HSK_WORKSPACE:?}"; : "${HSK_CONFIG:?}"; : "${HSK_KIT:?}"; : "${HSK_BASE_TOKEN:?}"; : "${HSK_TABLE_ID:?}"
AD="$HSK_KIT/adapters/feishu"
cd "$HSK_WORKSPACE"
PREV="${1:-out/report.csv}"   # harness 对比基线,默认当前 report
echo "════ 飞轮编排开始(基线 $PREV)════"

echo "── 1/5 阶段刷新 ──"
bash "$AD/refresh_stages.sh"
bash "$AD/pull_records.sh" out/aid2rid_raw.json application_id
${HSK_PYTHON:-python3} "$AD/parse_records.py" --mode aid2rid --raw out/aid2rid_raw.json --out-dir out
${HSK_PYTHON:-python3} "$AD/apply_stages.py" --workspace "$HSK_WORKSPACE"
N=$(python3 -c "import json;print(len(json.load(open('out/stage_upd_cmds.json'))))")
if [ "$N" -gt 0 ]; then
  OK=0; FAIL=0
  while IFS= read -r line; do
    RID=$(jq -r '.rid' <<<"$line"); FIELDS=$(jq -c '.fields' <<<"$line")
    R=$(lark-cli base +record-upsert --base-token "$HSK_BASE_TOKEN" --table-id "$HSK_TABLE_ID" --as user --record-id "$RID" --json "$FIELDS" 2>/dev/null | jq -r '.ok')
    if [ "$R" = "true" ]; then OK=$((OK+1)); else FAIL=$((FAIL+1)); echo "  FAIL $RID"; fi
  done < <(jq -c '.[]' out/stage_upd_cmds.json)
  echo "  飞书阶段写入 OK=$OK FAIL=$FAIL"
  # 回读核验(批量写外部系统后必须回读,零误差才算完成)
  sleep 20   # record-list 读有秒级滞后(见 PITFALLS),小批量写后立即回读会假报不一致
  bash "$AD/pull_records.sh" out/stages_raw.json application_id 当前阶段 状态
  ${HSK_PYTHON:-python3} "$AD/parse_records.py" --mode stages --raw out/stages_raw.json --out-dir out
  python3 - <<'PY'
import json
# 只核验实际下发了写指令的条目(stage_changes 里"无 rid 未写入"的不算不一致);
# new_as 不在 1/2 时未写状态字段,跳过状态比对。
cmds = {c['aid'] for c in json.load(open('out/stage_upd_cmds.json'))}
ch = [c for c in json.load(open('out/stage_changes.json')) if c['aid'] in cmds]
got = json.load(open('out/stages_readback.json'))
bad = 0
for c in ch:
    g = got.get(c['aid'], {})
    ok = g.get('当前阶段') == c['new_stage']
    if c['new_as'] in (1, 2):
        ok = ok and g.get('状态') == ('已终止' if c['new_as'] == 2 else '在招')
    if not ok:
        bad += 1
print(f"  阶段回读核验：不一致 {bad}/{len(ch)}")
PY
else
  echo "  无阶段变化"
fi

echo "── 2/5 标签回流 ──"
# ⚠️ 必须先重跑评分再建标签库:apply_stages 只把新 stage patch 进 applications_raw,
#    **report.csv 不会自动跟着变**;而 label_store 的 stage(进而终点真阳性集合)读的是 report.csv。
#    不补这一步,当轮新走到「面试通过/Offer沟通/待入职/已入职」的人**不会进护栏基准**。
#    安全性已验证:stage 不是评分输入,重跑只刷新 stage/active 两列,分数逐字节不变。
${HSK_PYTHON:-python3} "$HSK_KIT/core/score_engine.py" --config "$HSK_CONFIG" --workspace "$HSK_WORKSPACE" 2>&1 | tail -1
bash "$AD/pull_records.sh" out/manual_evals_raw.json application_id 姓名 粗排名 总分 顶尖背书 简历评估（人工） 当前阶段 LLM评分 LLM建议
${HSK_PYTHON:-python3} "$AD/parse_records.py" --mode manual --raw out/manual_evals_raw.json --out-dir out
# 面试结论+面评自动回流(hire 只读,串行拉取)——HM 不需要在表格里维护面试后续
bash "$AD/pull_interviews.sh"
${HSK_PYTHON:-python3} "$AD/parse_interviews.py" --workspace "$HSK_WORKSPACE"
${HSK_PYTHON:-python3} "$HSK_KIT/core/label_store.py" --config "$HSK_CONFIG" --workspace "$HSK_WORKSPACE" 2>&1 | tail -1

echo "── 3/5 护栏(先跑,产出终点漏网供归因队列) ──"
${HSK_PYTHON:-python3} "$HSK_KIT/core/harness.py" --config "$HSK_CONFIG" --workspace "$HSK_WORKSPACE" --prev "$PREV" --changed-dims "飞轮编排刷新" --version "${HSK_VERSION:-飞轮}" 2>&1 | sed -n '/闸门摘要/,/报告写出/p'

echo "── 4/5 归因队列(含终点漏网 F 类) ──"
${HSK_PYTHON:-python3} "$HSK_KIT/core/attribution.py" --config "$HSK_CONFIG" --workspace "$HSK_WORKSPACE"

echo "── 5/5 看板更新 ──"
bash "$AD/dashboard_sync.sh"

echo "════ 编排完成 ════"
DOC=$(python3 -c "import json;print(json.load(open('out/dashboard_doc.json'))['url'])")
echo "看板：$DOC"
