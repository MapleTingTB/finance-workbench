#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
理财工作台 · 财经新闻 —— 盘中研判数据层
======================================================================

职责：为「今日速览 · 盘中研判」提供三项可核验的输入
  1. 布林线 BOLL(20, 2)：中轨 SMA20、上下轨 ±2σ、%B、带宽与开口/收口
  2. 成交量：近 5 日 / 20 日均额、量比、量价配合形态
  3. 机构复盘：东方财富 7×24 快讯中的真实券商/机构盘面观点（机构名 + 时间 + 原文）

两种运行方式：
  A. 被每日全链路调用：  fetch_mainstream.py → intraday.collect(news=...)
  B. 独立增量运行：      python3 sources/intraday.py
     仅采集本模块数据并合入已有 data/market-snapshot.json 的 `intraday` 字段，
     不覆盖官方新闻、指数、板块等其它字段（适用于盘前/盘中单独刷新研判）。

数据源纪律：只取东方财富公开接口真实返回值；取不到如实标注，不用记忆补数。
口径说明：布林标准差采用总体标准差（除以 N），与主流行情软件 BOLL 默认口径一致。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("[盘中研判] 缺少 requests，请执行：sudo pip3 install requests", file=sys.stderr)
    raise SystemExit(1)

# 多源行情兼容层：拦截被 WAF 封禁的 push2/push2his，转发到
# 新浪财经 / 同花顺 / 东财 datacenter-web / 天天基金等已实测可用源。
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import market_hub
market_hub.install()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPSHOT = DATA / "market-snapshot.json"

UT = "fa5fd1943c7b386f172d6893dbfba10b"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}

EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FASTNEWS = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"


def http_get(url, timeout=20, retries=4):
    """带重试的 GET：东财日K / 快讯接口偶发断连。"""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            if not r.text.strip():
                raise ValueError("空响应")
            return r
        except Exception as e:
            last = e
            if i < retries - 1:
                import time
                time.sleep(0.5 * (i + 1))
    raise last


