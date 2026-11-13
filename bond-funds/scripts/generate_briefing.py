#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「债券基金」简报生成器：读取 bond-snapshot.json + popup-notify-log.json → 注入 index.html + 写推送摘要。

用法：python3 scripts/generate_briefing.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HTML = ROOT / "index.html"


def load_json(name, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def main():
    snap = json.loads((DATA / "bond-snapshot.json").read_text("utf-8"))
    meta = snap["meta"]
    rank = snap["rank"]
    baro = snap["barometer"] or {}
    alerts = snap.get("alerts", [])
    date = meta["date"]
    drop_threshold = snap.get("dropThreshold", meta.get("dropThreshold", 0.7))
    buy_records = snap.get("buyRecords", {})
    popup_push = snap.get("popupPush", {"enabled": True, "systemNotify": False,
                                        "channelName": "手机弹窗消息", "threshold": drop_threshold})
    notify_log = load_json("popup-notify-log.json", {})

    funds = [snap["funds"][c] for c in meta["watchlist"] if c in snap["funds"]]
    quick = []
    for x in rank["top5"]:
        quick.append({"code": x["code"], "name": x["name"]})
    quick = quick[:6]

    # ---------- 今日一句话（基础句，不含提醒片段；前端按当前触发态动态拼接） ----------
    # 提醒片段由前端 renderAlerts() 依据 currentDrops() 追加，避免服务端静态文案与
    # 用户即时录入买入后的触发态不一致；此处只保留晴雨与榜首，并去掉 action 尾部句号防止「。；」。
    top = rank["top5"][0] if rank["top5"] else None
    has_buy = meta.get("hasBuyCount", 0)
    action_base = (baro.get("action", "") or "").rstrip("。；")
    parts = [f"债市晴雨「{baro.get('label', '—')}」：{action_base}"]
    if top:
        parts.append(f"综合榜首 {top['name'][:12]}（{top['code']}）近三年 {top['y3']:.2f}%、最大回撤 {top['dd3y']:.2f}%")
    today_action = "；".join(parts) + "。"

    # ---------- 图例 ----------
    legend = [
        {"color": "up", "label": "红＝上涨 / 偏强"},
        {"color": "down", "label": "绿＝下跌 / 偏弱"},
        {"color": "risk", "label": "橙＝偏高 / 风险"},
        {"color": "neutral", "label": "蓝灰＝中性 / 震荡"},
    ]

    # ---------- 灰字依据规则 ----------
    ch_name = popup_push.get("channelName", "手机弹窗消息")
    basis_rules = [
        {"k": "排名样本（全市场）",
         "v": f"天天基金开放式基金排行 rankhandler.aspx（dt=kf & ft=zq）返回 {meta['universe']} 只场外债券型基金；"
              f"要求成立满三年（净值日 {meta['navDate']} 往前 3 年）且近三年收益有效，得 {meta['eligible']} 只。"},
        {"k": "异常样本剔除",
         "v": f"剔除近三年收益 > {meta['abnormalLine']:.0f}% 的 {meta['excluded']} 只：债券基金出现三位数收益通常为巨额赎回导致"
              "净值异动，非可持续收益，列入榜单会误导。剔除数量与阈值均如实记录，不修饰数据。"},
        {"k": "回撤计算候选池",
         "v": f"剔除异常后按近三年收益降序取头部 {meta['pool']} 只，逐只调用天天基金 pingzhongdata 累计净值全序列，"
              f"成功计算 {meta['computed']} 只近三年最大回撤（失败如实标注，不编造）。"},
        {"k": "综合排名公式",
         "v": "综合分 = 0.6 × 收益排名分位 + 0.4 × 回撤排名分位；两项均在候选池内按名次线性映射为 0~100 分"
              "（收益越高分越高、最大回撤越小分越高）。同一基金的 A/C/E 份额按名称归一后去重，保证 TOP5 是 5 只不同基金。"},
        {"k": "最大回撤定义（仅展示）",
         "v": "基于累计净值（含分红再投资）的峰谷回撤：max[(峰值 − 谷值) ÷ 峰值]。近三年区间 "
              f"{rank['cutoff']} ~ {meta['navDate']}；近一月按自然日 30 天窗口计算。近一月回撤仅作行情参考，不再作为通知触发条件。"},
        {"k": "相对上次买入涨跌幅口径",
         "v": "取 data/buy-records.json 中该基金「最近一次买入时间点」的单位净值为基准（录入成交净值优先；留空则按买入日"
              "或之前最近交易日从天天基金 pingzhongdata 真实单位净值序列带出），与最新单位净值比较："
              "涨跌幅 =（最新单位净值 ÷ 买入净值 − 1）× 100%。页面「买入记录」录入后即时用注入的真实净值序列试算，"
              "次日 14:00 定时任务更新净值后重算，逐条可回验，禁止编造。"},
        {"k": "买入跌幅弹窗提醒规则（0.7%）",
         "v": f"次日 14:00 更新时，若某只自选基金相对上次买入的「跌幅」超过 {drop_threshold:.1f}%（即涨跌幅 ≤ −{drop_threshold:.1f}%）"
              "即触发「需加强关注」：sources/notify_popup.py 复算同一口径并生成手机弹窗消息，写 data/popup-notify-log.json；"
              "手机页面打开时读取触发态，自动弹出消息卡片（同一天只弹一次），顶部橙红提醒条同步展示。"
              "未触发时不弹窗，仅显示监控状态；全过程不调用任何外部推送网关，不谎报已送达。"},
        {"k": "手机弹窗与系统通知说明",
         "v": "已不再使用微信推送网关。触发时由页面在手机屏幕中央弹出消息卡片（纯前端、无需密钥、开箱即用），"
              "列出触发基金、买入日/净值、最新净值、涨跌幅与阈值；点「知道了」后当天不再重复弹出。"
              "可选的手机状态栏系统通知走浏览器 Notification API：需在页面「提醒设置」里授权，"
              "且要求 HTTPS / localhost 并允许通知；未授权或不支持时如实降级为仅页内弹窗，不谎报已送达。"
              f"当前提醒方式：{ch_name}（{'已开启' if popup_push.get('enabled', True) else '已关闭'}）；"
              f"系统通知意向：{'开' if popup_push.get('systemNotify') else '关'}。"},
        {"k": "实时指数 / 成交量口径",
         "v": "场外债券基金无盘中盘口与实时估值，故「当天实时指数 / 成交量」取其对标债券指数的实时行情："
              "混合二级 / 可转债型 → 中证转债，利率债 / 纯债 → 国债指数，信用债 / 混合一级 → 企债指数"
              "（新浪财经 / 同花顺 / 东方财富 实时行情）。最终收益以基金公司公布净值为准。"},
        {"k": "涨幅估值口径",
         "v": "以对标债券指数盘中涨幅作估算，不是基金公司官方估值；已确认净值日涨幅单独列出，两者不混用。"},
        {"k": "估值高低口径",
         "v": "非 PE 估值。用基金累计净值近 250 个交易日分位衡量：≥70% 偏高、≤30% 偏低、其余合理；"
              "债券基金净值多创新高，分位偏高不代表股价意义上的高估。"},
        {"k": "技术指标 BOLL(20,2)",
         "v": "中轨 = MA20，上/下轨 = 中轨 ± 2σ（20 日总体标准差）；%B = (收盘 − 下轨) ÷ (上轨 − 下轨)；"
              "带宽 = (上轨 − 下轨) ÷ 中轨。区域判定取 %B 与近 250 日分位各半：≥68 顶部、≤32 底部、其余中部。"},
        {"k": "技术指标 MACD(12,26,9)",
         "v": "DIF = EMA12 − EMA26，DEA = DIF 的 EMA9，柱 = 2 × (DIF − DEA)。趋势五因子：MA5/MA20、MA20/MA60、"
              "DIF/DEA、MA20 近 5 日斜率、方向得分；≥2 上涨趋势、≤−2 下跌趋势、其余震荡整理。"},
        {"k": "未来走势预估",
         "v": "五因子各 ±1（DIF 方向、柱体方向、收盘与 MA20、%B 是否 <90、带宽是否 <25），"
              "得分 ≥3 偏强震荡、≤−2 偏弱调整、其余区间震荡。仅技术倾向，不预示未来。"},
        {"k": "晴雨表评分",
         "v": "技术分 0~10：趋势 ±1.5、MACD ±1、收盘与 BOLL 中轨 ±1、当日涨跌 ±1、%B ±0.5，"
              "近 250 日分位 ≥90 时高位折减 0.5；政策分 −2~2 由国内债市快讯与央行/财政部官网新闻的利好/"
              "利空关键词加权。合计 ≥7.5 晴、≥6 多云转晴、≥4.5 多云、≥3 阴、其余雨。"},
        {"k": "政策与官方新闻来源",
         "v": "东方财富 7×24 快讯全天翻页（fastColumn=102，600 条）→ 国内债市主体白名单筛出 "
              f"{len(baro.get('news', []))} 条；中国人民银行 / 财政部官网新闻栏目 → 债券政策强相关 "
              f"{len(baro.get('official', []))} 条。海外利率、股票回购、黄金、公司票据等误命中已排除。"
              "新闻仅作定性参考，不用于预测。"},
        {"k": "数据时间基准",
         "v": f"净值数据截至 {meta['navDate']}（天天基金公布口径）；债券指数行情为 {date} {meta['dataTime']} 盘中快照，"
              "盘前国债 / 企债指数可能尚未更新而显示持平，属正常。"},
    ]

    disclaimer = ("⚠ 本内容由 AI 基于公开行情与官方信息整理生成，仅供参考，不构成任何投资建议或具体产品推荐。"
                  "债券基金为场外基金，「实时指数 / 涨幅估值」取对标债券指数行情，最终收益以基金公司公布净值为准。"
                  "相对买入涨跌幅基于历史单位净值，不代表未来表现；买入跌幅提醒为手机页面弹窗消息，"
                  "需在页面打开时才会弹出，系统通知需另行授权浏览器通知权限。"
                  "历史收益与最大回撤不代表未来表现，技术分析基于历史价格，不预示未来。投资有风险，决策需谨慎。")

    # 弹窗提醒日志摘要（供页面展示「上次提醒」状态，如实呈现）
    notify_summary = {
        "runAt": notify_log.get("runAt", "—"),
        "date": notify_log.get("date", "—"),
        "popupStatus": notify_log.get("popupStatus", "not_run"),
        "popupDetail": notify_log.get("popupDetail", "尚未运行弹窗提醒脚本"),
        "enabled": notify_log.get("enabled", popup_push.get("enabled", True)),
        "systemNotify": notify_log.get("systemNotify", popup_push.get("systemNotify", False)),
        "channelName": notify_log.get("channelName", ch_name),
        "triggerCount": len(notify_log.get("triggered", [])),
        "message": notify_log.get("message", {}),
    }

    briefing = {
        "meta": dict(meta, dateText=f"{date} {meta['weekday']}"),
        "marketCards": snap["marketCards"],
        "legend": legend,
        "todayAction": today_action,
        "rank": {"top5": rank["top5"], "meta": {
            "universe": meta["universe"], "eligible": meta["eligible"],
            "excluded": meta["excluded"], "abnormalLine": meta["abnormalLine"],
            "pool": meta["pool"], "computed": meta["computed"], "cutoff": rank["cutoff"],
        }},
        "funds": funds,
        "watchlist": meta["watchlist"],
        "quick": quick,
        "alerts": alerts,
        "dropThreshold": drop_threshold,
        "buyRecords": buy_records,
        "popupPush": popup_push,
        "notifyLog": notify_summary,
        "barometer": baro,
        "basisRules": basis_rules,
        "channels": snap["channels"],
        "trace": snap["trace"],
        "disclaimer": disclaimer,
    }

    # ---------- 注入 HTML ----------
    payload = json.dumps(briefing, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    h = HTML.read_text("utf-8")
    pat = r'(<script id="briefing-data" type="application/json">).*?(</script>)'
    if not re.search(pat, h, re.S):
        raise RuntimeError("index.html 缺少 <script id=\"briefing-data\"> 注入点")
    h2 = re.sub(pat, lambda m: m.group(1) + payload + m.group(2), h, count=1, flags=re.S)
    HTML.write_text(h2, "utf-8")

    # ---------- 归档 briefing JSON ----------
    (DATA / f"bond-briefing-{date}.json").write_text(
        json.dumps(briefing, ensure_ascii=False, indent=1), "utf-8")
    (DATA / "bond-briefing-latest.json").write_text(
        json.dumps(briefing, ensure_ascii=False, indent=1), "utf-8")

    # ---------- 推送摘要 ----------
    lines = [
        f"# 债券基金 · 每日简报（{date} {meta['weekday']} 14:00 更新）",
        "",
        f"> 数据口径：净值截至 {meta['navDate']}；债券指数行情 {date} {meta['dataTime']} 盘中快照。",
        "",
        "## 一、今日债券市场晴雨",
        "",
        f"**{baro.get('label', '—')}**（综合 {baro.get('score', '—')} 分＝技术 {baro.get('techScore', '—')} + 政策 {baro.get('policyScore', '—')}）",
        "",
        baro.get("summary", ""),
        "",
        f"**给普通家庭的做法**：{baro.get('action', '')}",
        "",
        f"技术面：{baro.get('tech', {}).get('trend', {}).get('label', '—')} / "
        f"{baro.get('tech', {}).get('zone', {}).get('label', '—')} / "
        f"{baro.get('tech', {}).get('outlook', {}).get('label', '—')}；{baro.get('positionNote', '')}",
        "",
        f"## 二、买入跌幅提醒（相对上次买入 > {drop_threshold:.1f}% → 手机弹窗消息）",
        "",
    ]
    if alerts:
        for a in alerts:
            lines.append(f"- ⚠ **{a['name']}（{a['code']}）** 相对上次买入（{a['buyDate']}，净值 {a['buyNav']}）"
                         f"最新 {a['curDate']} 净值 {a['curNav']}，涨跌幅 **{a['chgPct']:+.2f}%**，"
                         f"跌幅超过 {a['threshold']:.1f}% 阈值，需加强关注。")
    else:
        lines.append(f"- 本期 {has_buy} 只已录入买入时间的自选基金，相对上次买入均未跌超 {drop_threshold:.1f}%，无需提醒（监控中）。")
    lines += [
        "",
        f"**弹窗提醒状态**：{notify_summary['popupStatus']} — {notify_summary['popupDetail']}"
        f"（{notify_summary['channelName']}，运行于 {notify_summary['runAt']}）。",
        "",
        "> 说明：相对上次买入涨跌幅 =（最新单位净值 ÷ 最近一次买入净值 − 1）；跌幅 > 0.7% 触发。"
        "触发时由 `sources/notify_popup.py` 生成消息、手机页面打开时自动弹出消息卡片（同一天只弹一次）；"
        "不再使用微信推送网关，无需任何密钥。",
        "",
        "## 三、债券基金综合排名 TOP5",
        "",
        f"全市场场外债券型基金 {meta['universe']} 只 → 满三年 {meta['eligible']} 只 → "
        f"剔除异常 {meta['excluded']} 只 → 收益头部 {meta['pool']} 只算回撤 → 综合排名（A/C 份额去重）。",
        "",
        "| 名次 | 基金名称 | 代码 | 近三年收益 | 近三年最大回撤 | 综合分 |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for x in rank["top5"]:
        lines.append(f"| {x['rank']} | {x['name']} | {x['code']} | {x['y3']:.2f}% | "
                     f"{x['dd3y']:.2f}% | {x['composite']:.2f} |")
    lines += ["", "## 四、我的自选债券基金（买入跟踪）", ""]
    for f in funds:
        m = {x["k"]: x["v"] for x in f.get("main", [])}
        sb = f.get("sinceBuy") or {}
        lines += [
            f"### {f['name']}（{f['code']}）",
            "",
            f"- 类型：{f.get('ftype') or '—'}　对标指数：{f.get('benchmark') or '—'}　"
            f"最新净值 {str((f.get('nav') or {}).get('value', '—'))}（{(f.get('nav') or {}).get('date', '—')}）",
            f"- 实时指数 {m.get('当天实时指数', '—')}｜涨幅估值 {m.get('涨幅估值', '—')}｜"
            f"成交量 {m.get('成交量', '—')}｜估值高低 {m.get('估值高低', '—')}｜"
            f"未来走势 {m.get('未来走势预估', '—')}",
            f"- 技术面：{f['tech']['trend']['label']} / {f['tech']['zone']['label']} / {f['tech']['outlook']['label']}",
            (f"- **买入跟踪**：最近买入 {sb.get('buyDate')}（净值 {sb.get('buyNav')}）→ 最新 {sb.get('curDate')}"
             f"（净值 {sb.get('curNav')}），相对买入 **{sb['chgPct']:+.2f}%**，{sb.get('status', '—')}"
             if sb.get("chgPct") is not None and sb.get("hasBuy") else
             "- **买入跟踪**：尚未录入买入时间，暂不计算相对买入涨跌幅"),
            "",
        ]
    lines += ["## 五、政策与官方新闻", "", "**财经快讯（国内债市）**", ""]
    for x in baro.get("news", []):
        lines.append(f"- {x['time']}｜{x['title']}（{x['source']}）")
    lines += ["", "**官方发布（央行 / 财政部）**", ""]
    for x in baro.get("official", []):
        lines.append(f"- {x['time'] or '日期未取到'}｜{x['title']}（{x['source']}）")
    lines += ["", "## 六、技术依据（摘要）", ""]
    for b in basis_rules[:8]:
        lines.append(f"- **{b['k']}**：{b['v']}")
    lines += [
        "",
        f"> 数据来源：{meta['dataSource']}。完整公式与逐只基金原始数值见页面页尾灰字备注。",
        "",
        "---",
        "",
        disclaimer,
        "",
    ]
    (DATA / f"push-{date}.md").write_text("\n".join(lines), "utf-8")

    print(f"已注入 {HTML.name}（{len(payload)} 字节数据）")
    print(f"简报生成完成：TOP5 {len(rank['top5'])} 只 / 自选 {len(funds)} 只 / "
          f"买入跌幅触发 {len(alerts)} 只 / 弹窗状态 {notify_summary['popupStatus']} / 晴雨 {baro.get('label', '—')} / {date}")
    print(f"推送摘要：{DATA / f'push-{date}.md'}")


if __name__ == "__main__":
    main()
