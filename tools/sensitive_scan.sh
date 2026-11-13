#!/bin/bash
# 扫描已跟踪文件，推送钩子额外检查提交说明和补丁。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

if [[ ${SENSITIVE_PATTERNS+x} ]]; then
  if [[ ! -f "$SENSITIVE_PATTERNS" || ! -r "$SENSITIVE_PATTERNS" ]]; then
    echo "❌ 无法读取 SENSITIVE_PATTERNS 指定的私有模式表" >&2
    exit 2
  fi
  # 私有模式表保留 grep 基本正则语义。
  pattern_args=(-f "$SENSITIVE_PATTERNS")
  patch_filter=(cat)
else
  echo "使用通用敏感模式 tools/sensitive_patterns.example.txt"
  example_patterns=tools/sensitive_patterns.example.txt
  if [[ ! -r "$example_patterns" ]]; then
    echo "❌ 无法读取通用模式表" >&2
    exit 2
  fi
  pattern_args=(-E)
  # 仅跳过 Git 提交说明中的标准协作者机器人地址，补丁行始终带 diff 前缀。
  patch_filter=(sed -E '/^Co-Authored-By: Claude( (Fable|Opus) [0-9]+([.][0-9]+)?)? <noreply[@]anthropic[.]com>$/d')
  while IFS= read -r pattern || [[ -n "$pattern" ]]; do
    [[ -z "$pattern" || "$pattern" == "#"* ]] && continue
    pattern_args+=(-e "$pattern")
  done < "$example_patterns"
  if [[ ${#pattern_args[@]} -eq 1 ]]; then
    echo "❌ 通用模式表为空" >&2
    exit 2
  fi
fi

git grep -I "${pattern_args[@]}" -- . >/dev/null
status=$?
case "$status" in
  0) echo "❌ 已跟踪文件命中敏感模式" >&2; exit 1 ;;
  1) ;;
  *) echo "❌ 已跟踪文件扫描失败" >&2; exit 2 ;;
esac

if [[ ${1-} != --pre-push ]]; then
  echo "✓ 敏感扫描通过($(git ls-files | wc -l | tr -d ' ') 个文件)"
  exit 0
fi

while read -r _local_ref local_sha _remote_ref remote_sha; do
  [[ "$local_sha" =~ ^0+$ ]] && continue  # 删除引用无需扫描
  range="$local_sha"
  if [[ ! "$remote_sha" =~ ^0+$ ]]; then
    range="$remote_sha..$local_sha"
  fi
  # %B 保留提交说明，省略 Git 作者元数据；-m 同时检查合并提交的补丁。
  git log -m -p --no-color --format=%B "$range" -- |
    "${patch_filter[@]}" | grep -I "${pattern_args[@]}" >/dev/null
  statuses=("${PIPESTATUS[@]}")
  if [[ ${statuses[0]} -ne 0 || ${statuses[1]} -ne 0 || ${statuses[2]} -gt 1 ]]; then
    echo "❌ 待推送区间扫描失败 ($range)" >&2
    exit 2
  fi
  if [[ ${statuses[2]} -eq 0 ]]; then
    echo "❌ 待推送提交的说明或补丁命中敏感模式 ($range)" >&2
    exit 1
  fi
done

echo "✓ pre-push 敏感闸门通过(文件 + commit message + 区间 diff)"
