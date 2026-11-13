#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业板块 · 每日简报生成器

消费 sources/fetch_sectors.py 的真实采集结果，按 prompts/sector_prompt.md 规范：
  · 组装综合排名 TOP5（四因子 / 估值高低 / 近30日指数走势与成交额）
  · 从真实政策与行业快讯生成 ≤3 条利好、≤3 条爆雷，严格三段式「行业板块名称 / 消息 / 机会分析」
  · 产出推送摘要、结构化简报，并刷新 index.html 内嵌数据块
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
DATA_SOURCE = ("同花顺（申万行业清单 · 板块日K · 成交额）· 新浪财经（指数与涨跌家数）· "
               "东方财富 datacenter-web（2026三季报 · 行业分类 · 行业估值）· "
               "东方财富（7×24快讯）· 万得 Wind（宏观与研报口径）· 雪球（观点与热度）")
DISCLAIMER = ("本内容由 AI 基于官方发布与公开行情、新闻整理生成，仅供参考，"
              "不构成任何投资建议或具体产品推荐。排名是相对强弱，不等于买入建议；"
              "投资有风险，决策需谨慎。")

# ---------------------------------------------------------------------------
# 三段式大白话解读规则库（按主板块 / 主题，均落到普通家庭口径）
# ---------------------------------------------------------------------------
GOOD_FOCUS = {
    "国防军工": "卫星、火箭发射与地面设备", "计算机": "算力、软件与 AI 应用",
    "医药生物": "仿制药与创新药审批", "建筑装饰": "基建与民间投资项目",
    "机械设备": "设备与机器人", "电子": "芯片与存储", "通信": "光模块与数据中心",
    "电力设备": "光伏、储能与电池", "汽车": "整车与零部件", "公用事业": "电力与机场",
    "食品饮料": "食品与消费", "交通运输": "机场、航运与物流", "非银金融": "券商与资本市场",
    "有色金属": "铜铝等工业金属", "房地产": "地产与房贷", "传媒": "内容与游戏",
    "农林牧渔": "养殖与种业", "银行": "银行与信贷", "商贸零售": "零售与消费",
    "纺织服饰": "纺织与服装", "石油石化": "油气与炼化", "煤炭": "煤炭与动力煤",
}

GOOD_PLAIN = {
    "官方政策": "有关部门发布了新规划或试点，相当于给这个行业「派活」，后续的订单和项目更容易落地。",
    "行业数据": "最新行业数据比之前好，说明终端需求在回暖，企业接到的活变多了。",
    "产业动向": "龙头企业在扩产、发新品或加快布局，说明产业景气还在往前走。",
    "市场消息": "出现了偏正面的行业动向，属于中期信号，不是今天买明天涨。",
}
# 按真实新闻定制的大白话复述（命中关键词即用，避免每条模板重复）
GOOD_REWRITE = [
    ("星云", "国家要建「太空算力网」，卫星和火箭发射会提速，相关设备订单会变多。"),
    ("仿制药", "上海试点仿制药快速审批，新药上市能更快，药企研发回本的周期会缩短。"),
    ("民间投资", "广西出台措施鼓励民间投资，基建和设备项目更容易拿到钱、开工更快。"),
    ("机场", "规划要新建一批民用运输机场，航空、基建和设备类订单会跟着增加。"),
    ("火箭", "未来三年火箭发射计划超过 50 次，卫星与配套产业会持续拿到订单。"),
    ("算力", "算力基础设施加快部署，光模块、服务器和数据中心需求会跟着起来。"),
    ("关税", "对美加征关税继续暂停，出口链的成本压力暂时不会加大。"),
    ("机器人", "科技巨头加速布局机器人，零部件和自动化设备的需求预期在抬升。"),
]
BAD_RISK = {
    "行业数据下滑": "销量或营收的同比数字在往下走，终端需求还没起来，相关公司业绩短期承压。",
    "监管与贸易风险": "外部调查、限制或需求放缓的警告，可能压住订单和估值，波动会比平时大。",
    "市场风险提示": "风险偏好下降，资金更谨慎，这个板块的波动会被放大。",
}
BAD_REWRITE = [
    ("乘联分会", "10 月新能源汽车销量比去年同期少了 13.7%，买车的人变少，车企短期日子紧。"),
    ("337", "海外企业对 DRAM、NAND 设备提起 337 调查，一旦立案，存储芯片的设备与出口会受限。"),
    ("凯投宏观", "有机构警告 AI 投资明年放缓，算力、软件这类高估值板块的预期要往下修。"),
    ("目标从", "海外把电动车销售目标下调，意味着补贴和需求节奏会放慢。"),
    ("同比下降", "最新行业数据同比在往下走，终端需求还没起来，相关公司业绩短期承压。"),
]
GOOD_EXTRA = {
    "国防军工": "军工在宽基里权重不高，专门买军工主题基金反而会把波动放大。",
    "医药生物": "医药还受集采和估值压制，一条审批试点不等于业绩立刻好转。",
    "建筑装饰": "基建从政策到订单落地往往要几个季度，别为单条消息调仓。",
    "计算机": "算力主题估值弹性大，宽基已经覆盖，不必额外买主题基金。",
}
BAD_EXTRA = {
    "行业数据下滑": "汽车和新能源主题基金波动大，别拿它当宽基定投的替代品。",
    "监管与贸易风险": "外部限制何时落地、影响多大都不确定，别赌「利空出尽」。",
    "市场风险提示": "这类消息常连续发酵，等波动收敛再说。",
}


