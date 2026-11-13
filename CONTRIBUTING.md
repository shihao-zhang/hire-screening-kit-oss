# 参与贡献

本地开发需要 Python ≥3.11，内核只使用标准库。安装 `uv` 后，可用 `uv run --python 3.11` 选择与 CI 一致的解释器。只有在独立测试环境运行飞书适配器时才需要 `jq`、`lark-cli` 和 bot、user 两种授权身份。环境变量格式见 [飞书适配器](adapters/feishu/README.md)。

从 `main` 创建主题分支。每个提交聚焦一项改动，并在提交说明里写清改动及原因。用 PR 说明验证方式。开发和复现使用合成数据，真实候选人资料、名单、私有配置与凭证均留在仓外。

先在仓库根目录启用推送钩子，再执行本地检查。

```bash
git config core.hooksPath .githooks
uv run --python 3.11 bash tools/smoke_test.sh
bash tools/sensitive_scan.sh
```

Smoke 会用合成数据在临时目录运行评分并检查 CSV，也会检查两个常用命令入口。单独运行内核可用 `uv run python core/score_engine.py --help` 查看参数，输入格式与配置见 [README](README.md)。

扫描默认使用通用模式表。维护者可以用环境变量 `SENSITIVE_PATTERNS` 指向仓外的可读私有模式表。启用钩子后，推送还会检查相关提交的说明和补丁。提交前检查注释和示例，避免把可识别个人的案例写入历史。
