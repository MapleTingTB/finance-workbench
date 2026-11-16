#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_hub · 多源行情兼容层
============================================================================
背景：东方财富 push2 / push2his 行情域名在本环境被 WAF 拦截（TCP 建连后
直接断开），导致理财工作台四个模块每日更新全部降级。本模块把对 push2 的
请求**就地转发**到已实测可用的公开数据源，并返回与东财完全同构的 JSON，
使原有采集脚本的解析逻辑零改动。

实测可用数据源（2026-11-12 复核）：
  · 新浪财经   hq.sinajs.cn                 实时行情（指数 / 债券 / 外围）
  · 新浪财经   quotes.sina.cn              日 K 线，单请求 250 根
  · 新浪财经   vip.stock.finance.sina.com  全市场 A 股分页（涨跌家数 / 成交额）
  · 同花顺     d.10jqka.com.cn             日 K 线（含成交额）、板块指数 bk_
  · 同花顺     q.10jqka.com.cn/thshy/      行业板块清单（名称 → 881xxx）
  · 东方财富   datacenter-web.eastmoney.com 财报报表（自带 BOARD_NAME 行业分类）
  · 东方财富   fund.eastmoney.com          基金净值序列 pingzhongdata
  · 腾讯行情   qt.gtimg.cn                 外围市场实时备份

设计原则：
  1. 只拦截 push2/push2his 两个域名，其余请求原样透传；
  2. 所有数值来自真实接口，取不到就返回空结构让原脚本如实降级，**不编造**；
  3. 结果按 TTL 缓存，避免翻页型调用重复打满接口；
  4. 板块使用自定义内部 ID（T881xxx）串联清单→K线→成分股，避免伪造东财代码。

用法：在采集脚本顶部（import requests 之后）加一行
    import market_hub; market_hub.install()
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

# 模块加载时保存真实函数，避免 install() 后内部请求递归进入自身
_REAL_GET = requests.get

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
TIMEOUT = 9

_cache: dict = {}
_lock = threading.Lock()
_board_ctx: dict = {}       # 内部板块 ID -> (申万一级名称, 同花顺 881xxx)
_installed = False

SW_LEVEL1 = [
    "电子", "银行", "医药生物", "非银金融", "电力设备", "通信", "机械设备",
    "石油石化", "有色金属", "公用事业", "基础化工", "汽车", "食品饮料",
    "计算机", "交通运输", "国防军工", "家用电器", "煤炭", "建筑装饰",
    "建筑材料", "房地产", "商贸零售", "社会服务", "纺织服饰", "轻工制造",
    "农林牧渔", "钢铁", "环保", "美容护理", "传媒", "综合",
]

# 申万一级行业 -> 同花顺代表板块名（用于取板块真实 K 线；名称取不到则跳过该行业）
SW_TO_THS = {
    "电子": "半导体", "银行": "银行", "医药生物": "化学制药", "非银金融": "证券",
    "电力设备": "电池", "通信": "通信服务", "机械设备": "通用设备",
    "石油石化": "炼化及贸易", "有色金属": "工业金属", "公用事业": "电力",
    "基础化工": "化学制品", "汽车": "乘用车", "食品饮料": "白酒",
    "计算机": "软件开发", "交通运输": "物流", "国防军工": "军工装备",
    "家用电器": "白色家电", "煤炭": "煤炭开采", "建筑装饰": "基础建设",
    "建筑材料": "水泥", "房地产": "房地产", "商贸零售": "零售",
    "社会服务": "旅游及景区", "纺织服饰": "服装家纺", "轻工制造": "家居用品",
    "农林牧渔": "养殖业", "钢铁": "钢铁", "环保": "环境治理",
    "美容护理": "化妆品", "传媒": "游戏", "综合": "综合",
}

# 中证主题指数（免费源均无 K 线）-> 对应场外基金，用真实单位净值序列计算技术指标
SECID_FUND = {
    "2.930901": "012769", "2.930713": "012734", "2.930734": "003359",
    "2.930598": "011036", "2.931160": "020900",
}

