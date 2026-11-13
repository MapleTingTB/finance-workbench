#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指数跟踪 · 每日简报生成器

消费 sources/fetch_funds.py 的真实采集结果，按 prompts/index_tracking_prompt.md 规范：
  · 组装每只自选基金：实时指数 / 涨幅估值 / 成交量 / 估值高低 / 未来走势预估
  · 技术面：BOLL / MACD / 均线 / 趋势 / 区域，逐条给出灰字依据
  · 辅助买基金：交易明细由界面录入（localStorage），本生成器提供净值与口径，
    前端按「持有收益 / 收益率 / 首次买入至今涨跌幅」实时计算
  · 产出推送摘要、结构化简报，刷新 index.html 内嵌数据块
用法：python3 scripts/generate_briefing.py [--date YYYY-MM-DD] [--no-html]
"""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

WEEKDAY = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
DATA_SOURCE = ("新浪财经（标的指数 · 大盘指数 250 日 K 线）· 同花顺（指数日K · 成交额）· "
               "腾讯行情（外围市场）· 天天基金（场外基金真实净值序列 · 基金档案）· "
               "东方财富（指数实时行情 · 基金档案）")
DISCLAIMER = ("本内容由 AI 基于公开行情与基金净值整理生成，仅供参考，"
              "不构成任何投资建议或具体产品推荐。指数基金为场外基金，"
              "「实时指数」为其跟踪标的指数的盘中行情，「涨幅估值」为按指数涨幅推算的估算值，"
              "最终以基金公司公布的净值为准。技术分析基于历史价格，不预示未来。投资有风险，决策需谨慎。")

LEGEND = [
    {"color": "up", "label": "上涨 / 偏强"},
    {"color": "down", "label": "下跌 / 偏弱 / 偏低"},
    {"color": "risk", "label": "偏高 / 顶部 / 风险"},
    {"color": "neutral", "label": "中性 / 合理 / 震荡"},
]

# 页尾灰字：技术面依据说明（公式 + 参数 + 判定规则 + 局限）
BASIS_RULES = [
    ["布林线 BOLL(20,2)", "中轨＝近 20 日收盘简单均线；上/下轨＝中轨 ± 2 倍 20 日收盘标准差；"
     "%B＝(收盘−下轨)/(上轨−下轨)×100%，衡量价格在通道内的相对位置；带宽＝(上轨−下轨)/中轨×100%，衡量波动收敛/扩张。"],
    ["MACD(12,26,9)", "DIF＝EMA(12)−EMA(26)；DEA＝DIF 的 9 日 EMA；柱＝(DIF−DEA)×2。"
     "DIF 上穿 DEA 为金叉、下穿为死叉；柱体由负转正或持续放大代表动能转强。"],
    ["趋势判定", "五因子打分：MA5>MA20、MA20>MA60、DIF>DEA、MA20 近 5 日斜率>+0.5% 各 +1，反之 −1；"
     "得分 ≥+2 判「上涨趋势」、≤−2 判「下跌趋势」、其余为「震荡整理」。"],
    ["区域判定", "综合位置＝0.5×%B ＋ 0.5×近 250 日价格分位；≥68% 为「顶部区域」、≤32% 为「底部区域」、"
     "其余为「中部区域」。价格分位＝(当前收盘−区间最低)/(区间最高−区间最低)×100%。"],
    ["估值高低", "因场外指数基金不公布实时 PE，本页以「标的指数近 250 个交易日收盘点位分位」作为相对高低口径："
     "≥70% 偏高、≤30% 偏低、其余合理。这是相对位置而非绝对估值，请结合 PE/PB 自行判断。"],
    ["未来走势预估", "五因子：DIF>DEA、MACD 柱走强、收盘>MA20、%B<90（未贴上轨）、带宽<25% 各 +1，"
     "部分项为 −1；得分 ≥+3「偏强震荡」、≤−2「偏弱调整」、其余「区间震荡」。仅为技术倾向，非涨跌预测。"],
    ["涨幅估值口径", "场外基金无盘中盘口，本页「估算涨幅」取其跟踪标的指数的实时涨幅作为盘中参考；"
     "「已确认净值涨幅」取基金公司最近一个交易日公布的单位净值日涨幅，二者可能不同。"],
    ["成交量口径", "场外基金本身无成交量，本页成交量/成交额取自其跟踪的标的指数（新浪财经 / 同花顺 / 东方财富实时行情）；中证主题指数无公开免费 K 线，技术指标改用该场外基金真实单位净值序列计算。"],
    ["持有收益计算", "份额＝买入金额÷成交净值；持有成本＝Σ买入金额−Σ卖出金额；持有市值＝持有份额×最新净值；"
     "持有收益＝市值−成本；收益率＝收益÷成本；首次买入至今涨跌幅＝最新净值÷首次买入日净值−1。"],
    ["更新规则", "自选清单（增删与排序）、交易明细保存在本机浏览器；编辑后于次日 14:00（Asia/Shanghai）"
     "随定时任务重新采集净值与技术面后更新展示，周末与节假日顺延至下一交易日。"],
]


def r(v, n=2, default=None):
    if v is None:
        return default
    try:
        return round(float(v), n)
    except Exception:
        return default


def pct(v):
    return "—" if v is None else ("+" if v >= 0 else "") + "%0.2f%%" % v


def build_fund(code, f):
    idx = f.get("index") or {}
    q = f.get("realtime") or {}
    nav = f.get("nav") or {}
    tech = f.get("tech") or {}
    boll = tech.get("boll") or {}
    macd = tech.get("macd") or {}
    val = tech.get("valuation") or {}
    trend = tech.get("trend") or {}
    zone = tech.get("zone") or {}
    out = f.get("outlook") or {}

    # 卡片 5 大主展示项
    main = [
        {"k": "实时指数", "v": ("%0.2f" % q["point"]) if q.get("point") is not None else "—",
         "sub": idx.get("short") or "标的指数", "tone": ("up" if (q.get("chgPct") or 0) >= 0 else "down")},
        {"k": "涨幅估值", "v": pct(f.get("estChg")),
         "sub": ("已确认净值 %s" % pct(nav.get("dailyChg"))) if nav else "盘中估算",
         "tone": ("up" if (f.get("estChg") or 0) >= 0 else "down")},
        {"k": "成交量", "v": q.get("amountText") or "—",
         "sub": q.get("volumeText") or "跟踪指数", "tone": "neutral"},
        {"k": "估值高低", "v": val.get("label") or "—",
         "sub": ("近250日分位 %s%%" % r(val.get("percentile"), 1, "—")), "tone": val.get("tone") or "neutral"},
        {"k": "未来走势", "v": out.get("label") or "—",
         "sub": "技术面综合研判", "tone": out.get("tone") or "neutral"},
    ]

    # 技术面三段（趋势 / 区域 / 动能）：卡片用短标签，完整数值留页尾灰字依据
    macd_state = ("DIF 在 DEA 上方" if macd.get("difUp") else "DIF 在 DEA 下方")
    tech_cards = [
        {"k": "当前趋势", "v": trend.get("label") or "—", "tone": trend.get("tone") or "neutral",
         "note": trend.get("short") or trend.get("detail") or ""},
        {"k": "所处区域", "v": zone.get("label") or "—", "tone": zone.get("tone") or "neutral",
         "note": zone.get("short") or zone.get("detail") or ""},
        {"k": "MACD 动能", "v": ("柱体转强" if macd.get("histUp") else "柱体走弱"),
         "tone": ("up" if macd.get("difUp") else "down"),
         "note": "%s%s" % (macd_state, "（%s）" % macd.get("cross") if macd.get("cross") and macd.get("cross") != "无" else "")},
    ]

    return {
        "code": code,
        "name": f.get("name") or code,
        "category": f.get("category") or "指数型",
        "indexName": idx.get("name") or "—",
        "indexShort": idx.get("short") or "—",
        "indexKnown": f.get("indexKnown", False),
        "nav": nav,
        "navSeries": f.get("navSeries") or [],
        "realtime": q,
        "estChg": f.get("estChg"),
        "main": main,
        "valuation": val,
        "tech": {
            "boll": boll, "macd": macd, "trend": trend, "zone": zone,
            "ma5": tech.get("ma5"), "ma20": tech.get("ma20"), "ma60": tech.get("ma60"),
            "pricePercentile": tech.get("pricePercentile"),
            "slope20": tech.get("slope20"),
            "techCards": tech_cards,
        },
        "outlook": out,
        "chart": f.get("chart"),
        "basis": f.get("basis") or [],
        "dataNote": f.get("dataNote") or "",
        "sources": f.get("sources") or [],
        "degraded": f.get("degraded") or [],
    }


def build_today(funds, cards):
    ups = [f for f in funds.values() if (f.get("realtime") or {}).get("chgPct", 0) >= 0]
    best = max(funds.values(), key=lambda f: (f.get("realtime") or {}).get("chgPct") or -99)
    q = best.get("realtime") or {}
    trends = {}
    for f in funds.values():
        t = ((f.get("tech") or {}).get("trend") or {}).get("label") or "震荡整理"
        trends[t] = trends.get(t, 0) + 1
    top_trend = max(trends.items(), key=lambda kv: kv[1])[0] if trends else "震荡整理"
    return ("当日 %d/%d 只跟踪指数收涨，%s 领涨（%s）；但技术面看有 %d 只仍处「%s」，"
            "短涨不改中期位置。定投照常、首付池不动，别因单日普涨去追主题基金。" %
            (len(ups), len(funds), best["name"][:12], pct(q.get("chgPct")),
             trends.get(top_trend, 0), top_trend))


def render_push(briefing):
    M, F, C = briefing["meta"], briefing["funds"], briefing["marketCards"]
    lines = ["# 指数跟踪 · 每日推送（%s %s）" % (M["date"], M["weekday"]), ""]
    lines.append("> %s 更新 · 数据基准 %s 收盘" % (M["updateTime"], M["lastTradeDate"]))
    lines.append("")
    lines.append("## 大盘")
    for c in C:
        lines.append("- %s %s %s" % (c["name"], ("%0.2f" % c["point"]) if c.get("point") else "—",
                                    pct(c.get("chgPct"))))
    lines.append("")
    lines.append("## 今日一句话")
    lines.append(M["todayAction"])
    lines.append("")
    lines.append("## 自选指数基金（%d 只，按自选顺序）" % len(F))
    lines.append("")
    lines.append("| 基金 | 代码 | 实时指数 | 估算涨幅 | 成交量 | 估值 | 趋势 | 区域 | 走势预估 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for f in F:
        q = f["realtime"] or {}
        t, z = f["tech"]["trend"], f["tech"]["zone"]
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            f["name"][:16], f["code"],
            ("%0.2f" % q["point"]) if q.get("point") is not None else "—",
            pct(f["estChg"]), q.get("amountText") or "—",
            f["valuation"].get("label") if f["valuation"] else "—",
            t.get("label", "—"), z.get("label", "—"),
            (f["outlook"] or {}).get("label", "—")))
    lines.append("")
    lines.append("> 场外指数基金以基金公司公布净值为准；表中「实时指数/成交量」为跟踪标的指数行情。")
    lines.append("> 持有收益、收益率、首次买入至今涨跌幅：在界面录入交易明细后，次日 14:00 随净值更新展示。")
    lines.append("")
    lines.append("---")
    lines.append("%s" % briefing["disclaimer"])
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--no-html", action="store_true")
    args = ap.parse_args()

    snap = json.loads((DATA / "fund-snapshot.json").read_text("utf-8"))
    sm = snap.get("meta") or {}
    d = args.date
    dt = datetime.strptime(d, "%Y-%m-%d")

    funds_map = snap.get("funds") or {}
    codes = sm.get("watchlist") or list(funds_map.keys())
    funds = [build_fund(c, funds_map[c]) for c in codes if c in funds_map]

    channels = [
        {"short": "东方财富", "status": "ok"},
        {"short": "天天基金", "status": "ok"},
        {"short": "净值/K线", "status": "ok"},
        {"short": "技术面", "status": "ok"},
        {"short": "交易明细", "status": "partial"},
        {"short": "本机存储", "status": "ok"},
    ]
    trace = [
        {"provider": "新浪财经 / 同花顺", "endpoint": "指数与大盘实时行情", "note": "标的指数点位/涨跌/成交量/成交额 + 大盘三指数"},
        {"provider": "新浪财经 / 天天基金", "endpoint": "指数日K / 基金净值 250根", "note": "BOLL / MACD / 均线 / 250日分位计算输入（中证主题指数用真实净值序列）"},
        {"provider": "天天基金", "endpoint": "FundSearchAPI", "note": "基金名称与类型逐只校验"},
        {"provider": "天天基金", "endpoint": "pingzhongdata + f10/lsjz", "note": "历史单位净值 260 条，校准净值日期"},
        {"provider": "本机浏览器", "endpoint": "localStorage", "note": "自选清单顺序、增删与交易明细；次日 14:00 随任务更新"},
    ]

    briefing = {
        "meta": {
            "date": d, "weekday": WEEKDAY[dt.weekday()],
            "updateTime": "14:00", "dataTime": "%s 收盘" % (sm.get("lastTradeDate") or d),
            "lastTradeDate": sm.get("lastTradeDate") or d,
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dataSource": DATA_SOURCE,
            "watchlist": codes,
            "fundCount": len(funds),
            "todayAction": build_today(funds_map, snap.get("marketCards") or []),
            "promptVersion": "v1 · 指数跟踪版",
            "marketStatus": "收盘后" if datetime.now().hour >= 15 else "盘中",
        },
        "legend": LEGEND,
        "marketCards": snap.get("marketCards") or [],
        "funds": funds,
        "basisRules": [{"k": k, "v": v} for k, v in BASIS_RULES],
        "channels": channels,
        "trace": trace,
        "commonIndex": snap.get("commonIndex") or {},
        "keywordIndex": snap.get("keywordIndex") or [],
        "disclaimer": DISCLAIMER,
        "footSource": DATA_SOURCE,
    }

    (DATA / ("fund-briefing-%s.json" % d)).write_text(
        json.dumps(briefing, ensure_ascii=False, indent=1), "utf-8")
    (DATA / "fund-briefing-latest.json").write_text(
        json.dumps(briefing, ensure_ascii=False, indent=1), "utf-8")
    push = render_push(briefing)
    (DATA / ("push-%s.md" % d)).write_text(push, "utf-8")

    # 注入 index.html
    if not args.no_html:
        html_path = ROOT / "index.html"
        html = html_path.read_text("utf-8")
        payload = json.dumps(briefing, ensure_ascii=False, separators=(",", ":"))
        # 避免 </script> 提前闭合
        payload = payload.replace("</", "<\\/")
        new, n = re.subn(
            r'(<script id="briefing-data" type="application/json">)(.*?)(</script>)',
            lambda m: m.group(1) + payload + m.group(3), html, count=1, flags=re.S)
        if n != 1:
            raise RuntimeError("未找到 briefing-data 数据块，注入失败")
        html_path.write_text(new, "utf-8")
        print("已注入 index.html（%d 字节数据）" % len(payload))
    print("简报生成完成：%d 只基金 / %s" % (len(funds), d))


if __name__ == "__main__":
    main()