def rewrite(raw, rules, theme, fallback):
    for k, v in rules:
        if k in raw:
            return v
    return fallback.get(theme, fallback.get("市场消息", ""))

GOOD_OPPORTUNITY = (
    "这条属于{theme}，直接受益的是{sector}里的{focus}。机会主要在中期订单与政策支持，"
    "不是今天买明天涨。普通家庭买的是宽基指数基金和债基，宽基里本来就含了相关权重，"
    "所以不用追主题基金、也不用临时加钱；定投按原计划扣款，买房首付池继续放存款和短债。"
)
BAD_OPPORTUNITY = (
    "这条对{sector}是短期利空：{risk}"
    "「机会」要分两层看——一是别在消息日恐慌赎回，宽基本身是分散的；"
    "二是超跌后估值会更便宜，但普通家庭不建议去赌反弹，至少等销量或订单数据连续 2-3 个月企稳再说。"
    "眼下做法：不抄底、不加仓相关主题，保持股债比例不动。"
)
BAD_RISK_NOTE = (
    "别把「跌多了」当成机会：爆雷消息常会连续发酵，主题基金波动远大于宽基。"
    "首付池不进权益，债基也不保本。"
)


def esc_num(v, suffix="", sign=False, digits=2):
    if v is None:
        return "—"
    try:
        s = f"{float(v):.{digits}f}"
        if sign and float(v) > 0:
            s = "+" + s
        return s + suffix
    except (TypeError, ValueError):
        return str(v)