# 东财 secid -> 同花顺代码。
# 重要：同花顺 `hs_` 前缀口径不统一，实测 hs_000905 / hs_000852 / hs_000688 /
# hs_000012 返回的是同代码**个股**价格（10 元 / 6 元 / 43 元 / 4 元），不是指数点位。
# 仅以下 3 个经逐根核对确认返回真实指数点位且含成交额，其余一律走新浪。
THS_SEC = {
    "1.000001": "hs_1A0001",   # 上证指数 4043→3680
    "0.399006": "hs_399006",   # 创业板指 3531→2572
    "0.399481": "hs_399481",   # 企债指数 127→128
}

# 外围市场 secid -> 腾讯代码
OVERSEAS_TENCENT = {
    "100.NDX": "usNDX", "100.DJIA": "usDJI", "100.SPX": "usINX",
    "100.HSI": "hkHSI", "124.HSTECH": "hkHSTECH",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _http(url: str, ref: str | None = None, timeout: int = TIMEOUT,
          retries: int = 2, enc: str = "utf-8") -> str:
    hdr = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if ref:
        hdr["Referer"] = ref
    last = None
    for i in range(retries):
        try:
            r = _REAL_GET(url, headers=hdr, timeout=timeout, verify=False)
            if r.status_code == 200:
                return r.content.decode(enc, "ignore") if enc else r.text
            last = RuntimeError("HTTP %s" % r.status_code)
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.25 * (i + 1))
    raise last


def _cached(key, fn, ttl=900):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:
        with _lock:
            return _cache[key][1] if key in _cache else None
    with _lock:
        _cache[key] = (now, val)
    return val


def _num(v, default=None):
    try:
        if v in (None, "", "-"):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _query(url: str) -> dict:
    """手工解析 query：保留 '+' 原样（板块 fs=m:90+t:2 里的 + 不能被转成空格）。"""
    out = {}
    for kv in urlparse(url).query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = v
    return out


def _f(v, digits=2):
    return round(v, digits) if isinstance(v, (int, float)) else v


class _Resp:
    """最小 Response 替身，满足 .json() / .text / .raise_for_status() / .content。"""

    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.text = json.dumps(data, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


# ---------------------------------------------------------------------------
# 数据源 1/2：实时行情（新浪 / 腾讯）
# ---------------------------------------------------------------------------
def _secid_to_sina(secid: str):
    if "." not in secid:
        return None
    mkt, code = secid.split(".", 1)
    if mkt == "1":
        return "sh" + code
    if mkt == "0":
        return "sz" + code
    if mkt == "2":
        return "sh" + code
    return None


def _sina_realtime(symbols):
    """返回 {symbol: {name, price, prev, open, high, low, volume, amount}}。"""
    if not symbols:
        return {}
    out = {}
    try:
        txt = _http("https://hq.sinajs.cn/list=" + ",".join(symbols),
                    ref="https://finance.sina.com.cn", enc="gbk")
        for line in txt.strip().split("\n"):
            if '="' not in line:
                continue
            sym = line.split("=")[0].replace("var hq_str_", "").strip()
            a = line.split('="')[1].rstrip('";').split(",")
            if len(a) < 10:
                continue
            price, prev = _num(a[3]), _num(a[2])
            # 指数/债券在盘中可能把 0 放在当前价，用昨收兜底
            if not price:
                price = prev
            out[sym] = {"name": a[0], "price": price, "prev": prev,
                        "open": _num(a[1]), "high": _num(a[4]), "low": _num(a[5]),
                        "volume": _num(a[8], 0), "amount": _num(a[9], 0)}
    except Exception:
        pass
    return out


def _tencent_realtime(symbols):
    out = {}
    if not symbols:
        return out
    try:
        txt = _http("https://qt.gtimg.cn/q=" + ",".join(symbols), enc="gbk")
        for line in txt.strip().split("\n"):
            if '="' not in line:
                continue
            sym = line.split("=")[0].replace("v_", "").strip()
            a = line.split('="')[1].rstrip('";').split("~")
            if len(a) < 7:
                continue
            out[sym] = {"name": a[1], "price": _num(a[3]), "prev": _num(a[4]),
                        "open": _num(a[5]), "high": None, "low": None,
                        "volume": _num(a[6], 0), "amount": None}
    except Exception:
        pass
    return out


def _csi_latest(code: str):
    """中证指数官网日线兜底（市场代码 2.xxxx 新浪/腾讯均无行情）。

    返回 {name, price, prev, open, high, low, volume, amount, date}；
    官方接口只提供收盘口径，盘中如实沿用最新收盘值。
    """
    import datetime as _dt
    today = _dt.date.today()
    start = today - _dt.timedelta(days=30)
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           "?indexCode=" + code +
           "&startDate=" + start.strftime("%Y%m%d") +
           "&endDate=" + today.strftime("%Y%m%d"))
    rows = (json.loads(_http(url, ref="https://www.csindex.com.cn/"))
            .get("data") or [])
    if not rows:
        return None
    last = rows[-1]
    close = _num(last.get("close"))
    if close is None:
        return None
    chg = _num(last.get("change"))
    d = str(last.get("tradeDate") or "")
    date = "-".join([d[:4], d[4:6], d[6:8]]) if len(d) == 8 else d
    return {
        "name": last.get("indexNameCn") or "",
        "price": close,
        "prev": (close - chg) if chg is not None else None,
        "open": _num(last.get("open")), "high": _num(last.get("high")),
        "low": _num(last.get("low")),
        "volume": (_num(last.get("tradingVol"), 0) or 0) / 100.0,   # 股 -> 手
        "amount": (_num(last.get("tradingValue"), 0) or 0) * 1e8,   # 亿 -> 元
        "date": date,
    }


