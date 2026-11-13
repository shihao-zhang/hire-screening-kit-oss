# 架构与移植地图

## 数据流(一轮完整运营)

```
飞书招聘(hire API, bot token)
   │  增量探测: 雪花ID阈值筛新建 → 逐条 GET 查 job_id
   ▼
原始数据 workspace/{applications,talents}_raw.ndjson
   │  core/score_engine.py  ← 岗位配置 TOML(私有)
   ▼
规则层报告 out/report.{md,csv}  + 精读网标记 llm_scope
   │  精读网新进者 → LLM 按 rubric 精读(批次 JSON)
   │  core/merge_reviews.py: first-wins 合并 + 劣化哨兵
   ▼
两层结论 merged.json
   │  adapters/feishu: build_payloads → sync_bitable → 回读核验
   ▼
飞书多维表格(HM/HR 工作界面, user token)
   │  人工评估/面试进展 回流
   ▼
飞轮: label_store → harness(护栏) → attribution(归因队列) → dashboard
```

## 移植地图(运营仓 → kit)

| 运营仓源文件 | kit 目标 | 状态 |
|---|---|---|
| merge_and_score.py (48KB) | core/score_engine.py + 配置外置 | ✅ 2026-07-31 zero-diff 验证通过(440人冻结快照,csv+md 双零差异,种子稳定) |
| llm_review/merge_results.py | core/merge_reviews.py | ✅ 2026-07-31 产物 zero-diff(时间戳归一) |
| build_label_store.py | core/label_store.py | ✅ 2026-07-31 产物 zero-diff(时间戳归一) |
| harness.py | core/harness.py | ✅ 2026-07-31 产物 zero-diff(时间戳归一) |
| attribution_queue.py | core/attribution.py | ✅ 2026-07-31 产物 zero-diff(时间戳归一) |
| build_dashboard.py + dashboard_sync.sh | core/dashboard.py + adapters | 生成器 ✅ 2026-07-31 产物 zero-diff(时间戳归一);sync 见 adapters |
| run_flywheel.sh | flywheel.sh(编排器,保持手动触发) | ✅ 2026-07-31(静态移植;下轮飞轮实测) |
| detect_v69.py | adapters/feishu/detect_new.py | ✅ 2026-07-31(逻辑逐行移植;下轮增量探测实测) |
| build_payloads_v6xx.py ×10 个版本快照 | adapters/feishu/build_payloads.py(参数化收敛) | ✅ 2026-07-31 同输入 payloads+manifest zero-diff |
| fb_sync_* ×12 个版本快照 | adapters/feishu/sync_bitable.sh(参数化收敛) | ✅ 2026-07-31(静态移植;creates 流程待收敛,下轮同步实测) |
| pull_fb_current/pull_manual_evals/build_aid2rid(各自硬编码 3 页,600 条上限 bug) | adapters/feishu/pull_records.sh + parse_records.py(收敛为一拉一解,自动翻页) | ✅ 2026-07-31 实测拉全 440 条(只读),430 条旧快照全吻合 |
| apply_stages.py / refresh_stages.sh | adapters/feishu/ | apply_stages ✅ 同输入三产物 zero-diff;refresh_stages 静态移植 |
| generate_reviews.py / build_dossiers.py | core/dossiers.py | 档案生成器 ✅;规则评语生成器(generate_reviews)评估中 |
| SPEC §5 踩坑库 | adapters/feishu/PITFALLS.md(通用化) | ✅ 2026-07-31 |
| llm_review/RUBRIC.md | playbook/RUBRIC-GUIDE.md(类型学,匿名化) | ✅ 2026-07-31 |
| SPEC.md 活文档模式 | playbook/SPEC-TEMPLATE.md | ✅ 2026-07-31(另有 QUICKSTART/METHODOLOGY) |
| pull_interviews.sh + 面试标签转换 | adapters/feishu/pull_interviews.sh + parse_interviews.py | ✅ 2026-07-31 实测(121 人只读拉取,136 条面评);已接入 flywheel 步骤2;labels_v2 桶退役为 legacy(参考层 final 改「最后一轮已判定」派生) |
| 规则评语生成器 generate_reviews.py | (未移植) | ⚠️ 已知缺口:reviews_by_appid.json 可选,build_payloads 已容错缺省 |
| creates(新增记录)批量创建流 | adapters/feishu/sync_bitable.sh | ⚠️ 已知缺口:当前只收敛了 updates;creates 按 payloads_<tag>/new_*.json 手动批量创建 |

**不移植**:score.py(v1 遗留,已被 merge_and_score 全面取代)、sim_stability_fix.py / diag_stability.py(一次性诊断脚本)、fb_discover/fb_map/pull_new 等早期探索版、build_hr_followup.py(岗位特定,待评估是否模板化)。

## 验收标准(P1)

1. **回归等价**:kit 引擎 + 私有配置,在运营仓冻结快照(440 人)上产出与原产线 `report.md` / `report.csv` **zero-diff**。
2. **护栏全绿**:harness 六检查在 kit 侧复跑结论一致。
3. **零敏感残留**:`tools/sensitive_scan.sh` 全仓扫描通过(候选人姓名、token、job_id、内部文档 id)。

## P3(开源前)处理清单

对抗 review(2026-07-31)判定团队内复用敏感面干净;开源前另需:
1. example.toml 的真实校名(t2/t3/indep_college 示例)换成明显虚构值;
2. 文档中的内部漏斗聚合数(440 投递/5 轮校准等)泛化为量级描述;
3. 代码 docstring 里的真实案例数字细节(如任期均值实例)合成化;
4. git 全历史敏感扫描(工作区扫描不覆盖历史)+ license 选择;
5. [架构图](assets/system-overview.svg) 使用可编辑的矢量图，只展示通用流程与系统边界。

## 配置边界(什么进 TOML)

岗位身份(标题/报告文案)、双子方向关键词表、公司分层名单(TIER1/TIER2/AI-native/pedigree)、学校三档名单、专业分类、评分权重与曲线、各类阈值(年限/短任职/超长简历/recency 衰减/精读网门槛/召回子句)、劣化信号正则、报告口径文本。**运行期参数**(评分基准日期)也在配置,保证复现性。

私有实例配置留在运营仓(本地 git),本仓只提供 `config/example.toml`(占位名单 + 每字段注释)。
