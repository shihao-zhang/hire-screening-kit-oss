#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
python="${PYTHON:-python3}"
if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo 'Smoke 需要 Python ≥3.11。可运行 uv run --python 3.11 bash tools/smoke_test.sh。' >&2
  exit 2
fi
workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT

# 全部输入均为合成数据。
cat > "$workspace/applications_raw.ndjson" <<'DATA'
{"id":"test-application","talent_id":"test-talent","stage":{"zh_name":"简历初筛"},"active_status":1}
DATA
cat > "$workspace/talents_raw.ndjson" <<'DATA'
{"id":"test-talent","basic_info":{"name":"测试候选人","working_years":3},"career_list":[{"company":"测试公司","start_time":"2023-01","end_time":"2025-01","desc":"主导测试工具开发"}]}
DATA
echo "test-application" > "$workspace/out_application_ids.txt"

cat > "$workspace/smoke.toml" <<'TOML'
skills_curve = [[0, 20], [1, 70]]

[run]
now_year = 2025
now_month = 1
now_ms = 1735689600000
stale_warn_days = 100000

[llm_scope]
main_th = 50
recent_days = 7
recent_th = 40
backing_co = 100
backing_default_role = "未匹配"

[job]
years_min = 1
years_max = 5
fresh_grad_stability = 70

[roles]
role_a_keywords = ["测试工具"]
role_b_keywords = []
role_a_label = "测试工具"
role_b_label = "其他方向"
role_both_label = "均匹配"
role_none_label = "未匹配"
role_fit_ratio = 2.0

[companies]
tier1 = []
tier2 = []
pedigree = []
ai_native = []
tech_extra = []

[schools]
t1 = []
t2 = []
t3 = []
abbr_tier = {}
tier_weight = {}
penalty_floor = 1.0
indep_college = []

[degrees]
label = {}
score = {}

[majors]
cs = []
quant = []
eng = []
biz = []

[signals]
quant_pattern = '[0-9]+'
ownership_markers = ["主导"]
support_markers = ["协助"]
self_assessment_headers = ["自我评价"]
trad_company_pattern = '(?!)'
ai_stint_pattern = '(?!)'
tool_claim_pattern = '(?!)'
hard_depth_pattern = '(?!)'
owner_evidence_pattern = '主导'
exec_evidence_pattern = '协助'
owner_title_pattern = '(?!)'
exec_title_pattern = '(?!)'
ops_title_pattern = '(?!)'
pm_title_pattern = '(?!)'
intern_title_pattern = '实习'

[thresholds]
ai_stale_months = 12
short_stint_months = 6
short_stint_window = 24
verbose_l1 = 1000
verbose_l2 = 2000
verbose_mult = { 0 = 1.0, 1 = 0.9, 2 = 0.8 }
recency_fresh_months = 12
recency_decay_months = 48
recency_floor = 0.4
missing_date_weight = 0.8
mult_shallow_env = 0.9
mult_ai_stale = 0.9
years_skills_guard = { min_years_score = 70, max_skills_score = 20, downgrade_to = 40 }

[weights]
education = 0.1
major = 0.1
years = 0.1
skills = 0.3
company = 0.1
stability = 0.1
project = 0.2

[report]
title = "# Smoke test"
notes = []
years_note = ""
pending_stages = ["简历初筛"]
TOML

"$python" core/score_engine.py --config "$workspace/smoke.toml" --workspace "$workspace"
"$python" - "$workspace/out/report.csv" <<'PY'
import csv
import sys

with open(sys.argv[1], encoding="utf-8", newline="") as report:
    reader = csv.DictReader(report)
    required = {"rank", "name", "current_company", "total", "stage", "active", "application_id", "llm_scope"}
    assert reader.fieldnames is not None and required <= set(reader.fieldnames), reader.fieldnames
    rows = list(reader)

assert len(rows) == 1, len(rows)
row = rows[0]
expected = {
    "rank": "1",
    "name": "测试候选人",
    "current_company": "测试公司",
    "stage": "简历初筛",
    "active": "在招",
    "application_id": "test-application",
}
for field, value in expected.items():
    assert row[field] == value, (field, row[field])
assert 0 <= float(row["total"]) <= 100, row["total"]
assert row["llm_scope"] in {"True", "False"}, row["llm_scope"]
print("✓ report.csv 含 1 条合成记录，字段和值符合预期")
PY

"$python" core/merge_reviews.py --help >/dev/null
"$python" core/harness.py --help >/dev/null
echo "✓ merge_reviews 和 harness 命令入口可用"
