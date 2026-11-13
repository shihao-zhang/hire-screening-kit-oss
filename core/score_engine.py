#!/usr/bin/env python3
"""通用规则层评分引擎 —— 两层漏斗的第一层(规则粗排 + LLM 精读网圈选)。

机制在代码,策略在配置:所有岗位判断(关键词表、公司/学校分层名单、阈值、
权重、报告文案)都来自 TOML 配置文件,换岗位不改本文件。

用法:
  python3 core/score_engine.py --config <job.toml> --workspace <dir> [--out-dir <dir>]

workspace 输入(由飞书适配层产出):
  applications_raw.ndjson   每行一个投递对象 (/hire/v1/applications/{id})
  talents_raw.ndjson        每行一个人才对象 (/hire/v1/talents/{id})
  out_application_ids.txt   该职位全量投递 id(探测层维护)
  out/withdrawn_ids.txt     源侧已撤回投递 id(可选)
输出(默认 workspace/out/,可用 --out-dir 重定向):
  report.md  report.csv     排序报告 + 全字段明细(含 llm_scope 精读网标记)

评分维度(0-100 加权和,权重在配置):
  education  学历 + 学校加成          company    公司分层 × 经历权重
  major      专业分类                 stability  平均任期 + 近期短任职信号
  years      年限 vs JD 区间          project    量化/详实度 × 经历权重
  skills     关键词加权命中数走插值曲线

关键机制(校准得来,细节见 playbook/):
  - 经历权重 = recency 连续衰减 × ownership 语言信号(防"协助/维护"挂靠大厂)
  - 自陈小节("自我评价"等)截断,不进技能语料——自陈是美化高发区
  - pedigree 乘数:学校三档/大厂背书取 max,双非无大厂触底 penalty_floor
  - 劣化信号族:近期短任职/超长简历/并行任职/运营序列/浅环境/AI断档
  - LLM 精读网 = 主分数网 ∪ 近期放宽 ∪ 强背书召回子句(召回终点真阳性)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置绑定:模块级"常量"全部来自 TOML。函数体与原产线实现保持一致,
# 保证与已校准行为逐字节等价(P1 验收 = 冻结快照 zero-diff)。
# ---------------------------------------------------------------------------


def _int_keys(d: dict) -> dict:
    return {int(k): v for k, v in d.items()}


def bind_config(cfg: dict) -> None:
    global NOW_YEAR, NOW_MONTH, NOW_MS, YEARS_DERIVE
    global LLM_MAIN_TH, LLM_RECENT_DAYS, LLM_RECENT_TH, LLM_BACKING_CO, LLM_BACKING_DEFAULT_ROLE
    global LLM_PEDIGREE_RECALL
    global YEARS_MIN, YEARS_MAX, YEARS_FRESH, YEARS_BELOW, YEARS_DECAY, YEARS_FLOOR, FRESH_GRAD_STABILITY
    global ROLE_A_KEYWORDS, ROLE_B_KEYWORDS, ALL_KEYWORDS
    global ROLE_A_LABEL, ROLE_B_LABEL, ROLE_BOTH_LABEL, ROLE_NONE_LABEL, ROLE_FIT_RATIO
    global TIER1_COMPANIES, TIER2_COMPANIES, ELITE_BONUS_TIERS
    global DEGREE_LABEL, DEGREE_SCORE
    global CS_MAJORS, QUANT_MAJORS, ENG_MAJORS, BIZ_MAJORS, MAJOR_OVERRIDE
    global QUANT_PATTERN, OWNERSHIP_MARKERS, SUPPORT_MARKERS, SELF_ASSESSMENT_HEADERS
    global SCHOOL_T1, SCHOOL_T2, SCHOOL_T3, SCHOOL_ABBR_TIER, SCHOOL_TIER_WEIGHT, SCHOOL_EXCLUDE
    global T3_STRIP_PREFIXES
    global CO_ENDORSE, JOINT_BONUS, COMPANY_TIER_SCORE, SCHOOL_TIER_BONUS
    global PENALTY_FLOOR, INDEP_COLLEGE, PEDIGREE_COMPANIES
    global AI_NATIVE_COMPANIES, TECH_EXTRA, TRAD_COMPANY_RE, AI_STINT_RE
    global TOOL_CLAIM_RE, HARD_DEPTH_RE
    global OWNER_EVIDENCE_RE, EXEC_EVIDENCE_RE, OWNER_TITLE_RE2, EXEC_TITLE_RE2, AI_STALE_MONTHS
    global INTERN_TITLE_RE, SHORT_STINT_MONTHS, SHORT_STINT_WINDOW, INTERN_COUNTS_FOR_PEDIGREE
    global VERBOSE_L1, VERBOSE_L2, VERBOSE_MULT, OPS_TITLE_RE, PM_TITLE_RE
    global RECENCY_FRESH_MONTHS, RECENCY_DECAY_MONTHS, RECENCY_FLOOR, MISSING_DATE_WEIGHT
    global SKILLS_CURVE, WEIGHTS, MULT_SHALLOW_ENV, MULT_AI_STALE
    global PROJECT_TRUST, PROJECT_NODATE_TRUST
    global YSG_MIN_YEARS, YSG_MAX_SKILLS, YSG_DOWNGRADE
    global REPORT_TITLE, REPORT_NOTES, REPORT_YEARS_NOTE, PENDING_STAGES

    run = cfg["run"]
    NOW_YEAR, NOW_MONTH = run["now_year"], run["now_month"]  # 评分基准日期(复现性:随跑分日期漂移是 bug)
    NOW_MS = run["now_ms"]                                   # 精读网 recency 判定基准
    YEARS_DERIVE = bool(cfg.get("job", {}).get("years_derive_from_careers", False))
    # 基准日陈旧告警(METHODOLOGY §3/§7.1):冻结基准日是为复现性,但不对表会让「近 N 天」子句
    # 退化成永久放宽(实测冻 8 周多留 37 人进网没人察觉)。只告警、不改分——真实时钟只用于提示,
    # 不进入任何打分逻辑,复现性不受影响。阈值 [run].stale_warn_days,缺省 14。
    import time as _time
    _stale = run.get("stale_warn_days", 14)
    _age_days = (_time.time() * 1000 - NOW_MS) / 86400000
    if _age_days > _stale:
        print(f"⚠️ 评分基准日 [run].now_ms 已落后当前日期 {_age_days:.0f} 天(阈值 {_stale}):"
              f"近期性子句正在悄悄放宽、任期算术少算在职月数。开轮先对表(走模拟不落盘+血范围)。",
              file=sys.stderr)

    llm = cfg["llm_scope"]
    LLM_MAIN_TH = llm["main_th"]          # 总分>=此值 一律精读(与池规模无关的质量线)
    LLM_RECENT_DAYS = llm["recent_days"]  # 近此天数的投递走放宽门槛
    LLM_RECENT_TH = llm["recent_th"]      # 近期投递总分>=此值即精读(召回近期优质)
    # 强背书召回子句:强背书候选可能被分项埋到主网下方 = 双重漏召回(多例终点真阳性实证)。
    # 只扩召回网、不动规则权重。纯 company 门槛精度低,叠加 role_fit≠默认 收紧
    # (实证:终点锚点 role_fit 全非默认,假阳性几乎全是默认值)。
    LLM_BACKING_CO = llm["backing_co"]
    LLM_BACKING_DEFAULT_ROLE = llm["backing_default_role"]
    # 顶尖背书召回子句:pedigree 命中即进精读,不看总分。缺省空 = 旧行为。
    # 取值是规则层输出的标签(双背书/名校/大厂/强校/无),不含"强校"才叫顶尖;
    # 由实例配置决定把哪几档纳入,机制层不预设。
    LLM_PEDIGREE_RECALL = tuple(llm.get("pedigree_recall") or ())

    job = cfg["job"]
    YEARS_MIN, YEARS_MAX = job["years_min"], job["years_max"]
    # 年限曲线形状(缺省 = 旧行为，向后兼容未升级的岗位配置)
    YEARS_FRESH = job.get("years_fresh", 70)    # 应届/不足下限一年内
    YEARS_BELOW = job.get("years_below", 40)    # 更少
    YEARS_DECAY = job.get("years_decay", 5)     # 超出上限后每年扣分
    YEARS_FLOOR = job.get("years_floor", 70)    # 资深下限
    # 纯实习无全职履历者的 stability 取值:不因"尚无全职任期"扣分(对齐 JD 对应届的立场),
    # 区分度让位给 years/project/skills 维度。
    FRESH_GRAD_STABILITY = job["fresh_grad_stability"]

    roles = cfg["roles"]
    ROLE_A_KEYWORDS = roles["role_a_keywords"]
    ROLE_B_KEYWORDS = roles["role_b_keywords"]
    ALL_KEYWORDS = list(dict.fromkeys(ROLE_A_KEYWORDS + ROLE_B_KEYWORDS))
    ROLE_A_LABEL = roles["role_a_label"]
    ROLE_B_LABEL = roles["role_b_label"]
    ROLE_BOTH_LABEL = roles["role_both_label"]
    ROLE_NONE_LABEL = roles["role_none_label"]
    ROLE_FIT_RATIO = roles["role_fit_ratio"]

    co = cfg["companies"]
    # tier1/tier2/ai_native 与 lower() 后文本比对——名单里含大写字母的条目将永不命中,加载期告警
    for _nm in ("tier1", "tier2", "ai_native"):
        _bad = [x for x in co[_nm] if x != x.lower()]
        if _bad:
            print(f"⚠️ companies.{_nm} 含大写条目将永不命中(匹配对 lower() 文本,请改小写): {_bad}",
                  file=sys.stderr)
    TIER1_COMPANIES = set(co["tier1"])
    TIER2_COMPANIES = set(co["tier2"])
    PEDIGREE_COMPANIES = set(co["pedigree"])      # 严格版名单(背书 bar 高于 tier1 宽名单)
    AI_NATIVE_COMPANIES = co["ai_native"]
    TECH_EXTRA = co["tech_extra"]                 # AI 转型激进的非典型"传统"公司,先于传统正则判为 tech
    # 公司背书**分档**(替代原二值)。缺省 {pedigree:1.0} 即旧行为:命中严格名单=1.0,否则 0。
    CO_ENDORSE = {"pedigree": 1.0, "tier1": 0.0, "tier2": 0.0}
    CO_ENDORSE.update({k: float(v) for k, v in (co.get("endorse_weight") or {}).items()})
    # 双背书叠加:学校与公司都有背书时在 max 之上再加,缺省 0 = 旧的取 max 行为
    JOINT_BONUS = float(co.get("joint_bonus", 0.0))
    # 公司分档位表。缺省 tier1/tier2/other = 100/70/50 即旧行为(无 pedigree 档)
    COMPANY_TIER_SCORE = {"tier1": 100, "tier2": 70, "other": 50}
    COMPANY_TIER_SCORE.update({k: int(v) for k, v in (co.get("tier_score") or {}).items()})

    sch = cfg["schools"]
    # elite_cn / elite_en 已废弃(见 school_bonus 注释):名校加成改由档位判定,
    # 不再维护第二份名单。旧实例配置里残留这两个键无害,core 不再读取。
    ELITE_BONUS_TIERS = set(sch.get("elite_bonus_tiers", [1, 2]))
    # 学历分的名校加成**按档分级**。缺省 = 旧行为(elite_bonus_tiers 内一律 +10)
    SCHOOL_TIER_BONUS = _int_keys(sch.get("tier_bonus") or {t: 10 for t in ELITE_BONUS_TIERS})
    SCHOOL_T1 = set(sch["t1"])
    SCHOOL_T2 = set(sch["t2"])
    SCHOOL_T3 = set(sch["t3"])
    SCHOOL_ABBR_TIER = sch["abbr_tier"]           # 易撞车缩写 → 前缀锚定
    SCHOOL_TIER_WEIGHT = _int_keys(sch["tier_weight"])
    PENALTY_FLOOR = sch["penalty_floor"]          # pedigree_strength=0 时的乘数下限
    INDEP_COLLEGE = set(sch["indep_college"])     # 独立学院/民办不给学校背书(防借名校名沾光)
    # 校名撞车黑名单:与名单里某校**同名/含缩写但实为另一所学校**,整条教育经历不参与判档。
    # 与 indep_college 区别:那是"办学性质"排除,这是"字符串撞车"排除。
    SCHOOL_EXCLUDE = set(sch.get("school_exclude", []))
    # T3 前缀锚定前可剥离的前缀(缺省空 = 旧行为)。T3 只认「以键开头」,于是候选人写成
    # 「<国名><校名>」或「The University of …」时永远匹配不上,且不报错。只放国名与 the,
    # **不放省份/「中国」**——「河南电子科技大学」≠「电子科技大学」的防撞正靠前缀锚定。
    T3_STRIP_PREFIXES = [str(x).lower() for x in (sch.get("t3_strip_prefixes") or [])]

    deg = cfg["degrees"]
    DEGREE_LABEL = _int_keys(deg["label"])
    DEGREE_SCORE = _int_keys(deg["score"])

    maj = cfg["majors"]
    CS_MAJORS = set(maj["cs"])
    QUANT_MAJORS = set(maj["quant"])
    ENG_MAJORS = set(maj["eng"])
    BIZ_MAJORS = set(maj["biz"])
    # 精确专业名 → 档位覆盖(cs/quant/eng/biz/other),优先于词表。见 major_score() 注释。
    MAJOR_OVERRIDE = dict(maj.get("override", {}))

    sig = cfg["signals"]
    QUANT_PATTERN = re.compile(sig["quant_pattern"], re.I)
    OWNERSHIP_MARKERS = sig["ownership_markers"]      # "主导/自建"语言 vs "支持/协调"语言
    SUPPORT_MARKERS = sig["support_markers"]
    SELF_ASSESSMENT_HEADERS = sig["self_assessment_headers"]  # 自陈小节,命中即截断
    TRAD_COMPANY_RE = re.compile(sig["trad_company_pattern"])
    AI_STINT_RE = re.compile(sig["ai_stint_pattern"], re.I)
    # 深度证据分两层——前沿工具名是自陈美化高发区,只能"展示+面试验证",不能豁免罚分;
    # 训练侧硬词难以连贯伪装,才可豁免(有实证案例支撑,细节见私有实例校准史)。
    TOOL_CLAIM_RE = re.compile(sig["tool_claim_pattern"], re.I)
    HARD_DEPTH_RE = re.compile(sig["hard_depth_pattern"], re.I)
    OWNER_EVIDENCE_RE = re.compile(sig["owner_evidence_pattern"], re.I)
    EXEC_EVIDENCE_RE = re.compile(sig["exec_evidence_pattern"], re.I)
    OWNER_TITLE_RE2 = re.compile(sig["owner_title_pattern"], re.I)
    EXEC_TITLE_RE2 = re.compile(sig["exec_title_pattern"], re.I)
    OPS_TITLE_RE = re.compile(sig["ops_title_pattern"])
    PM_TITLE_RE = re.compile(sig["pm_title_pattern"], re.I)
    INTERN_TITLE_RE = re.compile(sig["intern_title_pattern"], re.I)

    th = cfg["thresholds"]
    AI_STALE_MONTHS = th["ai_stale_months"]
    SHORT_STINT_MONTHS = th["short_stint_months"]
    SHORT_STINT_WINDOW = th["short_stint_window"]
    # 缺省 false = 社招口径(实习不算大厂背书);实习岗实例须显式置 true,见 company_hit()
    INTERN_COUNTS_FOR_PEDIGREE = bool(th.get("intern_counts_for_pedigree", False))
    VERBOSE_L1, VERBOSE_L2 = th["verbose_l1"], th["verbose_l2"]   # 描述总字数分位锚点(超长简历=抽象能力弱信号)
    VERBOSE_MULT = _int_keys(th["verbose_mult"])
    # recency 连续衰减(替代二元切分):近1年满权重,线性衰减到 floor;
    # floor 不设 0——不因一次转向就彻底折掉之前积累。
    RECENCY_FRESH_MONTHS = th["recency_fresh_months"]
    RECENCY_DECAY_MONTHS = th["recency_decay_months"]
    RECENCY_FLOOR = th["recency_floor"]
    MISSING_DATE_WEIGHT = th["missing_date_weight"]   # 经历缺日期给中性偏高权重,避免误伤
    # 项目经历可信度折扣(缺省 1.0/1.0 = 与工作经历等同;实测建议下调,见 build_project_entries)
    PROJECT_TRUST = th.get("project_trust", 1.0)
    PROJECT_NODATE_TRUST = th.get("project_nodate_trust", 1.0)
    MULT_SHALLOW_ENV = th["mult_shallow_env"]
    MULT_AI_STALE = th["mult_ai_stale"]
    # 年限达标但技能几乎不命中(最低档)→ 年限分打折:很可能不是目标领域的年限。
    # 技能阈值定得严格,避免误伤"命中不顶格但确实对口"的候选人。
    ysg = th["years_skills_guard"]
    YSG_MIN_YEARS, YSG_MAX_SKILLS, YSG_DOWNGRADE = ysg["min_years_score"], ysg["max_skills_score"], ysg["downgrade_to"]

    # 技能分:对"加权命中数 whits"分段线性插值,控制点用全池分位数锚定,
    # 低中高全域都有梯度(不在底部塌缩,也不在顶部饱和)。whits 已内含 recency+ownership 折扣。
    SKILLS_CURVE = [tuple(p) for p in cfg["skills_curve"]]
    WEIGHTS = cfg["weights"]

    rep = cfg["report"]
    REPORT_TITLE = rep["title"]
    REPORT_NOTES = rep["notes"]
    REPORT_YEARS_NOTE = rep["years_note"]
    PENDING_STAGES = set(rep["pending_stages"])


# ---------------------------------------------------------------------------
# 以下函数体与原产线实现一致(仅注释脱敏、常量改由配置绑定)。
# ---------------------------------------------------------------------------


_SCHOOL_SEP_RE = re.compile(r"[\s·・．.\-–—,，、]")
T3_STRIP_PREFIXES: list = []


def _norm_school(s: str) -> str:
    """校名归一:去空白与常见分隔符。候选人手写的间隔差异不该决定档位
    （实测「约翰 霍普金斯大学」因一个空格漏掉 t1 名单里的「约翰霍普金斯」）。"""
    return _SCHOOL_SEP_RE.sub("", s)


def _tier_hit(s: str) -> tuple[int, int]:
    """单个校名 → (档位, 命中键长)。未命中 (0, 0)。

    三条匹配纪律，均由实测误判倒推：
    1. **ASCII 键要词边界**——子串匹配让 3 字母键随机撞名:`mit` 命中
       「Gold**smit**hs, University of London」,把该校误抬到 T1（学历分 95）。
    2. **CJK 键比归一串**——吸收空格/间隔号书写差异（见 _norm_school）。
    3. **最长键优先(跨档)**——更具体的校名压过更泛的:t1 的「哥伦比亚」
       抢在 t2 的「英属哥伦比亚」前面,把 UBC 误判成 T1。
    T3 保持**前缀锚定**（v5-spec3 校准所得:「河南电子科技大学」≠「电子科技大学」）。
    """
    sl = s.lower()
    sn = _norm_school(sl)
    # T3 前缀锚定的候选串:原串 + 剥掉配置前缀后的串(缺省无前缀 = 只有原串,即旧行为)
    sl_vars = [sl] + [sl[len(x):].lstrip() for x in T3_STRIP_PREFIXES
                      if sl.startswith(x) and len(sl) > len(x)]
    sn_vars = [_norm_school(v) for v in sl_vars]
    best_tier, best_len = 0, 0
    for tier, keys, prefix_only in ((1, SCHOOL_T1, False), (2, SCHOOL_T2, False),
                                    (3, SCHOOL_T3, True)):
        for k in keys:
            kl = k.lower()
            if kl.isascii():
                if prefix_only:
                    if not any(v.startswith(kl) for v in sl_vars):
                        continue
                elif not re.search(rf"(?<![a-z0-9]){re.escape(kl)}(?![a-z0-9])", sl):
                    continue
            else:
                kn = _norm_school(kl)
                if prefix_only:
                    if not any(v.startswith(kn) for v in sn_vars):
                        continue
                elif kn not in sn:
                    continue
            n = len(_norm_school(kl))
            if n > best_len or (n == best_len and tier < (best_tier or 9)):
                best_tier, best_len = tier, n
    return best_tier, best_len


def school_tier_one(s: str) -> int:
    """单个校名的档位 (1 顶尖 / 2 强校 / 3 普通985)，无则 0。"""
    if not s:
        return 0
    sn = _norm_school(s)
    if any(m in s for m in INDEP_COLLEGE):                   # 独立学院不给背书
        return 0
    if any(_norm_school(m) in sn for m in SCHOOL_EXCLUDE):   # 校名撞车黑名单
        return 0
    tier, hit_len = _tier_hit(s)
    for k, tr in SCHOOL_ABBR_TIER.items():  # 缩写前缀锚定
        # 缩写只在**没被更长的完整校名键命中**时才作数(跨档最长优先同样适用于缩写)。
        # 注意这挡不住「西交利物浦大学」——它没有任何完整校名键命中,hit_len=0,
        # 仍会被「西交」抬到 T2;那类同名撞车只能走 SCHOOL_EXCLUDE 黑名单。
        if s.startswith(k) and len(k) > hit_len:
            tier = tr if tier == 0 else min(tier, tr)
    return tier


def school_tier(t: dict) -> int:
    """返回全部教育经历中的最高学校档位 (1 顶尖 / 2 强校 / 3 普通985)，无则 0。"""
    best = 0  # 0=无
    for e in t.get("education_list") or []:
        tier = school_tier_one(e.get("school") or "")
        if tier and (best == 0 or tier < best):
            best = tier
    return best


def is_intern(c: dict) -> bool:
    """单段职业经历是否为实习。

    **优先用官方结构化字段 career_type（1=实习 / 2=全职），title 正则只作缺失时的回退。**
    起因:靠 title 正则判实习是系统性漏判——候选人普遍把实习段的 title 写成
    「产品经理」「研发工程师」而不带「实习」字样。全池 1564 段实测:
      官方标实习 391 段,正则只认出 219 段（56%）→ **漏 172 段、涉 81 人**;
      反向误判仅 2 段（「实习律师」「多领域实习」——title 有"实习"但officially全职）。
    漏判一次污染三处:任期均值（把 4 个月实习当跳槽）、近期短任职计数、大厂背书口径。

    枚举可信度已验证(不是盲信字段):type=1 中位 4 月、81% ≤6 月、仅 2% >24 月;
    type=2 中位 20 月、39% >24 月;且正则命中的 221 段里 219 段官方也是 type=1。
    """
    ct = c.get("career_type")
    if ct in (1, 2):
        return ct == 1
    return bool(INTERN_TITLE_RE.search(c.get("title") or ""))


def company_hit(t: dict) -> bool:
    """任一职业经历命中大厂名单即算（保持二值）。

    实习是否计入 pedigree 由 [thresholds] intern_counts_for_pedigree 定（缺省 false）:
    - **社招岗 false**:实习大厂 ≠ 全职大厂
      （实测案例:强校+大厂实习被旧口径误判"双背书"抬高乘数）。
    - **实习岗 true**:全池 96% 的经历段都是实习,排除等于把该维度整体清零——
      "实习去过哪"正是该岗位的有效信号。
    ⚠️ 该开关必须配置,不能由 core 猜:is_intern() 换用官方 career_type 后实习识别率
    从 56% 升到 ~100%,靠正则漏判"意外保留"的实习背书会一次性消失。
    """
    for c in t.get("career_list") or []:
        if is_intern(c) and not INTERN_COUNTS_FOR_PEDIGREE:
            continue
        co = c.get("company") or ""
        col = co.lower()
        if any(k in col or k in co for k in PEDIGREE_COMPANIES):
            return True
    return False


def ai_env(t: dict) -> tuple[str, str]:
    """最近一段 AI 相关经历的公司环境: ai_native / tech / startup_other / traditional / none。"""
    careers = sorted(t.get("career_list") or [],
                     key=lambda w: w.get("start_time") or "", reverse=True)
    for w in careers:
        text = (w.get("title") or "") + " " + (w.get("desc") or "")
        if not AI_STINT_RE.search(text):
            continue
        co = w.get("company") or ""
        col = co.lower()
        if any(k in col for k in AI_NATIVE_COMPANIES):
            return "ai_native", co
        if any(k in col for k in TIER1_COMPANIES) or any(k in col for k in TIER2_COMPANIES) \
                or any(k in col or k in co for k in PEDIGREE_COMPANIES) \
                or any(k in col or k in co for k in TECH_EXTRA):
            return "tech", co
        if TRAD_COMPANY_RE.search(co):
            return "traditional", co
        return "startup_other", co
    return "none", ""


def _re_hits(pattern: re.Pattern, entries: list[dict]) -> list[str]:
    hits = []
    for e in entries:
        for m in pattern.findall(e["title"] + " " + e["desc_clean"]):
            k = m.lower().strip()
            if k and k not in hits:
                hits.append(k)
    return hits


def owner_exec_profile(t: dict) -> tuple[int, int, bool]:
    """近期 AI 相关经历(至多2段)中 owner 证据词 vs 执行词计数, 及执行主导判定。
    只做标注+面试验证提示,不罚分——自陈 owner 词会骗过正则,语义判断交给 LLM 层。

    同时检查 project_list 和 career_list，避免漏掉只写在项目描述里的职责证据。
    """
    def _norm(items, title_key):
        return [{"title": x.get(title_key) or "", "desc": x.get("desc") or "",
                 "start_time": x.get("start_time")} for x in (items or [])]

    def _pick(items):
        """AI 相关的取最近 2 段;无 AI 相关则取最近 1 段(与原逻辑一致)。"""
        s = sorted(items, key=lambda w: w.get("start_time") or "", reverse=True)
        ai = [w for w in s if AI_STINT_RE.search(w["title"] + " " + w["desc"])]
        return ai[:2] if ai else s[:1]

    scope = (_pick(_norm(t.get("career_list"), "title"))
             + _pick(_norm(t.get("project_list"), "role")))
    ow = ex = owt = ext = 0
    for w in scope:
        txt = w.get("desc") or ""
        ti = w.get("title") or ""
        ow += len(OWNER_EVIDENCE_RE.findall(txt))
        ex += len(EXEC_EVIDENCE_RE.findall(txt))
        if OWNER_TITLE_RE2.search(ti):
            owt += 1
        if EXEC_TITLE_RE2.search(ti):
            ext += 1
    exec_dom = (ex >= 3 and ex > ow * 2) or (ext >= 1 and owt == 0 and ow <= 1)
    return ow, ex, exec_dom


def ai_gap_months(t: dict):
    """最近一段 AI 相关经历结束距今的月数; 在职AI经历=0; 无AI经历/无日期=None。"""
    careers = sorted(t.get("career_list") or [],
                     key=lambda w: w.get("start_time") or "", reverse=True)
    for w in careers:
        if not AI_STINT_RE.search((w.get("title") or "") + " " + (w.get("desc") or "")):
            continue
        e = parse_yyyymm(w.get("end_time"))
        if e is None:
            return 0  # 在职或无结束日期,按无断档
        return max(0, months_between(e, (NOW_YEAR, NOW_MONTH)))
    return None


def career_flags(career_list: list[dict]) -> tuple[int, bool, bool, bool]:
    """返回 (近期短任职段数, 并行任职, 长空窗≥18月, 运营执行序列)。
    短任职口径:已离职、非实习兼职、任期≤阈值、结束在近窗口内;均值会稀释近期连续闪离,故单列。
    并行任职(两段不同公司重叠≥12月)=年限重复计算嫌疑,只标注不扣分。
    运营序列也可能同时在招,只标注提示核实,不扣分。"""
    now = (NOW_YEAR, NOW_MONTH)
    stints = []
    for w in career_list or []:
        s = parse_yyyymm(w.get("start_time"))
        if not s:
            continue
        title = w.get("title") or ""
        stints.append({"s": s, "e": parse_yyyymm(w.get("end_time")),
                       "title": title, "co": w.get("company") or "",
                       "intern": is_intern(w)})
    stints.sort(key=lambda x: x["s"], reverse=True)

    n_short = 0
    for st in stints:
        if st["intern"] or st["e"] is None:  # 在职中的当前工作不算短任职
            continue
        dur = months_between(st["s"], st["e"])
        ago = months_between(st["e"], now)
        if 0 <= dur <= SHORT_STINT_MONTHS and ago <= SHORT_STINT_WINDOW:
            n_short += 1

    overlap = False
    for i in range(len(stints)):
        for j in range(i + 1, len(stints)):
            a, b = stints[i], stints[j]
            if not a["co"] or a["co"] == b["co"]:
                continue
            lo = max(a["s"], b["s"])
            hi = min(a["e"] or now, b["e"] or now)
            if months_between(lo, hi) >= 12:
                overlap = True

    gap = False
    chron = sorted((st for st in stints if not st["intern"]), key=lambda x: x["s"])
    for a, b in zip(chron, chron[1:]):
        if a["e"] and months_between(a["e"], b["s"]) >= 18:
            gap = True

    ops = False
    for st in [st for st in stints if not st["intern"]][:2]:
        if OPS_TITLE_RE.search(st["title"]) and not PM_TITLE_RE.search(st["title"]):
            ops = True
    return n_short, overlap, gap, ops


def recency_weight(months_ago) -> float:
    if months_ago is None:
        return MISSING_DATE_WEIGHT
    if months_ago <= RECENCY_FRESH_MONTHS:
        return 1.0
    decayed = 1.0 - (months_ago - RECENCY_FRESH_MONTHS) / RECENCY_DECAY_MONTHS * (1.0 - RECENCY_FLOOR)
    return max(RECENCY_FLOOR, decayed)


def ownership_signal(text: str) -> float:
    """0~1，越接近1说明"主导/自建"语言越强，越接近0说明"协助/支持"语言越强。无信号时取中性 0.5。
    用"是否出现"(presence)而非出现次数(count)统计——防止简历靠堆砌"主导/从0到1"刷高
    （这类总结是履历美化高发区，不能被次数 gaming）。"""
    if not text:
        return 0.5
    own = sum(1 for m in OWNERSHIP_MARKERS if m in text)
    sup = sum(1 for m in SUPPORT_MARKERS if m in text)
    if own + sup == 0:
        return 0.5
    return own / (own + sup)


def strip_self_assessment(desc: str) -> str:
    """截掉"技术/软实力"、"自我评价"等自陈清单小节，只保留真实经历叙述部分。"""
    if not desc:
        return ""
    cut = len(desc)
    for h in SELF_ASSESSMENT_HEADERS:
        idx = desc.find(h)
        if idx != -1:
            cut = min(cut, idx)
    return desc[:cut]


def build_project_entries(project_list: list[dict]) -> list[dict]:
    """把 project_list 归一成与 career entries 同构的条目,供 skills / project / owner-exec 消费。

    ⚠️ 为什么必须单独做这件事(实测踩过的系统性缺陷):
    飞书 hire 的 `project_list` 与 `career_list` 是两个独立字段,大量候选人把简历主体写在
    项目经历里(实测 462 人中 220 人有项目内容、116 人项目字数超过工作经历、11 人工作经历
    完全为空)。原实现只扫 career_list,导致这些人的技能/项目/owner 信号全部失真——
    极端例子:工作经历为空、项目写了 4464 字者,项目分 0、技能分触底,排名 399。
    更糟的是 desc_total(简历冗长惩罚)**一直把 project 字数算进去**——项目内容不加分只扣分。

    字段差异:项目条目用 `role` 而非 `title`、用 `name` 存项目名(实测常被填成公司名,
    故**不映射到 company**,防止污染 company_score 与 pedigree 背书)。
    """
    def sort_key(p):
        ym = parse_yyyymm(p.get("start_time"))
        return ym if ym else (0, 0)

    out = []
    for p in sorted(project_list or [], key=sort_key, reverse=True):
        clean_desc = strip_self_assessment(p.get("desc", "") or "")
        ym = parse_yyyymm(p.get("start_time"))
        m_ago = ((NOW_YEAR - ym[0]) * 12 + (NOW_MONTH - ym[1])) if ym else None
        # 可信度折扣:项目描述是自选的、无雇主与绩效约束、难以核实,与 #2「自陈是美化高发区」
        # 一致,因此对项目经历使用下述折扣。
        #   项目条目统一乘 PROJECT_TRUST;缺起止时间者再乘 PROJECT_NODATE_TRUST(无法核实)。
        trust = PROJECT_TRUST * (1.0 if ym else PROJECT_NODATE_TRUST)
        weight = recency_weight(m_ago) * (0.6 + 0.4 * ownership_signal(clean_desc)) * trust
        out.append({
            "company": "",                       # 刻意留空:项目 name 不可信作公司
            "title": p.get("role", "") or "",
            "desc_clean": clean_desc,
            "weight": weight,
            "months_ago": m_ago,
        })
    return out


def build_weighted_entries(career_list: list[dict]) -> list[dict]:
    """每段经历 -> {company, title, desc_clean, weight, months_ago}。
    weight 融合"经历新鲜度"（recency，按 start_time 距今月数连续衰减）与
    "拥有权语言强弱"（ownership）两个信号：
    - LLM/Agent 应用层范式变化快，近期经验比久远经验更有参考价值；
    - "协助/维护"性质的经历即便挂靠大厂/听起来沾边，也不该等同于真实主导产出。
    关键修复：显式按 start_time 降序排列，不再假设 career_list[0] 就是最近一份工作
    （实测约 4% 候选人的原始数组顺序并非倒序，含 Top 候选人）。
    """
    def sort_key(w):
        ym = parse_yyyymm(w.get("start_time"))
        return ym if ym else (0, 0)

    ordered = sorted(career_list, key=sort_key, reverse=True)
    out = []
    for w in ordered:
        raw_desc = w.get("desc", "") or ""
        clean_desc = strip_self_assessment(raw_desc)
        ym = parse_yyyymm(w.get("start_time"))
        m_ago = ((NOW_YEAR - ym[0]) * 12 + (NOW_MONTH - ym[1])) if ym else None
        rw = recency_weight(m_ago)
        sig = ownership_signal(clean_desc)
        # ownership 影响幅度收窄到 0.6~1.0：既缓解对"美化词"的过度敏感，
        # 也减轻 recency×ownership 在 company/skills/project 三个维度上的叠加惩罚。
        weight = rw * (0.6 + 0.4 * sig)
        out.append({
            "company": w.get("company", "") or "",
            "title": w.get("title", "") or "",
            "desc_clean": clean_desc,
            "weight": weight,
            "months_ago": m_ago,
        })
    return out


def blend(base_score: float, weight: float, floor: float) -> float:
    """weight=1 时取满 base_score；weight=0 时退回 floor（中性/默认水平）。"""
    return floor + (base_score - floor) * weight


def load_ndjson(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line == "null" or line == "{}":
            continue
        out.append(json.loads(line))
    return out


def parse_yyyymm(s: str | None) -> tuple[int, int] | None:
    if not s or s in ("-", "至今", "now", "present"):
        return None
    m = re.match(r"(\d{4})-(\d{1,2})", s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def months_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    return (end[0] - start[0]) * 12 + (end[1] - start[1])


def degree_score(degree_code) -> tuple[int, str]:
    try:
        code = int(degree_code)
    except (TypeError, ValueError):
        return 30, "unknown"
    return DEGREE_SCORE.get(code, 30), DEGREE_LABEL.get(code, "unknown")


def school_bonus(school: str) -> int:
    """学历分的名校加成:**由 school_tier_one 判定**，档位口径见 [schools] elite_bonus_tiers。

    原实现读一份独立的 elite_cn/elite_en 名单,与 t1/t2/t3 各自漂移且无一致性机制。
    全池实测该名单已经失效:
      **58 所 T1/T2 学校拿不到加成**——名单里只有「北大」「上交」这类简称,
        候选人写全称(北京大学 / 上海交通大学 / 浙江大学 / 中国科学技术大学 /
        加州大学伯克利分校 / 帝国理工学院 / 芝加哥大学 …)一律漏;
      **11 所 0 档学校反而拿到**——「北大」子串撞 河北大学/中北大学/西北大学、
        「南大」撞 云南大学/海南大学、「天大」撞 沈阳航空航天大学、
        「西交」撞 西交利物浦、`mit` 撞 Goldsmiths;
        其中 河北大学工商学院、电子科技大学中山学院 还是已被拉黑的独立学院。
    两份必须一致却无机制保证一致的名单 = 缺陷源头,故合并为一份。
    """
    return SCHOOL_TIER_BONUS.get(school_tier_one(school), 0)


MAJOR_TIER_SCORE = {"cs": 100, "quant": 90, "eng": 70, "biz": 55, "other": 35}


def major_score(major: str) -> int:
    """专业 → 分档。**精确名覆盖优先于词表子串匹配**。

    覆盖表存在的理由:词表里「工程」「电子」这类短通用键做子串匹配会系统性误伤——
    实测「工程管理」「管理科学与工程」「工业设计工程」「服装设计与工程」「金融工程学」
    「物流工程与管理」「工业工程与物流管理」全被「工程」判成工科 70 分,
    「电子商务」「交互设计与电子艺术」被「电子」判成工科,
    「电子信息工程」被 cs 键「信息工程」判成 100 分。
    与 v6.20 的 `mit` 撞 Goldsmiths、「北大」撞河北大学**同源**:短键 + 子串 = 撞名。
    专业名是自由文本、长尾无穷,穷举词表不可行,故用精确覆盖表处理例外。
    覆盖表由 LLM 归类产出候选、人工核定后落配置(构建期产物,引擎仍是确定性的)。
    """
    if not major:
        return 30
    ov = MAJOR_OVERRIDE.get(major.strip())
    if ov is not None:
        return MAJOR_TIER_SCORE.get(ov, 35)
    m = major.lower()
    if any(k in m or k in major for k in CS_MAJORS):
        return 100
    if any(k in m or k in major for k in QUANT_MAJORS):
        return 90
    if any(k in m or k in major for k in ENG_MAJORS):
        return 70
    if any(k in m or k in major for k in BIZ_MAJORS):
        return 55
    return 35


def years_score(years) -> int:
    """年限打分。曲线形状全部由 [job] 配置驱动——「要资深」还是「要年轻」是岗位策略，不是机制。

    YEARS_FRESH / YEARS_DECAY / YEARS_FLOOR 缺省值复刻旧行为(70 / 5 / 70)，
    改成 90 / 8 / 40 即得到「年轻优先」曲线(应届抬高、资深递减更陡)。
    """
    if years is None:
        return 30
    try:
        years = float(years)
    except (TypeError, ValueError):
        return 30
    if YEARS_MIN <= years <= YEARS_MAX:
        return 100
    if years > YEARS_MAX:  # 超出目标区间后按 DECAY 每年递减，不低于 FLOOR
        return max(YEARS_FLOOR, 100 - int((years - YEARS_MAX) * YEARS_DECAY))
    if YEARS_MIN - years <= 1:
        return YEARS_FRESH  # 应届/接近应届
    return YEARS_BELOW


def company_endorse_weight(t: dict) -> float:
    """公司背书**分档**——原实现是二值(命中严格名单就 1.0,与 T1 名校并列)。

    为什么要分档:二值让「任何一段大厂经历」= 清华,一旦拉大档间差距,名校反被大厂压过
    (实测:纯拉带宽时 NUS 名校、已面试通过的候选人从 rank73 掉到 135)。

    ⚠️ 口径必须与 company_hit 一致(同一份严格名单 + 同一条实习规则),否则
    strength 与 pedigree 标签会各说各话。本函数只是在其上**向下延伸出 tier1/tier2 两档**。
    缺省配置下 tier1/tier2 权重为 0,退化成与 company_hit 完全等价的二值。
    """
    best = 0.0
    for c in t.get("career_list") or []:
        if is_intern(c) and not INTERN_COUNTS_FOR_PEDIGREE:
            continue
        co = c.get("company") or ""
        col = co.lower()
        if any(k in col or k in co for k in PEDIGREE_COMPANIES):
            best = max(best, CO_ENDORSE["pedigree"])
        elif any(k in col for k in TIER1_COMPANIES):
            best = max(best, CO_ENDORSE["tier1"])
        elif any(k in col for k in TIER2_COMPANIES):
            best = max(best, CO_ENDORSE["tier2"])
    return best


def company_score(entries: list[dict]) -> tuple[int, str]:
    """按公司牌子打分，但用 entry weight（近期+拥有权语言）折算——
    挂靠大厂但语言以"协助/维护"为主、或是很久远的旧工作，不给满分tier1待遇。"""
    if not entries:
        return 20, ""
    cur = entries[0]["company"] if entries else ""
    best = 0.0
    for e in entries:
        co = e["company"].lower()
        # 档位表由配置给;含 "pedigree" 键时在 tier1 之上再分出一档顶尖大厂
        if "pedigree" in COMPANY_TIER_SCORE and any(c in co for c in PEDIGREE_COMPANIES):
            tier = COMPANY_TIER_SCORE["pedigree"]
        elif any(c in co for c in TIER1_COMPANIES):
            tier = COMPANY_TIER_SCORE["tier1"]
        elif any(c in co for c in TIER2_COMPANIES):
            tier = COMPANY_TIER_SCORE["tier2"]
        else:
            tier = COMPANY_TIER_SCORE["other"]
        best = max(best, blend(tier, e["weight"], floor=50))
    return round(best), cur


def stability_score(career_list: list[dict], n_recent_short: int = 0) -> int:
    """平均任期之外叠加"近期短任职"信号——均值会稀释掉最近连续闪离
    （实测案例:avg≈1.65年 得60分，但最近两段各3-4个月已离职才是真信号）。

    两处关键修复(校准史 v6.6)：
    1. **实习不计入均值**——实习短是定义使然，不是跳槽信号。否则纯实习应届被
       钉死在最低档（=等同"已证明爱跳槽"），该维度对他们零区分度。
    2. **改用整月比较**——按 months/12 转 float 再比较，浮点累加误差会让恰好
       卡在整数边界的人静默翻档；且在职时长随跑分日期变化——是复现性 bug。
    """
    if not career_list:
        return 30
    if n_recent_short >= 2:
        return 10
    months_list = []
    for w in career_list:
        if is_intern(w):
            continue                      # 实习段不计入任期均值
        s = parse_yyyymm(w.get("start_time"))
        e = parse_yyyymm(w.get("end_time")) or (NOW_YEAR, NOW_MONTH)
        if not s:
            continue
        months = months_between(s, e)
        if months > 0:
            months_list.append(months)
    if not months_list:
        # 全实习/无可用全职段：无全职履历，稳定性不可知 → 配置口径取值。
        return FRESH_GRAD_STABILITY
    # 纯整数比较（两边乘 len，避开除法），彻底消除浮点边界翻档。
    # avg_months >= K  ⟺  sum >= K*len   —— 全整数，无舍入。
    tot_m, n_m = sum(months_list), len(months_list)
    if tot_m >= 36 * n_m:
        base = 100
    elif tot_m >= 24 * n_m:
        base = 80
    elif tot_m >= 12 * n_m:
        base = 60
    else:
        base = 30
    if n_recent_short == 1:
        return min(base, 45)
    return base


def project_quality_score(entries: list[dict]) -> int:
    """量化结果+详实描述仍是基础信号，但按 entry weight 折算——
    "培训内容/自我评价"式的术语堆砌已被 strip_self_assessment 剔除，
    协助型/久远的经历即便描述详细也不再等同于真实主导产出。"""
    if not entries:
        return 0
    best = 0.0
    for e in entries:
        text = e["desc_clean"]
        if not text.strip():
            continue
        has_quant = bool(QUANT_PATTERN.search(text))
        long_desc = len(text) > 300
        if has_quant and long_desc:
            base = 100
        elif has_quant or long_desc:
            base = 65
        else:
            base = 35
        best = max(best, blend(base, e["weight"], floor=35))
    return round(best)


def _interp(x: float, curve: list[tuple[float, float]]) -> int:
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return curve[-1][1]


def skills_score(entries: list[dict], extra_text: str = "") -> tuple[int, list[str], list[str], list[str]]:
    """按"加权命中数 whits"连续分档。近期/拥有权语言强的经历里命中的关键词贡献满额，
    久远/"协助"性质经历或自陈清单（已被 strip_self_assessment 剔除）里的命中贡献被折减。
    extra_text（专业/学校等）权重固定 1.0。"""
    hit_weight: dict[str, float] = {}
    for e in entries:
        text = (e["title"] + " " + e["desc_clean"]).lower()
        for kw in ALL_KEYWORDS:
            if kw.lower() in text:
                hit_weight[kw] = max(hit_weight.get(kw, 0.0), e["weight"])
    if extra_text:
        low_extra = extra_text.lower()
        for kw in ALL_KEYWORDS:
            if kw.lower() in low_extra:
                hit_weight[kw] = max(hit_weight.get(kw, 0.0), 1.0)

    hits_all = [kw for kw in ALL_KEYWORDS if kw in hit_weight]
    hits_a = [k for k in ROLE_A_KEYWORDS if k in hit_weight]
    hits_b = [k for k in ROLE_B_KEYWORDS if k in hit_weight]

    whits = sum(hit_weight.values())
    score = _interp(whits, SKILLS_CURVE)
    return score, hits_all, hits_a, hits_b


def role_fit(hits_a: list[str], hits_b: list[str]) -> str:
    if not hits_a and not hits_b:
        return ROLE_NONE_LABEL
    if len(hits_a) > len(hits_b) * ROLE_FIT_RATIO:
        return ROLE_A_LABEL
    if len(hits_b) > len(hits_a) * ROLE_FIT_RATIO:
        return ROLE_B_LABEL
    return ROLE_BOTH_LABEL


def years_or_derive(t: dict, basic: dict):
    """工作年限:优先取源侧 basic_info.experience_years;**缺失时可从 career_list 推算**。

    为什么:源侧年限字段缺失时 years_score 一律给 30 分兜底,哪怕工作经历日期完整。实测某实例
    19.4% 的人缺这个字段,其中有 14 年大厂经历的终点真阳性被打成 30 分;harness §2c 显示
    5 位被埋的终点真阳性里 4 位正是因此被拖分。
    推算口径:只算非实习段,按月合并重叠(并行任职不重复计年),在职段截到评分基准月。
    **完全没有非实习经历时仍返回 None(保持缺失兜底)**——把"没数据"解释成"应届"会稀释区分度,
    实测效应量 0.535 vs 0.476,故不做这层推断。
    开关 [job].years_derive_from_careers,缺省 false = 逐字节复现旧行为。
    """
    v = basic.get("experience_years")
    if v not in (None, "") or not YEARS_DERIVE:
        return v
    now = NOW_YEAR * 12 + NOW_MONTH
    segs = []
    for c in t.get("career_list") or []:
        if is_intern(c):
            continue
        st, en = str(c.get("start_time") or "")[:7], str(c.get("end_time") or "")[:7]
        try:
            a = int(st[:4]) * 12 + int(st[5:7])
        except ValueError:
            continue
        try:
            b = min(int(en[:4]) * 12 + int(en[5:7]), now)
        except ValueError:
            b = now
        if b >= a:
            segs.append((a, b))
    segs.sort()
    merged = []
    for a, b in segs:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    months = sum(b - a for a, b in merged)
    return round(months / 12, 1) if months > 0 else None


def flatten_talent(t: dict) -> dict:
    basic = t.get("basic_info", {}) or {}
    edus = t.get("education_list", []) or []
    careers = t.get("career_list", []) or []

    # 最高学历条目（education_list 顺序不保证按学位高低）。
    # ⚠️ 必须按「学历分」取 max，不能按原始 degree code——飞书 hire 的枚举把「其它」放在
    # 数值最大位（1小学…7硕士 8博士 **9其它**），按 code 取 max 会让任何一条"其它"经历
    # （进修/交换/辅修/未标准化学位）顶掉真实最高学历，且一次污染 学历分+院校+专业 三个字段。
    # 无法归类的附加教育经历不应覆盖评分更高的学位。
    def _edu_rank(e):
        try:
            return DEGREE_SCORE.get(int(e.get("degree")), 30)
        except (TypeError, ValueError):
            return 30
    top_edu = max(edus, key=_edu_rank, default={})
    degree_code = top_edu.get("degree")
    school = top_edu.get("school", "") or ""
    major = top_edu.get("field_of_study", "") or ""

    companies = [w.get("company", "") for w in careers if w.get("company")]
    entries = build_weighted_entries(careers)
    proj_entries = build_project_entries(t.get("project_list") or [])
    # 两套条目刻意分开:
    #   entries      → company_score(公司牌子/背书,项目条目无公司语义,纳入会污染)
    #   all_entries  → skills / project_quality(内容型信号,必须看全简历)
    all_entries = entries + proj_entries
    desc_total = (sum(len(w.get("desc") or "") for w in careers)
                  + sum(len(p.get("desc") or "") for p in (t.get("project_list") or [])))

    return {
        "desc_total": desc_total,
        "proj_entries": proj_entries,
        "all_entries": all_entries,
        "talent_id": t.get("id"),
        "name": basic.get("name", "(no name)"),
        "degree_code": degree_code,
        "school": school,
        "major": major,
        "companies": companies,
        "years": years_or_derive(t, basic),
        "career_list": careers,
        "entries": entries,
    }


def run(workspace: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    all_ids_path = workspace / "out_application_ids.txt"
    all_application_ids = [l.strip() for l in all_ids_path.read_text().splitlines() if l.strip()]

    # 源侧已撤回/删除的投递:保留在表中但标记,不参与新召回,供人工识别(不删表行)。
    withdrawn_path = workspace / "out" / "withdrawn_ids.txt"
    withdrawn_ids = set()
    if withdrawn_path.exists():
        withdrawn_ids = {l.strip() for l in withdrawn_path.read_text().splitlines() if l.strip()}

    applications = load_ndjson(workspace / "applications_raw.ndjson")
    talents = load_ndjson(workspace / "talents_raw.ndjson")
    talent_by_id = {t.get("id"): t for t in talents}

    parsed_ids = {a.get("id") for a in applications}
    empty_from_hire = [i for i in all_application_ids if i not in parsed_ids]

    skipped = []
    rows = []
    for app in applications:
        talent_id = app.get("talent_id")
        talent = talent_by_id.get(talent_id)
        if not talent:
            skipped.append(app.get("id", "?"))
            continue
        flat = flatten_talent(talent)

        deg_s, deg_label = degree_score(flat["degree_code"])
        deg_s = min(deg_s + school_bonus(flat["school"]), 110)
        maj_s = major_score(flat["major"])
        yrs_s = years_score(flat["years"])
        co_s, cur_co = company_score(flat["entries"])
        n_short, overlap_flag, gap_flag, ops_flag = career_flags(flat["career_list"])
        sta_s = stability_score(flat["career_list"], n_short)
        proj_s = project_quality_score(flat["all_entries"])   # 含 project_list(见 build_project_entries)
        extra_text = f"{flat['major']} {flat['school']}"
        sk_s, hits_all, hits_a, hits_b = skills_score(flat["all_entries"], extra_text)

        years_skills_flag = False
        if yrs_s >= YSG_MIN_YEARS and sk_s <= YSG_MAX_SKILLS:
            yrs_s = YSG_DOWNGRADE
            years_skills_flag = True

        total = round(
            deg_s * WEIGHTS["education"] + maj_s * WEIGHTS["major"] + yrs_s * WEIGHTS["years"]
            + sk_s * WEIGHTS["skills"] + co_s * WEIGHTS["company"]
            + sta_s * WEIGHTS["stability"] + proj_s * WEIGHTS["project"],
            1,
        )

        # 背书加权——学校分三档 + 大厂二值，取 max 作 strength，
        # 乘数 = floor + (1-floor)*strength。
        st = school_tier(talent)
        co = company_hit(talent)
        school_w = SCHOOL_TIER_WEIGHT.get(st, 0.0)
        # ⚠️ 标签与权重必须解耦:原实现用 `school_w >= 0.9` 判「名校」,把 tier_weight
        #    同时当成(a)乘数强度与(b)名校/强校标签阈值。后果是——为拉大档间差距把 t2 调到
        #    0.9 以下时,**全部 T2 学校会静默从「名校」降级为「强校」**,而 pedigree 是
        #    LLM rubric 的输入(无→封顶待定、强校→不给高分),等于改一个分数参数顺带改了
        #    精读层的判定口径。实测 t2 0.9→0.80 时「名校」113→41、「强校」75→147。
        #    改按档位判定后,tier_weight 可自由调而标签分布不动。
        #    (旧默认 t1/t2/t3=1.0/0.9/0.3 下两种写法等价,故此改动对老实例是 no-op。)
        top_school = st in (1, 2)
        mid_school = st == 3
        if top_school and co:
            pedigree = "双背书"
        elif top_school:
            pedigree = "名校"
        elif co:
            pedigree = "大厂"
        elif mid_school:
            pedigree = "强校"
        else:
            pedigree = "无"
        # 背书强度:学校档与公司档各自分级后取 max,**两侧都有时再叠加 joint_bonus**
        # (原实现取 max,导致「双背书」与「单背书」同分——头部拉不开)
        co_w = company_endorse_weight(talent)
        strength = min(1.0, max(school_w, co_w)
                       + (JOINT_BONUS if (school_w > 0 and co_w > 0) else 0.0))
        total = round(total * (PENALTY_FLOOR + (1 - PENALTY_FLOOR) * strength), 1)

        # 超长简历（描述总字数超全池 p95/p99 锚点）——抽象总结能力弱信号
        verbose_lv = 2 if flat["desc_total"] >= VERBOSE_L2 else (1 if flat["desc_total"] >= VERBOSE_L1 else 0)
        if verbose_lv:
            total = round(total * VERBOSE_MULT[verbose_lv], 1)

        # 传统企业 AI 经历 + 无训练侧硬深度证据 → 深度不足风险(面试反馈实证)。
        # 工具名自陈(TOOL_CLAIM)不豁免——正是美化高发区。
        env, env_co = ai_env(talent)
        hard_hits = _re_hits(HARD_DEPTH_RE, flat["entries"])
        tool_claims = _re_hits(TOOL_CLAIM_RE, flat["entries"])
        shallow_env_flag = (env == "traditional" and not hard_hits)
        if shallow_env_flag:
            total = round(total * MULT_SHALLOW_ENV, 1)

        # owner-vs-执行(只标注) + AI断档(乘数惩罚)
        ow_hits, ex_hits, exec_dom = owner_exec_profile(talent)
        gap = ai_gap_months(talent)
        ai_stale = gap is not None and gap > AI_STALE_MONTHS
        if ai_stale:
            total = round(total * MULT_AI_STALE, 1)

        stage = (app.get("stage") or {}).get("zh_name", "")
        active_status = app.get("active_status")
        resume_source = (app.get("resume_source_info") or {}).get("name", {}).get("zh_cn", "")

        rows.append({
            "name": flat["name"],
            "total": total,
            "education": deg_s,
            "major": maj_s,
            "years": yrs_s,
            "skills": sk_s,
            "company": co_s,
            "stability": sta_s,
            "project": proj_s,
            "school": flat["school"],
            "degree": deg_label,
            "major_text": flat["major"],
            "years_text": flat["years"],
            "current_company": cur_co,
            "skills_hit": ",".join(hits_all),
            "role_fit": role_fit(hits_a, hits_b),
            "stage": stage,
            "active": ("投递已撤回" if app.get("id") in withdrawn_ids
                       else "在招" if active_status == 1
                       else "已终止" if active_status == 2 else "未知"),
            "resume_source": resume_source,
            "talent_id": flat["talent_id"],
            "application_id": app.get("id"),
            "years_skills_flag": years_skills_flag,
            "pedigree": pedigree,
            "flag_short_stints": n_short,
            "flag_overlap": overlap_flag,
            "flag_gap": gap_flag,
            "flag_ops": ops_flag,
            "verbose_lv": verbose_lv,
            "desc_total": flat["desc_total"],
            "ai_env": env,
            "ai_env_company": env_co,
            "hard_depth": ",".join(hard_hits),
            "tool_claims": ",".join(tool_claims),
            "flag_shallow_env": shallow_env_flag,
            "owner_hits": ow_hits,
            "exec_hits": ex_hits,
            "flag_exec_dom": exec_dom,
            "ai_gap": gap if gap is not None else "",
            "flag_ai_stale": ai_stale,
            "create_time": app.get("create_time") or "",
        })

    rows.sort(key=lambda r: r["total"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # --- LLM 精读范围:分数阈值 ∪ 近期放宽 ∪ 强背书召回子句 ∪ 顶尖背书召回子句 ---
    # ① 主网 总分>=main_th:与池规模无关的质量线,池增大也不挤掉优质候选(固定 TopN 会)。
    # ② 近期放宽 近 recent_days 天 & 总分>=recent_th:给近期投递更低门槛。
    # ③ 强背书+非默认方向:召回被分项埋没的终点真阳性,LLM 语义层兜底。
    # ④ 顶尖背书(可选,缺省关闭):pedigree 命中即精读,不论总分。
    # 撤回投递不进范围。命中即需 LLM 精读(新进者才跑,已评的不重跑)。
    now_ms = NOW_MS
    for r in rows:
        t = r["total"]
        ct = r["create_time"]
        days_ago = ((now_ms - int(ct)) / 86400000) if str(ct).isdigit() else 9999
        in_scope = (r["active"] != "投递已撤回") and (
            t >= LLM_MAIN_TH
            or (days_ago <= LLM_RECENT_DAYS and t >= LLM_RECENT_TH)
            or (r["company"] >= LLM_BACKING_CO and r["role_fit"] != LLM_BACKING_DEFAULT_ROLE)
            or (r["pedigree"] in LLM_PEDIGREE_RECALL))
        r["llm_scope"] = in_scope

    cols = ["rank", "name", "total", "role_fit", "stage", "active", "education", "major",
            "years", "skills", "company", "stability", "project", "school", "degree",
            "major_text", "years_text", "current_company", "resume_source", "skills_hit",
            "talent_id", "application_id", "years_skills_flag", "pedigree",
            "flag_short_stints", "flag_overlap", "flag_gap", "flag_ops",
            "verbose_lv", "desc_total",
            "ai_env", "ai_env_company", "hard_depth", "tool_claims", "flag_shallow_env",
            "owner_hits", "exec_hits", "flag_exec_dom", "ai_gap", "flag_ai_stale",
            "create_time", "llm_scope"]
    csv_path = out_dir / "report.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    n_pending = sum(1 for r in rows if r["stage"] in PENDING_STAGES and r["active"] == "在招")
    n_advanced = sum(1 for r in rows if r["stage"] not in PENDING_STAGES)
    n_terminated = sum(1 for r in rows if r["active"] == "已终止")

    md = []
    md.append(REPORT_TITLE)
    md.append("")
    md.append("## 重要提醒")
    for line in REPORT_NOTES:
        md.append(line)
    md.append("")
    md.append(f"- 投递总数（该职位全量）: {len(all_application_ids)}")
    if empty_from_hire:
        md.append(f"- 飞书侧返回空数据、无法评分的投递: {', '.join(empty_from_hire)}"
                   f"（建议人工在飞书招聘后台核查，可能是候选人数据被撤回/清除）")
    if skipped:
        md.append(f"- 有投递记录但找不到对应人才详情、无法评分: {', '.join(skipped)}")
    md.append(f"- 成功评分: {len(rows)}")
    md.append(f"- 仍处于简历筛选/初筛阶段（待你处理）: {n_pending}")
    md.append(f"- 已推进到面试及以后阶段: {n_advanced}")
    md.append(f"- 已终止（淘汰/withdrawn）: {n_terminated}")
    md.append(f"- 年限期望: {YEARS_MIN}-{YEARS_MAX} 年{REPORT_YEARS_NOTE}")
    md.append("")
    md.append("## 排序表（按 total 降序）")
    md.append("")
    md.append("| Rank | 姓名 | 总分 | 方向匹配 | 当前阶段 | 状态 | 学历 | 专业 | 年限 | 技能 | 公司 | 稳定性 | 项目 | 院校 | 学位 | 年限(年) | 当前公司 | 简历来源 |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        md.append(
            f"| {r['rank']} | {r['name']} | **{r['total']}** | {r['role_fit']} | {r['stage']} | {r['active']} | "
            f"{r['education']} | {r['major']} | {r['years']} | {r['skills']} | {r['company']} | "
            f"{r['stability']} | {r['project']} | {r['school']} | {r['degree']} | "
            f"{r['years_text']} | {r['current_company']} | {r['resume_source']} |"
        )
    (out_dir / "report.md").write_text("\n".join(md))
    print(f"wrote {out_dir}/report.md and {csv_path}", file=sys.stderr)
    print(f"skipped (no data): {skipped}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="规则层评分引擎(配置驱动)")
    ap.add_argument("--config", required=True, help="岗位配置 TOML(私有实例)")
    ap.add_argument("--workspace", required=True, help="数据工作目录(ndjson 输入所在)")
    ap.add_argument("--out-dir", default=None, help="输出目录,默认 <workspace>/out")
    args = ap.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    bind_config(cfg)

    workspace = Path(args.workspace)
    out_dir = Path(args.out_dir) if args.out_dir else workspace / "out"
    return run(workspace, out_dir)


if __name__ == "__main__":
    sys.exit(main())