def _realtime(secids):
    """混合源：A 股/债券走新浪，外围走腾讯，中证指数走官网收盘兜底。返回 {secid: quote}。"""
    sina_map, tx_map, res = {}, {}, {}
    for sid in secids:
        sym = _secid_to_sina(sid)
        if sym:
            sina_map[sym] = sid
        elif sid in OVERSEAS_TENCENT:
            tx_map[OVERSEAS_TENCENT[sid]] = sid
    sq = _sina_realtime(list(sina_map))
    for sym, sid in sina_map.items():
        q = sq.get(sym)
        if q and q.get("price"):
            res[sid] = q
    if tx_map:
        tq = _tencent_realtime(list(tx_map))
        for sym, sid in tx_map.items():
            q = tq.get(sym)
            if q and q.get("price"):
                res[sid] = q
    # 中证主题指数（2.93xxxx）：新浪/腾讯均不提供，用中证官网最新收盘兜底
    missing = [s for s in secids if s not in res and s.startswith("2.")]
    for sid in missing:
        code = sid.split(".", 1)[-1]
        q = _cached("csi:" + code, lambda c=code: _csi_latest(c), ttl=900)
        if q and q.get("price"):
            res[sid] = q
    return res


# ---------------------------------------------------------------------------
# 数据源 3：日 K 线（新浪 250 根 / 同花顺含成交额 / 基金净值）
# ---------------------------------------------------------------------------
def _to_klines(bars):
    """bars -> 东财 klines 字符串数组。

    东财字段顺序：日期,开,收,高,低,量,额,振幅,涨跌幅,涨跌额,换手率
    """
    lines, prev = [], None
    for b in bars:
        close = b.get("close")
        if close is None:
            continue
        o = b.get("open") or close
        h = b.get("high") or close
        lo = b.get("low") or close
        chg = _num(b.get("chg"))
        if chg is None and prev:
            chg = (close / prev - 1) * 100
        amplitude = _num(b.get("amplitude"))
        if amplitude is None and prev:
            amplitude = (h - lo) / prev * 100
        lines.append(",".join([
            b["date"],
            "%.4f" % o, "%.4f" % close,
            "%.4f" % h, "%.4f" % lo,
            "%.0f" % (b.get("volume") or 0), "%.2f" % (b.get("amount") or 0),
            "%.3f" % (amplitude or 0), "%.3f" % (chg or 0),
            "%.4f" % (close - prev) if prev else "0",
            "%.3f" % (b.get("turnover") or 0),
        ]))
        prev = close
    return lines