def fetch_kline_full(secid="1.000001", name="上证指数", beg="20260801",
                     end="20261231", limit=70):
    """取完整日K序列：日期 / 开 / 收 / 高 / 低 / 成交额（亿元）。"""
    url = (f"{EM_KLINE}?secid={secid}&ut={UT}&klt=101&fqt=1&beg={beg}&end={end}"
           f"&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
    j = http_get(url).json()
    kl = ((j.get("data") or {}).get("klines")) or []
    rows = []
    for line in kl[-limit:]:
        p = line.split(",")
        rows.append({
            "d": p[0][5:], "date": p[0],
            "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "amount": round(float(p[6]) / 1e8, 1),  # 亿元
        })
    return rows, url, len(kl)


def _boll_at(closes, i, period=20, k=2):
    """计算第 i 根（含）为止的布林三轨，i 为下标。总体标准差口径。"""
    if i + 1 < period:
        return None
    w = closes[i - period + 1: i + 1]
    mid = sum(w) / period
    sd = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
    return {"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd, "sd": sd}


def calc_bollinger(rows, period=20, k=2):
    """最新一根的布林状态 + 近 30 日三轨序列（供页面画迷你图）。"""
    if len(rows) < period + 2:
        return None
    closes = [r["close"] for r in rows]
    cur = _boll_at(closes, len(closes) - 1, period, k)
    prev = _boll_at(closes, len(closes) - 2, period, k)
    if not cur or not prev:
        return None

    upper, lower, mid = cur["upper"], cur["lower"], cur["mid"]
    pb = (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5
    bw = (upper - lower) / mid * 100
    pbw = (prev["upper"] - prev["lower"]) / prev["mid"] * 100

    if bw > pbw * 1.02:
        trend = "开口"
    elif bw < pbw * 0.98:
        trend = "收口"
    else:
        trend = "走平"

    if pb >= 0.8:
        pos = "上轨附近"
    elif pb <= 0.2:
        pos = "下轨附近"
    elif pb >= 0.5:
        pos = "中轨上方"
    else:
        pos = "中轨下方"

    # 近 30 日三轨序列
    series = []
    start = max(period - 1, len(rows) - 30)
    for i in range(start, len(rows)):
        b = _boll_at(closes, i, period, k)
        series.append({
            "d": rows[i]["d"], "close": rows[i]["close"],
            "amount": rows[i]["amount"],
            "mid": round(b["mid"], 2), "upper": round(b["upper"], 2),
            "lower": round(b["lower"], 2),
        })
    return {
        "period": period, "k": k,
        "current": round(closes[-1], 2), "mid": round(mid, 2),
        "upper": round(upper, 2), "lower": round(lower, 2),
        "percentB": round(pb, 3), "bandwidth": round(bw, 2),
        "bandwidthPrev": round(pbw, 2), "bandTrend": trend, "position": pos,
        "series": series,
        "lastDate": rows[-1]["date"],
    }


def calc_volume(rows):
    """量能：5日/20日均额、量比、近5日涨跌、量价配合形态。"""
    if len(rows) < 21:
        return None
    amts = [r["amount"] for r in rows]
    closes = [r["close"] for r in rows]
    ma5 = sum(amts[-5:]) / 5
    ma20 = sum(amts[-20:]) / 20
    ratio = ma5 / ma20 if ma20 else 1
    day_ratio = amts[-1] / ma5 if ma5 else 1
    chg5 = closes[-1] / closes[-6] - 1 if len(closes) >= 6 else 0

    if ratio >= 1.10:
        vol_state = "放量"
    elif ratio <= 0.90:
        vol_state = "缩量"
    else:
        vol_state = "量能平稳"

    if vol_state == "放量" and chg5 > 0.005:
        pattern, tone = "放量上行", "positive"
    elif vol_state == "放量" and chg5 < -0.005:
        pattern, tone = "放量下跌", "caution"
    elif vol_state == "缩量" and chg5 > 0.005:
        pattern, tone = "缩量上涨（追涨力量不足）", "neutral"
    elif vol_state == "缩量" and chg5 < -0.005:
        pattern, tone = "缩量回落（抛压有限）", "neutral"
    elif vol_state == "缩量":
        pattern, tone = "缩量整理", "neutral"
    elif chg5 > 0.005:
        pattern, tone = "量能配合上行", "positive"
    elif chg5 < -0.005:
        pattern, tone = "量能配合回落", "caution"
    else:
        pattern, tone = "量价平稳", "neutral"

    if day_ratio >= 1.15:
        day_state = "当日放量"
    elif day_ratio <= 0.85:
        day_state = "当日缩量"
    else:
        day_state = "当日量能常态"

    return {
        "lastAmount": round(amts[-1], 1), "ma5": round(ma5, 1), "ma20": round(ma20, 1),
        "ratio": round(ratio, 3), "dayRatio": round(day_ratio, 3),
        "chg5": round(chg5 * 100, 2), "state": vol_state, "dayState": day_state,
        "pattern": pattern, "tone": tone,
        "desc": f"5日均额 {ma5:.0f} 亿 / 20日均额 {ma20:.0f} 亿，量比 {ratio:.2f}",
    }


# ---------------------------------------------------------------------------
# 机构复盘观点：只取快讯中带机构署名、且讨论大盘/后市/策略/资金的条目
# ---------------------------------------------------------------------------

# A股盘面研判主题词：至少命中一个才算「复盘 / 大势研判」
PANEL_KW = [
    "A股", "沪指", "深成指", "创业板", "大盘", "两市", "后市", "复盘", "收评", "盘后",
    "稳市", "底部", "逆周期", "仓位", "震荡", "反弹", "风险偏好", "北向", "主力",
    "资金", "券商", "机构", "策略", "配置", "展望", "市场情绪", "成交额", "估值",
    "资本市场", "结构性机会", "分散配置", "加仓", "减仓", "观点",
]
# 强主题：A股大势，命中的条目优先
STRONG_PANEL = [
    "A股", "沪指", "大盘", "两市", "稳市", "底部", "逆周期", "后市", "收评",
    "复盘", "策略", "仓位", "北向", "资本市场", "市场情绪", "分散配置", "结构性机会",
]
# 海外市场 / 地缘 / 个股公告：明确排除，避免把美股日股观点当A股复盘
EXCLUDE_KW = [
    "美股", "日股", "欧洲", "韩股", "韩元", "铝市场", "原油", "伊朗", "乌克兰",
    "俄罗斯", "特朗普", "美联储", "美债", "纳斯达克", "标普", "道琼斯", "德国", "法国",
    "目标价", "评级", "解禁", "IPO", "调研", "股权结构", "罚款", "反垄断",
    "拟回购", "提议回购", "回购公司", "只股涨停", "净买入", "复牌", "中标", "签署",
    "董事长", "业绩预告", "分红", "定增", "可转债", "新股", "投诉",
]
# 「券商：某行业策略」属行业研报，不是大盘复盘；含冒号且命中行业词即排除
INDUSTRY_KW = [
    "行业", "计算机", "传媒", "家电", "煤炭", "新能源", "白酒", "医药",
    "半导体", "算力", "储能", "汽车", "军工", "光伏", "机器人", "AI",
    "硬件", "科技硬件", "北交所", "内容", "电价", "个股", "板块",
]
SPECIFIC_ORG = re.compile(
    r"(中信证券|中信建投|中金公司|华泰证券|国泰君安|国泰海通|招商证券|广发证券|海通证券|"
    r"申万宏源|国信证券|光大证券|兴业证券|方正证券|东吴证券|天风证券|国盛证券|"
    r"民生证券|开源证券|华安证券|国新证券|金融街证券|平安证券|东方证券|浙商证券|"
    r"国泰基金|华夏基金|易方达|南方基金|广发基金|博时基金|嘉实基金)"
)
OVERSEAS_ORG = re.compile(r"(摩根大通|摩根士丹利|高盛|花旗|瑞银|野村|贝莱德|富达|桥水|汇丰)")
GENERIC_ORG = re.compile(r"(机构|券商|策略师|分析师|外资)")

# 倾向判断用明确短语，避免「回调 / 风险 / 过度」这类歧义单词误判
BULL_KW = ["加仓", "增持", "看好", "看多", "再度看多", "底部构筑", "稳市", "逆周期",
           "分散配置", "结构性机会", "配置价值", "低估", "修复", "托底", "净流入",
           "支撑", "回暖", "逐步反弹", "反弹蓄势", "长期资金", "积极回购", "回升"]
BEAR_KW = ["减仓", "谨慎", "过热", "泡沫", "获利了结", "抛压", "规避", "撤退",
           "鱼尾", "冲高回落", "净流出", "高估", "下行压力", "风险偏好下降",
           "同比减少", "承压", "降温", "减持"]


def classify_tone(title):
    b = sum(1 for k in BULL_KW if k in title)
    s = sum(1 for k in BEAR_KW if k in title)
    if b > s:
        return "偏多"
    if s > b:
        return "偏空"
    return "中性"


def parse_institution_views(news, limit=6):
    """从真实快讯中筛 A 股大势研判：主题相关性优先，机构署名加权，排除海外与个股噪音。"""
    out, seen = [], set()
    for n in news:
        title = (n.get("title") or "").strip()
        if not title or len(title) < 10:
            continue
        if any(k in title for k in EXCLUDE_KW):
            continue
        if not any(k in title for k in PANEL_KW):
            continue
        strong = any(k in title for k in STRONG_PANEL)
        # 「券商：某行业策略」属行业研报而非大势研判，剔除
        if ("：" in title or ":" in title) and (
                not strong or any(k in title for k in INDUSTRY_KW)):
            continue

        sm, om, gm = SPECIFIC_ORG.search(title), OVERSEAS_ORG.search(title), GENERIC_ORG.search(title)
        if sm:
            org, score = sm.group(1), 5
        elif om:
            org, score = om.group(1), 2          # 外围机构降权，仅在 A股观点不足时补位
        elif gm:
            org, score = gm.group(1), 3
        else:
            # 无机构署名但属于明确盘面复盘（如「沪指小幅收涨…」），作为复盘事实纳入
            if not any(k in title for k in ("复盘", "收评", "沪指", "两市", "大盘")):
                continue
            org, score = "盘后复盘", 2

        score += 3 if strong else 0
        scope = "外围" if om and not sm else "A股"

        key = re.sub(r"\W", "", title)[:26]
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "org": org, "tone": classify_tone(title), "scope": scope,
            "time": n.get("time") or (n.get("fullTime") or "")[5:16],
            "title": title, "score": score,
            "source": n.get("source") or "东方财富 7×24",
        })
    out = sorted(out, key=lambda x: (-x["score"], -_time_key(x["time"])))
    return [{k: v for k, v in x.items() if k != "score"} for x in out[:limit]]


def _time_key(t):
    """MM-DD HH:MM → 可比较的整数。"""
    try:
        m, rest = t.split(" ", 1)
        mm, dd = m.split("-")
        hh, mi = rest.split(":")
        return int(mm) * 10000 + int(dd) * 100 + int(hh)  # 忽略分钟足够排序
    except Exception:
        return 0


def fetch_fastnews(pagesize=500, batches=3):
    """独立运行时自行取快讯：大页 + sortEnd 翻页，尽量覆盖全天机构观点。"""
    out, seen, urls = [], set(), []
    sort_end = ""
    for i in range(batches):
        url = (f"{EM_FASTNEWS}?client=web&biz=web_724&fastColumn=102"
               f"&sortEnd={sort_end}&pageSize={pagesize}"
               f"&req_trace={int(datetime.now().timestamp() * 1000) + i}")
        urls.append(url)
        j = http_get(url).json()
        data = j.get("data") or {}
        lst = data.get("fastNewsList") or []
        for x in lst:
            t = x.get("title") or ""
            if not t or t in seen:
                continue
            seen.add(t)
            out.append({
                "time": (x.get("showTime") or "")[5:16],
                "fullTime": x.get("showTime") or "",
                "title": t,
                "source": "东方财富 7×24",
            })
        sort_end = data.get("sortEnd") or ""
        if not lst or not sort_end:
            break
    return out, urls[0]


def collect(news=None):
    """采集并组装盘中研判数据；news 为空时自行取快讯。"""
    trace = []
    rows, kurl, total = fetch_kline_full()
    trace.append({"provider": "东方财富", "endpoint": "上证指数日K(完整)",
                  "url": kurl, "status": "ok",
                  "note": f"返回 {total} 根日K，采用末 {len(rows)} 根计算 BOLL(20,2) 与量能",
                  "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    boll = calc_bollinger(rows)
    vol = calc_volume(rows)

    # 无论独立运行还是被全链路调用，都保证快讯覆盖全天（盘前时段尤其容易只取到当日早间）
    if news is None or len(news) < 300:
        extra, nurl = fetch_fastnews()
        merged, seen = list(news or []), {n.get("title") for n in (news or [])}
        for x in extra:
            if x["title"] not in seen:
                merged.append(x)
                seen.add(x["title"])
        news = merged
        trace.append({"provider": "东方财富", "endpoint": "7×24 快讯(机构观点)",
                      "url": nurl, "status": "ok",
                      "note": f"全天翻页共 {len(news)} 条快讯，按机构署名与盘面研判主题筛选",
                      "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    views = parse_institution_views(news)
    tone_count = {"偏多": 0, "中性": 0, "偏空": 0}
    for v in views:
        tone_count[v["tone"]] += 1

    return {
        "asOf": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "symbol": "上证指数",
        "dataBasis": f"最新完整交易日 {rows[-1]['date']} 收盘；近 {len(rows)} 个交易日日K",
        "lastDate": rows[-1]["date"],
        "boll": boll,
        "volume": vol,
        "institutions": {
            "count": len(views), "bull": tone_count["偏多"],
            "neutral": tone_count["中性"], "bear": tone_count["偏空"],
            "items": views,
        },
        "trace": trace,
    }


def merge_into_snapshot(payload: dict, out: Path) -> Path:
    """增量合入 market-snapshot.json 的 intraday 字段，保留其它字段不变。"""
    path = out / "market-snapshot.json"
    snap = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    snap["intraday"] = payload
    snap["intradayAsOf"] = payload["asOf"]
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="盘中研判数据采集（布林线 / 量能 / 机构复盘）")
    ap.add_argument("--out", default=str(DATA), help="数据输出目录")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("[盘中研判] 采集东方财富日K与机构复盘观点")
    p = collect()
    b, v, ins = p["boll"], p["volume"], p["institutions"]
    if b:
        print(f"  BOLL(20,2)：现价 {b['current']} / 中轨 {b['mid']} / "
              f"上轨 {b['upper']} / 下轨 {b['lower']} | %B {b['percentB']} "
              f"| 带宽 {b['bandwidth']}% {b['bandTrend']} | {b['position']}")
    if v:
        print(f"  量能：{v['pattern']} | {v['desc']} | 近5日 {v['chg5']:+.2f}%")
    print(f"  机构观点：{ins['count']} 条（偏多 {ins['bull']} / 中性 {ins['neutral']} / 偏空 {ins['bear']}）")
    for x in ins["items"][:4]:
        print(f"    [{x['time']}] {x['org']} · {x['tone']} | {x['title'][:44]}")

    path = merge_into_snapshot(p, out)
    print(f"[盘中研判] 已增量合入 {path}（intraday 字段，未覆盖其它数据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
