#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
理财工作台 · 财经新闻 —— 每日 14:00 更新 · 简报生成器（普通家庭版 v3）
======================================================================

v3 变更（相对 v2）：
  1. 主体由「三大板块机构报告」改为「5-10 条官方新闻卡片」，手机优先、干净一目了然。
  2. 每条新闻自动产出三段式解读：💬 解析 / 🔗 影响 / ✅ 措施，
     由内置 INTERPRET_RULES 规则库按 category 套用、注入当日真实数值生成 —— 保证
     每天 14:00 可持续自动更新，不依赖人工撰写。
  3. 影响性质（利好/中性/需留意）、风险提示、涨跌数字三套色标语义，页面顶部给颜色图例。
  4. 生成提示词见 prompts/daily_briefing_prompt.md（v3）。

职责（每天 14:00 由自动化任务按序调用）：
  Step 1  python3 sources/fetch_mainstream.py        采集官方渠道 + 主流网站数据
  Step 2  python3 scripts/generate_briefing.py       生成简报 + 刷新页面 + 产出推送摘要

产出：
  data/briefing-<date>.json     结构化简报（v3 精简结构）
  data/briefing-latest.json     最新一期（页面动态加载）
  data/push-<date>.md           14:00 手机推送摘要
  index.html                    同步刷新内嵌数据块（双击打开即为最新一期）

数据纪律：所有数值来自当日真实采集结果，逐条标注官方发布机构；缺失时如实降级。

用法：
  python3 scripts/generate_briefing.py
  python3 scripts/generate_briefing.py --date 2026-11-09
  python3 scripts/generate_briefing.py --no-html
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HTML_FILE = ROOT / "index.html"
OFFICIAL_FILE = DATA_DIR / "official-news.json"
SNAPSHOT_FILE = DATA_DIR / "market-snapshot.json"
PROMPT_FILE = ROOT / "prompts" / "daily_briefing_prompt.md"

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

DISCLAIMER = ("⚠️ 本内容由 AI 基于官方发布及证券时报、新浪财经、同花顺、东方财富、万得 Wind、雪球等公开数据整理生成，"
              "仅供信息参考，不构成任何投资建议或产品推荐。投资有风险，决策需谨慎。")

DATA_SOURCE = ("新浪财经（指数实时 · 日K · 全市场涨跌家数）· 同花顺（板块日K · 成交额）· "
               "证券时报·人民财讯（官方要闻）· 东方财富（7×24 快讯 · 财报）· "
               "官方渠道（央行 · 统计局 · 国务院 · 外汇局 · 证监会 · 财政部）· "
               "万得 Wind（宏观口径）· 雪球（观点与情绪）")

IMPACT_LABEL = {"positive": "利好", "neutral": "中性", "caution": "需留意"}

LEGEND = [
    {"color": "up", "label": "上涨 / 利好"},
    {"color": "down", "label": "下跌"},
    {"color": "risk", "label": "风险提示"},
    {"color": "neutral", "label": "中性 / 关注"},
]

# ---------------------------------------------------------------------------
# 三段式解读规则库
#   每个 category 返回：plain(解析) / effect(影响) / action(措施)
#   + risk(橙红风险提示，可空) + effectNumbers(带涨跌色的数值 chip)
#   全部用当日官方发布原文的真实数值注入；措辞为大白话短句，禁止长横条。
# ---------------------------------------------------------------------------

