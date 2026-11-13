# QUICKSTART:新岗位接入(约 30-60 分钟,不含校准)

目标读者:用飞书招聘 + 多维表格的 HM/招聘团队,由一个 AI 编程助手(Claude Code 等)陪跑。
接入产出:规则层排序表 + 精读网圈选跑通;LLM 精读与飞轮随运营逐轮启用。

## 0. 前置条件

- Python ≥ 3.11(tomllib)与 `jq`;`lark-cli` 已认证(bot + user 两个身份都要,见 adapters/feishu/PITFALLS.md 的两段式权限坑)
- 飞书招聘里目标职位的 job_id;一个多维表格作为结果表
- 建议:私有工作目录立即 `git init`,数据文件全部 .gitignore(参考本仓红线)

## 1. 建工作目录与私有配置(10 分钟)

```bash
mkdir -p ~/my-screening/{out,llm_review/{dossiers,results},config}
cp <kit>/config/example.toml ~/my-screening/config/job.toml     # 逐段替换
cp <kit>/config/model_routing.example.toml ~/my-screening/config/model_routing.toml  # 填执行通道
cp <kit>/adapters/feishu/env.example.sh ~/my-screening/config/feishu.env  # 填真值
```

配置先"粗填能跑":关键词表抄 JD 高频词,公司/学校名单填你认得的;**精细化交给校准轮**,
不要在冷启动阶段追求名单完美。

## 1.5 填模型路由表(5 分钟)

精读层要调用模型,但「哪个任务用哪个模型」不写死在协议里。按
[MODEL-ROUTING.md](MODEL-ROUTING.md) 在 `config/model_routing.toml` 里填**执行通道**
(当前会话 / 子代理 / 本机 CLI / 异构审查)并把任务映射到通道:

| 任务 | 默认执行者 | 说明 |
|---|---|---|
| 探测/合并/评分/建档/同步/回读/护栏 | deterministic | **不调用模型** |
| 词表归类、边界复核、漂移监测 | 轻量或常规模型 | 按表填 |
| 常规精读（dossier 批量） | 常规模型，effort medium | 批 10–12 人 |
| 高分复核（推荐且 ≥80） | 异构模型，effort high | 全量盲评 |
| 规则设计/归因/文档/飞书交互/改代码 | 当前会话直接处理 | **不另选模型** |

模型 ID 不确定就填 `"auto"`（用 CLI 当前配置）。换常规精读模型前先做漂移校准（见批处理准则第 5 条）。

## 2. 建结果表(10 分钟)

在多维表格建表,列名契约(与 adapters/feishu/build_payloads.py 一致):
`粗排名 姓名 总分 学历分 专业分 年限分 技能分 公司分 稳定性分 项目分 方向匹配 状态 当前阶段
顶尖背书 学位 专业 当前公司 院校 简历来源 详情页链接 application_id talent_id 简历评价
命中关键词(多选) 投递时间(日期) LLM评分 LLM建议 LLM方向 LLM评审意见 简历评估（人工）`
最后一列留给人工评语(格式约定:【通过/待定/不通过】开头,可加"排名偏高/偏低"提示)。

## 3. 首轮全量拉取(15 分钟,视投递量)

首轮无基线,直接全量:`--page-all` 拉全 org 投递 ID → 逐条 GET 过滤本职位(job_id 过滤 API
已失效,姿势见 PITFALLS)→ 攒出 `applications_raw.ndjson` / `talents_raw.ndjson` /
`out_application_ids.txt`。之后每轮增量两步:探测 + 合并入库:

```bash
python3 <kit>/adapters/feishu/detect_new.py --config config/job.toml --workspace .
source config/feishu.env && bash <kit>/adapters/feishu/merge_new.sh   # 幂等保护+自动备份
```

## 4. 评分与报告(1 分钟)

```bash
python3 <kit>/core/score_engine.py --config config/job.toml --workspace .
```

产出 `out/report.md`(排序表)与 `out/report.csv`(全字段含 llm_scope 精读网标记)。

## 5. LLM 精读(首轮 30 分钟起草 rubric)

1. 按 [RUBRIC-GUIDE.md](RUBRIC-GUIDE.md) 起草你的 rubric(放私有目录);
2. 圈选精读网并生成档案:

```bash
python3 -c "import csv;print('\n'.join(r['application_id'] for r in csv.DictReader(open('out/report.csv')) if r['llm_scope']=='True'))" > out/need_review_ids.txt
python3 <kit>/core/dossiers.py --config config/job.toml --workspace .
```

3. 让 AI 助手按 rubric 分批精读档案(大批量走 subagent 隔离),结果 JSON 落 `llm_review/results/`——
   **文件名必须匹配 `job.toml [merge]` 的 main_globs**(默认 `batch_*.json`),否则合并结果为 0;
4. 跑 `core/merge_reviews.py --config config/job.toml --workspace .` 合并(修正批注册进 override_globs)。

## 6. 同步到结果表

```bash
source config/feishu.env
bash <kit>/adapters/feishu/pull_records.sh out/fb_raw.json application_id 粗排名 总分 顶尖背书 简历评价 稳定性分 状态 当前阶段 LLM评分 LLM建议 LLM方向 LLM评审意见
python3 <kit>/adapters/feishu/parse_records.py --mode current --raw out/fb_raw.json --out-dir out
python3 <kit>/adapters/feishu/build_payloads.py --config config/job.toml --workspace . --tag r1
bash <kit>/adapters/feishu/sync_bitable.sh r1        # 存量更新;首轮 creates 见 adapters/README
```

可选输入(缺省自动跳过):`out/fb_kw_opts.json` = 「命中关键词」多选列的现有 options 数组
(field-get 拉取后存 JSON);`out/reviews_by_appid.json` = 规则评语文本(生成器未随 kit 提供)。

**写后必回读核验**(纪律,见 PITFALLS)——零误差才算完成:

```bash
bash <kit>/adapters/feishu/pull_records.sh out/fb_readback_raw.json     # 拉全列
python3 <kit>/adapters/feishu/parse_records.py --mode current --raw out/fb_readback_raw.json --out-dir out
cp out/fb_current.json out/fb_readback.json                            # 回读快照另存,别覆盖 diff 基准
python3 <kit>/adapters/feishu/verify_sync.py --workspace . --tag r1     # 退出码 0 = 零误差
```

## 7. 运营循环(每轮)

增量探测+合并 → 评分 → 新进网者精读 → 同步 → `flywheel.sh`(阶段刷新/标签回流/护栏/归因/看板)。
HM 在表里写人工评语,飞轮把它变成校准输入;归因队列只报现象,**改规则由你拍板**。

首次跑 flywheel 前的一次性准备:建一个飞书文档作常驻看板,把
`{"document_id": "...", "url": "..."}` 存到 `out/dashboard_doc.json`(dashboard_sync 覆写它)。
面试结论+面评由 flywheel 步骤2 自动从招聘系统回流(pull_interviews,只读),无需人工维护。

## 7.5 踩坑:不要清空 llm_review/dossiers 目录

档案目录是合并范围与「是否精读过」的事实源,`dossiers.py` 只写不删是**故意的**:

- 出网者的档案留在目录里 → `merge_reviews` 仍知道他们被精读过,结论保留在 merged 与结果表里。
- 一旦清空重建,出网者的结论从此进不了 merged(默认 `scope=dossiers` 只认目录里现存的人);
  之后他们再进网时会被当成新人**重新精读一遍**,而合并的 first-wins 会用**旧结论**压住新结论,
  结果表与你的记录静默漂移。一个真实实例:一次清空后,72 人的结论丢出 merged、3 人被重复精读,
  其中一人结果表显示 35/不推荐、而最新精读是 58/待定。

修复建议(择一,都不需要清空):刻意重评某人 → 新结论批次注册 `override_globs`(优先于旧批次);
想让 merged 覆盖所有精读过的人 → `[merge].scope = "reviewed"`;建档范围永远跟评分走 →
`[dossier].scope_source = "report"`。此外:merge 输出的问题清单**先数条数再看内容**,不要只看
前三行(长清单里藏着重复精读与范围问题)。

## 8. 校准(持续)

前几轮重点:攒 10+ 条人工评估 → 跑对齐 → 按归因修 rubric/规则 → harness 回归全绿再上线。
改规则的验证纪律见 [METHODOLOGY.md](METHODOLOGY.md) §6(模拟不落盘/血范围看精读网进出)。