def _sina_kline(sym, lmt=250):
    """新浪日 K：字段 day/open/high/low/close/volume（无成交额，如实留 0）。"""
    # 注意：URL 里的 %20_ 不能与 % 格式化混用（%_ 会被当成格式符），故用拼接
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_/"
           "CN_MarketDataService.getKLineData?symbol=" + sym +
           "&scale=240&ma=no&datalen=" + str(max(lmt, 250)))
    txt = _http(url, ref="https://finance.sina.com.cn")
    m = re.search(r"var _\((\[.*\])\)", txt, re.S) or re.search(r"(\[\{.*\}\])", txt, re.S)
    if not m:
        return []
    arr = json.loads(m.group(1))
    out = []
    for e in arr:
        c = _num(e.get("close"))
        if c is None:
            continue
        out.append({"date": e.get("day"), "open": _num(e.get("open"), c),
                    "high": _num(e.get("high"), c), "low": _num(e.get("low"), c),
                    "close": c, "volume": _num(e.get("volume"), 0),
                    "amount": 0, "chg": None})
    return out[-lmt:]


def _parse_ths_data(txt):
    m = re.search(r"\((\{.*\})\)", txt, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1)).get("data") or ""
    except Exception:
        return []
    out = []
    for seg in data.split(";"):
        p = seg.split(",")
        if len(p) < 7:
            continue
        try:
            d = p[0]
            date = "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else d
            out.append({"date": date, "open": _num(p[1]), "high": _num(p[2]),
                        "low": _num(p[3]), "close": _num(p[4]),
                        "volume": _num(p[5], 0), "amount": _num(p[6], 0),
                        "turnover": _num(p[7]) if len(p) > 7 else None,
                        "chg": None})
        except (ValueError, IndexError):
            continue
    return [b for b in out if b["close"] is not None]


def _ths_kline(sym, lmt=250):
    """同花顺日 K：只取 last.js（约 140 根，含真实成交额）。

    不拼年份文件：实测部分代码的年份文件与 last.js 口径不一致
    （如 hs_1A0001 的 last.js 是上证指数 3680 点，2025.js 却是 11 元个股），
    拼接会污染技术指标。140 根已足够 BOLL / MACD / MA60 计算。
    """
    base = "https://d.10jqka.com.cn/v6/line/%s/01/" % sym
    try:
        rows = _parse_ths_data(_http(base + "last.js", enc="gbk"))
    except Exception:
        return []
    rows.sort(key=lambda b: b["date"])
    return rows[-lmt:]


def _fund_kline(code, lmt=250):
    """场外基金真实单位净值序列（天天基金 pingzhongdata）作为技术指标输入。

    净值只有单一价格，故 open/high/low 同取该净值（口径定义，非编造行情），
    成交量/成交额留 0（场外基金无成交量），日涨跌幅取真实 equityReturn。
    """
    txt = _http("https://fund.eastmoney.com/pingzhongdata/%s.js" % code,
                ref="https://fund.eastmoney.com/")
    m = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    if not m:
        return []
    arr = json.loads(m.group(1))
    out = []
    for e in arr:
        nav = _num(e.get("y"))
        x = e.get("x")
        if nav is None or not x:
            continue
        d = (datetime.utcfromtimestamp(x / 1000) + timedelta(days=1)).strftime("%Y-%m-%d")
        out.append({"date": d, "open": nav, "high": nav, "low": nav,
                    "close": nav, "volume": 0, "amount": 0,
                    "chg": _num(e.get("equityReturn"), 0)})
    return out[-lmt:]


def _fresh(bars, max_lag_days=15):
    """数据末根必须落在近 15 个自然日内，防止拿停更多年的历史序列算今日指标。

    实测新浪 sh000832（中证转债）日 K 停在 2018-12-25，若不校验会把 8 年前
    的数据当成当前行情使用。
    """
    if not bars:
        return False
    try:
        last = datetime.strptime(bars[-1]["date"], "%Y-%m-%d")
        return (datetime.now() - last).days <= max_lag_days
    except Exception:
        return False


def _kline_for(secid, lmt=250):
    """按 secid 分层选源，返回东财 klines 格式。"""
    # 1) 仅 3 个已逐根核对的同花顺指数（含真实成交额）
    ths = THS_SEC.get(secid)
    if ths:
        try:
            bars = _ths_kline(ths, lmt)
            if len(bars) >= 30 and _fresh(bars):
                return _to_klines(bars)
        except Exception:
            pass
    # 2) 新浪 250 根（真实指数点位；无成交额字段，如实留 0）
    sym = _secid_to_sina(secid)
    if sym and not secid.startswith("2."):
        try:
            bars = _sina_kline(sym, lmt)
            if len(bars) >= 30 and _fresh(bars):
                return _to_klines(bars)
        except Exception:
            pass
    # 3) 中证主题指数 -> 场外基金真实单位净值序列
    fund = SECID_FUND.get(secid)
    if fund:
        try:
            bars = _fund_kline(fund, lmt)
            if len(bars) >= 30 and _fresh(bars):
                return _to_klines(bars)
        except Exception:
            pass
    # 无可靠来源时返回空，由原脚本如实降级（不编造、不用过期数据）
    return []


