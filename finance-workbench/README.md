# 理财工作台 · 鸿蒙 / HarmonyOS 适用整合版

把此前四个独立模块整合成**一个 App**：

| 模块 | 内容 | 数据日期 |
|---|---|---|
| 财经新闻 | 官方要闻 / 解析·影响·措施 / 盘中研判（BOLL + 成交量 + 机构复盘） | 2026-11-09 |
| 行业板块 | 季度营收·涨幅·量能·热度 TOP5 / 利好·爆雷 / 机会分析 | 2026-11-10 |
| 指数跟踪 | 7 只自选指数基金 / 收益与技术依据 / 编辑增删排序 / 买入明细 | 2026-11-10 |
| 债券基金 | 全场外债基 TOP5 / 自选 005909 / 晴雨表 / 买入跌幅手机弹窗提醒 | 2026-11-11 |

> 四个模块**保持原页面零改动**（自包含 HTML，数据内联注入），工作台用一个鸿蒙风格 App 壳 +
> 顶部胶囊切换 + iframe 装载；互相之间 CSS/JS 完全隔离，`localStorage` 键名不冲突。
> 各模块按自己的 14:00 任务独立更新，**卡片上标注的数据日期即真实日期**，不伪装成同一天。
>
> **单文件发布**：四个模块页面已以 base64(UTF-8) 内联进 `index.html`，iframe 改用 `srcdoc` 装载，
> 运行时**不再请求 `pages/*.html`**。在线发布 / 对象存储预览 / 资料库在线预览时
> **只需上传 `index.html` 这一个文件**，不会再出现 `NoSuchKey` / 404 /「点进去加载不出来」。

## 目录

```
finance-workbench/
├── index.html                  # 单文件 App：首页 + 四模块内联页面（发布只需这一个文件）
├── manifest.webmanifest        # PWA 清单：standalone 全屏 / 图标 / 4 个快捷方式
├── sw.js                       # Service Worker：离线缓存（HTML network-first）
├── start.sh                    # 一键本机预览服务
├── assets/                     # 应用图标（SVG + 192/512/maskable PNG）
├── pages/                      # 四模块页面源副本（构建时内联进 index.html，运行时不依赖）
│   ├── finance-news.html  industry-sectors.html
│   └── index-tracking.html bond-funds.html
├── dist/                       # 在线应用发布目录（单文件 index.html + PWA 资源，构建自动同步）
├── data/workbench-summary.json # 首页真实摘要（可回验）
├── scripts/
│   ├── build_workbench.py      # 同步 → 抽摘要 → 注入摘要 → 内联四模块 → 同步鸿蒙/dist → 刷缓存版本
│   ├── daily_update.py         # 每工作日 14:00 一键更新（采集→质量校验→构建→发布）
│   ├── verify_workbench.py     # 69 项校验（含单文件发布 / 无 pages 目录模拟）
│   └── make_icons.py           # 生成应用图标（纯几何，无字体依赖）
└── harmony/                    # 鸿蒙原生 ArkTS 工程骨架（DevEco Studio 可打开）
```

## 四种使用方式

> **在线发布请先读这一段**：把工作台发布成网页 / 上传到对象存储 / 放进资料库在线预览时，
> **只上传 `index.html` 这一个文件即可**。旧版把四个模块放在 `pages/*.html` 用 iframe `src` 引用，
> 在线环境往往只保留主文件，点模块就会报 `NoSuchKey: The specified key does not exist.`（加载不出来）。
> 现已改为四份页面 base64 内联、iframe 用 `srcdoc` 装载，运行时零子文件请求，从根上消除该问题。
> **若线上仍是旧版：重新执行 `python3 scripts/build_workbench.py` 后覆盖上传 / 重新发布即可。**

### ① 直接双击 `index.html`（最快看到效果）

`file://` 下四个模块与首页切换全部可用，无需任何服务与配置，**也不需要 `pages/` 目录**。
局限：浏览器不允许 `file://` 注册 Service Worker，所以**没有离线缓存与"添加到桌面"**；
但四个模块页面已内联打包进这一个文件，断网照样能打开。

### ② 本机 / 局域网预览（完整 PWA）

```bash
cd finance-workbench
chmod +x start.sh && ./start.sh 8080
# 浏览器打开 http://localhost:8080/
```

`localhost` 属于安全上下文，Service Worker 会注册，页面资源被离线缓存。
鸿蒙平板 / 手机与电脑同一局域网时，可用终端里打印的 `http://<本机IP>:8080/` 直接打开。

> **如实说明**：PWA「添加到桌面 / 安装」要求 **HTTPS 或 localhost**。
> 用局域网 `http://<IP>` 打开能看、能用，但浏览器不会提供安装入口，SW 也不会注册。
> 要在鸿蒙真机得到可安装的全屏 App，有两条路：给本机服务套一层 HTTPS（内网证书 / 反向代理），
> 或者走方式 ③ 的原生 .hap（**推荐，离线、无需证书、有桌面图标**）。

### ③ 鸿蒙真机原生 .hap（推荐）

`harmony/` 是一个可被 **DevEco Studio 5.x（HarmonyOS NEXT / API 12+）** 打开的 ArkTS 工程：