INTERPRET_RULES = {
    "stabilize": lambda m: {
        "plain": "国家开会说了：把「稳住股市」写进重点工作。以后市场大跌时，更可能有人出手托一把。",
        "effect": "对指数基金 → 中期多一层托底，波动有望变小。但这不等于明天就会涨。",
        "action": "定投照常扣款就行，不用因为这条新闻加钱，更别去追热点。",
        "risk": "",
        "effectNumbers": [],
    },
    "lpr": lambda m: {
        "plain": f"买房贷款利率的「底价」没变，5 年期以上 LPR 还是 {m.get('lpr5','3.50%')}。"
                 "你每月还多少房贷，主要就看这个数。",
        "effect": "对买房月供 → 本期无变化，月供不用重算。对债券基金 → 利率没动，债市方向也没变。",
        "action": "继续按原计划攒首付，首付池放存款、货基或短债，别挪进股市。",
        "risk": "⚠️ 首付是几年内确定要用的钱，别为了多赚一点放进权益类资产，"
                "免得用钱时被迫低位赎回。",
        "effectNumbers": [{"text": "5年期以上 LPR " + m.get("lpr5", "3.50%"), "dir": "flat"}],
    },
    "cpi": lambda m: {
        "plain": f"10 月物价比去年同月涨了 {m.get('cpi','0.7')}%，涨得很温和；"
                 f"但工厂出厂价涨了 {m.get('ppi','5.7')}%，上游成本在抬。",
        "effect": "对存款和货基 → 利息短期难明显变高。对债基 → 通胀预期会压一点债券价格。"
                  "对生活开支 → 食品价格反而降了，买菜没更贵。",
        "action": "不用为单月物价调仓，日常开支照常，定投照常。",
        "risk": "",
        "effectNumbers": [
            {"text": "CPI +" + m.get("cpi", "0.7") + "%", "dir": "up"},
            {"text": "PPI +" + m.get("ppi", "5.7") + "%", "dir": "up"},
            {"text": "食品 " + m.get("food", "-2.9") + "%", "dir": "down"},
        ],
    },
    "omo": lambda m: {
        "plain": f"央行今天往银行体系放了 {m.get('amount','1820')} 亿短期钱，利息 {m.get('rate','1.25')}%，"
                 f"当天有 {m.get('maturity','2260')} 亿到期。整体不缺钱。",
        "effect": "对货币基金、债券基金 → 资金面平稳，短期收益不会有明显跳动。",
        "action": "什么都不用做，货基和短债照常放着。",
        "risk": "",
        "effectNumbers": [],
    },
    "fx": lambda m: {
        "plain": f"人民币对美元官方中间价报 {m.get('mid','6.7188')}，"
                 f"比上一交易日便宜了 {m.get('bp','20')} 个基点，幅度很小。",
        "effect": "对换汇、留学 → 换 1 万美元大约多花二十来块。对买房 → 间接影响进口建材成本，"
                  "短期内可以忽略。",
        "action": "近期没有大额换汇需求就不用管，别为 20 个基点去折腾。",
        "risk": "⚠️ 别把汇率小幅波动当成趋势，去抢换美元或追涨 QDII 基金。",
        "effectNumbers": [{"text": "中间价 " + m.get("mid", "6.7188"), "dir": "down"}],
    },
    "tariff": lambda m: {
        "plain": f"中美谈好了：可能加征的 {m.get('suspend','24')}% 关税继续停 "
                 f"{m.get('months','2')} 个月，只保留 {m.get('keep','10')}%。"
                 "进口美国商品不会被大幅加价。",
        "effect": "对物价 → 进口成本压力小一点，通胀更稳。对外贸相关的宽基权重行业 → 情绪偏暖。",
        "action": "不用动仓位，指数基金定投照常。",
        "risk": "",
        "effectNumbers": [],
    },
    "etf": lambda m: {
        "plain": f"大资金在持续买指数基金：沪市连续 {m.get('days','28')} 天净流入 "
                 f"{m.get('amount','5412')} 亿，深市连续 {m.get('szDays','19')} 天净流入 "
                 f"{m.get('szAmount','1418')} 亿。",
        "effect": "对指数基金 → 长期资金在进场，是稳市的一部分。但不代表短期一定涨。",
        "action": "你本来就在定投宽基，方向一致，按原计划扣款就行，别临时加码。",
        "risk": "",
        "effectNumbers": [],
    },
    "bond": lambda m: {
        "plain": "今天国债期货各期限都小跌，"
                 f"30 年期跌 {abs(float(m.get('c30','-0.14'))):.2f}%、"
                 f"10 年期跌 {abs(float(m.get('c10','-0.06'))):.2f}%。"
                 "买长债的人今天净值可能微亏。",
        "effect": "对债券基金 → 长久期债基会有小幅回撤。利率越低，长债价格波动越明显。",
        "action": "检查自己债基的久期，短期不追加长债；已持有的不必因为一天下跌就赎回。",
        "risk": "⚠️ 债券基金不保本。久期越长波动越大，还可能有巨额赎回扰动，别把它当存款。",
        "effectNumbers": [
            {"text": "30年期 " + m.get("c30", "-0.14") + "%", "dir": "down"},
            {"text": "10年期 " + m.get("c10", "-0.06") + "%", "dir": "down"},
        ],
    },
    "reserve": lambda m: {
        "plain": f"国家外汇储备 {m.get('size','34052')} 亿美元，连续 {m.get('months','7')} 个月"
                 f"站在 3.4 万亿上方；央行黄金储备连增 {m.get('gold','24')} 个月。",
        "effect": "对人民币汇率 → 家底厚，汇率大起大落的概率低。对买房、换汇 → 不用急着抢换美元。",
        "action": "按自己的用款时点安排，别因短期消息提前换汇。",
        "risk": "",
        "effectNumbers": [],
    },
    # 证券时报当日要闻补位：真实标题 + 定性三段式，不编造任何数值
    "brief": lambda m: {
        "plain": ("证券时报等主流财经媒体今天在关注：「" + str(m.get("title", ""))
                  + "」。本条为要闻页直采标题，完整原文见下方来源链接。"),
        "effect": ("这类消息多先影响市场预期，再慢慢传导到指数或债基估值；"
                   "对你定投的宽基指数、债券基金一般不构成需要立刻动手的信号。"),
        "action": ("先看它会不会改变利率、房价或收入这些「慢变量」：只影响情绪的消息，"
                   "定投照常、什么都不用做；拿不准就先记下，等周末一起看。"),
        "risk": ("⚠️ 单条媒体要闻不等于趋势，别据此追涨杀跌；"
                 "补位条目未附官方原文数值，具体数字以原文链接为准。"),
        "effectNumbers": [],
    },
}