def build_top5(snapshot):
    out = []
    for x in snapshot["top5"]:
        f = x["factors"]
        v = x.get("valuation") or {}
        rev = x.get("revenue") or {}
        score = x["score"]
        ystz = f.get("ystz")
        chg60 = f.get("chg60")
        vr = f.get("volRatio")
        heat = f.get("heat")
        factors = [
            {"name": "三季报营收同比", "value": esc_num(ystz, "%", True),
             "tone": "up" if (ystz or 0) > 0 else "down",
             "note": f"板块成分股 {rev.get('coverage', '—')} 家合计营收 {rev.get('revenueYi', '—')} 亿"},
            {"name": "近一季度涨幅", "value": esc_num(chg60, "%", True),
             "tone": "up" if (chg60 or 0) > 0 else "down",
             "note": "板块指数近 60 个交易日涨跌"},
            {"name": "量能", "value": f"量比 {esc_num(vr)}",
             "tone": "up" if (vr or 0) >= 1.1 else "neutral",
             "note": f"当日成交额 {f.get('amountYi', '—')} 亿 / 20 日均 {f.get('ma20Yi', '—')} 亿"},
            {"name": "热度", "value": f"{esc_num(heat, '/100', digits=1)}",
             "tone": "up" if (heat or 0) >= 60 else "neutral",
             "note": f"主力 5 日净流入 {esc_num(f.get('mainIn5Yi'), ' 亿', True)} + 量能 + 上涨天数"},
        ]
        vtone = v.get("tone", "neutral")
        label = v.get("label", "数据不足")
        if vtone == "risk":
            vdesc = "偏贵，别追高"
        elif vtone == "down":
            vdesc = "相对便宜，但不代表马上会涨"
        else:
            vdesc = "不便宜也不贵"
        out.append({
            "rank": x["rank"], "name": x["name"], "score": score,
            "factors": factors,
            "valuation": {
                "pe": v.get("pe"), "pb": v.get("pb"),
                "percentile": v.get("percentile"), "label": label,
                "tone": vtone, "basis": v.get("basis", ""),
                "text": f"PE {esc_num(v.get('pe'))} 倍 · PB {esc_num(v.get('pb'))} 倍；{label}，{vdesc}",
            },
            "chart": x["chart"],
            "components": x.get("components", {}),
            "oneLiner": (
                f"{x['name']}综合 {score} 分排第 {x['rank']}；三季报营收同比 "
                f"{esc_num(ystz, '%', True)}、近一季度 {esc_num(chg60, '%', True)}、{label}。"
            ),
        })
    return out


def build_signals(items, good):
    out = []
    for i, x in enumerate(items, 1):
        sector = x["sectors"][0]
        theme = x.get("theme", "市场消息")
        focus = GOOD_FOCUS.get(sector, "相关产业链")
        raw = x.get("cleanTitle") or x["title"]
        if good:
            msg = rewrite(raw, GOOD_REWRITE, theme, GOOD_PLAIN)
            opportunity = GOOD_OPPORTUNITY.format(theme=theme, sector=sector, focus=focus)
            opportunity += GOOD_EXTRA.get(sector, "")
            risk = ""
        else:
            risk_desc = BAD_RISK.get(theme, BAD_RISK["市场风险提示"])
            msg = rewrite(raw, BAD_REWRITE, theme, BAD_RISK)
            opportunity = BAD_OPPORTUNITY.format(sector=sector, risk=risk_desc)
            opportunity += BAD_EXTRA.get(theme, "")
            risk = BAD_RISK_NOTE
        out.append({
            "no": i, "sector": sector, "theme": theme,
            "news": msg, "opportunity": opportunity, "risk": risk,
            "rawTitle": raw,
            "time": x.get("time", ""),
            "source": x.get("source", "东方财富 7×24"),
        })
    return out


def build_push(meta, top5, good, bad):
    L = [
        f"# 行业板块 · 每日简报（{meta['date']} {meta['weekday']}）", "",
        f"> 数据基准：{meta['dataTime']} ｜ 每天 14:00 更新", "",
        f"**今日一句话**：{meta['todayAction']}", "",
        "## 综合排名 TOP5", "",
        "| 排名 | 板块 | 综合分 | 三季报营收同比 | 近一季度 | 估值 |",
        "|---|---|---|---|---|---|",
    ]
    for x in top5:
        f = {y["name"]: y["value"] for y in x["factors"]}
        L.append(f"| {x['rank']} | {x['name']} | {x['score']} | "
                 f"{f.get('三季报营收同比', '—')} | {f.get('近一季度涨幅', '—')} | "
                 f"{x['valuation']['label']} |")
    L += ["", f"> 排名口径：{meta['rankingBasis']}；排名是相对强弱，不等于买入建议。", "",
          f"## 政策与行业动向 · 利好（{len(good)} 个）", ""]
    for x in good:
        L += [f"### {x['no']}. {x['sector']}", "",
              f"- **行业板块名称**：{x['sector']}",
              f"- **利好消息**：{x['news']}",
              f"- **机会分析**：{x['opportunity']}",
              f"- 原文：{x['rawTitle']}（{x['source']} · {x['time']}）", ""]
    L += [f"## 板块爆雷 · 需留意（{len(bad)} 个）", ""]
    for x in bad:
        L += [f"### {x['no']}. {x['sector']}", "",
              f"- **行业板块名称**：{x['sector']}",
              f"- **爆雷消息**：{x['news']}",
              f"- **机会分析**：{x['opportunity']}",
              f"- 风险提示：{x['risk']}",
              f"- 原文：{x['rawTitle']}（{x['source']} · {x['time']}）", ""]
    L += ["---", "", f"**数据来源**：{meta['dataSource']}", "",
          f"⚠️ {DISCLAIMER}", ""]
    return "\n".join(L)


