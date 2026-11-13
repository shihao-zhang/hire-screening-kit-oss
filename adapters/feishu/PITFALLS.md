# 飞书 API 踩坑库(全部实测,勿重复踩)

本表来自一次真实招聘运营(440+ 投递、十余轮增量同步)的实测记录。接入前通读一遍,能省一周。

## Token 与权限

| 坑 | 处置 |
|---|---|
| hire(招聘)API 只认 tenant token | 一律 `lark-cli api ... --as bot`;user token 报 1002003 |
| bitable **写入**只认 user token(读可用 bot) | 写(record-upsert/batch)一律 `--as user`;用 bot 写报 91403 "you don't have permission" |
| hire scope 是两段式:开放平台 scope + 招聘后台"角色授权" | 两者都要;报错 `action_scope_required` = 应用没申请/没发布,`action_privilege_required` = 后台没授角色 |

## 读取与解析

| 坑 | 处置 |
|---|---|
| bitable CLI flag 与 hire 不同 | base 用 `--base-token`(非 `--app-token`)、`--format json`,翻页用 `--limit/--offset`(≤200) |
| record-list/get 返回**列式** JSON | `.data.data`=行数组(按 `.data.fields` 列序)、`.data.record_id_list` 并行;用 parse_records.py |
| 多页输出是拼接的 pretty-JSON | 必须 `JSONDecoder.raw_decode` 流式解析,逐行 `json.loads` 会崩 |
| select 字段读回是数组(如 `["在招"]`) | 写入单选给字符串、多选给数组;写前用 field-list 看 `multiple` 标志 |
| url 样式 text 字段读回是 `[url](url)` markdown 包裹 | 校验时先剥壳再比对 |
| **record-list 读有秒级滞后**(read-after-write):写后立即 list 回读可能拿到旧值,小批量写时尤其明显(大批量写因耗时长从未暴露) | 回读前等 15-30 秒;失配先延迟重试一次再判失败;逐条精确核验可用 `+record-get`(实时) |
| `lark-cli api` 静默忽略 URL 里的 `?query` 参数 | query 必须走 `--params '{"k":"v"}'`,拼在路径里表现为"按默认参数请求" |
| applications list 分页:URL 参数被静默忽略 | 分页只能用 CLI flag:`--page-all` + `--page-size` + `--page-limit`(默认10要调大) |
| **翻页拉全表勿硬编码页数** | 表增长会静默截断(最危险的一类 bug)——用 pull_records.sh 自动翻页到页不满为止 |

## 写入

| 坑 | 处置 |
|---|---|
| record-batch-update 是"一个 patch 套所有记录" | per-row 不同值必须逐条 `record-upsert --record-id`;先拉 application_id→record_id 映射 |
| 无 auto-upsert by key | 带 `--record-id` 是更新,不带是创建;键映射自己维护 |
| **record-id 传空会静默创建脏行**(不报错):`--record-id ""` 等同不传 → 新建空白记录 | 调 upsert 前断言 rid 非空;手工取列勿用 `cut -dN`(列序会变),用 csv.DictReader 按列名取;误建后 `+record-delete --record-id X --yes`(高风险写需 --yes) |
| bitable 长文本字段**完整保留 `\n`**(与文档仪表盘文本块不同,后者吞换行) | 评审意见等长文本可放心用换行+项目符号排版,实测 183 条回读零失真 |
| 批量创建 ≤200 行/次;高频 upsert 限流(800004135 "limited") | 分批 + 8 并发;限流的串行 sleep 重试 |
| datetime 写入必须带秒 `YYYY-MM-DD HH:mm:ss` | 少秒会静默丢时间存成 00:00:00 |
| select 字段写入未知选项报 800030005 | 先 field-get 对比现有 options,缺的先 field-update 补全量 options |
| 文档仪表盘文本块:真实换行被服务端吞掉;PATCH 会多包一层引号 | 只用创建(删了重建)、内容用单段+分隔符;U+2028/`\r` 能存但 UI 不渲染 |
| jq 对象构造用中文键名报语法错 | 改 ASCII 键或 `--arg` |
| shell 批量写含空格的值 | 勿用 `xargs -L1`(按空白切分毁掉日期时间);用 `while read` + 分批 wait |