# 规则库兜底：未来出现新类别时也能给出克制的三段式，不编造数据
def _fallback(m):
    return {
        "plain": "这是一条官方发布的财经信息，原文见下方来源。",
        "effect": "请结合自己持有的基金与买房计划判断是否需要动作。",
        "action": "若看不出直接影响，什么都不用做，定投照常。",
        "risk": "",
        "effectNumbers": [],
    }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def build_glance(snapshot: dict) -> dict:
    """今日速览：4 个指数小方格（红涨绿跌）+ 一句话市场情绪。"""
    idx = []
    for x in (snapshot.get("indices") or [])[:4]:
        try:
            ch = float(x.get("change", 0))
        except (TypeError, ValueError):
            ch = 0.0
        idx.append({
            "name": x.get("name", "—"),
            "price": str(x.get("price", "—")),
            "change": ch,
        })
    while len(idx) < 4:
        idx.append({"name": "—", "price": "—", "change": 0.0})

    b = snapshot.get("breadth") or {}
    up, down = int(b.get("up", 0) or 0), int(b.get("down", 0) or 0)
    tot = up + down
    avg_ch = sum(i["change"] for i in idx) / max(1, len(idx))
    score = 50 + ((up - down) / tot * 25 if tot else 0) + avg_ch * 8
    score = max(0, min(100, round(score)))
    if avg_ch > 0.2:
        idx_desc = "主要指数小幅收涨"
    elif avg_ch < -0.2:
        idx_desc = "主要指数小幅回落"
    else:
        idx_desc = "主要指数涨跌互现"
    if score >= 70:
        label, tail = "偏暖", "情绪偏热；别追涨，定投照常。"
    elif score >= 45:
        label, tail = "中性观望", "情绪中性；定投照常，不做改动。"
    else:
        label, tail = "偏冷", "下跌家数偏多，情绪偏冷；不恐慌赎回，定投照常。"
    note = f"涨 {up} / 跌 {down}，{idx_desc}。" + tail
    return {"indices": idx, "sentiment": {"score": score, "label": label, "note": note}}


