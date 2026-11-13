# 飞书适配层

管道与飞书两侧系统的全部交互:**招聘(hire API,bot token,零写入)** 读投递/人才/面试;
**多维表格(bitable,user token)** 是 HM/HR 的工作界面,读写都在这。接入前先读 [PITFALLS.md](PITFALLS.md)。

## 环境契约

所有脚本从环境变量取实例信息(私有 env 文件放 kit 仓外,`source` 后使用):

```bash
export HSK_WORKSPACE=/path/to/your-workspace   # 数据工作目录(ndjson/out 所在)
export HSK_CONFIG=/path/to/your-job.toml       # 私有岗位配置
export HSK_KIT=/path/to/hire-screening-kit     # 本仓根目录
export HSK_BASE_TOKEN=xxx                      # bitable app_token
export HSK_TABLE_ID=tblxxx                     # 结果表 table_id
```

## 脚本清单

| 脚本 | 职责 | 副作用 |
|---|---|---|
| pull_records.sh + parse_records.py | 通用拉表(自动翻页)+ 列式 JSON 解析(current/manual/aid2rid/stages 四模式) | 只读 |
| detect_new.py | 增量探测新投递(雪花 ID 阈值姿势) | 只读 hire |
| refresh_stages.sh | 重拉全部投递详情刷新阶段/状态 | 只读 hire |
| apply_stages.py | 阶段 patch 进本地库 + 产出写表指令 | 改本地 ndjson |
| build_payloads.py --tag | 同步构建器:creates 全字段 + updates 字段级 diff | 写本地 payload |
| sync_bitable.sh <tag> | 按 manifest 逐条 upsert 存量更新 | **写 bitable** |
| dashboard_sync.sh | 重生成看板并覆写飞书常驻文档 | **写 docs** |
| ../flywheel.sh | 五步编排:阶段刷新(+写表+回读)→标签回流→护栏→归因→看板 | 写 bitable/docs |

新增记录(creates)当前走 `payloads_<tag>/new_*.json` 逐条创建(≤200/批,先补齐 select options),
尚未收敛进 sync_bitable.sh——见 ARCHITECTURE 移植地图的待办标注。

## 纪律(不可绕过)

1. **招聘系统零写入**:hire API 只读;所有写操作只落 bitable/docs。
2. **写后回读**:任何批量写表后全量拉回与本地基准 diff,零误差才算完成。
3. **同步竞态**:sync 启动到 marker 出现前,不碰 payload/manifest/快照。
4. **人在环**:写表动作由在场操作者逐轮授权,不挂 cron 无人值守。
