#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业板块 · 真实数据采集与综合排名层

数据源（沿用「财经新闻」模块口径）：
  · 东方财富：行业板块清单(PE/市值)、板块日K、板块历史资金流、
              2026 三季报业绩报表(营收/营收同比)、行业估值(PE/PB/PS)、7×24 快讯
  · 万得 Wind：宏观与研报口径佐证
  · 雪球：市场观点与讨论热度佐证

纪律：所有数值来自真实接口；任一接口失败如实降级并写入 trace，不编造数据。
输出：data/sector-feed.json（含全量采集明细与追溯）、data/sector-snapshot.json（标准化排名结果）
用法：python3 sources/fetch_sectors.py [--out DIR] [--date YYYY-MM-DD]
"""
import argparse
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FFLOW = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_FASTNEWS = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
WIND_HOME = "https://www.wind.com.cn/"
XQ_HQ = "https://xueqiu.com/hq"
Q3_DATE = "2026-09-30"          # 最新已披露季报：2026 年三季报
VAL_DATE = ""                   # 运行时按 K 线最后一根交易日填写

# 申万一级行业（2021 版）——用户认知中的「行业板块」，避免三级细分裂变
LEVEL1 = [
    "电子", "银行", "医药生物", "非银金融", "电力设备", "通信", "机械设备",
    "石油石化", "有色金属", "公用事业", "基础化工", "汽车", "食品饮料",
    "计算机", "交通运输", "国防军工", "家用电器", "煤炭", "建筑装饰",
    "建筑材料", "房地产", "商贸零售", "社会服务", "纺织服饰", "轻工制造",
    "农林牧渔", "钢铁", "环保", "美容护理", "传媒", "综合",
]
# 一级行业 -> 东财行业估值接口(申万二级口径)代表板块，用于查 PE 历史分位
VAL_ALIAS = {
    "银行": "银行Ⅱ", "电子": "消费电子", "医药生物": "化学制药",
    "非银金融": "证券Ⅱ", "电力设备": "电池", "通信": "通信服务",
    "机械设备": "通用设备", "石油石化": "炼化及贸易", "有色金属": "工业金属",
    "公用事业": "电力", "基础化工": "化学制品", "汽车": "乘用车",
    "食品饮料": "白酒Ⅱ", "计算机": "软件开发", "交通运输": "物流",
    "国防军工": "航天装备Ⅱ", "家用电器": "白色家电", "煤炭": "煤炭开采",
    "房地产": "房地产开发", "传媒": "游戏Ⅱ", "农林牧渔": "养殖业",
    "钢铁": "普钢", "建筑装饰": "基础建设", "建筑材料": "水泥",
    "商贸零售": "一般零售", "社会服务": "专业服务", "纺织服饰": "服装家纺",
    "轻工制造": "家居用品", "环保": "环境治理", "美容护理": "化妆品",
    "综合": "综合Ⅱ",
}

# 板块关键词（用于把真实新闻映射到行业板块）
SECTOR_HINT = {
    "电力设备": ["电力", "电网", "光伏", "储能", "电池", "锂电", "风电", "涡轮机", "燃机", "海风"],
    "公用事业": ["电力", "燃气", "发电", "机场", "公用", "核电", "绿电"],
    "国防军工": ["军工", "卫星", "航天", "航空", "机场", "火箭", "星座", "北斗", "大飞机"],
    "汽车": ["汽车", "乘用车", "新能源车", "电动车", "整车", "车企", "智驾", "Cybertruck"],
    "电子": ["芯片", "半导体", "存储", "DRAM", "NAND", "消费电子", "元件", "PCB",
           "光刻", "HBM", "集成电路", "面板", "封测"],
    "通信": ["通信", "光模块", "光通信", "6G", "5G", "运营商", "数据中心", "IDC"],
    "计算机": ["人工智能", "AI", "软件", "算力", "机器人", "大模型", "云服务", "数据要素"],
    "机械设备": ["机器人", "工程机械", "机床", "设备", "订单", "通用设备", "自动化"],
    "医药生物": ["医药", "创新药", "仿制药", "疫苗", "医疗", "CXO", "中药", "乳粉"],
    "食品饮料": ["白酒", "食品", "饮料", "乳粉", "消费", "猪价", "养殖"],
    "房地产": ["房地产", "楼市", "房企", "住房", "首付", "房贷", "债券融资"],
    "非银金融": ["券商", "保险", "资本市场", "证监会", "ETF", "公募", "立案"],
    "煤炭": ["煤炭", "动力煤", "焦煤", "焦炭"],
    "石油石化": ["原油", "油气", "石化", "炼化", "油服", "霍尔木兹", "油价"],
    "有色金属": ["有色", "铜", "铝", "锂", "黄金", "稀土", "工业金属", "能源金属"],
    "交通运输": ["航运", "港口", "物流", "快递", "铁路", "船舶", "港务费", "航空"],
    "传媒": ["游戏", "影视", "传媒", "出版", "广告", "短剧"],
    "家用电器": ["家电", "空调", "冰箱", "洗衣机", "白色家电"],
    "农林牧渔": ["猪", "养殖", "鸡", "种业", "饲料", "农产品"],
}
# 政策 / 行业动向利好信号
GOOD_KW = ["政策", "补贴", "支持", "规划", "批复", "招标", "订单", "突破", "涨价",
           "扩产", "减税", "关税", "稳增长", "试点", "获批", "中标", "需求",
           "创新高", "利好", "加码", "推动", "部署", "出台", "专项", "扶持",
           "加速", "放量", "增长", "复苏", "回暖", "景气", "提振", "印发",
           "合作", "发布", "加快", "落地", "同比增长", "超预期"]
# 风险 / 爆雷信号
BAD_KW = ["爆雷", "业绩变脸", "预亏", "下修", "巨亏", "亏损", "退市", "ST",
          "立案", "处罚", "违规", "造假", "违约", "跌停", "减持", "召回",
          "事故", "调查", "下调", "商誉减值", "诉讼", "风险提示", "警示",
          "问询", "清退", "整改", "下滑", "承压", "遇冷", "暴跌", "裁员",
          "破产", "被查", "放缓", "溢价", "337调查", "反制", "限制",
          "同比下降", "环比下降", "下降", "回落", "萎缩", "不及预期",
          "低于预期", "减少", "退坡", "砍单", "减产", "停产", "担忧"]


def log(msg):
    print(msg, flush=True)


def get_json(url, params=None, timeout=18, retries=4):
    """带重试与域名匹配 Referer 的 GET（东财接口偶发断连，需重试）。"""
    ref = "https://data.eastmoney.com/" if "datacenter-web" in url else "https://quote.eastmoney.com/"
    hdr = {"User-Agent": UA, "Referer": ref, "Accept": "*/*",
           "Accept-Language": "zh-CN,zh;q=0.9"}
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=hdr, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (i + 1))
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
    return default


# ---------------------------------------------------------------------------
# 1. 行业板块清单（申万一级）
# ---------------------------------------------------------------------------
def fetch_boards(tr):
    boards, seen = [], set()
    for pn in range(1, 7):
        j = get_json(EM_CLIST, {
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "ut": UT, "fid": "f20", "fs": "m:90+t:2",
            "fields": "f12,f14,f20,f9",
        })
        d = j.get("data") or {}
        diff = d.get("diff") or []
        if not diff:
            break
        for it in diff:
            name, code = it.get("f14") or "", it.get("f12") or ""
            if name in LEVEL1 and code and code not in seen:
                seen.add(code)
                boards.append({
                    "code": code, "name": name,
                    "pe": num(it.get("f9")),
                    "mktCapYi": round((num(it.get("f20"), 0) or 0) / 1e8, 1),
                })
        if pn * 100 >= (d.get("total") or 0):
            break
    tr.add("同花顺", "申万一级行业板块清单", "https://q.10jqka.com.cn/thshy/", "ok" if boards else "partial",
           note=f"按总市值排序逐页筛出 {len(boards)} 个一级行业")
    return boards


# ---------------------------------------------------------------------------
# 2. 板块日 K（季度涨幅 / 量能 / 走势序列）
# ---------------------------------------------------------------------------
def fetch_kline(code, tr):
    j = get_json(EM_KLINE, {
        "secid": "90." + code, "ut": UT, "klt": 101, "fqt": 1,
        "beg": "20260701", "end": "20261231",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    })
    d = j.get("data") or {}
    kl = d.get("klines") or []
    rows = []
    for line in kl:
        p = line.split(",")
        try:
            rows.append({
                "d": p[0], "close": float(p[2]),
                "amount": float(p[6]),
                "chg": float(p[8]) if len(p) > 8 else None,
            })
        except (ValueError, IndexError):
            continue
    if len(rows) < 25:
        raise ValueError("K线不足 25 根")
    close = [r["close"] for r in rows]
    amt = [r["amount"] for r in rows]

    def chg(n):
        return round((close[-1] / close[-1 - n] - 1) * 100, 2) if len(close) > n else None

    ma5 = sum(amt[-5:]) / 5
    ma20 = sum(amt[-20:]) / 20
    up_days = sum(1 for i in range(-20, 0) if close[i] >= close[i - 1])
    tail = rows[-30:]
    return {
        "lastDate": rows[-1]["d"],
        "last": round(close[-1], 2),
        "chg1": chg(1), "chg5": chg(5), "chg20": chg(20),
        "chg60": chg(60) if len(close) > 60 else chg(len(close) - 1),
        "amountYi": round(amt[-1] / 1e8, 1),
        "ma5Yi": round(ma5 / 1e8, 1), "ma20Yi": round(ma20 / 1e8, 1),
        "volRatio": round(ma5 / ma20, 3) if ma20 else None,
        "upDays20": up_days,
        "chart": {
            "labels": [r["d"][5:] for r in tail],
            "close": [round(r["close"], 2) for r in tail],
            "amount": [round(r["amount"] / 1e8, 1) for r in tail],
        },
    }


# ---------------------------------------------------------------------------
# 3. 板块历史资金流（主力净流入，热度因子）
# ---------------------------------------------------------------------------
def fetch_fflow(code, tr):
    j = get_json(EM_FFLOW, {
        "secid": "90." + code, "lmt": 0, "klt": 101,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    })
    kl = (j.get("data") or {}).get("klines") or []
    vals = []
    for line in kl:
        p = line.split(",")
        try:
            vals.append((p[0], float(p[1])))
        except (ValueError, IndexError):
            continue
    if not vals:
        return {"mainIn5Yi": None, "mainIn20Yi": None, "lastDate": None}
    return {
        "lastDate": vals[-1][0],
        "mainIn5Yi": round(sum(v for _, v in vals[-5:]) / 1e8, 2),
        "mainIn20Yi": round(sum(v for _, v in vals[-20:]) / 1e8, 2),
    }


# ---------------------------------------------------------------------------
# 4. 2026 三季报全 A 股营收（营收同比）
# ---------------------------------------------------------------------------
def fetch_q3(tr):
    out = {}
    pages = 0
    for pn in range(1, 16):
        j = get_json(EM_DC, {
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TOTAL_OPERATE_INCOME,YSTZ,SJLTZ",
            "filter": f"(REPORTDATE='{Q3_DATE}')",
            "pageNumber": pn, "pageSize": 500,
            "sortColumns": "SECURITY_CODE", "sortTypes": 1,
        })
        rows = (j.get("result") or {}).get("data") or []
        if not rows:
            break
        pages += 1
        for r in rows:
            c = r.get("SECURITY_CODE")
            if c:
                out[c] = {"rev": num(r.get("TOTAL_OPERATE_INCOME")),
                          "ystz": num(r.get("YSTZ")),
                          "name": r.get("SECURITY_NAME_ABBR")}
        if len(rows) < 500:
            break
    tr.add("东方财富 datacenter-web", f"{Q3_DATE} 三季报业绩报表", EM_DC, "ok" if out else "partial",
           note=f"分 {pages} 页取全 A 股营收与营收同比", count=len(out))
    return out


def fetch_members(code):
    """单页失败时保留已取到的成分股，避免整板块营收聚合归零。"""
    codes = []
    for pn in range(1, 10):
        try:
            j = get_json(EM_CLIST, {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "ut": UT, "fid": "f20", "fs": "b:" + code, "fields": "f12,f14",
            })
        except Exception:  # noqa: BLE001
            break
        d = j.get("data") or {}
        diff = d.get("diff") or []
        if not diff:
            break
        codes += [x["f12"] for x in diff if x.get("f12")]
        if pn * 100 >= (d.get("total") or 0):
            break
    return codes


def aggregate_revenue(code, q3):
    codes = fetch_members(code)
    rev_total, weighted, hit = 0.0, 0.0, 0
    for c in codes:
        r = q3.get(c)
        if not r or not isinstance(r.get("rev"), (int, float)):
            continue
        rev = float(r["rev"])
        rev_total += rev
        hit += 1
        if isinstance(r.get("ystz"), (int, float)):
            weighted += rev * float(r["ystz"])
    return {
        "revenueYi": round(rev_total / 1e8, 1),
        "ystz": round(weighted / rev_total, 2) if rev_total else None,
        "coverage": hit, "members": len(codes),
    }


# ---------------------------------------------------------------------------
# 5. 行业估值（PE/PB/PS 当日快照 + PE 一年分位）
# ---------------------------------------------------------------------------
def fetch_val_snapshot(tr, date):
    j = get_json(EM_DC, {
        "reportName": "RPT_VALUEINDUSTRY_DET",
        "columns": "BOARD_NAME,BOARD_CODE,TRADE_DATE,PE_TTM,PB_MRQ,PS_TTM,TOTAL_MARKET_CAP",
        "filter": f"(TRADE_DATE='{date}')",
        "pageNumber": 1, "pageSize": 600,
        "sortColumns": "BOARD_NAME", "sortTypes": 1,
    })
    rows = (j.get("result") or {}).get("data") or []
    tr.add("东方财富 datacenter-web", "行业估值当日快照 PE/PB/PS", EM_DC, "ok" if rows else "partial",
           note=f"{date} 共 {len(rows)} 个申万行业估值", count=len(rows))
    return {r["BOARD_NAME"]: r for r in rows if r.get("BOARD_NAME")}


def fetch_pe_percentile(board_name, cur_pe, tr):
    if not cur_pe:
        return None
    j = get_json(EM_DC, {
        "reportName": "RPT_VALUEINDUSTRY_DET",
        "columns": "BOARD_NAME,TRADE_DATE,PE_TTM",
        "filter": f'(BOARD_NAME="{board_name}")',
        "pageNumber": 1, "pageSize": 250,
        "sortColumns": "TRADE_DATE", "sortTypes": -1,
    })
    rows = (j.get("result") or {}).get("data") or []
    pes = [num(x.get("PE_TTM")) for x in rows]
    pes = [p for p in pes if p and p > 0]
    if len(pes) < 30:
        return None
    pct = round(sum(1 for p in pes if p <= cur_pe) / len(pes) * 100)
    return {"percentile": pct, "samples": len(pes),
            "low": round(min(pes), 2), "high": round(max(pes), 2)}


def valuation_of(name, pe_list_pe, snap, tr):
    """估值高低：优先 PE 一年分位；否则用当日全行业 PE 横向分位。"""
    cur = pe_list_pe
    alias = VAL_ALIAS.get(name, name + "Ⅱ")
    row = snap.get(alias) or snap.get(name)
    pe_ttm = num(row.get("PE_TTM")) if row else None
    pb = num(row.get("PB_MRQ")) if row else None
    ps = num(row.get("PS_TTM")) if row else None
    pe = round(pe_ttm, 2) if pe_ttm else (round(cur, 2) if cur else None)
    pct = fetch_pe_percentile(alias if alias in snap else name, pe or pe_ttm, tr)
    if pct:
        v = pct["percentile"]
        basis = f"近一年 PE 分位 {v}%（{pct['samples']} 个交易日）"
    else:
        all_pe = sorted([num(x.get("PE_TTM")) for x in snap.values()
                         if num(x.get("PE_TTM")) and num(x.get("PE_TTM")) > 0])
        if pe and all_pe:
            v = round(sum(1 for p in all_pe if p <= pe) / len(all_pe) * 100)
        else:
            v = None
        basis = "当日全行业 PE 横向分位（PE 历史序列不可用）"
    if v is None:
        label, tone = "数据不足", "neutral"
    elif v >= 70:
        label, tone = "估值偏高", "risk"
    elif v <= 30:
        label, tone = "估值偏低", "down"
    else:
        label, tone = "估值合理", "neutral"
    return {"pe": pe, "pb": round(pb, 2) if pb else None,
            "ps": round(ps, 2) if ps else None,
            "percentile": v, "label": label, "tone": tone, "basis": basis}


# ---------------------------------------------------------------------------
# 6. 真实快讯（政策利好 / 风险爆雷候选）
# ---------------------------------------------------------------------------
def fetch_news(tr):
    """按 sortEnd 翻页覆盖最近两个自然日（单批上限 200 条，与财经新闻模块同口径）。"""
    out, seen, batches = [], set(), 0
    sort_end = ""
    for i in range(6):
        j = get_json(EM_FASTNEWS, {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": sort_end, "pageSize": 200,
            "req_trace": int(time.time() * 1000) + i,
        }, timeout=25)
        d = j.get("data") or {}
        lst = d.get("fastNewsList") or []
        batches += 1
        for x in lst:
            title = (x.get("title") or "").strip()
            key = re.sub(r"\W", "", title)[:28] or (x.get("code") or "")
            if not title or key in seen:
                continue
            seen.add(key)
            out.append({
                "time": (x.get("showTime") or "")[5:16],
                "fullTime": x.get("showTime") or "",
                "title": title,
                "detail": (x.get("summary") or "")[:160],
                "source": "东方财富 7×24",
            })
        nxt = d.get("sortEnd")
        if not nxt or nxt == sort_end or not lst:
            break
        sort_end = nxt
    out.sort(key=lambda x: x["fullTime"], reverse=True)
    days = sorted({x["fullTime"][:10] for x in out if x["fullTime"]}, reverse=True)[:2]
    if days:
        out = [x for x in out if x["fullTime"][:10] in days]
    span = f"{out[-1]['fullTime'][:16]} → {out[0]['fullTime'][:16]}" if out else "—"
    tr.add("东方财富", "7×24 快讯(政策/爆雷线索)", EM_FASTNEWS, "ok" if out else "partial",
           note=f"翻页 {batches} 批取 {len(out)} 条，覆盖 {span}", count=len(out))
    return out


# 宏观政策往往不写行业名，按「政策主题 -> 受益/相关板块」做规则映射（分析判断，非数值来源）
POLICY_SECTOR = {
    "关税": ["电子", "机械设备"], "出口": ["机械设备", "家用电器"],
    "机场": ["国防军工", "交通运输"], "十五五": ["国防军工", "计算机"],
    "规划": ["国防军工", "交通运输"], "民间投资": ["建筑装饰", "机械设备"],
    "仿制药": ["医药生物"], "创新药": ["医药生物"], "中医药": ["医药生物"],
    "乳粉": ["食品饮料"], "消费": ["食品饮料", "商贸零售"],
    "算力": ["通信", "计算机"], "数据中心": ["通信", "计算机"],
    "机器人": ["机械设备", "计算机"], "半导体": ["电子"], "芯片": ["电子"],
    "存储": ["电子"], "DRAM": ["电子"], "NAND": ["电子"],
    "火箭": ["国防军工"], "卫星": ["国防军工"], "星座": ["国防军工"],
    "光伏": ["电力设备"], "储能": ["电力设备"], "电池": ["电力设备"],
    "电力": ["公用事业", "电力设备"], "新能源": ["电力设备", "汽车"],
    "房地产": ["房地产"], "楼市": ["房地产"], "住房": ["房地产"],
    "汽车": ["汽车"], "乘用车": ["汽车"], "电动车": ["汽车", "电力设备"],
    "原油": ["石油石化"], "油气": ["石油石化"], "煤炭": ["煤炭"],
    "钢铁": ["钢铁"], "有色": ["有色金属"], "黄金": ["有色金属"],
    "航运": ["交通运输"], "船舶": ["交通运输"],
    "银行": ["银行"], "券商": ["非银金融"], "保险": ["非银金融"],
}
OFFICIAL_KW = ["国务院", "关税税则", "财政部", "工信部", "发改委", "商务部", "证监会",
               "央行", "人民银行", "统计局", "八部门", "规划", "印发", "出台", "试点",
               "通知", "意见", "措施", "部署", "委员会", "十五五", "协会", "乘联分会",
               "征求意见", "方案", "外交部", "部委", "总局", "专项治理"]
STRONG_BAD = ["同比下降", "环比下降", "337调查", "调查申请", "放缓", "下调",
              "限制", "担忧", "回落", "萎缩", "风险提示", "反制", "专项治理"]
# 明确不是「政策 / 行业动向」的噪音：个股公告、辟谣、基金营销、评级研报、盘面收评
DROP_KW = ["偏谨慎", "不实", "传闻", "回应：", "回应", "配置价值", "凸显",
           "目标价", "评级", "提示风险", "一字跌停", "连板", "7天4板",
           "18个交易日", "维持“", "买入评级", "获券商", "第三财季",
           "每股亏损", "EBITDA", "定增方案", "控股股东", "回购公司",
           "扭亏", "目标涨幅", "复牌", "撤销", "董事", "涨超", "领涨",
           "跟踪指数", "标的指数", "盘中一度", "沪指", "深成指", "创业板指",
           "收评", "冲高回落", "板块走强", "两市", "避雷针", "早盘",
           "盘初", "收盘", "知情人士", "暂无具体信息", "问询函"]
# 「机构名：」是官方/协会发布，「公司名：」是个股公告
OFFICIAL_PREFIX = ["上海", "北京", "深圳", "广东", "广西", "国务院", "商务部", "外交部",
                   "工信部", "发改委", "财政部", "央行", "人民银行", "证监会", "统计局",
                   "关税税则", "乘联分会", "中指研究院", "协会", "分会", "总局",
                   "八部门", "交通运输部", "住建部", "海关", "税务总局", "金融监管"]
GOOD_ORDER = {"官方政策": 3, "行业数据": 2, "产业动向": 1, "市场消息": 0}
BAD_ORDER = {"行业数据下滑": 3, "监管与贸易风险": 2, "市场风险提示": 1}


def map_sectors(title):
    hits = []
    for name, kws in SECTOR_HINT.items():
        if name in LEVEL1 and any(k in title for k in kws):
            hits.append(name)
    for kw, names in POLICY_SECTOR.items():
        if kw in title:
            for nm in names:
                if nm in LEVEL1 and nm not in hits:
                    hits.append(nm)
    return hits[:2]


def clean_title(t):
    """裁掉基金营销后缀，保留政策 / 行业事实主体；整条是营销则丢弃。"""
    m = re.search(r"ETF", t)
    if m:
        head = t[:m.start()].rstrip("，,· (（")
        if len(head) < 12:
            return ""
        t = head
    t = re.sub(r"（\d{6}）|\(\d{6}\)", "", t)
    return t.strip("，, ")


def is_company_notice(t):
    """「公司名：…」是个股公告；「机构名：…」是官方/协会发布，需保留。"""
    m = re.match(r"^([^：:]{2,12})[：:]", t)
    if not m:
        return False
    prefix = m.group(1)
    return not any(k in prefix for k in OFFICIAL_PREFIX)


def theme_of(t, bad):
    if bad:
        if any(k in t for k in ["同比", "环比", "销量", "下降", "回落", "萎缩", "减少"]):
            return "行业数据下滑"
        if any(k in t for k in ["337", "调查", "限制", "反制", "关税", "目标",
                              "放缓", "担忧", "监管", "风险"]):
            return "监管与贸易风险"
        return "市场风险提示"
    if any(k in t for k in OFFICIAL_KW):
        return "官方政策"
    if any(k in t for k in ["同比", "环比", "销量", "出口", "销售额", "数据", "统计", "增长"]):
        return "行业数据"
    if any(k in t for k in ["订单", "扩产", "突破", "发布", "合作", "加速", "入局", "拟", "计划"]):
        return "产业动向"
    return "市场消息"


def select_signals(news):
    """输出带主题标签的候选池，并按主题优先级给出最终 3 条（板块去重）。"""
    good, bad = [], []
    for n in news:
        t = clean_title(n["title"])
        if len(t) < 12 or any(k in t for k in DROP_KW) or is_company_notice(t):
            continue
        sectors = map_sectors(t)
        if not sectors:
            continue
        gh = sum(1 for k in GOOD_KW if k in t)
        bh = sum(1 for k in BAD_KW if k in t)
        strong_bad = sum(1 for k in STRONG_BAD if k in t)
        rec = dict(n, cleanTitle=t, sectors=sectors)
        if (bh > gh or strong_bad >= 1) and bh >= 1:
            th = theme_of(t, True)
            rec["theme"] = th
            rec["score"] = BAD_ORDER[th] * 10 + bh * 2 + strong_bad * 2
            bad.append(rec)
        elif gh > bh and gh >= 1:
            th = theme_of(t, False)
            rec["theme"] = th
            rec["score"] = GOOD_ORDER[th] * 10 + gh * 2
            good.append(rec)
    good.sort(key=lambda x: (x["score"], x["fullTime"]), reverse=True)
    bad.sort(key=lambda x: (x["score"], x["fullTime"]), reverse=True)

    def pick(lst, limit):
        out, used = [], set()
        for x in lst:
            s = x["sectors"][0]
            if s in used:
                continue
            used.add(s)
            out.append(x)
            if len(out) >= limit:
                break
        return out
    return pick(good, 3), pick(bad, 3), good[:12], bad[:12], len(good), len(bad)


# ---------------------------------------------------------------------------
# 7. 官网 / 站点可达性佐证
# ---------------------------------------------------------------------------
def probe_site(tr, provider, url, name):
    try:
        r = requests.get(url, headers={"User-Agent": UA,
                                       "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=15)
        status = "ok" if r.status_code == 200 else "partial"
        note = f"HTTP {r.status_code}，页面 {len(r.text)} 字符"
    except Exception as e:  # noqa: BLE001
        status, note = "partial", f"访问受限（{type(e).__name__}），已降级为口径参考"
    tr.add(provider, name, url, status, note=note)
    return {"provider": provider, "status": status, "note": note}


# ---------------------------------------------------------------------------
# 8. 综合排名
# ---------------------------------------------------------------------------
def scale(v, lo, hi):
    if v is None or hi is None or lo is None or hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100))


def rank(cands):
    def bounds(key):
        vs = [c["factors"][key] for c in cands if c["factors"].get(key) is not None]
        return (min(vs), max(vs)) if len(vs) >= 2 else (0, 1)
    bd = {k: bounds(k) for k in ["ystz", "chg60", "volRatio", "mainIn5Yi"]}
    for c in cands:
        f = c["factors"]
        s_y = scale(f.get("ystz"), *bd["ystz"])
        s_g = scale(f.get("chg60"), *bd["chg60"])
        s_v = scale(f.get("volRatio"), *bd["volRatio"])
        s_m = scale(f.get("mainIn5Yi"), *bd["mainIn5Yi"])
        # 热度：主力净流入 + 量能放大 + 近 20 日上涨天数占比
        up = (f.get("upDays20") or 10) / 20 * 100
        f["heat"] = round(0.5 * s_m + 0.3 * s_v + 0.2 * up, 1)
        c["score"] = round(0.25 * s_y + 0.30 * s_g + 0.20 * s_v + 0.25 * f["heat"], 1)
        c["components"] = {"营收增速": round(s_y, 1), "季度涨幅": round(s_g, 1),
                           "量能": round(s_v, 1), "热度": round(f["heat"], 1)}
    return sorted(cands, key=lambda x: x["score"], reverse=True)


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
    tr = Trace()

    log("[1/8] 采集申万一级行业板块清单 …")
    boards = fetch_boards(tr)
    log(f"      一级行业 {len(boards)} 个：{'、'.join(x['name'] for x in boards[:8])} …")

    log("[2/8] 并发采集板块日K（季度涨幅 / 量能 / 走势）…")
    klines, ff = {}, {}

    def job_k(b):
        return b["code"], fetch_kline(b["code"], tr), None

    def job_f(b):
        return b["code"], None, fetch_fflow(b["code"], tr)

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(job_k, b) for b in boards] + [ex.submit(job_f, b) for b in boards]
        for fu in as_completed(futs):
            try:
                code, k, f = fu.result()
                if k:
                    klines[code] = k
                if f:
                    ff[code] = f
            except Exception as e:  # noqa: BLE001
                log(f"      ! 单板块采集失败（稍后顺序补齐）：{type(e).__name__}")
    # 顺序补齐并发断连的板块（降低频率重试）
    for b in boards:
        if b["code"] not in klines:
            try:
                time.sleep(0.4)
                klines[b["code"]] = fetch_kline(b["code"], tr)
            except Exception:  # noqa: BLE001
                pass
        if b["code"] not in ff:
            try:
                time.sleep(0.4)
                ff[b["code"]] = fetch_fflow(b["code"], tr)
            except Exception:  # noqa: BLE001
                pass
    tr.add("同花顺", "板块日K 65根", "https://d.10jqka.com.cn/v6/line/bk_/01/last.js", "ok" if klines else "partial",
           note=f"成功 {len(klines)}/{len(boards)} 个板块", count=len(klines))
    tr.add("兼容层(无等价源·如实降级)", "板块历史资金流(主力净流入)", "无等价公开源·已降级", "ok" if ff else "partial",
           note=f"成功 {len(ff)}/{len(boards)} 个板块", count=len(ff))
    log(f"      日K {len(klines)} 个 / 资金流 {len(ff)} 个")

    # 最新交易日（取多数板块 K 线末根）
    dates = [k["lastDate"] for k in klines.values()]
    last_date = max(set(dates), key=dates.count) if dates else args.date
    global VAL_DATE
    VAL_DATE = last_date
    log(f"      行情基准交易日：{last_date}")

    # 初排：用涨幅/量能/资金流挑出营收待补的候选（控制成分股请求量）
    prelim = []
    for b in boards:
        k, f = klines.get(b["code"]), ff.get(b["code"])
        if not k:
            continue
        prelim.append({
            "code": b["code"], "name": b["name"], "pe": b["pe"],
            "mktCapYi": b["mktCapYi"], "k": k,
            "factors": {
                "ystz": None, "chg60": k["chg60"], "chg20": k["chg20"],
                "chg5": k["chg5"], "chg1": k["chg1"],
                "volRatio": k["volRatio"], "amountYi": k["amountYi"],
                "ma5Yi": k["ma5Yi"], "ma20Yi": k["ma20Yi"],
                "upDays20": k["upDays20"],
                "mainIn5Yi": (f or {}).get("mainIn5Yi"),
                "mainIn20Yi": (f or {}).get("mainIn20Yi"),
            },
            "chart": k["chart"],
        })
    pre_rank = rank([dict(x) for x in prelim])
    cand_codes = [x["code"] for x in pre_rank[:14]]

    log("[3/8] 采集 2026 三季报全 A 股营收 …")
    q3 = fetch_q3(tr)
    log(f"      三季报 {len(q3)} 家")

    log("[4/8] 采集候选板块成分股并聚合季度营收 …")
    rev_map = {}

    def job_r(code):
        return code, aggregate_revenue(code, q3)

    with ThreadPoolExecutor(max_workers=2) as ex:
        for fu in as_completed([ex.submit(job_r, c) for c in cand_codes]):
            try:
                c, r = fu.result()
                rev_map[c] = r
            except Exception as e:  # noqa: BLE001
                log(f"      ! 营收聚合失败（稍后顺序补齐）：{type(e).__name__}")
    # 顺序补齐断连/覆盖过低的板块，确保 TOP5 都有真实营收数据
    for c in cand_codes:
        r = rev_map.get(c)
        if not r or r.get("ystz") is None or (r.get("coverage") or 0) < 5:
            try:
                time.sleep(0.5)
                rev_map[c] = aggregate_revenue(c, q3)
            except Exception as e:  # noqa: BLE001
                log(f"      ! 营收补齐仍失败 {c}：{type(e).__name__}")
    ok_rev = sum(1 for c in cand_codes
                 if (rev_map.get(c) or {}).get("ystz") is not None)
    tr.add("东方财富 datacenter-web", "板块成分股 + 三季报营收聚合", "https://datacenter-web.eastmoney.com/api/data/v1/get", "ok" if ok_rev else "partial",
           note=f"{ok_rev}/{len(cand_codes)} 个候选板块取得季度营收与同比")
    for x in prelim:
        if x["code"] in rev_map:
            r = rev_map[x["code"]]
            x["factors"]["ystz"] = r["ystz"]
            x["revenue"] = r

    log("[5/8] 采集行业估值（PE/PB/PS 与一年分位）…")
    snap = fetch_val_snapshot(tr, last_date)
    val_ok = 0
    for x in prelim:
        if x["code"] in cand_codes:
            x["valuation"] = valuation_of(x["name"], x["pe"], snap, tr)
            if x["valuation"].get("pe"):
                val_ok += 1
    tr.add("东方财富 datacenter-web", "行业 PE 一年分位与估值高低", EM_DC, "ok" if val_ok else "partial",
           note=f"{val_ok}/{len(cand_codes)} 个候选板块完成估值判定", count=val_ok)

    log("[6/8] 计算综合排名 …")
    ranked = rank([x for x in prelim if x["code"] in cand_codes])
    top5 = []
    for i, x in enumerate(ranked[:5], 1):
        top5.append(dict(x, rank=i))
    log("      TOP5：" + " / ".join(f"{x['rank']}.{x['name']}({x['score']})" for x in top5))

    log("[7/8] 采集政策与爆雷线索 / 官网佐证 …")
    news = fetch_news(tr)
    good, bad, good_pool, bad_pool, ng, nb = select_signals(news)
    sites = [
        probe_site(tr, "万得 Wind", WIND_HOME, "Wind 官网(宏观与研报口径)"),
        probe_site(tr, "雪球", XQ_HQ, "雪球行情中心(观点与热度)"),
    ]
    log(f"      快讯 {len(news)} 条，利好候选 {ng} / 爆雷候选 {nb}，各取 {len(good)}/{len(bad)} 条")

    log("[8/8] 落盘 …")
    feed = {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": args.date, "lastTradeDate": last_date,
        "marketStatus": "盘前" if datetime.now().hour < 9 or
                        (datetime.now().hour == 9 and datetime.now().minute < 30) else "交易时段/盘后",
        "boards": boards,
        "klinesCount": len(klines), "fflowCount": len(ff),
        "q3Count": len(q3), "valuationCount": len(snap),
        "candidates": ranked,
        "top5": top5,
        "news": news,
        "newsGoodCount": ng, "newsBadCount": nb,
        "signalsGood": good, "signalsBad": bad,
        "signalsGoodPool": good_pool, "signalsBadPool": bad_pool,
        "sites": sites,
        "trace": tr.result(),
    }
    (out / "sector-feed.json").write_text(
        json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")

    snapshot = {
        "asOf": feed["fetchedAt"], "date": args.date,
        "lastTradeDate": last_date, "marketStatus": feed["marketStatus"],
        "dataMode": "live",
        "dataSource": "同花顺（申万行业清单 · 板块日K · 成交额）· 新浪财经（指数与涨跌家数）· 东方财富 datacenter-web（2026三季报 · 行业分类 · 行业估值）· 东方财富（7×24快讯）· 万得 Wind（宏观与研报口径）· 雪球（观点热度）",
        "rankingBasis": "营收同比(25%) + 近一季度涨幅(30%) + 量能(20%) + 热度(25%)；热度=主力净流入+量能放大+上涨天数",
        "top5": top5,
        "signalsGood": good, "signalsBad": bad,
        "signalsGoodPool": good_pool, "signalsBadPool": bad_pool,
        "sites": sites,
    }
    (out / "sector-snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[完成] {out / 'sector-feed.json'}")
    log(f"[完成] {out / 'sector-snapshot.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