1. DevEco Studio → `File > Open` 选择 `finance-workbench/harmony/`
2. `File > Project Structure > Signing Configs` 开启**自动签名**（需登录华为账号）
3. 连接鸿蒙真机 / 启动模拟器 → `Run` 或 `Build > Build Hap(s)`
4. 桌面出现「理财工作台」图标，打开即用 ArkWeb 加载本地 rawfile 页面

`Index.ets` 已配置 `domStorageAccess(true)`（保存自选清单、买入记录、提醒设置）、
系统级返回键分级（模块页先回工作台）、外链交给系统浏览器、状态栏品牌蓝配色。

> 本沙箱是 Ubuntu，**没有鸿蒙 SDK，无法编译出 .hap**；这里交付的是可直接导入 IDE 的完整工程骨架与
> 已同步好的 rawfile 资源，不会谎报"已打包完成"。详见 `harmony/README.md`。

### ④ 发布成在线应用

`dist/` 是发布专用目录，只含单文件 `index.html` 与 PWA 资源（图标 / manifest / sw.js / 摘要），
**不含** `pages/`、`scripts/*.py`、`harmony/` 源码。构建脚本每次都会自动同步该目录；
发布时用 `--language static` 指向 `dist/` 即可（避免目录里的 `.py` / `.ts` 干扰运行时探测）。
分享链接由发布平台生成，重复发布会覆盖同一链接。

> 实测：线上分享链接是 `dist/` 目录的**实时服务**，因此 `build_workbench.py` 一执行，
> 线上内容立即随之更新，无需再次发布；`publish.js` 用于建立 / 确认这条服务。
> 也正因如此，`daily_update.py --dry-run` 会在结束前完整还原 `dist/`，确保演练不改线上。

## 每天 14:00 更新链路

**一键入口（定时任务使用这个）**：

```bash
cd /workspace/finance-workbench && python3 scripts/daily_update.py
# 演练：加 --dry-run，只更新与校验、完整还原 dist、绝不改线上
```

脚本依次执行四模块采集 → 生成 → 工作台构建 → 产物校验 → 发布，并带三层保护：
采集失败或关键数据降级的模块**自动沿用上一版有效页面**；构建/校验失败则不发布；
全部模块都无有效新数据时跳过发布；日志在 `data/daily-update.log`。
（当前沙箱出口对东方财富 `push2` / `push2his` 行情域名偶发不通，盘前尤其明显，
该保护可避免把「大盘 0/0」「晴雨 —/10」这类残缺数据发上线。）

四个模块数据源仍为东方财富 / 天天基金 / 央行 / 财政部 / 国务院官网等，逐条 trace 可回验。
各模块生成器产出新页面后，回到工作台跑**一条命令**即可全量联动：

```bash
# 各模块按自身任务更新（示例，均为真实脚本）
cd /workspace/finance-news      && python3 scripts/generate_briefing.py
cd /workspace/industry-sectors  && python3 scripts/generate_briefing.py
cd /workspace/index-tracking    && python3 scripts/generate_briefing.py
cd /workspace/bond-funds        && python3 sources/fetch_bonds.py && \
                                  python3 sources/notify_popup.py && \
                                  python3 scripts/generate_briefing.py

# 工作台整合：同步页面 → 重建摘要 → 注入摘要 → 把四页面内联进 index.html → 同步鸿蒙 → 刷缓存版本
cd /workspace/finance-workbench && python3 scripts/build_workbench.py

# 构建后 index.html 即完整单文件产物，dist/ 同步刷新；线上重新发布时覆盖这一个文件即可
```

定时任务**尚未创建**（保持与此前一致），命令链路保留即可；如需 Cron：`0 14 * * 1-5` / `Asia/Shanghai`。
加 `--no-sync` 可只重建摘要、不同步页面。

## 已实现的鸿蒙适配

- **HarmonyOS Design 风格**：品牌蓝渐变头、圆角卡片、胶囊切换、无底部导航条（沿用此前明确要求）
- **沉浸式状态栏**：`viewport-fit=cover` + `env(safe-area-inset-*)`；PWA `standalone` 时隐藏模拟状态栏，
  原生壳由 `EntryAbility.ets` 设置状态栏配色
- **离线**：四个模块页面与数据全部内联在 `index.html`，Service Worker 缓存首页 / 图标 / 摘要
  （不再预缓存 `pages/` 子页面，且逐条容错，缺资源不影响其余缓存）；HTML 采用 network-first，
  14:00 重新生成后联网打开必拿最新版，断网回退缓存
- **桌面图标与快捷方式**：manifest 提供 4 个模块直达入口（`?m=<id>`）
- **竖屏优先**、禁用缩放、`HarmonyOS Sans` 字体优先、`theme-color` 与状态栏同色
- **红涨绿跌**（A 股惯例），橙红仅用于风险/提醒，功能色与涨跌色分离

## 免责声明

⚠ 本工作台由 AI 基于公开行情与官方信息整理生成，仅供参考，不构成任何投资建议或具体产品推荐。
基金为场外产品，估值与最终收益以基金公司公布净值为准；技术分析基于历史价格，不预示未来。
投资有风险，决策需谨慎。