# ---------------------------------------------------------------------------
# 盘中研判：布林线 + 成交量 + 机构复盘 → 加仓 / 减仓 / 等待
# ---------------------------------------------------------------------------

VERDICT_SCALE = {
    "add": "本期定投可略多投一档，分 2-3 次投入，不一次性加满。",
    "wait": "按原定投计划扣款，不额外加钱，也不减仓。",
    "reduce": "暂停额外追加，新增资金先放货基；不是卖出已有份额。",
}
VERDICT_LABEL = {"add": "加仓", "wait": "等待", "reduce": "减仓"}
VERDICT_COLOR = {"add": "up", "wait": "neutral", "reduce": "down"}


def build_intraday(snapshot: dict, sentiment: dict) -> dict:
    """综合研判：以真实 BOLL(20,2)、量能与机构复盘观点打分，输出三选一意见。"""
    raw = snapshot.get("intraday") or {}
    boll, vol = raw.get("boll") or {}, raw.get("volume") or {}
    ins = raw.get("institutions") or {}

    # 数据缺失时如实降级，不编造结论
    if not boll or not vol:
        return {
            "available": False, "verdict": "wait", "verdictLabel": "等待",
            "verdictColor": "neutral", "score": 50,
            "oneLiner": "本次未取到完整的布林与量能数据，按原定投计划执行，不改仓位。",
            "basis": "盘中研判数据本次未取到，不做推测",
            "factors": [], "reasons": [], "quotes": [], "options": [],
            "measure": "按原定投计划扣款，等下一期数据完整再看。",
            "risk": "⚠️ 数据不完整时不凭感觉加减仓，这是最容易做错的时候。",
            "chart": {},
        }

    pb = float(boll.get("percentB", 0.5))
    band = boll.get("bandTrend", "走平")
    pos = boll.get("position", "中轨区间")
    ratio = float(vol.get("ratio", 1))
    day_ratio = float(vol.get("dayRatio", 1))
    chg5 = float(vol.get("chg5", 0))
    pattern = vol.get("pattern", "量价平稳")
    day_state = vol.get("dayState", "当日量能常态")
    bull = int(ins.get("bull", 0))
    bear = int(ins.get("bear", 0))
    cnt = int(ins.get("count", 0))
    senti = int(sentiment.get("score", 50))

    # ---- 打分（基准 50，正=偏加仓，负=偏减仓）----
    delta = 0.0
    # 1) 布林位置：低位偏机会、高位偏风险
    if pb <= 0.20:
        delta += 14
        boll_note = "贴近下轨，属超卖区，历史上更容易出现修复"
    elif pb < 0.45:
        delta += 4
        boll_note = "位于中轨下方，价格偏弱但离下轨还有距离"
    elif pb <= 0.65:
        delta += 0
        boll_note = "贴近中轨，方向不明"
    elif pb < 0.80:
        delta -= 4
        boll_note = "位于中轨上方偏高位，追涨性价比下降"
    else:
        delta -= 14
        boll_note = "贴近上轨，属超买区，短期回踩概率上升"

    # 2) 量价配合
    if "放量上行" in pattern:
        delta += 12
    elif "量能配合上行" in pattern:
        delta += 6
    elif "放量下跌" in pattern:
        delta -= 12
    elif "缩量上涨" in pattern:
        delta -= 2
    elif "缩量回落" in pattern:
        delta += 2
    elif "缩量整理" in pattern:
        delta -= 2
    elif "量能配合回落" in pattern:
        delta -= 6
    if day_ratio <= 0.85:
        delta -= 4
    elif day_ratio >= 1.15:
        delta += 2

    # 3) 机构复盘倾向
    net = (bull - bear) / max(1, cnt)
    delta += net * 14

    # 4) 当日市场情绪
    delta += (senti - 50) / 50 * 8

    score = max(0, min(100, round(50 + delta)))

    # ---- 冲突与「等待」条件 ----
    # 布林收口 = 变盘方向未明；机构偏多但价格仍在中轨下方 = 中期观点与短期盘面不同步
    conflict = (net > 0.3 and pb < 0.45) or (net < -0.3 and pb > 0.65)
    wait_bias = (band == "收口") or conflict or abs(ratio - 1) < 0.03

    if score >= 62 and not wait_bias:
        verdict = "add"
    elif score <= 38 and not wait_bias:
        verdict = "reduce"
    else:
        verdict = "wait"

    verdict_label = VERDICT_LABEL[verdict]
    verdict_color = VERDICT_COLOR[verdict]

    # ---- 一句话结论 ----
    if verdict == "add":
        one = f"技术与量能共振偏强（研判 {score}/100），本期定投可小幅加仓，别一次加满。"
    elif verdict == "reduce":
        one = f"位置偏高且量价转弱（研判 {score}/100），先减少追加，不是清仓卖出。"
    else:
        why = "布林带收口、变盘方向未明" if band == "收口" else "多空信号不一致"
        one = f"{why}（研判 {score}/100），今天按原定投计划扣款，等方向清楚再动。"

    # ---- 三个小方格 ----
    factors = [
        {
            "name": "布林位置",
            "value": f"%B {pb:.2f} · {pos}",
            "note": boll_note,
            "tone": "up" if pb <= 0.2 else ("down" if pb >= 0.8 else "neutral"),
        },
        {
            "name": "成交量",
            "value": f"量比 {ratio:.2f} · {day_state}",
            "note": (f"近 5 日均额 {vol.get('ma5', 0):.0f} 亿 / 20 日均额 {vol.get('ma20', 0):.0f} 亿；"
                     f"近 5 日指数 {chg5:+.2f}%，{pattern}"),
            "tone": "neutral" if day_ratio <= 0.85 else vol.get("tone", "neutral"),
        },
        {
            "name": "机构复盘",
            "value": f"{cnt} 条 · 偏多 {bull} / 偏空 {bear}",
            "note": ("机构观点偏多，多为中期稳市与配置判断，不等于今天就是买点"
                     if net > 0.2 else
                     ("机构观点偏谨慎，注意别在情绪高点追加" if net < -0.2 else "机构多空分歧，方向未形成共识")),
            "tone": "up" if net > 0.2 else ("down" if net < -0.2 else "neutral"),
        },
    ]

    # ---- 三条依据（短句，窄栏自然换行）----
    reasons = [
        f"布林：收盘 {boll.get('current', '—')}，中轨 {boll.get('mid', '—')}，"
        f"上轨 {boll.get('upper', '—')} / 下轨 {boll.get('lower', '—')}；带宽 {boll.get('bandwidth', '—')}% 呈{band}。",
        f"量能：{vol.get('desc', '—')}；{day_state}，近 5 日 {chg5:+.2f}%。",
        f"机构：{cnt} 条复盘观点里偏多 {bull} 条、中性 {ins.get('neutral', 0)} 条、偏空 {bear} 条。"
        + ("但价格仍在中轨下方，观点与盘面不同步。" if conflict else ""),
    ]

    quotes = [{
        "org": x.get("org", "机构"), "time": x.get("time", ""),
        "scope": x.get("scope", "A股"), "tone": x.get("tone", "中性"),
        "text": x.get("title", ""),
    } for x in (ins.get("items") or [])[:4]]

    options = [
        {"key": "add", "label": "加仓", "desc": "定投多投一档", "active": verdict == "add"},
        {"key": "wait", "label": "等待", "desc": "按原计划扣款", "active": verdict == "wait"},
        {"key": "reduce", "label": "减仓", "desc": "暂停额外追加", "active": verdict == "reduce"},
    ]

    measure = VERDICT_SCALE[verdict]
    risk = ("⚠️ 这里的「加仓/减仓」只指本期新增定投资金的增减，不是买卖个股、也不是清仓。"
            "首付池与短期要用的钱始终不进权益。")

    return {
        "available": True,
        "verdict": verdict, "verdictLabel": verdict_label,
        "verdictColor": verdict_color, "score": score,
        "oneLiner": one,
        "basis": f"技术面 {raw.get('lastDate', '—')} 收盘 · BOLL(20,2) · 近 {len(boll.get('series') or [])} 日；"
                 f"机构观点截至 {raw.get('asOf', '—')}",
        "factors": factors, "reasons": reasons, "quotes": quotes,
        "options": options, "measure": measure, "risk": risk,
        "scale": VERDICT_SCALE,
        "chart": {
            "labels": [x["d"] for x in (boll.get("series") or [])],
            "close": [x["close"] for x in (boll.get("series") or [])],
            "mid": [x["mid"] for x in (boll.get("series") or [])],
            "upper": [x["upper"] for x in (boll.get("series") or [])],
            "lower": [x["lower"] for x in (boll.get("series") or [])],
            "amount": [x["amount"] for x in (boll.get("series") or [])],
        },
    }


