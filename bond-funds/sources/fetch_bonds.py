#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「债券基金」真实数据采集层。

数据源（全部真实接口，逐条 trace，禁止编造）：
  1. 天天基金开放式基金排行 rankhandler.aspx（dt=kf & ft=zq，全市场场外债券型基金）
  2. 天天基金 pingzhongdata/<code>.js（累计净值全序列 → 近三年最大回撤 / 近一月回撤）
  3. 新浪财经 / 同花顺 / 东方财富（国债指数、企债指数、中证转债实时行情与日 K）
  4. 东方财富 7×24 快讯（债券政策相关新闻）
  5. 中国人民银行 / 财政部官网（官方新闻标题）
  6. 本机 data/buy-records.json（买入流水，页面录入导出）、data/popup-config.json（手机弹窗提醒配置）

产出：data/bond-feed.json（原始留痕）、data/bond-snapshot.json（结构化快照，含
      relative-to-last-buy 涨跌幅与买入跌幅触发 alerts，供 notify_popup.py 与生成器消费）
用法：python3 sources/fetch_bonds.py [--pool 120]
"""
import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# 多源行情兼容层：拦截被 WAF 封禁的 push2/push2his，转发到
# 新浪财经 / 同花顺 / 东财 datacenter-web / 天天基金等已实测可用源。
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import market_hub
market_hub.install()
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"
OUT.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UT = "fa5fd1943c7b386f172d6893dbfba10b"

FUND_RANK = "https://fund.eastmoney.com/data/rankhandler.aspx"
FUND_PZ = "https://fund.eastmoney.com/pingzhongdata/%s.js"
FUND_SEARCH = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
EM_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FASTNEWS = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
PBC_URL = "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
MOF_URL = "http://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/"

# 债券市场三个基准指数（secid 均实测可稳定返回）
BOND_INDEX = [
    {"secid": "1.000012", "name": "国债指数", "short": "国债指数"},
    {"secid": "0.399481", "name": "企债指数", "short": "企债指数"},
    {"secid": "1.000832", "name": "中证转债", "short": "中证转债"},
]
BENCH_SEC = "1.000012"          # 晴雨表技术基准
DROP_THRESHOLD = 0.7                 # 相对上次买入「跌幅」的手机弹窗提醒阈值（%）
MONTH_DAYS = 30                      # 近一月最大回撤展示窗口（自然日 30 天，仅展示、不作通知）
POOL_DEFAULT = 120                  # 进入回撤计算的收益头部候选池大小
BUY_RECORDS_FILE = "buy-records.json"   # 买入流水（页面录入后导出替换，次日更新读取）
POPUP_CONFIG_FILE = "popup-config.json" # 手机弹窗提醒配置（开关 / 系统通知意向 / 阈值）

BOND_KW = ["债券", "国债", "利率", "央行", "货币政策", "LPR", "MLF", "逆回购", "降准", "降息",
           "资金面", "DR007", "SHIBOR", "财政部", "地方债", "专项债", "可转债", "信用债", "违约",
           "流动性", "公开市场", "收益率", "债市", "存单", "回购", "再贷款", "金融债", "降成本"]
# 债券政策新闻的核心命中词（比 BOND_KW 更聚焦债市本身）
NEWS_CORE = ["逆回购", "MLF", "公开市场", "国债", "债券", "利率", "LPR", "降准", "降息",
             "资金面", "债市", "可转债", "信用债", "收益率", "流动性", "票据", "存单",
             "地方债", "专项债", "金融债", "央行", "货币政策", "再贷款"]
# 明确排除：海外财政/股市回购/汇率互换等与国内债市无关的误命中
# 国内债市主体白名单：必须命中其一，避免海外利率/黄金/公司票据混入
NEWS_DOMESTIC = ["央行", "人民银行", "财政部", "国债", "债市", "资金面", "逆回购", "MLF", "LPR",
                 "票据利率", "同业存单", "存单", "信用债", "可转债", "地方债", "专项债",
                 "金融债", "人民币债券", "政策性银行", "公开市场", "降准", "降息"]
NEWS_EXCLUDE = ["美财政", "美股", "美联储", "联储", "美债", "英国央行", "欧洲央行", "日本央行",
                "达拉斯", "堪萨斯", "韩国", "美国", "日本", "欧洲", "英国", "新加坡", "搬至", "军事",
                "燃油税", "股票回购", "回购增持", "拟注资", "注资", "本币互换", "互换协议",
                "地缘", "关税", "核电", "稳定市场预期", "存金", "金库", "海外央行",
                "Anthropic", "博通", "美元票据", "两年期", "俄罗斯", "卢布", "预算赤字",
                "财政部长", "会见", "出席", "峰会", "董事长", "金融监管合作"]
# 官网新闻须命中债市强相关词，排除会见/会议/人事等非政策内容
OFFICIAL_CORE = ["国债", "债券", "利率", "准备金率", "货币政策", "逆回购", "MLF", "LPR",
                 "资金面", "降准", "降息", "金融债", "地方债", "专项债", "再贷款", "公开市场"]
GOOD_KW = ["降准", "降息", "宽松", "净投放", "超额续作", "超量续作", "流动性充裕", "收益率下行",
           "避险", "呵护", "下调", "成功发行", "引导下行", "地板价"]
BAD_KW = ["收紧", "净回笼", "收益率上行", "通胀", "加息", "资金面紧张", "供给放量",
          "上行压力", "违约", "赎回", "抛售", "走弱", "调整压力"]
# 近三年收益异常阈值：债基超过该值通常为巨额赎回导致净值异动，剔除以免误导
Y3_ABNORMAL = 50.0

# 份额后缀（用于 A/C 份额去重，让 TOP5 是 5 只不同基金）
SHARE_SUFFIX = re.compile(
    r"(人民币[ACE]?|美元现汇[ACE]?|美元[ACE]?|[ACEIHDRY]\d?|类|份额|联接[ACE]?)$")
# 基金类型 → 对标债券指数（场外债基无盘中盘口，用对标指数提供实时盘面）
TYPE_INDEX = [
    (("可转债", "混合二级", "二级债", "转债"), "1.000832", "中证转债"),
    (("信用债", "混合一级", "一级债", "企业债"), "0.399481", "企债指数"),
]


def log(msg):
    print("[bond] " + msg, flush=True)


def get(url, params=None, timeout=18, retries=3, as_json=True, headers=None):
    """带重试的 GET；非 JSON 按探测编码解码。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last = ""
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=timeout)
            if as_json:
                return r.json()
            enc = r.apparent_encoding or "utf-8"
            if enc.lower() in ("gb2312", "gbk", "iso-8859-1"):
                enc = "gb18030"
            return r.content.decode(enc, "ignore")
        except Exception as e:
            last = str(e)[:90]
            if i < retries - 1:
                time.sleep(0.35 * (i + 1))
    raise RuntimeError(f"请求失败 {url} :: {last}")