## 增量探测(hire 侧)

| 坑 | 处置 |
|---|---|
| **applications list 的 job_id 过滤已全失效**(实测:单数/数组编码均无效,返回全组织混合职位) | 增量姿势:全量拉 org ID(降序,最新在前)→ **application_id 是 19 位雪花、随 create_time 单调**,用「基线最大 ID」阈值筛新建 → 只对这一小批逐条 GET 查 `.data.application.job_id`(注意详情结构是 `data.application.*`) |
| 基线硬编码会在入库后重复命中 | 基线动态读取=已入库最大 application_id(detect_new.py 已内建) |
| 基线投递从现集里消失 | = 源侧撤回/硬删除(详情返回 `data.application={}` 空对象即确认)。**不擅自删表行**,surface 给人定夺 |
| 详情偶发返回空 application(抖动) | stage 与 active_status 同时为 None → 跳过不 patch、不清空旧值(apply_stages.py 已内建) |
| 逐条 GET 有零星 TLS 超时 | 8 并发安全;每项 4~5 次重试;长任务后台跑+进度日志 |

## 数据完整性纪律(踩过血亏的)

- **并发 `>>` 追加大体积行会字节交错损坏**:`>>` 对超过 PIPE_BUF(macOS 512B)的写非原子,近 10KB 的 JSON 行两路并发写会产出非 UTF-8 字节 + 截断。大体积 ndjson 一律**内存聚合最后串行写盘**(detect_new.py 的写法),或每条独立文件再合并;小行(id 列表)可并发。
- **同步进行中不要重跑 build_payloads**:同步后台跑时重建 payload/manifest 会让同步后半程读到不一致状态(done 计数停滞且 fail=0,最难诊断)。纪律:同步启动到 marker 出现前不碰 payload/manifest/快照;重跑同步幂等,诊断不清直接「重拉快照→重算 diff→重跑」。
- **批量写后必须全量回读核验**:与本地基准逐字段 diff,零误差才算完成;失败区分"限流/超时(重试)"与"数据错(修复)"。
- **回读的形态伪差有固定几种,归一别放宽**:实测同一批同步假报 46 条不符,全是形态差异——超链接列回读为 `[url](url)`、日期列回读为 ISO8601 带时区、**单值多选列回读为裸字符串**(见下表 `cell()` 解包)、数值列 int↔float、`application_id` 是 `fb_current` 的字典键不在值里。已固化进 `adapters/feishu/verify_sync.py`;**新形态出现时加归一规则,不要放宽比对**——放宽会把真错一起吃掉。
- **同一人可能有两条投递,`talent_id` 层面查重抓不到**:实测全池 0 条同 `talent_id` 多投,却有 2 人分别在相隔 6 周与 11 个月重投,飞书为其新建了 `talent_id`,系统视作两个人(分数、排名、标签各算一份)。**姓名+院校+专业+年限四元组**才抓得到(注意只按姓名会误报:同名但院校专业全不同的是真重名)。旁证:旧投递的 hire API 返回空对象(疑似已失效)。处置纪律:两行都保留(不擅自删候选人行),在新行加标注指向旧行、注明以新行为准。**标注不能手打进 payload**——评审意见/简历评价是生成字段,下一轮 build_payloads 会原样覆盖掉(实测踩过);且标注里引用的旧行粗排名每轮都变,写死文本一轮就过期。人工确认后登记到配置 `[duplicates].same_person = {新aid = 旧aid}`,由 `build_payloads.py` 每轮重新生成。
- **更新存量记录前先逐字段 diff**:只写变化的字段,非目标字段(尤其人工填写列)零触碰,防意外污染。
- **同步 diff 要区分真实差异与格式伪差**:生成文本的序列化函数改版后,存量表数据还是旧格式,朴素 exact-diff 会误报大片"变化"。先归一化比对定位真实差异,格式统一搭车必要更新一次性回写。
- **名单子串匹配短名必锚定**:两字缩写会撞车(同类 bug 一轮内踩过两次),上线前全量扫命中详情。
- **精读批次 JSON 的 application_id 必须从 dossier 读取,不得手写**:凭印象填 aid 会全部错且不报错,只在 merge 时表现为"缺失 N 人"(实测一次错 4/4)。生成批次前先从 `llm_review/dossiers/*.json` 取 `application_id` 建姓名→aid 映射。
- **实习经历未标注是履历信号的系统性盲区**:候选人把在读期实习的 title 写成正式岗位名(如"AI产品经理"、"暑期培训生"),`INTERN_TITLE_RE` 匹配不到 → 被判频繁短任职(stability 触底),大厂实习还会被 `company_hit` 当全职算进背书。判据:经历时间段与在读学历期重叠。目前靠 LLM 层在评审里点破,规则层未改(改动会牵动 pedigree 口径,须先攒样本)。
- **给 LLM rubric 加负信号必须带量化边界+反例**:无边界的"X 是红旗"会被小模型扩大化误伤;写清阈值与"明确不扣分"的例外。