# ---------------------------------------------------------------------------
# 数据源 4：行业板块（同花顺清单 + K 线 + 东财财报行业分类）
# ---------------------------------------------------------------------------
def _ths_board_codes():
    """抓同花顺行业板块清单页，返回 {板块名: 881xxx}。"""
    def load():
        txt = _http("https://q.10jqka.com.cn/thshy/", enc="gbk")
        ms = re.findall(r"detail/code/(881\d{3})/\"[^>]*>([^<]+)</a>", txt)
        if not ms:
            ms = re.findall(r"detail/code/(881\d{3})[^>]*>\s*([^<]+)</a>", txt)
        return {name.strip(): code for code, name in ms if name.strip()}
    return _cached("ths_boards", load) or {}


def _board_list():
    """构造申万一级行业清单，内部 ID 形如 T881121。"""
    def load():
        codes = _ths_board_codes()
        alias = {v: k for k, v in codes.items()}
        out = []
        for name in SW_LEVEL1:
            cand = SW_TO_THS.get(name, name)
            ths = codes.get(cand)
            if not ths:
                # 放宽：同花顺名包含候选词，或候选词包含同花顺名
                for n, c in codes.items():
                    if cand in n or n in cand:
                        ths = c
                        break
            if not ths:
                continue
            bid = "T" + ths
            with _lock:
                _board_ctx[bid] = (name, "bk_" + ths)
            out.append({"f12": bid, "f14": name, "f20": None, "f9": None, "f3": None})
        return out
    return _cached("board_list", load) or []


def _board_changes():
    """用同花顺板块指数真实 K 线算最近一日涨跌幅。"""
    def load():
        boards = _board_list()
        out = {}
        for b in boards:
            bid = b["f12"]
            with _lock:
                ctx = _board_ctx.get(bid)
            if not ctx:
                continue
            try:
                bars = _ths_kline(ctx[1], 10)
                if len(bars) >= 2 and bars[-2]["close"]:
                    out[bid] = round((bars[-1]["close"] / bars[-2]["close"] - 1) * 100, 2)
            except Exception:
                pass
        return out
    return _cached("board_changes", load) or {}


def _kline_for_board(bid, lmt=70):
    with _lock:
        ctx = _board_ctx.get(bid) or _board_ctx.get("T" + bid)
    if not ctx:
        # 形如 90.T881121 或 90.881121
        code = re.sub(r"[^0-9]", "", bid)
        if len(code) == 6 and code.startswith("881"):
            ctx = ("", "bk_" + code)
    if not ctx:
        return []
    try:
        bars = _ths_kline(ctx[1], lmt)
        if len(bars) < 25:
            return []
        return _to_klines(bars)
    except Exception:
        return []


def _dc_members(board_name):
    """东财 datacenter-web 三季报报表自带 BOARD_NAME，按行业取真实成分股代码。"""
    def load():
        by_name = {}
        for pn in range(1, 14):
            url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
                   "reportName=RPT_LICO_FN_CPD&columns=SECURITY_CODE,BOARD_NAME"
                   "&filter=(REPORTDATE='2026-09-30')&pageNumber=%d&pageSize=500"
                   "&sortColumns=SECURITY_CODE&sortTypes=1" % pn)
            try:
                j = json.loads(_http(url, ref="https://data.eastmoney.com/"))
            except Exception:
                break
            rows = (j.get("result") or {}).get("data") or []
            if not rows:
                break
            for r in rows:
                n, c = r.get("BOARD_NAME"), r.get("SECURITY_CODE")
                if n and c:
                    by_name.setdefault(n, []).append(c)
            if len(rows) < 500:
                break
        return by_name
    allmap = _cached("dc_members", load) or {}
    if board_name in allmap:
        return allmap[board_name]
    # 放宽匹配（同花顺细分板块名 / 申万名互含）
    hit = []
    for n, codes in allmap.items():
        if board_name in n or n in board_name:
            hit += codes
    return list(dict.fromkeys(hit))


