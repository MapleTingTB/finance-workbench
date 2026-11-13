#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""理财工作台 · 手机视口 / 交互 / PWA / 鸿蒙工程一致性校验 + 截图。

用法：python3 scripts/verify_workbench.py
自带临时 HTTP 服务（随机端口），校验首页 / 四模块内联切换 / PWA / 鸿蒙工程 /
单文件发布（file:// 且目录内无 pages/）并输出 data/preview-*.png。
"""
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

report = {"checks": [], "errors": []}


def ck(name, ok, detail=""):
    report["checks"].append({"name": name, "ok": bool(ok)})
    print(("  PASS " if ok else "  FAIL ") + name + ("  " + str(detail)[:180] if detail else ""))


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


Handler = functools.partial(Quiet, directory=str(ROOT))
httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
PORT = httpd.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"
print(f"本地服务：{BASE}\n")

js_errors = []
MOD_IDS = ["finance-news", "industry-sectors", "index-tracking", "bond-funds"]

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path="/usr/bin/chromium",
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width": 390, "height": 844},
                              device_scale_factor=2, is_mobile=True, has_touch=True,
                              service_workers="allow")
    page = ctx.new_page()
    page.on("pageerror", lambda e: js_errors.append("pageerror: " + str(e)))
    page.on("console", lambda m: js_errors.append("console.error: " + m.text) if m.type == "error" else None)

    # ================= A. 首页 =================
    page.goto(BASE + "/index.html", wait_until="load", timeout=30000)
    page.wait_for_timeout(1000)

    print("--- A. 首页 ---")
    dim = page.evaluate("()=>({iw:innerWidth,sw:document.documentElement.scrollWidth,bw:document.body.scrollWidth})")
    ck("首页无横向溢出", dim["sw"] <= dim["iw"] + 1 and dim["bw"] <= dim["iw"] + 1, dim)

    over = page.evaluate("""()=>{
      var bad=[],w=innerWidth;
      document.querySelectorAll('#shell *').forEach(function(el){
        if(el.closest('svg'))return;
        var cs=getComputedStyle(el);
        if(cs.display==='none'||cs.visibility==='hidden')return;
        var r=el.getBoundingClientRect();
        if(r.width===0||r.height===0)return;
        if(r.right>w+1.5||r.left<-1.5)bad.push(el.tagName+'.'+(el.className||'')+'@'+Math.round(r.left)+'~'+Math.round(r.right));
      });
      return bad.slice(0,10);
    }""")
    ck("首页无越界元素", len(over) == 0, over)

    txt = page.evaluate("()=>document.body.innerText")
    for bad in ("undefined", "NaN", "[object Object]"):
        ck(f"首页文本无 {bad}", bad not in txt)
    ck("首页无 JS 错误", len([e for e in js_errors if "pageerror" in e]) == 0,
       [e for e in js_errors if "pageerror" in e])

    ck("四张功能卡齐全", page.locator("#mod-grid .mc").count() == 4,
       page.locator("#mod-grid .mc").count())
    ck("今日速览 4 条", page.locator("#quick-list .qi").count() == 4)
    for kw in ("9 条要闻", "电力设备", "7 只自选", "+41.67%", "1.76%"):
        ck(f"首页含真实数据「{kw}」", kw in txt)
    ck("提醒横幅为监控中（未触发）", "监控中" in txt and "0.7%" in txt and "不弹窗" in txt)
    ck("无底部导航条", page.locator(".tabbar, nav").count() == 0)
    ck("免责声明存在", "不构成任何投资建议" in txt)
    ck("页脚含鸿蒙适配说明", "鸿蒙" in txt and "数据来源" in txt)
    ck("manifest / theme-color 已声明",
       page.locator('link[rel="manifest"]').count() == 1 and
       page.locator('meta[name="theme-color"]').get_attribute("content") == "#0a59f7")
    ck("状态栏时间格式正确", len(page.locator("#sb-time").inner_text()) == 5)
    ck("头部标注最新数据日与模块数", "2026-11-11" in txt and "4 个功能模块" in txt)

    page.screenshot(path=str(DATA / "preview-home.png"))
    # 独立页面展开内部滚动容器，生成完整首页长图（不改变交付页面行为）
    shot = ctx.new_page()
    shot.goto(BASE + "/index.html", wait_until="load", timeout=30000)
    shot.wait_for_timeout(600)
    shot.add_style_tag(content="""
      html,body{height:auto!important;overflow:visible!important}
      #shell{height:auto!important;min-height:100vh;overflow:visible!important}
      #view-home{overflow:visible!important;flex:none!important}
    """)
    shot.wait_for_timeout(250)
    shot.screenshot(path=str(DATA / "preview-home-full.png"), full_page=True)
    shot.close()

    # ================= B. 模块切换 =================
    print("\n--- B. 模块切换 ---")
    page.locator('#mod-grid .mc[data-go="finance-news"]').click()
    page.wait_for_timeout(1200)
    ck("首页隐藏 / 模块视图显示",
       page.locator("#view-home").is_hidden() and page.locator("#view-module").is_visible())
    ck("已创建财经新闻 iframe（内联 srcdoc，无外部 src）",
       page.locator('#stage iframe[data-mod="finance-news"]').count() == 1 and
       page.locator('#stage iframe[data-mod="finance-news"]').get_attribute("src") is None)
    ck("胶囊高亮唯一且为财经新闻",
       page.locator("#chips .chip.on").count() == 1 and
       "财经新闻" in page.locator("#chips .chip.on").inner_text())

    def frame_of(key):
        # 内联 srcdoc 的 frame URL 恒为 about:srcdoc，改用 iframe 的 name 属性定位
        return page.frame(name="wb-" + key)

    fnf = frame_of("finance-news")
    if fnf:
        try:
            fnf.locator(".app").wait_for(state="attached", timeout=10000)
        except Exception:
            pass
    ck("财经新闻 iframe 已加载出内容", fnf is not None and fnf.locator(".app").count() >= 1)
    if fnf:
        ft = fnf.locator("body").inner_text()
        ck("财经新闻 iframe 含 14:00 与盘中分析", "14:00" in ft and "盘中" in ft)
        ck("财经新闻 iframe 无 undefined/NaN", "undefined" not in ft and "NaN" not in ft)
        ck("财经新闻 iframe 无底部导航条", fnf.locator(".tabbar, nav").count() == 0)

    page.screenshot(path=str(DATA / "preview-modules.png"))

    # 依次切换
    expect = [
        ("industry-sectors", "电力设备"),
        ("index-tracking", "指数跟踪"),
        ("bond-funds", "债券基金"),
    ]
    for key, kw in expect:
        page.locator(f'#chips .chip[data-go="{key}"]').click()
        page.wait_for_timeout(1100)
        f = frame_of(key)
        if f:
            try:
                f.locator(".app").wait_for(state="attached", timeout=10000)
            except Exception:
                pass
        ok = f is not None and kw in f.locator("body").inner_text()
        ck(f"切换到「{key}」并加载出内容", ok)
        ck(f"「{key}」胶囊高亮", key in (page.locator("#chips .chip.on").get_attribute("data-go") or ""))

    ck("四个 iframe 均保留在 DOM（状态不销毁）", page.locator("#stage iframe").count() == 4,
       page.locator("#stage iframe").count())

    # 直击发布环境 NoSuchKey：bundle 已内联、iframe 不再请求外部子页面
    bundle_keys = page.evaluate(
        "()=>{try{return Object.keys(JSON.parse(document.getElementById('page-bundle').textContent||'{}'))}"
        "catch(e){return []}}"
    )
    ck("page-bundle 已内联 4 个模块页面", len(bundle_keys) == 4, len(bundle_keys))
    srcdocs = page.evaluate(
        "()=>Array.from(document.querySelectorAll('#stage iframe')).map(f=>({"
        "mod:f.getAttribute('data-mod'),len:(f.srcdoc||'').length,src:f.getAttribute('src')}))"
    )
    ck("四个 iframe 均无外部 src（不会触发 NoSuchKey）",
       len(srcdocs) == 4 and all(x["src"] is None for x in srcdocs),
       [x["mod"] for x in srcdocs if x["src"] is not None])
    ck("四份 srcdoc 均为解码后的真实页面",
       len(srcdocs) == 4 and all(x["len"] > 30000 for x in srcdocs),
       [(x["mod"], x["len"]) for x in srcdocs])

    bf = frame_of("bond-funds")
    if bf:
        try:
            bf.locator("#btn-notify").wait_for(state="attached", timeout=10000)
        except Exception:
            pass
        bt = bf.locator("body").inner_text()
        ck("债券基金 iframe 含自选 005909", "005909" in bt)
        ck("债券基金 iframe 含提醒设置按钮", bf.locator("#btn-notify").count() == 1)
        ck("债券基金 iframe 无 undefined/NaN", "undefined" not in bt and "NaN" not in bt)
        # 验证 iframe 内部交互：打开提醒设置弹层
        bf.locator("#btn-notify").click()
        page.wait_for_timeout(400)
        ck("iframe 内「提醒设置」弹层可打开", bf.locator("#nt-mask.show").count() == 1)
        page.screenshot(path=str(DATA / "preview-notify.png"))
        bf.locator("#nt-close").click()
        page.screenshot(path=str(DATA / "preview-bond.png"))

    # 返回首页
    page.locator("#btn-back").click()
    page.wait_for_timeout(300)
    ck("「‹ 工作台」返回首页", page.locator("#view-home").is_visible() and page.locator("#view-module").is_hidden())

    # 系统返回（popstate）：重新进入模块后 go_back
    page.locator('#quick-list .qi[data-go="finance-news"]').click()
    page.wait_for_timeout(400)
    page.go_back()
    page.wait_for_timeout(500)
    ck("浏览器返回键可回首页（popstate）", page.locator("#view-home").is_visible())

    # URL 参数直达
    page.goto(BASE + "/index.html?m=bond-funds", wait_until="load", timeout=30000)
    page.wait_for_timeout(1000)
    ck("?m=bond-funds 快捷方式直达债券基金",
       page.locator("#view-module").is_visible() and frame_of("bond-funds") is not None)

    # ================= C. PWA =================
    print("\n--- C. PWA ---")
    r = page.request.get(BASE + "/manifest.webmanifest")
    try:
        mf = r.json()
        ck("manifest 可访问且 JSON 有效", r.status == 200 and mf.get("name") == "理财工作台")
        ck("manifest standalone / 竖屏 / 主题色", mf.get("display") == "standalone" and
           mf.get("orientation") == "portrait" and mf.get("theme_color") == "#0a59f7")
        ck("manifest 3 个图标 + 4 个快捷方式",
           len(mf.get("icons", [])) == 3 and len(mf.get("shortcuts", [])) == 4)
    except Exception as e:
        ck("manifest 可访问且 JSON 有效", False, e)

    swreg = page.evaluate("()=>navigator.serviceWorker.getRegistration().then(r=>!!r)")
    ck("Service Worker 注册成功", bool(swreg))
    swtxt = page.request.get(BASE + "/sw.js").text()
    ck("sw.js 含 network-first 与离线清单", "fetch(req)" in swtxt and "ASSETS" in swtxt)
    for icon in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "icon.svg"):
        ck(f"图标 {icon} 可访问", page.request.get(BASE + "/assets/" + icon).status == 200)

    # ================= D. 鸿蒙工程 =================
    print("\n--- D. 鸿蒙工程一致性 ---")
    rf = ROOT / "harmony/entry/src/main/resources/rawfile/web"
    ck("rawfile 首页与上层一致", (rf / "index.html").exists() and
       (rf / "index.html").read_bytes() == (ROOT / "index.html").read_bytes())
    rf_home_txt = (rf / "index.html").read_text("utf-8")
    ck("rawfile 首页已内联 4 个模块（单文件自足）",
       "PAGE_BUNDLE_START" in rf_home_txt and all(f'"{mid}"' in rf_home_txt for mid in MOD_IDS))
    ck("sw.js ASSETS 不再含 pages/ 子页面", "'./pages/" not in (ROOT / "sw.js").read_text("utf-8"))
    ck("首页不再通过 src 加载外部子页面",
       'setAttribute("src", m.page)' not in (ROOT / "index.html").read_text("utf-8"))
    idx_ets = (ROOT / "harmony/entry/src/main/ets/pages/Index.ets").read_text("utf-8")
    for kw in ("$rawfile('web/index.html')", "domStorageAccess(true)", "onBackPress()",
               "harmonyBridge", "javaScriptProxy", "onUrlLoadIntercept"):
        ck(f"Index.ets 含 {kw}", kw in idx_ets)
    ab_ets = (ROOT / "harmony/entry/src/main/ets/entryability/EntryAbility.ets").read_text("utf-8")
    ck("EntryAbility.ets 配置状态栏", "setWindowSystemBarProperties" in ab_ets)
    ck("module.json5 / main_pages.json 存在",
       (ROOT / "harmony/entry/src/main/module.json5").exists() and
       (ROOT / "harmony/entry/src/main/resources/base/profile/main_pages.json").exists())
    ck("两份 README 存在", (ROOT / "README.md").exists() and (ROOT / "harmony/README.md").exists())

    # ================= E. 单文件发布（模拟只上传 index.html）=================
    print("\n--- E. 单文件可用性（目录内无 pages/）---")
    solo = DATA / "_solo"
    solo.mkdir(exist_ok=True)
    (solo / "index.html").write_bytes((ROOT / "index.html").read_bytes())
    fp = page.context.new_page()
    ferr = []
    fp.on("pageerror", lambda e: ferr.append(str(e)))
    fp.goto("file://" + str(solo / "index.html"), wait_until="load", timeout=30000)
    fp.wait_for_timeout(700)
    ck("单文件目录首页可打开", fp.locator(".hero-t h1").count() == 1)
    solo_ok = True
    for i, key in enumerate(MOD_IDS):
        if i == 0:
            fp.locator(f'#mod-grid .mc[data-go="{key}"]').click()
        else:
            fp.locator(f'#chips .chip[data-go="{key}"]').click()
        fp.wait_for_timeout(800)
        fr = fp.frame(name="wb-" + key)
        if fr:
            txt = fr.locator("body").inner_text()
            solo_ok &= (fr.locator(".app").count() >= 1
                        and "模块页面未内联" not in txt and "NoSuchKey" not in txt)
        else:
            solo_ok = False
    ck("单文件目录四个模块全部加载成功（无 NoSuchKey）", solo_ok)
    ck("单文件模式零 JS 错误", not ferr, ferr[:3])
    fp.close()
    for x in solo.iterdir():
        x.unlink()
    solo.rmdir()

    browser.close()

httpd.shutdown()

print("\n--- 汇总 ---")
failed = [c["name"] for c in report["checks"] if not c["ok"]]
print(f"共 {len(report['checks'])} 项，通过 {len(report['checks']) - len(failed)}，失败 {len(failed)}")
if failed:
    print("失败项：")
    for n in failed:
        print("  - " + n)
print("JS 错误：", js_errors[:8] if js_errors else "无")
sys.exit(1 if failed else 0)