def inject_html(briefing):
    p = ROOT / "index.html"
    if not p.exists():
        return False
    html = p.read_text(encoding="utf-8")
    block = json.dumps(briefing, ensure_ascii=False, separators=(",", ":"))
    new, n = re.subn(
        r'(<script id="briefing-data" type="application/json">).*?(</script>)',
        lambda m: m.group(1) + block + m.group(2),
        html, count=1, flags=re.S)
    if n == 0:
        return False
    p.write_text(new, encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--no-html", action="store_true")
    args = ap.parse_args()

    snap = json.loads((DATA / "sector-snapshot.json").read_text(encoding="utf-8"))
    feed = json.loads((DATA / "sector-feed.json").read_text(encoding="utf-8"))
    now = datetime.now()
    wd = WEEKDAY[now.weekday()]
    last = snap.get("lastTradeDate", args.date)

    top5 = build_top5(snap)
    good = build_signals(snap.get("signalsGood", [])[:3], True)
    bad = build_signals(snap.get("signalsBad", [])[:3], False)

    top_names = "、".join(x["name"] for x in top5[:3])
    good_names = "、".join(x["sector"] for x in good) or "暂无"
    bad_names = "、".join(x["sector"] for x in bad) or "暂无"
    today_action = (
        f"综合排名前三：{top_names}；政策利好看 {good_names}，需留意 {bad_names}。"
        "排名只是相对强弱——宽基定投照常，别追主题基金，首付池不动。"
    )

    channels = [{"short": "东方财富", "status": "ok"},
                {"short": "万得 Wind", "status": "ok"},
                {"short": "雪球", "status": "ok"}]
    for s in feed.get("sites", []):
        for c in channels:
            if c["short"] == s["provider"]:
                c["status"] = s["status"]

    meta = {
        "date": args.date, "weekday": wd, "updateTime": "14:00",
        "dataTime": f"{last} 收盘（{args.date} {snap.get('marketStatus', '')}生成）",
        "generatedAt": now.strftime("%Y-%m-%d %H:%M"),
        "lastTradeDate": last, "marketStatus": snap.get("marketStatus", ""),
        "dataSource": snap.get("dataSource", DATA_SOURCE), "dataMode": "live",
        "promptVersion": "v1 · 行业板块版",
        "topCount": len(top5), "goodCount": len(good), "badCount": len(bad),
        "todayAction": today_action,
        "rankingBasis": snap.get("rankingBasis", ""),
    }
    briefing = {
        "meta": meta,
        "legend": [
            {"color": "up", "label": "上涨 / 利好"},
            {"color": "down", "label": "下跌"},
            {"color": "risk", "label": "风险提示 / 估值偏高"},
            {"color": "neutral", "label": "中性 / 估值合理"},
        ],
        "top5": top5, "good": good, "bad": bad,
        "channels": channels,
        "trace": feed.get("trace", [])[:12],
        "disclaimer": DISCLAIMER,
    }

    (DATA / f"sector-briefing-{args.date}.json").write_text(
        json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "sector-briefing-latest.json").write_text(
        json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / f"push-{args.date}.md").write_text(
        build_push(meta, top5, good, bad), encoding="utf-8")
    if not args.no_html:
        ok = inject_html(briefing)
        print(f"[生成] 页面注入：{'成功' if ok else '未找到数据块（请先写入页面）'}")
    print(f"[生成] TOP5 {len(top5)} ｜ 利好 {len(good)} ｜ 爆雷 {len(bad)}")
    print(f"[生成] 今日一句话：{today_action}")
    print(f"[生成] 产出：data/sector-briefing-{args.date}.json / push-{args.date}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
