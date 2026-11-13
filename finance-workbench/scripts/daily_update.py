#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""理财工作台 · 工作日 14:00 全量更新并发布。

链路：四模块采集 → 生成 → 工作台整合(dist) → 产物校验 → 发布（覆盖同一线上链接）。

可靠性设计（应对行情接口偶发/长期不可用）：
  1. 运行前备份四个模块的 index.html（工作台最终只消费这些页面）；
  2. 某模块采集失败 → 跳过该模块生成，沿用上一版有效页面；
  3. 生成后校验注入数据质量；若「降级」（如指数 0 条、榜单为空）→ 回滚该页面；
  4. 其余模块照常更新；构建失败或产物不自足 → 不发布，线上保留上次成功版本；
  5. 全部模块都无有效更新 → 跳过发布，避免无意义覆盖。

用法：
  python3 scripts/daily_update.py              # 完整执行（含发布）
  python3 scripts/daily_update.py --dry-run    # 只跑更新与校验，不发布
  python3 scripts/daily_update.py --deploy     # 云端 CI：跳过平台发布脚本，产物写 dist/ 与 docs/
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # 仓库根：本地 /workspace，CI 为 runner 工作目录
WB = ROOT / "finance-workbench"
LOG = WB / "data/daily-update.log"
PUBLISH_JS = Path("/root/.codebuddy/skills/sites/scripts/publish.js")

# (模块目录, 中文名, 采集步骤[(标签, 脚本)], 质量检查器)
MODULES = [
    ("finance-news", "财经新闻",
     [("采集", "sources/fetch_mainstream.py"), ("盘中研判", "sources/intraday.py")], "finance"),
    ("industry-sectors", "行业板块",
     [("采集", "sources/fetch_sectors.py")], "sector"),
    ("index-tracking", "指数跟踪",
     [("采集", "sources/fetch_funds.py")], "fund"),
    ("bond-funds", "债券基金",
     [("采集", "sources/fetch_bonds.py"), ("弹窗提醒", "sources/notify_popup.py")], "bond"),
]

GENERATOR = "scripts/generate_briefing.py"


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cwd: Path, argv: list[str], timeout: int = 420) -> tuple[bool, str]:
    """执行子命令，返回 (是否成功, 输出尾部)。"""
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"超时 {timeout}s"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    tail = "\n".join(out.splitlines()[-6:])
    return r.returncode == 0, tail


def briefing_of(html: Path) -> dict:
    try:
        s = html.read_text("utf-8")
        m = re.search(r'<script id="briefing-data" type="application/json">([\s\S]*?)</script>', s)
        return json.loads(m.group(1)) if m else {}
    except Exception:  # noqa: BLE001
        return {}


def page_ok(kind: str, d: dict, old: dict | None = None) -> tuple[bool, str]:
    """检查生成后页面的注入数据是否完整（防止把降级数据发上线）。

    行情接口不通时，大盘列表会变空、晴雨表变成「—」、要闻数会骤降。因此除
    关键字段有效外，还与上一版做数量对比，明显退化一律判为降级并沿用上一版。
    """
    try:
        if kind == "finance":
            n = (d.get("meta") or {}).get("newsCount", 0)
            g = d.get("glance") or {}
            intra = g.get("intraday") or {}
            idx = g.get("indices") or []
            ok = n > 0 and bool(intra.get("verdictLabel")) and len(idx) > 0
            why = f"要闻{n}条/指数{len(idx)}条/盘中{'有' if intra.get('verdictLabel') else '无'}"
        elif kind == "sector":
            n = len(d.get("top5") or [])
            sig = len(d.get("good") or []) + len(d.get("bad") or [])
            ok, why = n >= 3 and sig > 0, f"TOP榜{n}条/信号{sig}条"
        elif kind == "fund":
            n = len(d.get("funds") or [])
            mk = d.get("marketCards") or []
            ok, why = n > 0 and len(mk) >= 3, f"自选{n}只/大盘{len(mk)}条"
        elif kind == "bond":
            n = len(((d.get("rank") or {}).get("top5")) or [])
            b = d.get("barometer") or {}
            ok = n >= 3 and bool(b.get("label")) and b.get("score") not in (None, "", "—")
            why = f"债基TOP{n}条/晴雨「{b.get('label', '—')}」{b.get('score', '—')}"
        else:
            return False, "未知模块"

        def key_count(x: dict) -> int:
            if kind == "finance":
                return (x.get("meta") or {}).get("newsCount", 0)
            if kind == "sector":
                return len(x.get("top5") or [])
            if kind == "fund":
                return len(x.get("marketCards") or [])
            return len(((x.get("rank") or {}).get("top5")) or [])

        if ok and old:
            oc, nc = key_count(old), key_count(d)
            if oc >= 3 and nc < max(2, int(oc * 0.6)):
                return False, f"{why}；较上一版明显下降（{oc}→{nc}）"
        return ok, why
    except Exception:  # noqa: BLE001
        return False, "结构异常"