## 2026-08-10 实测补充

| 坑 | 处置 |
|---|---|
| `pull_records.sh` 不传字段(拉全列)时崩:`FIELDS[@]: unbound variable` | macOS bash 3.2 下 `set -u` + 空数组展开会报错(bash 4.4+ 才修)。写 `"${A[@]+"${A[@]}"}"`。一直传字段所以长期未暴露 |
| 多选字段读回被拼成一坨(`["prompt","agent"]` → `"promptagent"`) | `cell()` 原用空串拼接数组——对富文本分段 `[{"text":..}]` 是对的,对 select 不是。现改:含 dict 走空串拼接(富文本);纯字符串数组 len==1 解包成标量(单选,与 diff 侧字符串比对一致)、len>1 原样返回列表(多选) |
| **创建记录没有幂等键,重跑必产生重复行** | `create_records.sh` 先拿 `fb_current.json` 按 application_id 拦截已存在者;批级重试 5 次仍失败即中止(不留半截状态),失败后先回读查重再决定续跑 |
| 「最高学历」用原始 degree code 取 max 会挑中 `9=其它` | 飞书 hire 的学位枚举把「其它」放在数值最大位(1小学…7硕士 8博士 **9其它**),`max(edus, key=degree)` 会让任何一条"其它"经历(进修/交换/辅修/未标准化)顶掉真实最高学历,**且一次污染学历分+院校+专业三个字段**。按学历分取 max 才对。改动会连带改写"院校"显示,属规则改动须人拍板 |
| `build_payloads` 的 creates 以探测名单为准 → 同轮第二遍留下"两不管"过期行 | 同一轮里若已 `create_records.sh` 过一次(例如后来改了规则要重同步),那些 aid 仍被当 creates:既不重建,又被 updates 分支 `continue` 跳过(实测漏掉 5 条粗排名)。**creates 判据改为「不在飞书快照里」**(`aid not in fb`),探测名单只作候选 |
| **`project_list` 与 `career_list` 是两个独立字段,大量简历的主体在前者** | 实测 462 人中 220 人有项目内容、116 人项目字数超工作经历、**11 人工作经历完全为空**(极端例:careers 0 字 / projects 4464 字)。评分若只扫 careers,这些人的技能/项目/owner 信号全部失真,而 `desc_total`(冗长惩罚)却一直把项目字数算进去——**项目内容不加分只扣分**。修法见 `core/score_engine.build_project_entries`:归一为同构条目喂 skills/project/owner-exec;`company/stability/years` **不要**纳入(项目无任期语义,`name` 字段常被填成公司名,纳入会污染背书) |
| 项目描述较长不代表证据更可靠 | 项目描述可能缺少可核实的时间和职责信息。用 `project_trust` 调整项目条目权重，用 `project_nodate_trust` 对缺少起始时间的项目再折权。项目证据仍可用于技能、项目质量与职责分析。 |
