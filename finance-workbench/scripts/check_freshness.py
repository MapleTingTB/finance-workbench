#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
理财工作台 · 数据新鲜度检测
================================================
职责：判断仓库内四个模块的数据是否为「北京时间当天最新」。
  - 全部四个模块 meta.date == 今天  -> 退出码 0（FRESH，跳过更新）
  - 任一模块不是今天                -> 退出码 1（STALE，触发补跑）
  - 非工作日                        -> 退出码 0（无需更新）

供 .github/workflows/daily.yml 的 13:30 主更新 / 14:00 检测补跑调用：
  python3 finance-workbench/scripts/check_freshness.py
脚本不联网、不写文件，只读仓库内四个模块的 index.html，纯判定。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # /workspace
MODULES = ["finance-news", "industry-sectors", "index-tracking", "bond-funds"]
CN_TZ = timezone(timedelta(hours=8))


def module_date(mid: str):
    p = ROOT / mid / "index.html"
    if not p.exists():
        return None
    try:
        s = p.read_text("utf-8", errors="replace")
        m = re.search(r'<script id="briefing-data" type="application/json">([\s\S]*?)</script>', s)
        if not m:
            return None
        d = json.loads(m.group(1))
        return (d.get("meta") or {}).get("date")
    except Exception:
        return None


def main() -> int:
    now = datetime.now(CN_TZ)
    today = now.date().isoformat()
    weekday = now.weekday()  # 0=周一 ... 6=周日

    print(f"[新鲜度检测] 北京时间 {now:%Y-%m-%d %H:%M:%S}，今天={today}，weekday={weekday}")

    if weekday >= 5:  # 周六/周日
        print("[新鲜度检测] 非工作日，跳过更新 (FRESH)")
        return 0

    stale = []
    for mid in MODULES:
        d = module_date(mid)
        flag = "OK" if d == today else "STALE"
        print(f"[新鲜度检测] {mid}: {d}  [{flag}]")
        if d != today:
            stale.append(mid)

    if stale:
        print(f"[新鲜度检测] 判定 STALE -> 需补跑，滞后模块: {', '.join(stale)}")
        return 1

    print(f"[新鲜度检测] 判定 FRESH -> 四个模块均为当天最新，跳过更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
