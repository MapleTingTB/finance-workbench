# 鸿蒙原生壳 · ArkTS 工程说明

这是「理财工作台」的 **HarmonyOS NEXT 原生应用工程骨架**，用 ArkWeb（`Web` 组件）
加载随应用打包的本地 Web 产物，得到一个有桌面图标、可离线、可上架的 `.hap`。

## 目标环境

| 项 | 值 |
|---|---|
| IDE | DevEco Studio 5.x |
| SDK | HarmonyOS NEXT / API 12（`compatibleSdkVersion: 5.0.0(12)`） |
| 语言 | ArkTS（Stage 模型） |
| 设备 | phone / tablet / 2in1，竖屏 |
| bundleName | `com.workbuddy.financeworkbench`（可改成自己的） |

## 导入与运行

1. `File > Open` 选择本 `harmony/` 目录；IDE 提示安装依赖 / Sync 时按提示执行
2. `File > Project Structure > Signing Configs` → 勾选 **Automatically generate signature**（需华为账号），
   `build-profile.json5` 里 `signingConfigs` 留空即由 IDE 生成
3. 连接鸿蒙真机（开启开发者模式 / USB 调试）或启动模拟器
4. `Run 'entry'`，或 `Build > Build Hap(s)> Build Hap(s)` 产出 `.hap`

> 本交付环境为 Ubuntu，**不含鸿蒙 SDK，无法在此编译 .hap**。工程文件、资源与 rawfile 页面均已备齐，
> 需在 DevEco Studio 中完成签名与构建，此处不谎报已打包。

## 关键实现

| 文件 | 作用 |
|---|---|
| `entry/src/main/ets/pages/Index.ets` | ArkWeb 加载 `$rawfile('web/index.html')`；`domStorageAccess(true)` 保留 localStorage；`javaScriptProxy` 注入 `window.harmonyBridge`；`onBackPress` 返回键分级；外链交系统浏览器 |
| `entry/src/main/ets/entryability/EntryAbility.ets` | 状态栏品牌蓝 `#0A59F7` + 白图标、导航栏浅色，与 H5 `theme-color` 一致 |
| `entry/src/main/module.json5` | 桌面图标、竖屏、`INTERNET` 权限（仅为将来在线更新预留） |
| `entry/src/main/resources/rawfile/web/` | **Web 产物**：由上层 `scripts/build_workbench.py` 自动同步，勿手工编辑 |

### rawfile 同步

上层任一模块 14:00 更新后，跑一次即可刷新原生壳内容：

```bash
cd /workspace/finance-workbench && python3 scripts/build_workbench.py
```

会同步 `index.html`、`manifest.webmanifest`、`pages/*.html`、`assets/*`、`data/*.json`
到 `rawfile/web/`。其中 **`index.html` 已把四个模块页面 base64 内联**（iframe 用 `srcdoc` 装载），
原生壳运行时**实际只需加载这一个文件**，`pages/` 副本仅用于单独查看与溯源，不再被请求。
原生壳**不加载 Service Worker**（`file://` / `resource://` 非安全上下文），
但页面本身已离线自带，无需 SW；H5 的 `registerSW()` 会检测协议并自动跳过，不会报错。

### 返回键分级

- H5 在 `openModule()` / `goHome()` 里调用 `window.harmonyBridge.setAtHome(bool)` 上报路由
- 模块页按返回键 → `onBackPress` 执行 `window.__workbenchBack()` 回到工作台
- 首页按返回键 → 交系统默认行为
- 浏览器 / PWA 环境下没有 `harmonyBridge`，H5 侧已做 `try/catch` 降级，仍可用 `popstate` 返回

### 本地存储

四个模块在 `resource://` 同源下共享 localStorage，但键名互不相同：

- `idxtracker.v1`（指数跟踪自选与交易明细）
- `bondfunds.v1`（债券基金自选、买入记录、提醒设置）
- `bondfunds.popup.<date>`（当日弹窗去重）

因此互不覆盖。若开启混淆 / 更换 bundleName，注意数据不迁移属预期行为。

## 可选：沉浸式状态栏

当前 `EntryAbility.ets` **未**启用 `setWindowLayoutFullScreen(true)`，
让 ArkWeb 内容自然排在状态栏下方，最稳妥、不会被遮挡。若要做全屏沉浸式，需要同时处理 H5 顶部安全区：

1. 在 `onWindowStageCreate` 里启用：
   ```ts
   mainWindow.setWindowLayoutFullScreen(true);
   const avoid = mainWindow.getWindowAvoidArea(window.AvoidAreaType.TYPE_SYSTEM);
   const topPx: number = avoid.topRect.height;   // 状态栏高度（px）
   ```
2. 把 `topPx` 通过 `AppStorage` 或 `javaScriptProxy` 传给 H5，在 `onPageEnd` 注入：
   ```ts
   this.controller.runJavaScript(
     `document.documentElement.style.setProperty('--safe-top', '${px2vp(topPx)}px')`
   );
   ```
3. H5 的 `#shell` 在原生壳下读取该变量做 `padding-top`（当前 `standalone` 媒体查询在
   ArkWeb 里不生效，不能只依赖 `env(safe-area-inset-top)`）。

## 常见问题

- **页面空白**：确认 `rawfile/web/index.html` 存在（跑一次构建脚本），并确认 `src: $rawfile('web/index.html')` 路径正确
- **模块打不开**：四模块页面已内联进 `index.html`，**不再依赖 `pages/` 目录**。
  若弹窗提示「模块页面未内联」，说明 `rawfile/web/index.html` 是旧版（未含 `PAGE_BUNDLE_START` 块），
  重跑一次上层 `scripts/build_workbench.py` 再编译即可；这也是在线发布 `NoSuchKey` 的同源修复
- **自选保存不了**：检查 `.domStorageAccess(true)` 是否保留
- **编译报 SDK / hvigor 版本**：`build-profile.json5` 与 `oh-package.json5` 的版本号按本机 DevEco 实际版本调整，
  或用 IDE 的 `Sync Project` 自动补全
- **想改成在线更新**：把 `Web` 的 `src` 换成自己的 HTTPS 地址，并在 `module.json5` 保留 `INTERNET` 权限
