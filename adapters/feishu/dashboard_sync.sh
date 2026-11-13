#!/usr/bin/env bash
# 看板同步:重新生成归因队列 + dashboard.md 并覆写飞书常驻文档。
# 看板是纯机器生成物(无人工评论/图片),overwrite 安全;文档 id 存 workspace/out/dashboard_doc.json。
# 用法: dashboard_sync.sh   需要环境变量: HSK_WORKSPACE, HSK_CONFIG, HSK_KIT(kit 仓根目录)
set -uo pipefail
: "${HSK_WORKSPACE:?}"; : "${HSK_CONFIG:?}"; : "${HSK_KIT:?}"
cd "$HSK_WORKSPACE"
${HSK_PYTHON:-python3} "$HSK_KIT/core/attribution.py" --config "$HSK_CONFIG" --workspace "$HSK_WORKSPACE" >/dev/null
${HSK_PYTHON:-python3} "$HSK_KIT/core/dashboard.py" --config "$HSK_CONFIG" --workspace "$HSK_WORKSPACE" >/dev/null
DOC=$(python3 -c "import json;print(json.load(open('out/dashboard_doc.json'))['document_id'])")
R=$(lark-cli docs +update --doc "$DOC" --command overwrite --doc-format markdown --content @out/dashboard.md 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('ok'))")
echo "dashboard sync ok=$R doc=$DOC"