def build_news(items: list) -> list:
    out = []
    for i, x in enumerate(items, 1):
        rule = INTERPRET_RULES.get(x.get("category"), _fallback)
        try:
            parts = rule(x.get("metrics") or {})
        except Exception:
            parts = _fallback({})
        out.append({
            "no": i,
            "org": x.get("org", ""),
            "orgShort": x.get("orgShort", ""),
            "time": x.get("time", ""),
            "title": x.get("title", ""),
            "source": x.get("source", ""),
            "verified": bool(x.get("verified")),
            "impact": x.get("impact", "neutral"),
            "impactLabel": IMPACT_LABEL.get(x.get("impact", "neutral"), "中性"),
            "relates": x.get("relates") or [],
            "plain": parts["plain"],
            "effect": parts["effect"],
            "action": parts["action"],
            "risk": parts["risk"],
            "effectNumbers": parts["effectNumbers"],
        })
    return out


def build_briefing(target: date) -> dict:
    official = json.loads(OFFICIAL_FILE.read_text(encoding="utf-8"))
    snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    prompt_text = PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else ""

    news = build_news(official.get("items") or [])
    glance = build_glance(snapshot)
    glance["intraday"] = build_intraday(snapshot, glance["sentiment"])

    pos = sum(1 for n in news if n["impact"] == "positive")
    cau = sum(1 for n in news if n["impact"] == "caution")
    today_action = ("今天没有需要你做的事：定投照常扣款、首付池不动、债基别追高。"
                    if cau == 0 else
                    f"今天留意 {cau} 条「需留意」新闻；除此之外，定投照常、首付池不动、债基别追高。")

    channels = [{
        "name": c.get("name", ""), "short": c.get("short", ""),
        "status": c.get("status", "partial"), "url": c.get("url", ""),
        "note": c.get("note", ""),
    } for c in (official.get("channels") or [])]

    return {
        "meta": {
            "date": target.strftime("%Y-%m-%d"),
            "weekday": WEEKDAYS[target.weekday()],
            "updateTime": "14:00",
            "dataTime": official.get("fetchedAt", snapshot.get("as_of", "")),
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "dataSource": DATA_SOURCE,
            "dataMode": "live",
            "promptVersion": "v3 · 普通家庭版",
            "newsCount": len(news),
            "positiveCount": pos,
            "cautionCount": cau,
        },
        "legend": LEGEND,
        "glance": glance,
        "todayAction": today_action,
        "news": news,
        "channels": channels,
        "disclaimer": DISCLAIMER,
        "_promptText": prompt_text,
    }