def verify_dist() -> tuple[bool, str]:
    p = WB / "dist/index.html"
    if not p.exists():
        return False, "dist/index.html 不存在"
    s = p.read_text("utf-8")
    m = re.search(r'<script id="page-bundle" type="application/json">([\s\S]*?)</script>', s)
    try:
        bundle = json.loads(m.group(1)) if m else {}
    except Exception:  # noqa: BLE001
        bundle = {}
    need = [x[0] for x in MODULES]
    checks = [
        ("含内联标记", "PAGE_BUNDLE_START" in s),
        ("srcdoc 装载", "f.srcdoc = html" in s),
        ("无外部 src", 'setAttribute("src", m.page)' not in s),
        ("体积合理", p.stat().st_size > 400000),
        ("四模块内联齐全", all(k in bundle for k in need)),
    ]
    bad = [n for n, ok in checks if not ok]
    return not bad, f"{p.stat().st_size} B；" + ("全部通过" if not bad else "失败项 " + ",".join(bad))


def sync_docs() -> None:
    """云端模式：把 dist/ 产物同步到仓库根 docs/，供 GitHub / Cloudflare Pages 托管。"""
    src, dst = WB / "dist", ROOT / "docs"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    # GitHub Pages 默认走 Jekyll，会忽略下划线开头资源；.nojekyll 关闭该行为
    (dst / ".nojekyll").write_text("", encoding="utf-8")
    n = sum(1 for x in dst.rglob("*") if x.is_file())
    log(f"  [Pages·产物] docs/ 已同步（{n} 个文件）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只跑更新与校验，不发布")
    ap.add_argument("--deploy", action="store_true",
                     help="云端 CI 模式：不调用 publish.js，构建产物同步到 docs/ 供 Pages 托管")
    args = ap.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 120_000:
        lines = LOG.read_text("utf-8", errors="replace").splitlines()[-400:]
        LOG.write_text("\n".join(lines) + "\n", "utf-8")
    log(f"===== 每日更新开始  dry={args.dry_run} deploy={args.deploy} =====")

    # 线上链接是 dist/ 目录的实时服务：只要本次「不发布」（dry-run / 构建失败 /
    # 校验失败 / 无有效新数据 / 发布失败），都必须完整还原 dist。否则
    # build_workbench.py 已重写过 dist，「线上保持旧版」只是名义上的。
    dist_bak = None
    if (WB / "dist").exists():
        dist_bak = Path(tempfile.mkdtemp(prefix="wb-dist-bak-")) / "dist"
        shutil.copytree(WB / "dist", dist_bak)

    def restore_dist():
        if dist_bak is None:
            return
        shutil.rmtree(WB / "dist", ignore_errors=True)
        shutil.copytree(dist_bak, WB / "dist")
        shutil.rmtree(dist_bak.parent, ignore_errors=True)
        log("  已还原 dist/（线上内容保持不变）")

    def drop_backup():
        if dist_bak is not None:
            shutil.rmtree(dist_bak.parent, ignore_errors=True)

    # 1) 备份各模块页面
    backups = {}
    old_brief = {}
    for mid, name, _, _ in MODULES:
        p = ROOT / mid / "index.html"
        backups[mid] = p.read_bytes() if p.exists() else None
        old_brief[mid] = briefing_of(p) if p.exists() else {}
    log("已备份四个模块 index.html")

    # 2) 逐模块：采集 → 生成 → 质量校验（不合格回滚）
    updated, kept = [], []
    for mid, name, steps, kind in MODULES:
        cwd = ROOT / mid
        fetch_ok = True
        for label, script in steps:
            ok, tail = run(cwd, ["python3", script])
            log(f"  [{name}·{label}] {'OK' if ok else 'FAIL'}" + (f"  {tail[:220]}" if not ok else ""))
            if not ok:
                fetch_ok = False
                break
        if fetch_ok:
            ok, tail = run(cwd, ["python3", GENERATOR])
            log(f"  [{name}·生成] {'OK' if ok else 'FAIL'}" + (f"  {tail[:220]}" if not ok else ""))
            if ok:
                good, why = page_ok(kind, briefing_of(cwd / "index.html"), old_brief[mid])
                log(f"  [{name}·质量] {'合格' if good else '降级'}（{why}）")
                if good:
                    updated.append(mid)
                    continue
        # 回滚：保留上一版有效页面
        if backups[mid] is not None:
            (cwd / "index.html").write_bytes(backups[mid])
        kept.append(mid)
        log(f"  [{name}] 沿用上一版有效页面（未覆盖）")

    log(f"模块结果：更新 {len(updated)} 个（{', '.join(updated) or '无'}）／沿用 {len(kept)} 个（{', '.join(kept) or '无'}）")

    # 3) 工作台整合
    ok, tail = run(WB, ["python3", "scripts/build_workbench.py"])
    log(f"  [工作台·构建] {'OK' if ok else 'FAIL'}" + (f"  {tail[:220]}" if not ok else ""))
    if not ok:
        restore_dist()
        log("FAILED: 构建失败，已中止，未发布（线上保持旧版）")
        return 1

    # 4) 产物自足校验
    good, why = verify_dist()
    log(f"  [产物校验] {'OK' if good else 'FAIL'}  {why}")
    if not good:
        restore_dist()
        log("FAILED: 产物校验失败，已中止，未发布（线上保持旧版）")
        return 1

    # 5) 发布
    if not updated:
        restore_dist()
        log("NO_CHANGE_OK: 本次无模块取得有效新数据，docs/ 保持不变" if args.deploy
            else "NO_CHANGE_OK: 本次无模块取得有效新数据，跳过发布（线上保持上一版）")
        return 0
    if args.deploy:
        sync_docs()
        drop_backup()
        log(f"DEPLOY_OK: 云端构建完成（{len(updated)} 个模块新数据，{len(kept)} 个沿用上一版），"
            "产物已写入 dist/ 与 docs/")
        return 0
    if args.dry_run:
        restore_dist()
        log(f"DRYRUN_OK: {len(updated)} 个模块可更新，全链路通过，未发布（dist 已还原，线上不变）")
        log(f"正式执行将运行：node {PUBLISH_JS} --dir {WB / 'dist'} --language static")
        return 0

    ok, tail = run(WB, ["node", str(PUBLISH_JS), "--dir", str(WB / "dist"),
                        "--language", "static"], timeout=600)
    log(f"  [发布] {'OK' if ok else 'FAIL'}  {tail[:300]}")
    if not ok or not re.search(r'"verified"\s*:\s*true', tail):
        restore_dist()
        log("FAILED: 发布未通过校验，已还原 dist（线上保持旧版），请查看日志")
        return 1
    drop_backup()
    log(f"PUBLISH_OK: 线上已更新（{len(updated)} 个模块新数据，{len(kept)} 个沿用上一版）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