# ---------------------------------------------------------------------------
# 数据源 5：全市场涨跌家数（新浪 A 股分页）
# ---------------------------------------------------------------------------
def _market_rows():
    """并发抓新浪 hs_a 全部 A 股，返回东财 clist diff 同构行。"""
    def load():
        base = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData?page=%d&num=80&sort=symbol&asc=1&node=hs_a")
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def page(pn):
            try:
                txt = _http(base % pn, ref="https://finance.sina.com.cn")
                return json.loads(txt) or []
            except Exception:
                return []

        rows, empty = [], 0
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(page, p): p for p in range(1, 80)}
            bucket = {}
            for f in as_completed(futs):
                bucket[futs[f]] = f.result() or []
        for p in range(1, 80):
            data = bucket.get(p) or []
            if not data:
                empty += 1
                if empty >= 3:
                    break
                continue
            empty = 0
            for x in data:
                ch = _num(x.get("changepercent"))
                if ch is None:
                    continue
                rows.append({"f12": x.get("code"), "f14": x.get("name"),
                             "f2": _num(x.get("trade")), "f3": round(ch, 2),
                             "f6": _num(x.get("amount"), 0)})
        return rows
    return _cached("market_rows", load, ttl=600) or []


# ---------------------------------------------------------------------------
# 请求分派
# ---------------------------------------------------------------------------
def _emulate(url, params):
    q = _query(url)
    if params:
        q.update({k: str(v) for k, v in params.items()})
    path = urlparse(url).path
    pn = int(_num(q.get("pn"), 1) or 1)
    pz = int(_num(q.get("pz"), 100) or 100)

    try:
        # ---- K 线 ----
        if path.endswith("/stock/kline/get"):
            secid = q.get("secid", "")
            lmt = int(_num(q.get("lmt"), 250) or 250)
            if secid.startswith("90."):
                ks = _kline_for_board(secid[3:], lmt or 70)
            else:
                ks = _kline_for(secid, lmt)
            return {"data": {"klines": ks, "name": "", "code": secid}}

        # ---- 资金流：无等价公开源，如实返回空让原脚本降级 ----
        if path.endswith("/stock/fflow/daykline/get"):
            return {"data": {"klines": []}}

        # ---- 单标的实时行情 ----
        if path.endswith("/stock/get"):
            secid = q.get("secid", "")
            rt = _realtime([secid]).get(secid)
            if not rt:
                return {"data": None}
            p, prev = rt["price"], rt.get("prev")
            chg_pct = (p / prev - 1) * 100 if prev else 0
            return {"data": {
                "f43": round(p * 100), "f44": round((rt.get("high") or p) * 100),
                "f45": round((rt.get("low") or p) * 100),
                "f46": round((rt.get("open") or p) * 100),
                "f47": rt.get("volume"), "f48": rt.get("amount"),
                "f57": secid.split(".")[-1], "f58": rt.get("name") or "",
                "f60": round((prev or p) * 100),
                "f169": round((p - (prev or p)) * 100),
                "f170": round(chg_pct * 100),
                "f86": int(time.time()),
            }}

        # ---- 多标的实时行情（ulist.np）----
        if path.endswith("/ulist.np/get"):
            secids = [s for s in q.get("secids", "").split(",") if s]
            rt = _realtime(secids)
            diff = []
            for sid in secids:
                r = rt.get(sid)
                if not r:
                    continue
                p, prev = r["price"], r.get("prev")
                chg = (p / prev - 1) * 100 if prev else 0
                amp = ((r.get("high") or p) - (r.get("low") or p)) / prev * 100 if prev else 0
                diff.append({"f12": sid.split(".")[-1], "f14": r.get("name") or "",
                             "f2": _f(p), "f3": _f(chg), "f4": _f(p - (prev or p)),
                             "f6": r.get("amount") or 0, "f7": _f(amp)})
            return {"data": {"diff": diff, "total": len(diff)}}

        # ---- 列表（板块 / 涨跌家数 / 成分股）----
        if path.endswith("/clist/get"):
            fs = q.get("fs", "")

            # 全市场涨跌统计
            if "m:0+t:6" in fs or "m:1+t:2" in fs:
                rows = _market_rows()
                return {"data": {"diff": rows[(pn - 1) * pz: pn * pz],
                                 "total": len(rows)}}

            # 板块成分股
            if fs.startswith("b:"):
                bid = fs[2:]
                with _lock:
                    ctx = _board_ctx.get(bid)
                name = ctx[0] if ctx else ""
                codes = _dc_members(name) if name else []
                page = codes[(pn - 1) * pz: pn * pz]
                return {"data": {"diff": [{"f12": c, "f14": c} for c in page],
                                 "total": len(codes)}}

            # 板块清单 / 涨跌榜
            if "m:90" in fs:
                boards = _board_list()
                changes = _board_changes() if "f3" in q.get("fields", "") else {}
                rows = []
                for b in boards:
                    rows.append({"f12": b["f12"], "f14": b["f14"],
                                 "f3": changes.get(b["f12"]),
                                 "f20": None, "f9": None})
                withchg = [r for r in rows if r["f3"] is not None]
                if withchg and q.get("fid") == "f3":
                    rows = sorted(withchg, key=lambda r: r["f3"],
                                  reverse=(q.get("po") == "1"))
                return {"data": {"diff": rows[(pn - 1) * pz: pn * pz],
                                 "total": len(rows)}}

            return {"data": {"diff": [], "total": 0}}
    except Exception:
        pass
    return {"data": {"diff": [], "klines": [], "total": 0}}