def num(v, default=None):
    try:
        if v is None or v == "" or v == "--":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def ts_to_date(x):
    """pingzhongdata 时间戳为 UTC 零点，对应净值日期 = UTC 日期 +1 天（经 lsjz 校准）。"""
    d = datetime.fromtimestamp(x / 1000, timezone.utc).date()
    return (d + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# 1) 全市场场外债券型基金排行
# ---------------------------------------------------------------------------
def fetch_rank():
    params = {"op": "ph", "dt": "kf", "ft": "zq", "rs": "", "gs": "0",
              "sc": "3n", "st": "desc", "sd": "", "ed": "", "qdii": "",
              "tabSubtype": ",,,,", "pi": 1, "pn": 6000, "dx": 1}
    txt = get(FUND_RANK, params, as_json=False, timeout=30, retries=3,
              headers={"Referer": "https://fund.eastmoney.com/data/fundranking.html"})
    m = re.search(r"datas\s*:\s*\[(.*?)\]\s*,\s*allRecords", txt, re.S)
    am = re.search(r"allRecords\s*:\s*(\d+)", txt)
    universe = int(am.group(1)) if am else 0
    items = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    out = []
    for it in items:
        a = it.split(",")
        if len(a) < 17:
            continue
        out.append({
            "code": a[0], "name": a[1], "navDate": a[3],
            "nav": num(a[4]), "accNav": num(a[5]),
            "dayChg": num(a[6]), "w1": num(a[7]), "m1": num(a[8]),
            "m3": num(a[9]), "m6": num(a[10]), "y1": num(a[11]),
            "y2": num(a[12]), "y3": num(a[13]), "ytd": num(a[14]),
            "since": num(a[15]), "setup": a[16],
        })
    log(f"排行全量 {len(out)} 只（allRecords={universe}）")
    return out, universe, txt


# ---------------------------------------------------------------------------
# 2) 累计净值序列 + 回撤
# ---------------------------------------------------------------------------
def fetch_pz(code):
    txt = get(FUND_PZ % code, as_json=False, timeout=22, retries=3,
              headers={"Referer": "https://fund.eastmoney.com/"})
    ma = re.search(r"var\s+Data_ACWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    mu = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    mn = re.search(r'var\s+fS_name\s*=\s*"([^"]+)"', txt)
    acc = json.loads(ma.group(1)) if ma else []
    unit = json.loads(mu.group(1)) if mu else []
    if not acc:
        raise RuntimeError("无累计净值序列")
    umap = {u["x"]: u for u in unit}
    series = []
    for ts, v in acc:
        u = umap.get(ts) or {}
        series.append({"date": ts_to_date(ts), "acc": v,
                       "unit": u.get("y"), "chg": num(u.get("equityReturn"), 0.0)})
    return {"name": mn.group(1) if mn else "", "series": series, "len": len(series)}


def since(series, date_str):
    return [p for p in series if p["date"] >= date_str and p["acc"] is not None]


def max_drawdown(pts):
    """基于累计净值的峰谷最大回撤（%），返回 (回撤, 峰日, 谷日)。"""
    peak, peak_d, best, pair = None, None, 0.0, (None, None)
    for p in pts:
        v = p["acc"]
        if peak is None or v > peak:
            peak, peak_d = v, p["date"]
        dd = (peak - v) / peak if peak else 0.0
        if dd > best:
            best, pair = dd, (peak_d, p["date"])
    return round(best * 100, 4), pair


def base_name(n):
    s = re.sub(r"[（(].*?[)）]", "", n or "").strip()
    for _ in range(2):
        s2 = SHARE_SUFFIX.sub("", s)
        if s2 == s:
            break
        s = s2
    return s.strip() or (n or "")


# ---------------------------------------------------------------------------
# 2b) 相对上次买入涨跌幅（买入跌幅手机弹窗提醒的判定依据）
# ---------------------------------------------------------------------------
def unit_on_or_before(series, date_str):
    """取不晚于买入日的最近一个交易日累计/单位净值点。序列按日期升序。"""
    hit = None
    for p in series:
        if p["date"] <= date_str:
            hit = p
        else:
            break
    return hit or (series[0] if series else None)


def compute_since_buy(series, records, threshold=DROP_THRESHOLD):
    """比较「最新单位净值」与「最近一次买入时间点单位净值」的涨跌幅。

    records: [{date, amount, nav}]；nav 为空则按买入日（或之前最近交易日）
    从真实净值序列带出单位净值，绝不编造。返回结构供页面与通知脚本共用。
    """
    out = {
        "hasBuy": False, "buyDate": None, "buyNav": None, "buyNavSrc": "",
        "buyAmount": None, "buyCount": 0, "curDate": None, "curNav": None,
        "chgPct": None, "drop": False, "threshold": threshold,
        "status": "未录入买入时间，暂不监控",
        "records": [],
    }
    if not series:
        out["status"] = "无净值序列，无法计算（如实标注）"
        return out
    cur = series[-1]
    out["curDate"] = cur["date"]
    cur_unit = cur["unit"] if cur["unit"] is not None else cur["acc"]
    out["curNav"] = round(cur_unit, 4) if cur_unit is not None else None
    recs = [r for r in (records or []) if isinstance(r, dict) and r.get("date")]
    if not recs:
        return out
    recs = sorted(recs, key=lambda r: r["date"])
    out["hasBuy"] = True
    out["buyCount"] = len(recs)
    last = recs[-1]
    out["buyDate"] = last["date"]
    out["buyAmount"] = last.get("amount")
    buy_unit = num(last.get("nav"))
    if buy_unit:
        out["buyNavSrc"] = "录入的成交净值"
    else:
        hit = unit_on_or_before(series, last["date"])
        buy_unit = (hit["unit"] if hit["unit"] is not None else hit["acc"]) if hit else None
        out["buyNavSrc"] = f"净值序列带出（{hit['date']}）" if hit else "取不到"
    out["buyNav"] = round(buy_unit, 4) if buy_unit else None
    # 展示用买入流水（含每条按序列带出的净值，可回验）
    for r in recs:
        hit = unit_on_or_before(series, r["date"])
        nav = num(r.get("nav")) or ((hit["unit"] if hit["unit"] is not None else hit["acc"]) if hit else None)
        out["records"].append({"date": r["date"], "amount": r.get("amount"),
                               "nav": round(nav, 4) if nav else None})
    if buy_unit and cur_unit:
        chg = round((cur_unit / buy_unit - 1) * 100, 4)
        out["chgPct"] = chg
        out["drop"] = chg <= -threshold
        out["status"] = ("跌幅 %.2f%% 超过 %.1f%% · 需加强关注" % (abs(chg), threshold)
                         if out["drop"] else
                         "相对上次买入 %+.2f%% · 未触发" % chg)
    else:
        out["status"] = "净值缺失，无法计算涨跌幅（如实标注）"
    return out


def build_rank(all_items, universe, data_date, pool_size, trace):
    cutoff = (datetime.strptime(data_date, "%Y-%m-%d") - timedelta(days=365 * 3 + 1)).date().isoformat()
    eligible = [x for x in all_items
                if x["y3"] is not None and re.match(r"\d{4}-\d{2}-\d{2}", x["setup"] or "")
                and x["setup"] <= cutoff and x["nav"] is not None]
    # 剔除疑似巨额赎回 / 净值异动样本：债基近三年收益超过阈值并非可持续收益，列入榜单会误导
    normal = [x for x in eligible if x["y3"] <= Y3_ABNORMAL]
    excluded = len(eligible) - len(normal)
    normal.sort(key=lambda x: -x["y3"])
    pool = normal[:pool_size]
    log(f"满三年且有效 {len(eligible)} 只 → 剔除异常 {excluded} 只 → 收益头部候选池 {len(pool)} 只（cutoff={cutoff}）")
    trace.append({"provider": "天天基金", "endpoint": "rankhandler.aspx?dt=kf&ft=zq",
                  "note": f"全市场场外债券型基金 {universe} 只，满三年有效 {len(eligible)} 只，"
                          f"剔除近三年收益>{Y3_ABNORMAL:.0f}%的疑似净值异动 {excluded} 只，取收益头部 {len(pool)} 只",
                  "status": "ok"})

    def one(x):
        try:
            pz = fetch_pz(x["code"])
            pts = since(pz["series"], cutoff)
            if len(pts) < 60:
                return x["code"], {"ok": False, "err": "近三年净值样本不足 60 条"}
            dd, pair = max_drawdown(pts)
            return x["code"], {"ok": True, "dd3y": dd, "ddPeak": pair[0], "ddTrough": pair[1],
                               "navLen": pz["len"], "pzName": pz["name"], "series": pz["series"]}
        except Exception as e:
            return x["code"], {"ok": False, "err": str(e)[:90]}

    res = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(one, x): x["code"] for x in pool}
        for fu in as_completed(futs):
            c, v = fu.result()
            res[c] = v
    ok = []
    for x in pool:
        v = res.get(x["code"]) or {}
        if not v.get("ok"):
            continue
        y = dict(x)
        y.update({"dd3y": v["dd3y"], "ddPeak": v["ddPeak"], "ddTrough": v["ddTrough"],
                  "navLen": v["navLen"]})
        ok.append(y)
    failed = len(pool) - len(ok)
    log(f"回撤计算成功 {len(ok)} 只，失败 {failed} 只（如实标注，不编造）")

    ret_sorted = sorted(ok, key=lambda x: -x["y3"])
    n = max(1, len(ret_sorted) - 1)
    for i, x in enumerate(ret_sorted):
        x["retScore"] = round(100 * (1 - i / n), 2)
    dd_sorted = sorted(ok, key=lambda x: x["dd3y"])
    for i, x in enumerate(dd_sorted):
        x["ddScore"] = round(100 * (1 - i / n), 2)
    for x in ok:
        x["composite"] = round(0.6 * x["retScore"] + 0.4 * x["ddScore"], 2)

    # A/C 份额去重 → 5 只不同基金
    seen, top5 = set(), []
    for x in sorted(ok, key=lambda x: -x["composite"]):
        b = base_name(x["name"])
        if b in seen:
            continue
        seen.add(b)
        top5.append({
            "rank": len(top5) + 1, "code": x["code"], "name": x["name"],
            "type": "", "y3": x["y3"], "y1": x["y1"], "m1": x["m1"],
            "dd3y": x["dd3y"], "ddPeak": x["ddPeak"], "ddTrough": x["ddTrough"],
            "retScore": x["retScore"], "ddScore": x["ddScore"], "composite": x["composite"],
            "nav": x["nav"], "navDate": x["navDate"], "setup": x["setup"],
        })
        if len(top5) == 5:
            break
    return {"universe": universe, "eligible": len(eligible), "excluded": excluded,
            "abnormalLine": Y3_ABNORMAL, "pool": len(pool),
            "computed": len(ok), "failed": failed, "cutoff": cutoff, "top5": top5,
            "candidates": [{"code": x["code"], "name": x["name"], "y3": x["y3"],
                            "dd3y": x["dd3y"], "composite": x["composite"]} for x in ok]}


# ---------------------------------------------------------------------------
# 3) 债券指数实时行情 / 日 K
# ---------------------------------------------------------------------------
def fetch_quote(secid):
    j = get(EM_QUOTE, {"secid": secid,
                       "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170,f86",
                       "ut": UT}, timeout=15, retries=3,
            headers={"Referer": "https://quote.eastmoney.com/"})
    d = j.get("data") or {}
    if not d or num(d.get("f43")) is None:
        return None
    return {"name": d.get("f58"), "point": round(num(d.get("f43")) / 100, 2),
            "chgPct": round(num(d.get("f170"), 0) / 100, 2),
            "chgAmt": round(num(d.get("f169"), 0) / 100, 2),
            "amount": num(d.get("f48")), "volume": num(d.get("f47")),
            "ts": d.get("f86")}


def fetch_kline(secid, lmt=250):
    j = get(EM_KLINE, {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                       "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                       "klt": "101", "fqt": "1", "end": "20500101", "lmt": lmt,
                       "ut": UT}, timeout=20, retries=4,
            headers={"Referer": "https://quote.eastmoney.com/"})
    ks = (j.get("data") or {}).get("klines") or []
    bars = []
    for k in ks:
        a = k.split(",")
        if len(a) < 9:
            continue
        bars.append({"date": a[0], "open": num(a[1]), "close": num(a[2]),
                     "high": num(a[3]), "low": num(a[4]), "volume": num(a[5], 0),
                     "amount": num(a[6], 0), "chgPct": num(a[8], 0)})
    return bars


# ---------------------------------------------------------------------------
# 4) 技术指标（BOLL / MACD / 均线 / 分位 / 趋势 / 区域 / 走势）
# ---------------------------------------------------------------------------
def sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


def ema_series(a, n):
    if not a:
        return []
    k = 2.0 / (n + 1)
    out = [a[0]]
    for v in a[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def std(a):
    if not a:
        return 0.0
    m = sum(a) / len(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / len(a))


def compute_tech(closes):
    if len(closes) < 60:
        return None
    last = closes[-1]
    ma5, ma20, ma60 = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    win = closes[-20:]
    mid = sum(win) / 20
    sd = std(win)
    upper, lower = mid + 2 * sd, mid - 2 * sd
    pct_b = (last - lower) / (upper - lower) * 100 if upper > lower else 50.0
    bandwidth = (upper - lower) / mid * 100 if mid else 0.0
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema_series(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    d0, d1 = dif[-1], dea[-1]
    h0, h1 = hist[-1], hist[-2] if len(hist) > 1 else hist[-1]
    dif_up, hist_up = d0 > d1, h0 > h1
    look = closes[-250:] if len(closes) >= 250 else closes
    lo, hi = min(look), max(look)
    price_pct = (last - lo) / (hi - lo) * 100 if hi > lo else 50.0
    prev20 = sma(closes[:-5], 20)
    slope20 = (ma20 - prev20) / prev20 * 100 if len(closes) >= 25 and prev20 else 0.0

    score, reasons = 0, []
    if ma5 and ma20 and ma5 > ma20:
        score += 1; reasons.append(f"MA5({ma5:.4f}) 在 MA20({ma20:.4f}) 上方")
    else:
        score -= 1; reasons.append("MA5 在 MA20 下方")
    if ma20 and ma60 and ma20 > ma60:
        score += 1; reasons.append(f"MA20 在 MA60({ma60:.4f}) 上方")
    else:
        score -= 1; reasons.append("MA20 在 MA60 下方")
    if dif_up:
        score += 1; reasons.append(f"MACD DIF({d0:.5f})>DEA({d1:.5f})")
    else:
        score -= 1; reasons.append("MACD DIF<DEA")
    if slope20 > 0.5:
        score += 1; reasons.append(f"MA20 近 5 日上行 {slope20:.2f}%")
    elif slope20 < -0.5:
        score -= 1; reasons.append(f"MA20 近 5 日下行 {slope20:.2f}%")
    trend, tone = ("上涨趋势", "up") if score >= 2 else (("下跌趋势", "down") if score <= -2 else ("震荡整理", "neutral"))

    zp = 0.5 * pct_b + 0.5 * price_pct
    zone, ztone = ("顶部区域", "risk") if zp >= 68 else (("底部区域", "down") if zp <= 32 else ("中部区域", "neutral"))
    val, vtone = ("偏高", "risk") if price_pct >= 70 else (("偏低", "down") if price_pct <= 30 else ("合理", "neutral"))

    osc = 0
    osc += 1 if dif_up else -1
    osc += 1 if hist_up else -1
    osc += 1 if (ma20 and last > ma20) else -1
    osc += 1 if pct_b < 90 else -1
    osc += 1 if bandwidth < 25 else 0
    outlook, otone = ("偏强震荡", "up") if osc >= 3 else (("偏弱调整", "down") if osc <= -2 else ("区间震荡", "neutral"))
    return {
        "close": last, "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "boll": {"mid": mid, "upper": upper, "lower": lower, "pctB": pct_b, "bandwidth": bandwidth},
        "macd": {"dif": d0, "dea": d1, "hist": h0, "histPrev": h1,
                 "difUp": dif_up, "histUp": hist_up},
        "pricePercentile": price_pct, "rangeLow": lo, "rangeHigh": hi, "slope20": slope20,
        "trend": {"label": trend, "tone": tone, "detail": "；".join(reasons),
                  "short": "均线多头 · DIF>DEA" if score >= 2 else ("均线空头 · DIF<DEA" if score <= -2 else "多空交织")},
        "zone": {"label": zone, "tone": ztone,
                 "detail": f"%B={pct_b:.1f}%、近250日分位={price_pct:.1f}%，综合 {zp:.1f}%",
                 "short": f"%B {pct_b:.0f}% · 分位 {price_pct:.0f}%"},
        "valuation": {"percentile": price_pct, "label": val, "tone": vtone,
                      "basis": f"近 {len(look)} 个交易日累计净值分位 {price_pct:.1f}%（区间 {lo:.4f}~{hi:.4f}）"},
        "outlook": {"label": outlook, "tone": otone, "score": osc},
    }


def build_chart(series, tech):
    """近 60 日累计净值 + BOLL 三轨 + 日涨跌柱（红涨绿跌）。"""
    tail = series[-60:]
    closes_all = [p["acc"] for p in series if p["acc"] is not None]
    up, mid, low = [], [], []
    off = len(series) - len(tail)
    for i in range(off, len(series)):
        if i < 19:
            up.append(None); mid.append(None); low.append(None); continue
        w = closes_all[i - 19:i + 1]
        m = sum(w) / 20; s = std(w)
        up.append(round(m + 2 * s, 4)); mid.append(round(m, 4)); low.append(round(m - 2 * s, 4))
    return {"labels": [p["date"][5:] for p in tail],
            "close": [round(p["acc"], 4) for p in tail],
            "amount": [round(abs(p["chg"] or 0), 3) for p in tail],
            "bollUpper": up, "bollMid": mid, "bollLower": low}


def fmt_amount(v):
    if v is None:
        return "—"
    return f"{v / 1e8:.2f} 亿" if v >= 1e8 else f"{v / 1e4:.2f} 万"


# ---------------------------------------------------------------------------
# 5) 政策新闻：东财快讯 + 央行/财政部官网
# ---------------------------------------------------------------------------
def fetch_fastnews(today):
    out, seen, sort_end = [], set(), ""
    for i in range(3):
        url = (f"{EM_FASTNEWS}?client=web&biz=web_724&fastColumn=102"
               f"&sortEnd={sort_end}&pageSize=200&req_trace={int(time.time() * 1000) + i}")
        try:
            j = get(url, as_json=True, timeout=25, retries=3)
        except Exception as e:
            log(f"快讯第{i + 1}批失败：{e}")
            break
        data = j.get("data") or {}
        lst = data.get("fastNewsList") or []
        for x in lst:
            title = (x.get("title") or x.get("summary") or "").strip()
            t = (x.get("showTime") or "")[:16]
            key = (t, title[:30])
            if not title or key in seen:
                continue
            seen.add(key)
            out.append({"time": t, "date": (x.get("showTime") or "")[:10],
                        "title": title, "source": "东方财富 7×24", "href": ""})
        sort_end = data.get("sortEnd") or ""
        if not lst or not sort_end:
            break
    # 仅保留聚焦国内债券市场的新闻，按时间倒序
    bond = [x for x in out if bond_relevant(x["title"])]
    bond.sort(key=lambda x: x["time"], reverse=True)
    news = bond[:6]
    log(f"快讯 {len(out)} 条 → 债券政策相关 {len(bond)} 条，取 {len(news)} 条")
    return news, len(out), len(bond)


def fetch_official():
    rows = []
    for prov, url in [("中国人民银行", PBC_URL), ("财政部", MOF_URL)]:
        try:
            html = get(url, as_json=False, timeout=18, retries=2)
        except Exception as e:
            log(f"{prov} 抓取失败：{e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a"):
            tx = " ".join(a.get_text(" ", strip=True).split())
            if (not (10 <= len(tx) <= 62) or not bond_relevant(tx)
                    or not any(k in tx for k in OFFICIAL_CORE)):
                continue
            href = a.get("href") or ""
            if href.startswith("/"):
                href = ("http://www.pbc.gov.cn" if prov == "中国人民银行" else "http://www.mof.gov.cn") + href
            ctx = a.parent.get_text(" ", strip=True) if a.parent else ""
            dm = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", ctx)
            date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
            rows.append({"time": date, "date": date, "title": tx,
                         "source": prov, "href": href})
    # 债市相关度高的排前面（国债/利率/货币政策优先），去重限 6 条
    def relevance(t):
        return sum(1 for k in ["国债", "逆回购", "利率", "货币政策", "债券", "MLF", "LPR"] if k in t)
    rows.sort(key=lambda r: -relevance(r["title"]))
    seen, out = set(), []
    for r in rows:
        if r["title"] in seen:
            continue
        seen.add(r["title"]); out.append(r)
        if len(out) == 6:
            break
    log(f"官网债券/政策相关新闻 {len(out)} 条")
    return out


# ---------------------------------------------------------------------------
# 6) 晴雨表
# ---------------------------------------------------------------------------
def bond_relevant(title):
    """聚焦国内债券市场的政策/行情新闻，排除海外利率、黄金、公司票据等误命中。"""
    if any(k in title for k in NEWS_EXCLUDE):
        return False
    return any(k in title for k in NEWS_DOMESTIC)


def news_tone(title):
    g = sum(1 for k in GOOD_KW if k in title)
    b = sum(1 for k in BAD_KW if k in title)
    return "up" if g > b else ("down" if b > g else "neutral"), g, b


def build_barometer(bars, quotes, news, official, trace):
    closes = [b["close"] for b in bars if b["close"] is not None]
    tech = compute_tech(closes)
    if not tech:
        return None
    last_chg = bars[-1]["chgPct"] if bars else 0
    # 技术分 0~10：趋势 3 / MACD 2 / 中轨 2 / 当日涨跌 2 / 量能 1
    ts = 5.0
    ts += 1.5 if tech["trend"]["label"] == "上涨趋势" else (-1.5 if tech["trend"]["label"] == "下跌趋势" else 0)
    ts += 1.0 if tech["macd"]["difUp"] else -1.0
    ts += 1.0 if closes[-1] > tech["boll"]["mid"] else -1.0
    ts += 1.0 if last_chg > 0 else -1.0
    ts += 0.5 if tech["boll"]["pctB"] > 50 else -0.5
    pp = tech["pricePercentile"]
    if pp >= 90:
        ts -= 0.5          # 指数已处历史高位，利率进一步下行空间有限，折减
    ts = max(0.0, min(10.0, ts))
    pos_note = (f"国债指数近250日分位 {pp:.1f}%（{tech['zone']['label']}），"
                "已处高位，长久期利率债追涨需谨慎") if pp >= 70 else (
                f"国债指数近250日分位 {pp:.1f}%（{tech['zone']['label']}），位置尚不极端")
    chg_txt = (f"当日 {last_chg:+.2f}%" if abs(last_chg) >= 0.005
               else "当日基本持平（盘前债券指数尚未更新）")
    # 政策分 -2~2
    g = b = 0
    for x in news + official:
        _, gi, bi = news_tone(x["title"])
        g += gi; b += bi
    pol = max(-2.0, min(2.0, (g - b) / 3.0))
    final = round(max(0.0, min(10.0, ts + pol)), 2)   # 综合分统一封顶 0~10
    if final >= 7.5:
        label, tone = "晴", "up"
        summary = (f"技术面偏强：国债指数处{tech['trend']['label']}、站上 BOLL 中轨，"
                   f"{chg_txt}，MACD DIF{'>' if tech['macd']['difUp'] else '<'}DEA。"
                   f"但{pos_note}；本结论为技术倾向，不预示未来。")
    elif final >= 6.0:
        label, tone = "多云转晴", "up"
        summary = (f"债市震荡偏强，国债指数{tech['trend']['label']}、{chg_txt}，"
                   f"回撤与上行都不大。{pos_note}。")
    elif final >= 4.5:
        label, tone = "多云", "neutral"
        summary = (f"债市方向不明，长短端分化，国债指数{tech['trend']['label']}、"
                   f"处{tech['zone']['label']}，以区间震荡为主。{pos_note}。")
    elif final >= 3.0:
        label, tone = "阴", "down"
        summary = (f"债市偏弱，国债指数{tech['trend']['label']}、{chg_txt}，"
                   f"利率上行或资金面收敛，长久期债券基金回撤压力上升。{pos_note}。")
    else:
        label, tone = "雨", "down"
        summary = (f"债市明显调整，国债指数{tech['trend']['label']}、MACD 走弱。"
                   "建议暂停加仓，并留意自选基金相对买入的跌幅提醒。")
    action = {
        "晴": "债基照常持有、定投照常；首付池保持流动性，不追涨长久期利率债。",
        "多云转晴": "中短债可分批配置，定投照常；别因为一天上涨就加大金额。",
        "多云": "维持原配置，优先中短债与高等级信用债，等待方向明确。",
        "阴": "减少长久期利率债敞口，留意自选基金相对买入的跌幅提醒。",
        "雨": "暂停加仓，核对买入跌幅弹窗提醒；首付池不参与抄底。",
    }[label]
    return {
        "label": label, "tone": tone, "score": final, "techScore": round(ts, 2),
        "policyScore": round(pol, 2), "goodHits": g, "badHits": b,
        "summary": summary, "action": action, "positionNote": pos_note,
        "position": {"label": tech["zone"]["label"], "percentile": round(pp, 1)},
        "bench": {"name": "国债指数", "lastDate": bars[-1]["date"] if bars else "—",
                  "point": closes[-1], "chgPct": last_chg},
        "tech": {
            "trend": tech["trend"], "zone": tech["zone"], "outlook": tech["outlook"],
            "boll": tech["boll"], "macd": tech["macd"], "ma5": tech["ma5"],
            "ma20": tech["ma20"], "ma60": tech["ma60"],
            "pricePercentile": tech["pricePercentile"], "slope20": tech["slope20"],
        },
        "news": [dict(x, tone=news_tone(x["title"])[0]) for x in news],
        "official": [dict(x, tone=news_tone(x["title"])[0]) for x in official],
        "bars": [{"date": b["date"][5:], "close": b["close"]} for b in bars[-60:]],
    }


# ---------------------------------------------------------------------------
# 7) 自选债券基金
# ---------------------------------------------------------------------------
def fund_type(code):
    try:
        j = get(FUND_SEARCH, {"m": 1, "key": code}, timeout=12, retries=2,
                headers={"Referer": "https://fund.eastmoney.com/"})
        ds = j.get("Datas") or []
        if ds:
            fb = ds[0].get("FundBaseInfo") or {}
            return ds[0].get("NAME") or "", fb.get("FTYPE") or "", fb.get("ISBUY") or ""
    except Exception:
        pass
    return "", "", ""


def benchmark_for(ftype):
    for keys, sec, name in TYPE_INDEX:
        if any(k in (ftype or "") for k in keys):
            return sec, name
    return "1.000012", "国债指数"


def build_fund(code, rank_map, quote_map, kline_map, data_date, trace, buy_records=None):
    """组装单只自选基金的全部展示字段与技术依据。"""
    out = {"code": code, "name": "", "ftype": "", "isBuy": "", "benchmark": "",
           "nav": None, "navSeries": [], "realtime": None, "estChg": None,
           "main": [], "ddMonth": None, "ddMonthPair": (None, None),
           "sinceBuy": None, "buyRecords": [], "drop": False,
           "tech": None, "chart": None, "basis": [], "degraded": []}
    name, ftype, isbuy = fund_type(code)
    out["name"], out["ftype"], out["isBuy"] = name or f"债券基金 {code}", ftype, isbuy
    sec, bname = benchmark_for(ftype)
    out["benchmark"] = bname
    trace.append({"provider": "天天基金", "endpoint": f"FundSearchAPI.ashx?key={code}",
                  "note": f"基金名称/类型校验：{out['name']}（{ftype or '类型未返回'}，可申购={isbuy or '未知'}）",
                  "status": "ok" if name else "partial"})
    try:
        pz = fetch_pz(code)
    except Exception as e:
        out["degraded"].append(f"净值序列获取失败：{str(e)[:70]}")
        out["basis"].append("净值序列获取失败，本只基金指标暂不可用（如实标注，不编造）")
        return out
    series = pz["series"]
    out["navSeries"] = [[p["date"], p["acc"], p["unit"], p["chg"]] for p in series]
    if pz["name"] and not name:
        out["name"] = pz["name"]
    latest = series[-1]
    out["nav"] = {"value": latest["unit"] or latest["acc"], "acc": latest["acc"],
                  "date": latest["date"], "dailyChg": latest["chg"] or 0.0}
    # 近一月最大回撤（自然日 30 天）——仅作展示参考，不再作为通知触发条件
    m_cut = (datetime.strptime(data_date, "%Y-%m-%d") - timedelta(days=MONTH_DAYS)).date().isoformat()
    mpts = since(series, m_cut)
    dd, pair = max_drawdown(mpts) if len(mpts) >= 5 else (None, (None, None))
    out["ddMonth"] = dd
    out["ddMonthPair"] = pair
    # 相对上次买入涨跌幅（手机弹窗提醒的判定依据）
    sb = compute_since_buy(series, buy_records or [], DROP_THRESHOLD)
    out["sinceBuy"] = sb
    out["buyRecords"] = sb["records"]
    out["drop"] = sb["drop"]
    # 对标指数实时行情
    q = quote_map.get(sec)
    out["realtime"] = q
    est = None
    if q:
        est = round(q["chgPct"], 2)
    out["estChg"] = est
    # 技术面基于基金累计净值序列
    closes = [p["acc"] for p in series if p["acc"] is not None]
    tech = compute_tech(closes)
    if not tech:
        out["degraded"].append("净值样本不足 60 条，技术指标不可用")
        return out
    out["tech"] = tech
    out["chart"] = build_chart(series, tech)
    # 五大主展示项（与「指数跟踪」字段一致，口径改写为债券基金）
    q = q or {}
    pt = f"{q['point']:.2f}" if q.get("point") is not None else "—"
    out["main"] = [
        {"k": "当天实时指数", "v": pt,
         "sub": f"{bname} 盘中点位；场外债基无盘口",
         "tone": "up" if (est or 0) > 0 else ("down" if (est or 0) < 0 else "neutral")},
        {"k": "涨幅估值", "v": (f"{est:+.2f}%" if est is not None else "—"),
         "sub": f"指数涨幅估算；已确认净值日涨幅 {out['nav']['dailyChg']:+.2f}%",
         "tone": "up" if (est or 0) > 0 else ("down" if (est or 0) < 0 else "neutral")},
        {"k": "成交量", "v": fmt_amount(q.get("amount")),
         "sub": f"{bname} 成交额；成交量 {fmt_amount(q.get('volume'))}手",
         "tone": "neutral"},
        {"k": "估值高低", "v": tech["valuation"]["label"],
         "sub": f"非 PE：累计净值近250日分位 {tech['pricePercentile']:.1f}%（≥70%偏高）",
         "tone": tech["valuation"]["tone"]},
        {"k": "未来走势预估", "v": tech["outlook"]["label"],
         "sub": f"BOLL+MACD+均线五因子 {tech['outlook']['score']} 分；不预示未来",
         "tone": tech["outlook"]["tone"]},
    ]
    # 技术面三段（简短标签）：趋势 / 区域 / 相对买入涨跌幅
    sb = out["sinceBuy"]
    sb_v = (f"{sb['chgPct']:+.2f}%" if sb and sb["chgPct"] is not None else "未买入")
    sb_tone = ("risk" if (sb and sb["drop"]) else
               ("up" if (sb and sb["chgPct"] is not None and sb["chgPct"] > 0) else
                ("down" if (sb and sb["chgPct"] is not None and sb["chgPct"] < 0) else "neutral")))
    sb_note = (f"较 {sb['buyDate']} 买入" if sb and sb["hasBuy"] else "先录入买入时间")
    out["techCards"] = [
        {"k": "当前趋势", "v": tech["trend"]["label"], "tone": tech["trend"]["tone"],
         "note": tech["trend"]["short"]},
        {"k": "所处区域", "v": tech["zone"]["label"], "tone": tech["zone"]["tone"],
         "note": tech["zone"]["short"]},
        {"k": "相对买入", "v": sb_v, "tone": sb_tone, "note": sb_note},
    ]
    # 灰字依据
    sb_basis = (
        f"相对上次买入：买入 {sb['buyDate']} 净值 {sb['buyNav']}（{sb['buyNavSrc']}）→ 最新 {sb['curDate']} 净值 {sb['curNav']}，"
        f"涨跌幅 {sb['chgPct']:+.2f}%，阈值 -{sb['threshold']:.1f}%（{'触发' if sb['drop'] else '未触发'}）；"
        f"共 {sb['buyCount']} 笔买入记录" if sb and sb["hasBuy"] and sb["chgPct"] is not None else
        "相对上次买入：尚未录入买入时间，暂不计算涨跌幅（如实标注，不编造）")
    out["basis"] = [
        f"净值来源：天天基金 pingzhongdata/{code}.js，累计净值序列 {len(series)} 条，最新 {latest['date']} 单位净值 {out['nav']['value']:.4f}",
        f"对标指数：{bname}（secid={sec}），实时点位 {pt}、涨幅 {q.get('chgPct', '—')}%、成交额 {fmt_amount(q.get('amount'))}",
        f"近一月最大回撤：{dd:.2f}%（{m_cut} 起 {len(mpts)} 条净值；峰 {pair[0]} → 谷 {pair[1]}），仅作展示、不作通知",
        sb_basis,
        f"BOLL(20,2)：中轨 {tech['boll']['mid']:.4f}、上轨 {tech['boll']['upper']:.4f}、下轨 {tech['boll']['lower']:.4f}，%B={tech['boll']['pctB']:.1f}%，带宽 {tech['boll']['bandwidth']:.2f}%",
        f"MACD(12,26,9)：DIF={tech['macd']['dif']:.5f}、DEA={tech['macd']['dea']:.5f}、柱={tech['macd']['hist']:.5f}（DIF{'>' if tech['macd']['difUp'] else '<'}DEA）",
        f"均线：MA5={tech['ma5']:.4f}、MA20={tech['ma20']:.4f}、MA60={tech['ma60']:.4f}，20日斜率 {tech['slope20']:.2f}%",
        f"趋势判定：{tech['trend']['label']}（{tech['trend']['detail']}）",
        f"区域判定：{tech['zone']['label']}（{tech['zone']['detail']}）",
        f"估值高低：{tech['valuation']['label']}（{tech['valuation']['basis']}）",
    ]
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=POOL_DEFAULT)
    args = ap.parse_args()

    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    trace = []

    all_items, universe, _ = fetch_rank()
    data_date = max((x["navDate"] for x in all_items if x["navDate"]), default=today)
    rank = build_rank(all_items, universe, data_date, args.pool, trace)

    # 指数行情与 K 线
    quotes, klines = {}, {}
    for bi in BOND_INDEX:
        try:
            quotes[bi["secid"]] = fetch_quote(bi["secid"])
            trace.append({"provider": "新浪财经 / 同花顺", "endpoint": f"指数实时行情 secid={bi['secid']}",
                          "note": f"{bi['name']} 实时点位/涨跌/成交额", "status": "ok" if quotes[bi["secid"]] else "partial"})
        except Exception as e:
            quotes[bi["secid"]] = None
            log(f"{bi['name']} 行情失败：{e}")
    for sec in [BENCH_SEC]:
        try:
            klines[sec] = fetch_kline(sec, 250)
            trace.append({"provider": "新浪财经 / 同花顺", "endpoint": f"指数日K kline secid={sec}",
                          "note": f"晴雨表技术基准日K {len(klines[sec])} 根（BOLL/MACD/均线）", "status": "ok"})
        except Exception as e:
            klines[sec] = []
            log(f"基准K线失败：{e}")

    # 政策新闻
    news, news_total, bond_total = fetch_fastnews(today)
    official = fetch_official()
    trace.append({"provider": "东方财富", "endpoint": "7×24 快讯 fastColumn=102",
                  "note": f"全天 {news_total} 条，聚焦国内债市 {bond_total} 条，取 {len(news)} 条",
                  "status": "ok" if news else "partial"})
    trace.append({"provider": "央行 / 财政部", "endpoint": "官网新闻栏目",
                  "note": f"官方债券/政策标题 {len(official)} 条", "status": "ok" if official else "partial"})

    market_cards = []
    for bi in BOND_INDEX:
        q = quotes.get(bi["secid"]) or {}
        market_cards.append({"name": bi["name"], "point": q.get("point"),
                             "chgPct": q.get("chgPct"), "chgAmt": q.get("chgAmt")})

    baro = build_barometer(klines.get(BENCH_SEC) or [], quotes, news, official, trace)

    # 买入流水 / 手机弹窗提醒配置（页面录入或设置后导出替换这两个文件，次日更新读取）
    buy_cfg = {}
    popup_cfg = {}
    if (OUT / BUY_RECORDS_FILE).exists():
        try:
            buy_cfg = json.loads((OUT / BUY_RECORDS_FILE).read_text("utf-8"))
        except Exception as e:
            log(f"买入记录读取失败（按未录入处理）：{e}")
    if (OUT / POPUP_CONFIG_FILE).exists():
        try:
            popup_cfg = json.loads((OUT / POPUP_CONFIG_FILE).read_text("utf-8"))
        except Exception as e:
            log(f"弹窗提醒配置读取失败（按默认开启处理）：{e}")
    buy_records_map = buy_cfg.get("records", {}) if isinstance(buy_cfg, dict) else {}
    popup_push = {"enabled": bool(popup_cfg.get("enabled", True)),
                  "systemNotify": bool(popup_cfg.get("systemNotify", False)),
                  "threshold": float(popup_cfg.get("threshold", DROP_THRESHOLD)),
                  "channelName": "手机弹窗消息"}

    wl = json.loads((OUT / "watchlist.json").read_text("utf-8"))
    codes = [str(c) for c in wl.get("codes", []) if re.match(r"^\d{6}$", str(c))] or ["005909"]
    funds = {}
    for c in codes:
        log(f"组装自选基金 {c}")
        funds[c] = build_fund(c, rank, quotes, klines, data_date, trace,
                              buy_records=buy_records_map.get(c, []))

    # 手机弹窗提醒触发：相对上次买入「跌幅」超过阈值（不再用近一月回撤）
    alerts = []
    for c, f in funds.items():
        sb = f.get("sinceBuy")
        if sb and sb.get("drop"):
            alerts.append({"code": c, "name": f["name"],
                           "buyDate": sb["buyDate"], "buyNav": sb["buyNav"],
                           "curNav": sb["curNav"], "curDate": sb["curDate"],
                           "chgPct": sb["chgPct"], "threshold": sb["threshold"],
                           "navDate": (f["nav"] or {}).get("date"),
                           "status": sb["status"]})
    has_buy = sum(1 for f in funds.values() if (f.get("sinceBuy") or {}).get("hasBuy"))
    trace.append({"provider": "本机数据", "endpoint": BUY_RECORDS_FILE,
                  "note": f"买入流水：{has_buy}/{len(funds)} 只自选已录入买入时间；"
                          "按真实单位净值计算相对上次买入涨跌幅", "status": "ok"})
    trace.append({"provider": "手机弹窗提醒", "endpoint": POPUP_CONFIG_FILE,
                  "note": f"弹窗提醒{'开启' if popup_push['enabled'] else '关闭'}、系统通知意向"
                          f"{'开' if popup_push['systemNotify'] else '关'}；"
                          f"跌幅 > {popup_push['threshold']:.1f}% 触发时页面自动弹出消息卡片，"
                          "无需外部网关与密钥",
                  "status": "ok" if popup_push["enabled"] else "partial"})

    meta = {
        "date": today, "weekday": "周" + "一二三四五六日"[now.weekday()],
        "navDate": data_date, "dataTime": now.strftime("%H:%M"),
        "watchlist": codes,
        "dataSource": "东方财富 / 天天基金 / 中国人民银行 / 财政部官网",
        "universe": universe, "eligible": rank["eligible"],
        "excluded": rank["excluded"], "abnormalLine": rank["abnormalLine"],
        "pool": rank["pool"], "computed": rank["computed"],
        "dropThreshold": DROP_THRESHOLD, "hasBuyCount": has_buy,
    }
    snapshot = {"meta": meta, "marketCards": market_cards, "rank": rank,
                "funds": funds, "barometer": baro, "alerts": alerts,
                "dropThreshold": DROP_THRESHOLD, "buyRecords": buy_records_map,
                "popupPush": popup_push,
                "channels": [
                    {"short": "天天基金", "name": "基金排行 / 净值序列", "status": "ok"},
                    {"short": "东方财富", "name": "债券指数行情 / 日K / 快讯", "status": "ok"},
                    {"short": "央行", "name": "货币政策官方新闻", "status": "ok" if official else "partial"},
                    {"short": "财政部", "name": "国债发行官方新闻", "status": "ok" if official else "partial"},
                    {"short": "技术指标", "name": "BOLL / MACD / 均线本机计算", "status": "ok"},
                    {"short": "弹窗提醒", "name": "手机页面消息卡片·" +
                     ("已开启" if popup_push["enabled"] else "已关闭"),
                     "status": "ok" if popup_push["enabled"] else "partial"},
                ],
                "trace": trace}
    (OUT / "bond-feed.json").write_text(json.dumps({
        "meta": meta, "rankRawCount": len(all_items), "rankTop": all_items[:20],
        "newsRaw": news, "officialRaw": official, "trace": trace,
    }, ensure_ascii=False, indent=1), "utf-8")
    (OUT / "bond-snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), "utf-8")
    log(f"完成：TOP5 {len(rank['top5'])} 只 / 自选 {len(funds)} 只 / 触发提醒 {len(alerts)} 只 / 晴雨 {baro['label'] if baro else '—'}")
    log(f"输出：{OUT / 'bond-feed.json'} 与 {OUT / 'bond-snapshot.json'}")


if __name__ == "__main__":
    main()
