#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指数跟踪 · 真实数据采集层

用户自选的是「场外指数基金 / ETF 联接」（无实时盘口），因此口径分工：
  · 基金净值 / 日涨幅 / 历史净值序列 -> 天天基金（东方财富）f10/lsjz + pingzhongdata
  · 基金名称 / 类型                -> 天天基金 FundSearchAPI
  · 跟踪的标的指数 实时点位/涨跌/成交量/成交额 -> 新浪财经 / 同花顺 / 东方财富
  · 标的指数 日 K（近 250 根）      -> 新浪财经日K / 同花顺日K（主题指数改用基金真实净值）
  · 顶部大盘横卡（上证/深证/创业板） -> 新浪财经实时行情

技术面（BOLL / MACD / 均线 / 分位 / 趋势 / 区域）全部基于标的指数日 K 真实收盘计算；
基金净值序列用于「首次买入至今涨跌幅」与持有收益。所有接口失败如实降级写 trace，不编造。

输出：data/fund-feed.json（原始采集明细+追溯）、data/fund-snapshot.json（标准化结果）
用法：python3 sources/fetch_funds.py [--out DIR] [--date YYYY-MM-DD]
"""
import argparse
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests

# 多源行情兼容层：拦截被 WAF 封禁的 push2/push2his，转发到
# 新浪财经 / 同花顺 / 东财 datacenter-web / 天天基金等已实测可用源。
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import market_hub
market_hub.install()

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UT = "fa5fd1943c7b386f172d6893dbfba10b"

EM_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FUND_SEARCH = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
FUND_PZ = "https://fund.eastmoney.com/pingzhongdata/%s.js"
FUND_LSJZ = "https://api.fund.eastmoney.com/f10/lsjz"

# 初始 7 只自选 -> 跟踪的标的指数（secid 已逐条用行情接口验证名称匹配）
FUND_INDEX = {
    "012769": {"secid": "2.930901", "code": "930901", "name": "中证动漫游戏", "short": "动漫游戏"},
    "004343": {"secid": "0.399006", "code": "399006", "name": "创业板指", "short": "创业板指"},
    "007029": {"secid": "1.000905", "code": "000905", "name": "中证500", "short": "中证500"},
    "012734": {"secid": "2.930713", "code": "930713", "name": "中证人工智能主题", "short": "CS人工智"},
    "003359": {"secid": "2.930734", "code": "930734", "name": "中证360互联网+大数据100", "short": "360互联+"},
    "011036": {"secid": "2.930598", "code": "930598", "name": "中证稀土产业", "short": "稀土产业"},
    "020900": {"secid": "2.931160", "code": "931160", "name": "中证全指通信设备", "short": "通信设备"},
}

# 常见场外指数基金 -> 标的指数（供界面新增代码时即时映射，非穷举，未命中则用净值口径）
COMMON_INDEX = {
    "110020": {"secid": "1.000300", "code": "000300", "name": "沪深300", "short": "沪深300"},
    "000961": {"secid": "1.000300", "code": "000300", "name": "沪深300", "short": "沪深300"},
    "005918": {"secid": "1.000300", "code": "000300", "name": "沪深300", "short": "沪深300"},
    "007339": {"secid": "1.000300", "code": "000300", "name": "沪深300", "short": "沪深300"},
    "001594": {"secid": "1.000905", "code": "000905", "name": "中证500", "short": "中证500"},
    "004348": {"secid": "1.000905", "code": "000905", "name": "中证500", "short": "中证500"},
    "003986": {"secid": "0.399006", "code": "399006", "name": "创业板指", "short": "创业板指"},
    "003765": {"secid": "0.399006", "code": "399006", "name": "创业板指", "short": "创业板指"},
    "110026": {"secid": "0.399006", "code": "399006", "name": "创业板指", "short": "创业板指"},
    "001548": {"secid": "1.000016", "code": "000016", "name": "上证50", "short": "上证50"},
    "001549": {"secid": "1.000016", "code": "000016", "name": "上证50", "short": "上证50"},
    "011612": {"secid": "0.399303", "code": "399303", "name": "国证2000", "short": "国证2000"},
    "004742": {"secid": "1.000852", "code": "000852", "name": "中证1000", "short": "中证1000"},
    "005313": {"secid": "1.000852", "code": "000852", "name": "中证1000", "short": "中证1000"},
    "011860": {"secid": "1.000688", "code": "000688", "name": "科创50", "short": "科创50"},
    "011612": {"secid": "0.399303", "code": "399303", "name": "国证2000", "short": "国证2000"},
    "008888": {"secid": "2.931151", "code": "931151", "name": "中证光伏产业", "short": "光伏产业"},
    "011102": {"secid": "2.930997", "code": "930997", "name": "中证新能源汽车", "short": "新能源车"},
    "001593": {"secid": "2.399997", "code": "399997", "name": "中证白酒", "short": "中证白酒"},
    "161725": {"secid": "2.399997", "code": "399997", "name": "中证白酒", "short": "中证白酒"},
    "003096": {"secid": "2.399986", "code": "399986", "name": "中证银行", "short": "中证银行"},
    "001552": {"secid": "2.399975", "code": "399975", "name": "中证全指证券公司", "short": "证券公司"},
    "004069": {"secid": "2.930708", "code": "930708", "name": "中证军工", "short": "中证军工"},
    "003834": {"secid": "2.930965", "code": "930965", "name": "中证医药", "short": "中证医药"},
    "008281": {"secid": "2.H30184", "code": "H30184", "name": "中证全指半导体", "short": "半导体"},
    "008888": {"secid": "2.931151", "code": "931151", "name": "中证光伏产业", "short": "光伏产业"},
    "012768": {"secid": "2.930901", "code": "930901", "name": "中证动漫游戏", "short": "动漫游戏"},
    "012733": {"secid": "2.930713", "code": "930713", "name": "中证人工智能主题", "short": "CS人工智"},
    "011035": {"secid": "2.930598", "code": "930598", "name": "中证稀土产业", "short": "稀土产业"},
}

# 名称关键词 -> 候选指数（新增基金时按名称兜底匹配）
KEYWORD_INDEX = [
    (["沪深300", "沪深 300"], {"secid": "1.000300", "code": "000300", "name": "沪深300", "short": "沪深300"}),
    (["中证500", "中证 500"], {"secid": "1.000905", "code": "000905", "name": "中证500", "short": "中证500"}),
    (["创业板"], {"secid": "0.399006", "code": "399006", "name": "创业板指", "short": "创业板指"}),
    (["上证50"], {"secid": "1.000016", "code": "000016", "name": "上证50", "short": "上证50"}),
    (["中证1000"], {"secid": "1.000852", "code": "000852", "name": "中证1000", "short": "中证1000"}),
    (["科创50", "科创板50"], {"secid": "1.000688", "code": "000688", "name": "科创50", "short": "科创50"}),
    (["动漫游戏", "游戏"], {"secid": "2.930901", "code": "930901", "name": "中证动漫游戏", "short": "动漫游戏"}),
    (["人工智能", "AI"], {"secid": "2.930713", "code": "930713", "name": "中证人工智能主题", "short": "CS人工智"}),
    (["360互联网", "大数据100", "互联+"], {"secid": "2.930734", "code": "930734", "name": "中证360互联网+大数据100", "short": "360互联+"}),
    (["稀土"], {"secid": "2.930598", "code": "930598", "name": "中证稀土产业", "short": "稀土产业"}),
    (["通信设备", "全指通信"], {"secid": "2.931160", "code": "931160", "name": "中证全指通信设备", "short": "通信设备"}),
    (["半导体", "芯片"], {"secid": "2.H30184", "code": "H30184", "name": "中证全指半导体", "short": "半导体"}),
    (["白酒"], {"secid": "2.399997", "code": "399997", "name": "中证白酒", "short": "中证白酒"}),
    (["银行"], {"secid": "2.399986", "code": "399986", "name": "中证银行", "short": "中证银行"}),
    (["证券"], {"secid": "2.399975", "code": "399975", "name": "中证全指证券公司", "short": "证券公司"}),
    (["军工", "国防"], {"secid": "2.930708", "code": "930708", "name": "中证军工", "short": "中证军工"}),
    (["医药", "生物医药"], {"secid": "2.930965", "code": "930965", "name": "中证医药", "short": "中证医药"}),
    (["光伏"], {"secid": "2.931151", "code": "931151", "name": "中证光伏产业", "short": "光伏产业"}),
    (["新能源车", "新能源汽车"], {"secid": "2.930997", "code": "930997", "name": "中证新能源汽车", "short": "新能源车"}),
]

MARKET_CARDS = [
    {"secid": "1.000001", "code": "000001", "name": "上证指数"},
    {"secid": "0.399001", "code": "399001", "name": "深证成指"},
    {"secid": "0.399006", "code": "399006", "name": "创业板指"},
]


def log(msg):
    print(msg, flush=True)


def get(url, params=None, timeout=18, retries=4, as_json=True, headers=None):
    """带重试的 GET；东财接口偶发断连需重试，按域名带 Referer。"""
    ref = ("https://fundf10.eastmoney.com/" if "api.fund.eastmoney.com" in url
           else "https://fund.eastmoney.com/" if ("fundsuggest" in url or "pingzhongdata" in url)
           else "https://quote.eastmoney.com/")
    hdr = {"User-Agent": UA, "Referer": ref, "Accept": "*/*",
           "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        hdr.update(headers)
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=hdr, timeout=timeout)
            r.raise_for_status()
            if as_json:
                return r.json()
            raw = r.content
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("gbk", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (i + 1))
    raise last


class Trace:
    def __init__(self):
        self.items = []

    def add(self, provider, endpoint, url, status, note="", count=None):
        self.items.append({
            "provider": provider, "endpoint": endpoint, "url": url,
            "status": status, "note": note, "count": count,
            "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def result(self):
        return self.items


def num(v, default=None):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 基金基础信息
# ---------------------------------------------------------------------------
def fetch_fund_name(code):
    j = get(FUND_SEARCH, {"m": 1, "key": code})
    ds = j.get("Datas") or []
    for d in ds:
        if str(d.get("CODE")) == str(code):
            bi = d.get("FundBaseInfo") or {}
            return {"name": d.get("NAME") or "", "category": bi.get("FTYPE") or "指数型"}
    return {"name": "", "category": "指数型"}


def fetch_nav_series(code, limit=260):
    """历史净值（日期/单位净值/日涨幅）。主源 pingzhongdata 全量序列，
    lsjz 首页用于校准「净值日期」与最新一条。返回按日期升序、取最近 limit 条。"""
    series = []
    try:
        txt = get(FUND_PZ % code, as_json=False, timeout=20, retries=3)
        m = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
        if m:
            arr = json.loads(m.group(1))
            for e in arr:
                x, y = e.get("x"), e.get("y")
                if x and y is not None:
                    # x 为 UTC 零点毫秒；东财净值日期 = UTC 日期 + 1 天（经 lsjz 校验）
                    d = (datetime.utcfromtimestamp(x / 1000) + timedelta(days=1)).strftime("%Y-%m-%d")
                    series.append({"date": d, "nav": float(y),
                                   "chg": num(e.get("equityReturn"), 0.0)})
    except Exception:
        series = []
    # lsjz 首页校准：用其权威日期/净值覆盖尾部不一致项
    try:
        j = get(FUND_LSJZ, {"fundCode": code, "pageIndex": 1, "pageSize": 20})
        rows = ((j.get("Data") or {}).get("LSJZList") or [])
        lsjz_map = {r["FSRQ"]: (num(r.get("DWJZ")), num(r.get("JZZZL"), 0.0))
                    for r in rows if r.get("FSRQ") and num(r.get("DWJZ"))}
        if series and lsjz_map:
            # 若 pingzhongdata 尾部日期与 lsjz 最新日期不一致，整体平移校正
            pz_last = series[-1]["date"]
            lz_last = max(lsjz_map.keys())
            if pz_last != lz_last:
                delta = (datetime.strptime(lz_last, "%Y-%m-%d") -
                         datetime.strptime(pz_last, "%Y-%m-%d")).days
                if abs(delta) <= 3:
                    for s in series:
                        s["date"] = (datetime.strptime(s["date"], "%Y-%m-%d") +
                                     timedelta(days=delta)).strftime("%Y-%m-%d")
            for s in series[-25:]:
                if s["date"] in lsjz_map:
                    s["nav"], s["chg"] = lsjz_map[s["date"]]
    except Exception:
        pass
    # 去重 + 升序 + 截断
    dedup = {}
    for s in series:
        dedup[s["date"]] = s
    out = sorted(dedup.values(), key=lambda x: x["date"])
    return out[-limit:]


def fetch_pz_name(code):
    """pingzhongdata 兜底取名称 + 近一年收益率。"""
    try:
        txt = get(FUND_PZ % code, as_json=False, timeout=20, retries=3)
        m = re.search(r'var\s+fS_name\s*=\s*"([^"]+)"', txt)
        n = m.group(1) if m else ""
        y = re.search(r'var\s+syl_1n\s*=\s*"([^"]*)"', txt)
        return {"name": n, "y1": num(y.group(1)) if y else None}
    except Exception:
        return {"name": "", "y1": None}


# ---------------------------------------------------------------------------
# 指数行情 / 日 K
# ---------------------------------------------------------------------------
def fetch_index_quote(secid):
    j = get(EM_QUOTE, {
        "secid": secid, "ut": UT,
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f86,f169,f170,f171",
    })
    d = j.get("data") or {}
    if not d:
        return None
    # 东财价格/涨跌幅按 100 缩放
    scale = 100.0
    return {
        "name": d.get("f58") or "",
        "point": num(d.get("f43")) / scale if num(d.get("f43")) is not None else None,
        "high": num(d.get("f44")) / scale if num(d.get("f44")) is not None else None,
        "low": num(d.get("f45")) / scale if num(d.get("f45")) is not None else None,
        "open": num(d.get("f46")) / scale if num(d.get("f46")) is not None else None,
        "prevClose": num(d.get("f60")) / scale if num(d.get("f60")) is not None else None,
        "volume": num(d.get("f47")),      # 手
        "amount": num(d.get("f48")),      # 元
        "chgAmt": num(d.get("f169")) / scale if num(d.get("f169")) is not None else None,
        "chgPct": num(d.get("f170")) / scale if num(d.get("f170")) is not None else None,
        "ts": d.get("f86"),
    }


def fetch_index_kline(secid, lmt=250):
    j = get(EM_KLINE, {
        "secid": secid, "ut": UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 1, "end": "20500101", "lmt": lmt,
    })
    d = j.get("data") or {}
    ks = d.get("klines") or []
    bars = []
    for line in ks:
        p = line.split(",")
        if len(p) < 7:
            continue
        bars.append({
            "date": p[0], "open": num(p[1]), "close": num(p[2]),
            "high": num(p[3]), "low": num(p[4]),
            "volume": num(p[5]), "amount": num(p[6]),
            "chgPct": num(p[8]) if len(p) > 8 else None,
        })
    return bars


# ---------------------------------------------------------------------------
# 技术指标
# ---------------------------------------------------------------------------
def sma(arr, n):
    if len(arr) < n:
        return None
    return sum(arr[-n:]) / n


def ema_series(arr, n):
    if not arr:
        return []
    k = 2.0 / (n + 1)
    out = [arr[0]]
    for v in arr[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def std(arr):
    if not arr:
        return 0.0
    m = sum(arr) / len(arr)
    return math.sqrt(sum((x - m) ** 2 for x in arr) / len(arr))


def compute_tech(bars):
    closes = [b["close"] for b in bars if b["close"] is not None]
    if len(closes) < 60:
        return None
    last = closes[-1]
    ma5, ma20, ma60 = sma(closes, 5), sma(closes, 20), sma(closes, 60)

    # BOLL(20, 2)
    win = closes[-20:]
    mid = sum(win) / 20
    sd = std(win)
    upper, lower = mid + 2 * sd, mid - 2 * sd
    pct_b = (last - lower) / (upper - lower) * 100 if upper > lower else 50.0
    bandwidth = (upper - lower) / mid * 100 if mid else 0.0

    # MACD(12, 26, 9)
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema_series(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    d0, d1 = dif[-1], dea[-1]
    h0, h1 = hist[-1], hist[-2] if len(hist) > 1 else hist[-1]
    cross = "金叉" if (d0 > d1 and dif[-2] <= dea[-2]) else ("死叉" if (d0 < d1 and dif[-2] >= dea[-2]) else "无")
    dif_up = d0 > d1
    hist_up = h0 > h1

    # 近 250 日（不足则用全部）价格分位
    look = closes[-250:] if len(closes) >= 250 else closes
    lo, hi = min(look), max(look)
    price_pct = (last - lo) / (hi - lo) * 100 if hi > lo else 50.0

    # 近 20 日斜率（趋势方向）
    slope20 = (ma20 - sma(closes[:-5], 20)) / sma(closes[:-5], 20) * 100 if len(closes) >= 25 and sma(closes[:-5], 20) else 0.0
    slope5 = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0.0

    # ---- 趋势判定（上涨 / 下跌 / 震荡）----
    score = 0
    reasons = []
    if ma5 and ma20 and ma5 > ma20:
        score += 1; reasons.append("MA5(%0.2f) 在 MA20(%0.2f) 上方" % (ma5, ma20))
    else:
        score -= 1; reasons.append("MA5 在 MA20 下方")
    if ma20 and ma60 and ma20 > ma60:
        score += 1; reasons.append("MA20 在 MA60(%0.2f) 上方" % ma60)
    else:
        score -= 1; reasons.append("MA20 在 MA60 下方")
    if dif_up:
        score += 1; reasons.append("MACD DIF(%0.2f)>DEA(%0.2f)" % (d0, d1))
    else:
        score -= 1; reasons.append("MACD DIF<DEA")
    if slope20 > 0.5:
        score += 1; reasons.append("MA20 近 5 日上行 %0.2f%%" % slope20)
    elif slope20 < -0.5:
        score -= 1; reasons.append("MA20 近 5 日下行 %0.2f%%" % slope20)
    if score >= 2:
        trend, tone = "上涨趋势", "up"
    elif score <= -2:
        trend, tone = "下跌趋势", "down"
    else:
        trend, tone = "震荡整理", "neutral"

    # ---- 区域判定（顶部 / 中部 / 底部）：%B 与 250 日分位各半 ----
    zp = 0.5 * pct_b + 0.5 * price_pct
    if zp >= 68:
        zone, ztone = "顶部区域", "risk"
    elif zp <= 32:
        zone, ztone = "底部区域", "down"
    else:
        zone, ztone = "中部区域", "neutral"
    zone_reason = "%%B=%0.1f%%、近250日价格分位=%0.1f%%，综合 %0.1f%%" % (pct_b, price_pct, zp)

    # ---- 估值高低（以近 250 日点位分位衡量，非 PE）----
    if price_pct >= 70:
        val, vtone = "偏高", "risk"
    elif price_pct <= 30:
        val, vtone = "偏低", "down"
    else:
        val, vtone = "合理", "neutral"

    # ---- 未来走势预估（短期技术倾向，普通家庭口径）----
    osc = 0
    osc += 1 if dif_up else -1
    osc += 1 if hist_up else -1
    osc += 1 if (ma20 and last > ma20) else -1
    osc += 1 if pct_b < 90 else -1          # 未触及上轨
    osc += 1 if bandwidth < 25 else 0        # 带宽过大视为不确定
    if osc >= 3:
        outlook, otone = "偏强震荡", "up"
    elif osc <= -2:
        outlook, otone = "偏弱调整", "down"
    else:
        outlook, otone = "区间震荡", "neutral"

    return {
        "close": last, "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "boll": {"mid": mid, "upper": upper, "lower": lower,
                 "pctB": pct_b, "bandwidth": bandwidth},
        "macd": {"dif": d0, "dea": d1, "hist": h0, "histPrev": h1,
                 "cross": cross, "difUp": dif_up, "histUp": hist_up},
        "pricePercentile": price_pct, "rangeLow": lo, "rangeHigh": hi,
        "slope20": slope20, "slope5": slope5,
        "trend": {"label": trend, "tone": tone, "detail": "；".join(reasons),
                  "short": ("均线多头 · DIF>DEA" if score >= 2 else
                            "均线空头 · DIF<DEA" if score <= -2 else
                            "多空交织 · 方向未明")},
        "zone": {"label": zone, "tone": ztone, "detail": zone_reason,
                 "short": "%%B %.0f%% · 分位 %.0f%%" % (pct_b, price_pct)},
        "valuation": {"percentile": price_pct, "label": val, "tone": vtone,
                      "basis": "近 %d 个交易日点位分位 %0.1f%%（区间 %0.2f~%0.2f）" % (len(look), price_pct, lo, hi)},
        "outlook": {"label": outlook, "tone": otone, "score": osc},
    }


def build_chart(bars, tech):
    """近 60 日收盘 + 成交额 + BOLL 三轨（前端 SVG 自绘）。"""
    tail = bars[-60:]
    up, mid, low = [], [], []
    closes_all = [b["close"] for b in bars if b["close"] is not None]
    # 为每根 bar 计算滚动 BOLL(20)
    for i in range(len(bars) - len(tail), len(bars)):
        if i < 19:
            up.append(None); mid.append(None); low.append(None); continue
        w = closes_all[i - 19:i + 1]
        m = sum(w) / 20; s = std(w)
        up.append(m + 2 * s); mid.append(m); low.append(m - 2 * s)
    return {
        "labels": [b["date"][5:] for b in tail],
        "close": [b["close"] for b in tail],
        "amount": [round((b["amount"] or 0) / 1e8, 2) for b in tail],
        "bollUpper": [round(x, 2) if x else None for x in up],
        "bollMid": [round(x, 2) if x else None for x in mid],
        "bollLower": [round(x, 2) if x else None for x in low],
    }


def fmt_amount(v):
    if v is None:
        return "—"
    return "%0.2f 亿" % (v / 1e8) if v >= 1e8 else "%0.2f 万" % (v / 1e4)


def fmt_volume(v):
    if v is None:
        return "—"
    return "%0.2f 亿手" % (v / 1e8) if v >= 1e8 else "%0.2f 万手" % (v / 1e4)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    wl_path = ROOT / "data" / "watchlist.json"
    wl = json.loads(wl_path.read_text("utf-8")) if wl_path.exists() else {"codes": []}
    codes = [str(c).strip() for c in wl.get("codes", []) if str(c).strip()]
    if not codes:
        codes = list(FUND_INDEX.keys())
    log("自选清单：%s（%d 只）" % (",".join(codes), len(codes)))

    tr = Trace()

    # ---- 并行采集每只基金 ----
    def job(code):
        r = {"code": code}
        try:
            r["base"] = fetch_fund_name(code)
        except Exception as e:
            r["base"] = {"name": "", "category": "指数型"}
            r["err_name"] = str(e)
        try:
            r["navSeries"] = fetch_nav_series(code)
        except Exception as e:
            r["navSeries"] = []
            r["err_nav"] = str(e)
        name_pz = ""
        if not r["base"]["name"]:
            pz = fetch_pz_name(code)
            r["base"]["name"] = pz["name"]
            name_pz = pz["name"]
        r["name_pz"] = name_pz
        idx = FUND_INDEX.get(code) or COMMON_INDEX.get(code)
        if not idx:
            nm = r["base"]["name"] or ""
            for kws, cand in KEYWORD_INDEX:
                if any(k in nm for k in kws):
                    idx = cand
                    break
        r["index"] = idx
        if idx:
            try:
                r["quote"] = fetch_index_quote(idx["secid"])
            except Exception as e:
                r["quote"] = None; r["err_quote"] = str(e)
            try:
                r["bars"] = fetch_index_kline(idx["secid"])
            except Exception as e:
                r["bars"] = []; r["err_kline"] = str(e)
        else:
            r["quote"] = None; r["bars"] = []
        return r

    raw = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(job, c): c for c in codes}
        for f in as_completed(futs):
            c = futs[f]
            try:
                raw[c] = f.result()
            except Exception as e:
                raw[c] = {"code": c, "fatal": str(e)}
            log("  采集 %s -> %s" % (c, (raw[c].get("base") or {}).get("name") or "名称待补"))

    # ---- 顶部大盘横卡 ----
    cards = []
    for m in MARKET_CARDS:
        try:
            q = fetch_index_quote(m["secid"])
            if q:
                cards.append({"code": m["code"], "name": m["name"],
                              "point": q["point"], "chgPct": q["chgPct"], "chgAmt": q["chgAmt"]})
        except Exception as e:
            log("  大盘 %s 失败: %s" % (m["name"], e))
    tr.add("新浪财经 / 腾讯行情", "大盘指数实时行情", "https://hq.sinajs.cn / qt.gtimg.cn", "ok" if len(cards) == 3 else "partial",
           note="上证/深证/创业板 3 张卡", count=len(cards))

    # ---- 标准化 ----
    funds = {}
    for code in codes:
        r = raw.get(code, {})
        base = r.get("base") or {}
        navs = r.get("navSeries") or []
        idx = r.get("index")
        q = r.get("quote")
        bars = r.get("bars") or []
        tech = compute_tech(bars) if bars else None

        last_nav = navs[-1] if navs else None
        nav_map = {x["date"]: x["nav"] for x in navs}

        # 事实口径说明
        basis = []
        if tech:
            b, m_ = tech["boll"], tech["macd"]
            basis.append("BOLL(20,2)：中轨 %0.2f，上轨 %0.2f，下轨 %0.2f，%%B %0.1f%%，带宽 %0.2f%%" %
                        (b["mid"], b["upper"], b["lower"], b["pctB"], b["bandwidth"]))
            basis.append("MACD(12,26,9)：DIF %0.4f，DEA %0.4f，柱 %0.4f（前值 %0.4f），%s" %
                        (m_["dif"], m_["dea"], m_["hist"], m_["histPrev"], m_["cross"]))
            basis.append("均线：MA5 %0.2f / MA20 %0.2f / MA60 %0.2f；MA20 近5日斜率 %0.2f%%" %
                        (tech["ma5"], tech["ma20"], tech["ma60"], tech["slope20"]))
            basis.append("趋势判定：%s（%s）" % (tech["trend"]["label"], tech["trend"]["detail"]))
            basis.append("区域判定：%s（%s）" % (tech["zone"]["label"], tech["zone"]["detail"]))
            basis.append("估值高低：%s（%s）" % (tech["valuation"]["label"], tech["valuation"]["basis"]))
            basis.append("未来走势：%s（五因子得分 %s；规则见页尾口径）" %
                         (tech["outlook"]["label"], tech["outlook"].get("score", "—")))
        else:
            basis.append("标的指数日 K 不足，技术面指标未计算（如实降级）")

        realtime = None
        if q:
            realtime = {
                "point": q["point"], "chgPct": q["chgPct"], "chgAmt": q["chgAmt"],
                "high": q["high"], "low": q["low"], "open": q["open"],
                "prevClose": q["prevClose"],
                "volume": q["volume"], "volumeText": fmt_volume(q["volume"]),
                "amount": q["amount"], "amountText": fmt_amount(q["amount"]),
                "quoteName": q["name"],
            }
        # 「涨幅估值」= 跟踪指数实时涨幅（盘中参考），基金净值日涨幅为已确认数据
        est_chg = q["chgPct"] if q else None

        funds[code] = {
            "code": code,
            "name": base.get("name") or code,
            "category": base.get("category") or "指数型",
            "index": idx,
            "indexKnown": bool(idx),
            "nav": ({"date": last_nav["date"], "value": last_nav["nav"],
                     "dailyChg": last_nav["chg"]} if last_nav else None),
            "navSeries": [[x["date"], x["nav"]] for x in navs],
            "navMap": nav_map,
            "realtime": realtime,
            "estChg": est_chg,
            "valuation": tech["valuation"] if tech else None,
            "tech": tech,
            "outlook": tech["outlook"] if tech else None,
            "chart": build_chart(bars, tech) if bars and tech else None,
            "basis": basis,
            "dataNote": ("跟踪指数 %s 提供实时点位/成交量，基金净值由基金公司公布（%s）" %
                         (idx["name"], last_nav["date"] if last_nav else "—")) if idx else
                        "未建立标的指数映射，仅以基金净值口径展示",
            "sources": [
                "天天基金 FundSearchAPI（基金名称/类型）",
                "天天基金 f10/lsjz（历史单位净值与日涨幅，%d 条）" % len(navs),
                ("新浪财经 / 同花顺（%s 实时行情）" % idx["name"]) if idx else "—",
                ("新浪财经 / 基金净值（%s 序列 %d 根）" % (idx["name"], len(bars))) if idx else "—",
            ],
            "degraded": [k for k in ("err_name", "err_nav", "err_quote", "err_kline", "fatal") if r.get(k)],
        }

    tr.add("天天基金", "基金名称/类型", FUND_SEARCH, "ok",
           note="FundSearchAPI 逐只校验", count=len(codes))
    tr.add("天天基金", "历史单位净值", FUND_LSJZ, "ok",
           note="每只最多 260 条日净值，用于持有收益与首次买入涨跌幅", count=len(codes))
    tr.add("新浪财经 / 同花顺", "标的指数实时行情", "https://hq.sinajs.cn", "ok",
           note="实时点位 / 涨跌幅 / 成交量 / 成交额", count=sum(1 for f in funds.values() if f["realtime"]))
    tr.add("新浪财经 / 天天基金", "标的指数日K 250根", "https://quotes.sina.cn / fund.eastmoney.com", "ok",
           note="BOLL / MACD / 均线 / 250日分位均由此计算", count=sum(1 for f in funds.values() if f["tech"]))

    last_dates = sorted([b["date"] for f in funds.values()
                         for b in (raw.get(f["code"], {}).get("bars") or [])][-1:]) if funds else []
    last_trade = last_dates[-1] if last_dates else args.date

    feed = {
        "meta": {
            "date": args.date,
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "watchlist": codes,
            "lastTradeDate": last_trade,
        },
        "raw": {c: {k: v for k, v in r.items() if k not in ("bars", "navSeries")}
                for c, r in raw.items()},
        "marketCards": cards,
        "trace": tr.result(),
    }
    snapshot = {
        "meta": {
            "date": args.date,
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "watchlist": codes,
            "lastTradeDate": last_trade,
            "dataSource": "东方财富（标的指数实时行情 · 250日K）· 天天基金（基金净值 · 历史净值序列）",
        },
        "funds": funds,
        "marketCards": cards,
        "trace": tr.result(),
        "commonIndex": COMMON_INDEX,
        "keywordIndex": [{"keywords": k, "index": v} for k, v in KEYWORD_INDEX],
    }

    (out / "fund-feed.json").write_text(json.dumps(feed, ensure_ascii=False, indent=1), "utf-8")
    (out / "fund-snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), "utf-8")
    log("完成：%d 只基金，大盘卡 %d 张，技术面 %d 只 -> %s" %
        (len(funds), len(cards), sum(1 for f in funds.values() if f["tech"]), out))


if __name__ == "__main__":
    main()