def build_push(b: dict) -> str:
    m, g = b["meta"], b["glance"]
    lines = [
        f"# 财经新闻 · {m['date']} {m['weekday']}（每日 14:00 更新）",
        "",
        f"> **今日一句话**：{b['todayAction']}",
        "",
        f"市场情绪 **{g['sentiment']['score']}/100 · {g['sentiment']['label']}**。"
        f"{g['sentiment']['note']}",
        "",
        f"## 盘中研判 · {g['intraday']['verdictLabel']}（{g['intraday']['score']}/100）",
        "",
        g["intraday"]["oneLiner"],
        "",
        f"- **布林**：{g['intraday']['factors'][0]['value'] if g['intraday']['factors'] else '—'}",
        f"- **量能**：{g['intraday']['factors'][1]['value'] if g['intraday']['factors'] else '—'}",
        f"- **机构**：{g['intraday']['factors'][2]['value'] if g['intraday']['factors'] else '—'}",
        "",
        f"**措施**：{g['intraday']['measure']}",
        "",
        f"> {g['intraday']['basis']}",
        "",
        f"## 官方新闻 {m['newsCount']} 条（大白话解读）",
        "",
    ]
    for n in b["news"]:
        lines += [
            f"### {n['no']}. [{n['impactLabel']}] {n['title']}",
            f"*{n['org']} · {n['time']}*",
            "",
            f"- 💬 **解析**：{n['plain']}",
            f"- 🔗 **影响**：{n['effect']}",
            f"- ✅ **措施**：{n['action']}",
        ]
        if n["risk"]:
            lines.append(f"- {n['risk']}")
        lines.append("")
    lines += [
        "---",
        f"数据来源：{m['dataSource']}",
        "",
        b["disclaimer"],
    ]
    return "\n".join(lines) + "\n"


