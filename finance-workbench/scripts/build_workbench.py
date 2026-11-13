#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「理财工作台」整合构建脚本

一条命令完成四件事（可随每天 14:00 更新链路重跑）：
  1. 同步四个源模块的 index.html → pages/<模块>.html（保持零改动、自包含）
  2. 解析各页面内联 briefing-data，抽取「真实数据摘要」→ data/workbench-summary.json
  3. 幂等注入首页 index.html 的 <script id="workbench-data"> 摘要数据块
  4. 把四个模块页面 base64 内联进 index.html（iframe srcdoc 装载，单文件自足，
     发布到在线环境只需上传 index.html 一个文件，不再请求 pages/*.html）
  5. 同步 web 产物到鸿蒙原生壳 harmony/.../rawfile/web/，并刷新 sw.js 缓存版本
  6. 同步单文件发布产物到 dist/（在线应用发布目录）

用法：
  python3 scripts/build_workbench.py              # 全量同步 + 重建
  python3 scripts/build_workbench.py --no-sync   # 不同步源页面，仅用现有 pages 重建摘要

注意：使用 python3（不是 python）。所有摘要字段均来自四个模块的真实注入数据，不编造。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # /workspace/finance-workbench
WORKSPACE = ROOT.parent                            # /workspace
PAGES = ROOT / "pages"
DATA = ROOT / "data"
ASSETS = ROOT / "assets"
RAWFILE_WEB = ROOT / "harmony" / "entry" / "src" / "main" / "resources" / "rawfile" / "web"
DIST = ROOT / "dist"                          # 在线应用发布目录（单文件产物 + PWA 资源）

MODULES = [
    ("finance-news", "财经新闻"),
    ("industry-sectors", "行业板块"),
    ("index-tracking", "指数跟踪"),
    ("bond-funds", "债券基金"),
]

ACCENT = {
    "finance-news": "#2b6cf6",
    "industry-sectors": "#8a63e8",
    "index-tracking": "#ef7a32",
    "bond-funds": "#0e9aa7",
}
ICON = {
    "finance-news": "news",
    "industry-sectors": "grid",
    "index-tracking": "chart",
    "bond-funds": "shield",
}

DATA_START = "<!-- WORKBENCH_DATA_START -->"
DATA_END = "<!-- WORKBENCH_DATA_END -->"
BUNDLE_START = "<!-- PAGE_BUNDLE_START -->"
BUNDLE_END = "<!-- PAGE_BUNDLE_END -->"


def log(msg: str) -> None:
    print(f"  {msg}")


def clip(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def brief_of(path: Path) -> dict:
    """从自包含 HTML 里读出注入的 briefing-data。"""
    s = path.read_text("utf-8")
    m = re.search(
        r'<script id="briefing-data" type="application/json">([\s\S]*?)</script>', s
    )
    if not m:
        raise RuntimeError(f"{path.name} 缺少 briefing-data 注入块")
    return json.loads(m.group(1))


def sync_pages() -> None:
    """把四个源模块的 index.html 同步进 pages/，保持字节级一致。"""
    for mid, name in MODULES:
        src = WORKSPACE / mid / "index.html"
        dst = PAGES / f"{mid}.html"
        if not src.exists():
            log(f"[跳过] 源模块不存在：{src}")
            continue
        shutil.copy2(src, dst)
        log(f"[同步] {name:6s} ← {src.relative_to(WORKSPACE)}  ({dst.stat().st_size} B)")


def fmt_pct(v, digits: int = 2) -> str:
    try:
        return f"{float(v):+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def build_summary() -> dict:
    fn = brief_of(PAGES / "finance-news.html")
    ind = brief_of(PAGES / "industry-sectors.html")
    idx = brief_of(PAGES / "index-tracking.html")
    bd = brief_of(PAGES / "bond-funds.html")

    fn_meta, glance = fn["meta"], fn["glance"]
    intra = glance.get("intraday", {}) if isinstance(glance, dict) else {}
    ind_meta = ind["meta"]
    idx_meta = idx["meta"]
    bd_meta = bd["meta"]

    baro = bd.get("barometer", {})
    top1 = ind["top5"][0] if ind.get("top5") else {}
    rank1 = bd["rank"]["top5"][0] if bd.get("rank", {}).get("top5") else {}
    alerts = bd.get("alerts", []) or []
    nlog = bd.get("notifyLog", {}) or {}
    since = (bd.get("funds") or [{}])[0].get("sinceBuy", {}) or {}
    threshold = bd.get("dropThreshold", 0.7)
    watchlist = bd.get("watchlist", []) or []

    market = [
        {"name": c.get("name", "—"), "point": c.get("point"), "chgPct": c.get("chgPct")}
        for c in (idx.get("marketCards") or [])[:3]
    ]

    # ---- 今日速览：四条真实结论 ----
    focus = [
        {
            "id": "finance-news",
            "label": "盘中研判",
            "tone": intra.get("verdictColor", "neutral"),
            "text": clip(
                intra.get("oneLiner")
                or f"研判 {intra.get('score', '—')}/100，{intra.get('verdictLabel', '观望')}。",
                110,
            ),
        },
        {
            "id": "industry-sectors",
            "label": "行业榜首",
            "tone": "neutral",
            "text": clip(
                f"综合榜首 {top1.get('name', '—')} {top1.get('score', '—')} 分；"
                f"{len(ind.get('good') or [])} 条利好 / {len(ind.get('bad') or [])} 条爆雷。",
                110,
            ),
        },
        {
            "id": "index-tracking",
            "label": "自选指数",
            "tone": "up" if all((m.get("chgPct") or 0) >= 0 for m in market) else "neutral",
            "text": clip(idx_meta.get("todayAction") or "—", 110),
        },
        {
            "id": "bond-funds",
            "label": "债市晴雨",
            "tone": baro.get("tone", "neutral"),
            "text": clip(
                f"晴雨「{baro.get('label', '—')}」{baro.get('score', '—')}/10；{baro.get('action', '')}",
                110,
            ),
        },
    ]

    # ---- 四张功能卡 ----
    modules = [
        {
            "id": "finance-news",
            "name": "财经新闻",
            "sub": "每天 14:00 更新 · 大白话版",
            "icon": ICON["finance-news"],
            "accent": ACCENT["finance-news"],
            "date": fn_meta.get("date", "—"),
            "weekday": fn_meta.get("weekday", ""),
            "badge": f"{fn_meta.get('newsCount', 0)} 条要闻",
            "headline": clip(
                (fn.get("news") or [{}])[0].get("title", "—"), 26
            ),
            "desc": clip(
                f"盘中研判「{intra.get('verdictLabel', '—')}」{intra.get('score', '—')}/100 · "
                f"利好 {fn_meta.get('positiveCount', 0)} / 需留意 {fn_meta.get('cautionCount', 0)}",
                46,
            ),
            "stats": [
                {"k": "要闻", "v": f"{fn_meta.get('newsCount', 0)} 条"},
                {"k": "利好", "v": str(fn_meta.get("positiveCount", 0))},
                {"k": "需留意", "v": str(fn_meta.get("cautionCount", 0))},
            ],
            "tone": intra.get("verdictColor", "neutral"),
        },
        {
            "id": "industry-sectors",
            "name": "行业板块",
            "sub": "季度营收 · 涨幅 · 量能 · 热度",
            "icon": ICON["industry-sectors"],
            "accent": ACCENT["industry-sectors"],
            "date": ind_meta.get("date", "—"),
            "weekday": ind_meta.get("weekday", ""),
            "badge": "TOP5 综合排名",
            "headline": clip(f"综合榜首：{top1.get('name', '—')} {top1.get('score', '—')} 分", 26),
            "desc": clip(
                f"{len(ind.get('good') or [])} 条利好 / {len(ind.get('bad') or [])} 条爆雷 · "
                f"{ind_meta.get('marketStatus', '')}",
                46,
            ),
            "stats": [
                {"k": "TOP1", "v": top1.get("name", "—")},
                {"k": "利好", "v": f"{len(ind.get('good') or [])} 条"},
                {"k": "爆雷", "v": f"{len(ind.get('bad') or [])} 条"},
            ],
            "top3": [
                {"name": t.get("name", "—"), "score": t.get("score", "—")}
                for t in (ind.get("top5") or [])[:3]
            ],
            "tone": "neutral",
        },
        {
            "id": "index-tracking",
            "name": "指数跟踪",
            "sub": "自选基金 · 收益 · 技术依据",
            "icon": ICON["index-tracking"],
            "accent": ACCENT["index-tracking"],
            "date": idx_meta.get("date", "—"),
            "weekday": idx_meta.get("weekday", ""),
            "badge": f"{idx_meta.get('fundCount', 0)} 只自选",
            "headline": clip(
                f"大盘 {sum(1 for m in market if (m.get('chgPct') or 0) >= 0)}/{len(market)} 上涨"
                f" · {idx_meta.get('fundCount', 0)} 只自选",
                30,
            ),
            "desc": clip(
                "大盘：" + " / ".join(
                    f"{m['name']} {fmt_pct(m['chgPct'])}" for m in market
                ),
                46,
            ),
            "stats": [
                {"k": m.get("name", "—").replace("指数", ""),
                 "v": fmt_pct(m.get("chgPct")),
                 "tone": "up" if (m.get("chgPct") or 0) >= 0 else "down"}
                for m in market
            ],
            "market": market,
            "tone": "neutral",
        },
        {
            "id": "bond-funds",
            "name": "债券基金",
            "sub": "排名 · 自选 · 晴雨表 · 弹窗提醒",
            "icon": ICON["bond-funds"],
            "accent": ACCENT["bond-funds"],
            "date": bd_meta.get("date", "—"),
            "weekday": bd_meta.get("weekday", ""),
            "navDate": bd_meta.get("navDate", "—"),
            "badge": f"晴雨「{baro.get('label', '—')}」{baro.get('score', '—')}/10",
            "headline": clip(
                f"榜首近三年 +{rank1.get('y3', '—')}% · 最大回撤 "
                f"{(round(rank1['dd3y'], 2) if isinstance(rank1.get('dd3y'), (int, float)) else '—')}%",
                34,
            ),
            "desc": clip(
                f"{watchlist[0] if watchlist else '自选'} 相对上次买入 "
                f"{fmt_pct(since.get('chgPct')) if since.get('hasBuy') else '未录入买入'}；"
                f"跌幅提醒{'已触发' if alerts else '未触发'}",
                46,
            ),
            "stats": [
                {"k": "榜首近3年", "v": f"+{rank1.get('y3', '—')}%" if isinstance(rank1.get("y3"), (int, float)) else "—"},
                {"k": "最大回撤", "v": f"{round(rank1['dd3y'], 2):.2f}%" if isinstance(rank1.get("dd3y"), (int, float)) else "—"},
                {"k": "弹窗提醒", "v": f"{len(alerts)} 只触发" if alerts else "未触发"},
            ],
            "alert": {
                "triggered": len(alerts),
                "threshold": threshold,
                "status": nlog.get("popupStatus", "not_run"),
                "detail": nlog.get("popupDetail", "尚未运行弹窗提醒脚本"),
                "sinceBuy": clip(since.get("status", "未录入买入"), 40) if since.get("hasBuy") else "未录入买入",
            },
            "tone": baro.get("tone", "neutral"),
        },
    ]

    dates = [m["date"] for m in modules]
    summary = {
        "meta": {
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "moduleCount": len(modules),
            "updateTime": "14:00",
            "timezone": "Asia/Shanghai",
            "latestDate": max(d for d in dates if d and d != "—"),
            "dateNote": "四个模块按各自 14:00 任务独立更新，数据日期以各卡片标注为准",
            "dataMode": "真实行情 / 官方信息离线快照",
            "sources": ["东方财富", "天天基金", "中国人民银行", "财政部", "国务院", "万得 / 雪球（参考）"],
        },
        "overview": {
            "intraday": {
                "label": intra.get("verdictLabel", "—"),
                "score": intra.get("score", "—"),
                "color": intra.get("verdictColor", "neutral"),
                "oneLiner": intra.get("oneLiner", ""),
                "date": fn_meta.get("date", "—"),
            },
            "sectorTop": {"name": top1.get("name", "—"), "score": top1.get("score", "—"),
                          "date": ind_meta.get("date", "—")},
            "market": market,
            "barometer": {
                "label": baro.get("label", "—"), "tone": baro.get("tone", "neutral"),
                "score": baro.get("score", "—"), "action": baro.get("action", ""),
                "date": bd_meta.get("date", "—"),
            },
            "alert": {
                "triggered": len(alerts), "threshold": threshold,
                "status": nlog.get("popupStatus", "not_run"),
                "detail": nlog.get("popupDetail", "尚未运行弹窗提醒脚本"),
            },
        },
        "focus": focus,
        "modules": modules,
        "disclaimer": (
            "⚠ 本工作台由 AI 基于公开行情与官方信息整理生成，仅供参考，"
            "不构成任何投资建议或具体产品推荐。基金为场外产品，估值与最终收益以基金公司公布净值为准；"
            "技术分析基于历史价格，不预示未来。投资有风险，决策需谨慎。"
        ),
    }
    return summary


def build_page_bundle() -> dict:
    """把四个模块页面以 base64(UTF-8) 内联，供 iframe srcdoc 装载。

    发布到在线环境时只需上传 index.html 一个文件，不再请求 pages/*.html，
    从根本上避免子页面缺失导致的 NoSuchKey / 404 /「点进去加载不出来」。
    """
    out = {}
    for mid, name in MODULES:
        p = PAGES / f"{mid}.html"
        if not p.exists():
            raise RuntimeError(f"缺少模块页面：{p}")
        raw = p.read_bytes()
        out[mid] = {
            "name": name,
            "bytes": len(raw),
            "sha1": hashlib.sha1(raw).hexdigest()[:12],
            "encoding": "base64-utf8",
            "html": base64.b64encode(raw).decode("ascii"),
        }
    return out


def inject_bundle(bundle: dict) -> None:
    """幂等替换 index.html 的 PAGE_BUNDLE 注入块。"""
    home = ROOT / "index.html"
    s = home.read_text("utf-8")
    i, j = s.find(BUNDLE_START), s.find(BUNDLE_END)
    if i < 0 or j < 0 or j < i:
        raise RuntimeError("index.html 缺少 PAGE_BUNDLE_START/END 标记")
    block = (
        BUNDLE_START + "\n"
        + '<script id="page-bundle" type="application/json">\n'
        + json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
        + "\n</script>\n"
        + BUNDLE_END
    )
    home.write_text(s[:i] + block + s[j + len(BUNDLE_END):], "utf-8")
    raw_bytes = sum(v["bytes"] for v in bundle.values())
    b64_bytes = sum(len(v["html"]) for v in bundle.values())
    log(f"[内联] 4 个模块页面写入 index.html（{raw_bytes} B → base64 {b64_bytes} B）")


def inject_home(summary: dict) -> None:
    """幂等替换首页注入块。"""
    home = ROOT / "index.html"
    s = home.read_text("utf-8")
    i, j = s.find(DATA_START), s.find(DATA_END)
    if i < 0 or j < 0 or j < i:
        raise RuntimeError("index.html 缺少 WORKBENCH_DATA_START/END 标记")
    block = (
        DATA_START + "\n"
        + '<script id="workbench-data" type="application/json">\n'
        + json.dumps(summary, ensure_ascii=False, indent=2)
        + "\n</script>\n"
        + DATA_END
    )
    s = s[:i] + block + s[j + len(DATA_END):]
    home.write_text(s, "utf-8")
    log(f"[注入] index.html 摘要已刷新（{len(json.dumps(summary, ensure_ascii=False))} 字符）")


def bump_sw(summary: dict, bundle: dict) -> None:
    """用摘要 + 内联页面 hash 刷新 Service Worker 缓存版本（内容变即版本变）。"""
    sw = ROOT / "sw.js"
    s = sw.read_text("utf-8")
    ver = hashlib.sha1(
        (summary["meta"]["generatedAt"][:10] + "|" +
         "|".join(m["date"] for m in summary["modules"]) + "|" +
         "|".join(bundle[k]["sha1"] for k in sorted(bundle))).encode()
    ).hexdigest()[:10]
    s2, n = re.subn(r"(const CACHE\s*=\s*['\"])fwb-[^'\"]*(['\"])",
                    rf"\g<1>fwb-{ver}\g<2>", s, count=1)
    if n != 1:
        raise RuntimeError("sw.js 未找到 CACHE 版本常量")
    if s2 != s:
        sw.write_text(s2, "utf-8")
    log(f"[版本] sw.js 缓存版本 → fwb-{ver}")


def sync_rawfile() -> None:
    """同步 web 产物到鸿蒙 ArkWeb 原生产物的 rawfile/web/。

    首页 index.html 已内联四个模块页面，原生壳实际只需加载这一个文件；
    pages/ 仍同步一份便于单独查看与溯源，但运行时不再依赖。
    """
    if not RAWFILE_WEB.parent.exists():
        log("[跳过] 鸿蒙 rawfile 目录尚未建立")
        return
    RAWFILE_WEB.mkdir(parents=True, exist_ok=True)
    for sub in ("pages", "assets", "data"):
        (RAWFILE_WEB / sub).mkdir(exist_ok=True)
    shutil.copy2(ROOT / "index.html", RAWFILE_WEB / "index.html")
    shutil.copy2(ROOT / "manifest.webmanifest", RAWFILE_WEB / "manifest.webmanifest")
    for p in PAGES.glob("*.html"):
        shutil.copy2(p, RAWFILE_WEB / "pages" / p.name)
    for p in ASSETS.glob("*"):
        if p.is_file():
            shutil.copy2(p, RAWFILE_WEB / "assets" / p.name)
    for p in DATA.glob("*.json"):
        shutil.copy2(p, RAWFILE_WEB / "data" / p.name)
    n = len(list(RAWFILE_WEB.rglob("*")))
    log(f"[鸿蒙] rawfile/web 已同步（{n} 个条目，供 ArkWeb $rawfile 加载）")


def sync_dist() -> None:
    """同步单文件发布产物到 dist/（在线应用发布目录）。

    dist/ 只含单文件 index.html 与 PWA 资源，不含 pages/、Python 源码、鸿蒙工程；
    发布时用 `--language static` 指向此目录即可，避免运行时探测被 .py/.ts 干扰。
    """
    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "assets").mkdir(exist_ok=True)
    (DIST / "data").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "index.html", DIST / "index.html")
    shutil.copy2(ROOT / "manifest.webmanifest", DIST / "manifest.webmanifest")
    shutil.copy2(ROOT / "sw.js", DIST / "sw.js")
    for f in ASSETS.iterdir():
        if f.is_file():
            shutil.copy2(f, DIST / "assets" / f.name)
    src = DATA / "workbench-summary.json"
    if src.exists():
        shutil.copy2(src, DIST / "data" / src.name)
    n = len([x for x in DIST.rglob("*") if x.is_file()])
    log(f"[发布] dist/ 已同步（单文件 index.html + PWA 资源，{n} 个文件）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true", help="不同步源模块页面，仅重建摘要")
    args = ap.parse_args()

    print("理财工作台 · 整合构建")
    print("-" * 52)
    if args.no_sync:
        log("[跳过] --no-sync：直接使用现有 pages/*.html")
    else:
        sync_pages()

    summary = build_summary()
    DATA.mkdir(exist_ok=True)
    (DATA / "workbench-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
    )
    log(f"[摘要] data/workbench-summary.json 已写入")

    inject_home(summary)
    bundle = build_page_bundle()
    inject_bundle(bundle)
    bump_sw(summary, bundle)
    sync_rawfile()
    sync_dist()

    print("-" * 52)
    print(f"完成：{summary['meta']['moduleCount']} 个模块 / 最新数据日 {summary['meta']['latestDate']}")
    for m in summary["modules"]:
        print(f"  · {m['name']:6s} {m['date']}  {m['badge']}  |  {m['headline']}")
    print(f"  · 弹窗提醒：{summary['overview']['alert']['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
