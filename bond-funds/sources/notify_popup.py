#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「债券基金」买入跌幅 · 手机弹窗消息生成脚本（次日 14:00 定时任务的一环）。

职责：
  1. 读取 data/buy-records.json（买入流水）、data/popup-config.json（弹窗提醒配置）、
     data/bond-snapshot.json（含每只基金真实单位净值序列 navSeries）。
  2. 对每只自选基金，取「最近一次买入时间点」的单位净值为基准，与「最新单位净值」
     比较，算相对上次买入的涨跌幅；跌幅 > 0.7% 判定触发。
  3. 触发时组装一条「手机弹窗消息」（标题 + 正文 + 逐只明细），写
     data/popup-notify-log.json；手机页面打开时读取触发态，自动弹出消息卡片。
  4. 本脚本不调用任何外部推送网关、不发送微信/短信，只负责判定与生成消息；
     真正弹出由页面完成，绝不谎报「已弹出 / 已送达」。

口径（与页面前端一致，均为单位净值、可回验）：
  买入净值 = 买入记录里的 nav；留空则取不晚于买入日的最近交易日单位净值。
  当前净值 = navSeries 最后一个交易日的单位净值。
  涨跌幅 = (当前净值 / 买入净值 - 1) × 100%；chg <= -阈值(0.7) 即「跌幅超阈值」。

用法：
  python3 sources/notify_popup.py                # 正常：计算 + 组装弹窗消息 + 写日志
  python3 sources/notify_popup.py --dry-run      # 只计算与组装，不写日志（校验用）
  python3 sources/notify_popup.py --test         # 生成一条测试弹窗消息（忽略是否触发）
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def log(msg):
    print("[notify_popup] " + msg)


