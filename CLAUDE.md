# 给贡献者与 AI 编程助手的项目引导

本仓提供简历筛选管道的参考实现。外部贡献者可以用合成数据测试内核。使用飞书适配器开展招聘运营时，由团队维护独立的私有工作区。

## 首读顺序

1. [README.md](README.md) 介绍项目结构和红线。
2. [playbook/QUICKSTART.md](playbook/QUICKSTART.md) 说明如何接入新岗位。
3. [adapters/feishu/PITFALLS.md](adapters/feishu/PITFALLS.md) 记录调用飞书 API 前需要了解的行为。
4. [playbook/TEAM-GUIDE.md](playbook/TEAM-GUIDE.md) 说明招聘负责人、HR 和操作员的分工。
5. [playbook/MODEL-ROUTING.md](playbook/MODEL-ROUTING.md) 说明如何配置模型与任务的对应关系。
6. [playbook/METHODOLOGY.md](playbook/METHODOLOGY.md) 说明评分规则的设计和验证方法。

## 七条铁律

1. **人在环。** 评分规则、rubric 变更和外部写入须经人类逐轮确认。运营流程须有人值守。归因只报告现象，由人类作决定。
2. **招聘系统零写入。** 招聘 API 仅以 bot 身份读取。获准写入的运营结果只同步到多维表格或云文档，并使用 user 身份。
3. **写后回读。** 批量写表后全量回读，与本地基准逐项比较。零误差才算完成。
4. **同步期间冻结输入。** 从启动同步到完成标记出现，保持 payload、manifest 和快照稳定。
5. **敏感信息留在仓外。** 候选人数据、真实名单和凭证不得进入文件、代码注释或提交说明。将真实配置与数据保存在独立私有工作区。运行 `git config core.hooksPath .githooks` 启用推送检查。维护者通过 `SENSITIVE_PATTERNS` 指定私有模式表。
6. **改规则先模拟。** 使用授权的数据复现基线，比较哪些投递进入或退出精读网。改动后运行 `core/harness.py` 检查回归。详见方法论文档第 6 节。
7. **声明 AI 身份。** 经授权代发飞书消息或评论时，在开头标明实际发送者是 AI 助手。

## 仓库拓扑

GitHub 是本仓的贡献入口和默认推送目标。维护者可为内部镜像单独配置 Git 远端。

## 环境契约

- 内核使用 Python ≥3.11 标准库。飞书适配还需要 `jq`、`lark-cli` 以及 bot 和 user 两种身份。
- 适配器读取 `HSK_WORKSPACE`、`HSK_CONFIG`、`HSK_KIT`、`HSK_BASE_TOKEN` 和 `HSK_TABLE_ID`。从 [环境变量模板](adapters/feishu/env.example.sh) 了解格式，实际值留在私有工作区。
- 内核命令使用 `--config <配置 TOML> --workspace <数据目录>`。支持重定向产物的命令可加 `--out-dir <输出目录>`。

## 运营流程

探测并合并增量投递，人工核定词表变更，运行规则评分，再对进入精读网的投递建档和复核。结果经人类确认后同步表格，回读验证，再刷新阶段、标签、回归护栏和看板。各步骤的输入、产物和验收口径见快速开始文档。