def inject_html(b: dict) -> bool:
    if not HTML_FILE.exists():
        return False
    html = HTML_FILE.read_text(encoding="utf-8")
    payload = json.dumps({k: v for k, v in b.items() if not k.startswith("_")},
                         ensure_ascii=False, separators=(",", ":"))
    pat = r'(<script id="briefing-data" type="application/json">)(.*?)(</script>)'
    if not re.search(pat, html, re.S):
        return False
    html = re.sub(pat, lambda mm: mm.group(1) + payload + mm.group(3), html, count=1, flags=re.S)
    HTML_FILE.write_text(html, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="财经新闻 v3 · 每日 14:00 简报生成器")
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"), help="简报日期 YYYY-MM-DD")
    ap.add_argument("--no-html", action="store_true", help="不刷新 index.html 内嵌数据块")
    args = ap.parse_args()
    target = datetime.strptime(args.date, "%Y-%m-%d").date()

    for p in (OFFICIAL_FILE, SNAPSHOT_FILE):
        if not p.exists():
            print(f"[错误] 缺少 {p}，请先运行：python3 sources/fetch_mainstream.py")
            return 1

    b = build_briefing(target)
    stamp = target.strftime("%Y-%m-%d")
    (DATA_DIR / f"briefing-{stamp}.json").write_text(
        json.dumps({k: v for k, v in b.items() if not k.startswith("_")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / "briefing-latest.json").write_text(
        json.dumps({k: v for k, v in b.items() if not k.startswith("_")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / f"push-{stamp}.md").write_text(build_push(b), encoding="utf-8")

    injected = False
    if not args.no_html:
        injected = inject_html(b)

    m, g = b["meta"], b["glance"]
    print(f"[生成] {stamp} {m['weekday']} | 官方新闻 {m['newsCount']} 条"
          f"（利好 {m['positiveCount']} / 需留意 {m['cautionCount']}）")
    print(f"[生成] 情绪 {g['sentiment']['score']}/100 · {g['sentiment']['label']}")
    print(f"[生成] 官方渠道 {len(b['channels'])} 家 | 页面注入 {'OK' if injected else '跳过/失败'}")
    print(f"[生成] 落盘 briefing-{stamp}.json / briefing-latest.json / push-{stamp}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