def _is_em(url):
    u = str(url)
    return ("push2.eastmoney.com" in u) or ("push2his.eastmoney.com" in u)


def install():
    """安装拦截：只接管 push2 / push2his，其余请求原样透传。"""
    global _installed
    if _installed:
        return
    orig_get = requests.get
    orig_session_get = requests.Session.get

    def patched_get(url, params=None, **kw):
        if _is_em(url):
            return _Resp(_emulate(url, params))
        return orig_get(url, params=params, **kw)

    def patched_session_get(self, url, params=None, **kw):
        if _is_em(url):
            return _Resp(_emulate(url, params))
        return orig_session_get(self, url, params=params, **kw)

    requests.get = patched_get
    requests.Session.get = patched_session_get
    _installed = True


# ---------------------------------------------------------------------------
# 供新闻模块直接调用的增强源（证券时报 / 雪球）
# ---------------------------------------------------------------------------
def stcn_news(limit=12):
    """证券时报网要闻页真实标题（人民财讯 · 证券时报）。"""
    try:
        txt = _http("https://www.stcn.com/article/list/yw.html")
        items = re.findall(r'<a[^>]+href="(/article/detail/\d+\.html)"[^>]*>([^<]{8,90})</a>', txt)
        out, seen = [], set()
        for href, title in items:
            t = re.sub(r"\s+", "", title).strip()
            if not t or t in seen:
                continue
            seen.add(t)
            out.append({"title": t, "url": "https://www.stcn.com" + href,
                        "source": "证券时报 · 人民财讯"})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def xueqiu_hots(limit=8):
    """雪球热议（hots.json）。仅作市场情绪参考，不作行情数值。

    雪球对 hots.json 有随机 JS 反爬校验（返回 HTML 而非 JSON），故做多次
    会话预热 + 重试；仍受限则返回空列表，由调用方如实降级，不编造内容。
    """
    try:
        import requests as _rq
        for _ in range(3):
            s = _rq.Session()
            s.headers.update({
                "User-Agent": UA, "Referer": "https://xueqiu.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            try:
                s.get("https://xueqiu.com/", timeout=TIMEOUT, verify=False)
                s.get("https://xueqiu.com/hq", timeout=TIMEOUT, verify=False)
                r = s.get("https://xueqiu.com/statuses/hots.json?a=1&count=%d" % limit,
                          timeout=TIMEOUT, verify=False)
                body = (r.text or "").strip()
                if not body.startswith("["):
                    time.sleep(0.5)
                    continue
                arr = json.loads(body) or []
                out = []
                for x in arr:
                    raw = (x.get("description") or x.get("text")
                           or x.get("title") or "")
                    t = re.sub(r"<[^>]+>", "", raw).strip()
                    if t:
                        out.append({"text": t[:110], "source": "雪球 · 热议"})
                if out:
                    return out
            except Exception:
                pass
            time.sleep(0.5)
    except Exception:
        pass
    return []