def load_json(name, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception as e:
        log(f"{name} 读取失败：{e}")
        return default


def unit_on_or_before(nav_series, date_str):
    """取不晚于 date_str 的最近交易日 [date, acc, unit, chg] 及其单位净值。"""
    hit = None
    for row in nav_series:
        if row[0] <= date_str:
            hit = row
        else:
            break
    # nav_series 按日期升序；若买入日早于序列起点，用第一条
    if hit is None and nav_series:
        hit = nav_series[0]
    return hit


def evaluate(snap, buy_records, threshold):
    """返回 (逐只评估列表, 触发列表)。全部基于真实单位净值序列。"""
    funds = snap.get("funds", {})
    watchlist = snap.get("meta", {}).get("watchlist", list(funds.keys()))
    evaluated, triggered = [], []
    for code in watchlist:
        f = funds.get(code)
        if not f:
            continue
        ns = f.get("navSeries") or []
        name = f.get("name") or f"债券基金 {code}"
        recs = [r for r in (buy_records.get(code) or [])
                if isinstance(r, dict) and r.get("date")]
        if not ns:
            evaluated.append({"code": code, "name": name, "hasBuy": bool(recs),
                              "status": "无净值序列，无法计算（如实标注）",
                              "triggered": False})
            continue
        cur = ns[-1]
        cur_date, cur_unit = cur[0], (cur[2] if cur[2] is not None else cur[1])
        if not recs:
            evaluated.append({"code": code, "name": name, "hasBuy": False,
                              "curDate": cur_date, "curNav": cur_unit,
                              "status": "未录入买入时间，暂不监控", "triggered": False})
            continue
        # 最近一次买入 = 日期最大的一笔
        recs_sorted = sorted(recs, key=lambda r: r["date"])
        last = recs_sorted[-1]
        buy_date = last["date"]
        hit = unit_on_or_before(ns, buy_date)
        buy_unit = last.get("nav")
        buy_src = "录入成交净值"
        if buy_unit is None:
            buy_unit = (hit[2] if hit[2] is not None else hit[1]) if hit else None
            buy_src = f"序列带出（{hit[0]}）" if hit else "取不到"
        chg = None
        if buy_unit and cur_unit is not None:
            chg = round((cur_unit / buy_unit - 1) * 100, 4)
        trig = chg is not None and chg <= -threshold
        item = {
            "code": code, "name": name, "hasBuy": True,
            "buyDate": buy_date, "buyNav": round(float(buy_unit), 4) if buy_unit else None,
            "buyNavSrc": buy_src, "buyAmount": last.get("amount"),
            "buyCount": len(recs_sorted),
            "curDate": cur_date, "curNav": round(float(cur_unit), 4) if cur_unit is not None else None,
            "chgPct": chg, "threshold": threshold, "triggered": trig,
            "status": ("跌幅超阈值 · 需加强关注" if trig else "未触发 · 正常监控"),
        }
        evaluated.append(item)
        if trig:
            triggered.append(item)
    return evaluated, triggered


def message_items(triggered):
    """弹窗卡片逐只明细（结构化，页面与日志共用）。"""
    return [{"code": t["code"], "name": t["name"], "buyDate": t["buyDate"],
             "buyNav": t["buyNav"], "curDate": t["curDate"], "curNav": t["curNav"],
             "chgPct": t["chgPct"]} for t in triggered]


def build_message(triggered, threshold, date_str, is_test=False):
    """返回 (title, content, items)。content 为纯文本，供消息预览与说明留痕。"""
    if is_test:
        title = "债券基金 · 买入跌幅提醒（测试）"
        lines = [
            "这是一条测试弹窗消息，用于预览触发时手机页面弹出的内容。",
            "",
            f"触发规则：相对上次买入时间点跌幅 > {threshold:.1f}% 时，页面自动弹出消息卡片。",
            f"数据基准日：{date_str}｜口径：单位净值，逐条可回验。",
            "",
            "如非本人操作，请忽略本条消息。",
        ]
        # 测试时把当前已触发的基金一并列出，便于核对渲染
        items = message_items(triggered)
        if items:
            lines.insert(2, "当前已触发 " + str(len(items)) + " 只：")
            for t in items:
                lines.insert(3, f"· {t['name']}（{t['code']}）{t['chgPct']:+.2f}%")
    elif not triggered:
        title = "债券基金 · 买入跌幅监控（未触发）"
        lines = [
            f"本次 14:00 更新：无自选基金相对上次买入跌幅超过 {threshold:.1f}%，按规则不弹窗。",
            "",
            f"触发规则：相对上次买入时间点跌幅 > {threshold:.1f}% 时，页面自动弹出消息卡片。",
            f"数据基准：{date_str}｜单位净值口径，逐条可回验。",
            "本记录由自动化脚本生成，仅供参考，不构成投资建议。",
        ]
        items = []
    else:
        title = f"债券基金 · 买入跌幅提醒（{len(triggered)} 只）"
        lines = [
            f"以下自选基金相对上次买入时间点跌幅已超过 {threshold:.1f}%，请加强关注：",
            "",
        ]
        for t in triggered:
            lines.append(f"· {t['name']}（{t['code']}）")
            lines.append(
                f"  买入 {t['buyDate']} 净值 {t['buyNav']} → 最新 {t['curDate']} 净值 {t['curNav']}，"
                f"涨跌幅 {t['chgPct']:+.2f}%")
        lines += [
            "",
            "普通家庭做法：债基以稳为主，先核对是否触及自己的止损/加仓计划，"
            "不因单日波动追涨杀跌；首付池保持流动性。",
            "",
            f"数据基准：{date_str}｜来源：天天基金单位净值（真实序列，可回验）。",
            "本提醒由自动化脚本生成，仅供参考，不构成投资建议。",
        ]
        items = message_items(triggered)
    return title, "\n".join(lines), items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只计算与组装消息，不写日志")
    ap.add_argument("--test", action="store_true", help="生成一条测试弹窗消息（忽略是否触发）")
    args = ap.parse_args()

    now = datetime.now(timezone(timedelta(hours=8)))
    date_str = now.strftime("%Y-%m-%d")
    run_at = now.strftime("%Y-%m-%d %H:%M:%S")

    snap = load_json("bond-snapshot.json", {})
    buy_cfg = load_json("buy-records.json", {})
    popup_cfg = load_json("popup-config.json", {})
    buy_records = buy_cfg.get("records", {}) if isinstance(buy_cfg, dict) else {}
    threshold = float(popup_cfg.get("threshold", 0.7))
    enabled = bool(popup_cfg.get("enabled", True))
    system_notify = bool(popup_cfg.get("systemNotify", False))

    meta = snap.get("meta", {})
    nav_date = meta.get("navDate", date_str)

    evaluated, triggered = evaluate(snap, buy_records, threshold)
    log(f"评估 {len(evaluated)} 只 / 触发 {len(triggered)} 只 / 阈值 -{threshold:.1f}% / "
        f"弹窗提醒 {'开启' if enabled else '关闭'} / 系统通知意向 {'开' if system_notify else '关'}")

    title, content, items = build_message(triggered, threshold, nav_date, is_test=args.test)
    result = {
        "date": date_str, "runAt": run_at, "navDate": nav_date,
        "threshold": threshold, "enabled": enabled, "systemNotify": system_notify,
        "channelName": "手机弹窗消息",
        "evaluated": evaluated, "triggered": triggered,
        "popupStatus": "", "popupDetail": "",
        "message": {"title": title, "content": content, "items": items},
        "note": "脚本只负责判定与生成弹窗消息并写日志；真正弹出由手机页面在打开时读取触发态完成，"
                "不调用任何外部推送网关，不谎报已弹出 / 已送达。",
    }

    if args.test:
        result["popupStatus"] = "test_ready"
        result["popupDetail"] = "已生成测试弹窗消息，可在页面「提醒设置 → 测试弹窗」查看同款效果"
    elif not enabled:
        result["popupStatus"] = "disabled"
        result["popupDetail"] = "弹窗提醒已在配置中关闭，本次仅记录判定结果，页面不会自动弹出"
    elif not triggered:
        result["popupStatus"] = "skipped_no_trigger"
        result["popupDetail"] = f"本次无基金跌幅超过 {threshold:.1f}%，按规则不弹窗（未触发）"
    else:
        result["popupStatus"] = "popup_ready"
        result["popupDetail"] = (f"{len(triggered)} 只触发，已生成手机弹窗消息；"
                                 "用户下次打开页面时自动弹出消息卡片（同一天只弹一次）")

    log(f"结果：{result['popupStatus']} — {result['popupDetail']}")
    if args.dry_run:
        log("--dry-run：未写日志")
        return

    (DATA / "popup-notify-log.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    (DATA / f"popup-notify-{date_str}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    log(f"已写 {DATA / 'popup-notify-log.json'}")


if __name__ == "__main__":
    main()
