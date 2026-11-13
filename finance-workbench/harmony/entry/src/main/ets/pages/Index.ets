/**
 * 理财工作台 · 鸿蒙原生壳首页
 *
 * 用 ArkWeb（HarmonyOS Web 组件）加载 rawfile 里的自包含 Web 产物：
 *   $rawfile('web/index.html') —— 首页工作台
 *   web/pages/*.html           —— 四个功能模块（由 iframe 相对路径加载）
 *
 * 关键点：
 *  - domStorageAccess(true)：保留自选清单 / 买入记录 / 提醒设置的 localStorage
 *  - javaScriptProxy：H5 主动上报「是否在首页」，用于返回键分级处理
 *  - onBackPress：模块页先回工作台，首页再交给系统
 *  - 外链拦截：交给系统浏览器，避免跳出后丢失工作台上下文
 *
 * 目标 SDK：HarmonyOS NEXT / API 12+（DevEco Studio 5.x）
 */
import { webview } from '@kit.ArkWeb';
import { common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const DOMAIN: number = 0x0000;
const TAG: string = 'FinanceWorkbench';

/** 注入到 H5 的 window.harmonyBridge */
class HarmonyBridge {
  onRoute: (atHome: boolean) => void = (_v: boolean) => {};

  setAtHome(atHome: boolean): void {
    this.onRoute(atHome);
  }

  log(msg: string): void {
    hilog.info(DOMAIN, TAG, 'H5: %{public}s', msg);
  }
}

@Entry
@Component
struct Index {
  private controller: webview.WebviewController = new webview.WebviewController();
  private bridge: HarmonyBridge = new HarmonyBridge();
  @State private atHome: boolean = true;
  @State private pageReady: boolean = false;

  aboutToAppear(): void {
    this.bridge.onRoute = (v: boolean): void => {
      this.atHome = v;
    };
    // 开发期可开启 ArkWeb 调试；上架前关闭
    try {
      webview.WebviewController.setWebDebuggingAccess(false);
    } catch (e) {
      hilog.warn(DOMAIN, TAG, 'setWebDebuggingAccess unavailable');
    }
  }

  /** 返回键：模块页 → 回工作台；首页 → 交给系统默认行为 */
  onBackPress(): boolean | undefined {
    if (!this.atHome) {
      this.controller.runJavaScript('window.__workbenchBack ? window.__workbenchBack() : false')
        .catch((err: BusinessError) => {
          hilog.warn(DOMAIN, TAG, 'runJavaScript failed: %{public}d', err.code);
        });
      return true;
    }
    return false;
  }

  build() {
    Stack({ alignContent: Alignment.TopStart }) {
      Web({
        src: $rawfile('web/index.html'),
        controller: this.controller
      })
        .domStorageAccess(true)
        .javaScriptAccess(true)
        .fileAccess(true)
        .imageAccess(true)
        .onlineImageAccess(false)
        .zoomAccess(false)
        .backgroundColor('#FFF4F6F9')
        .width('100%')
        .height('100%')
        .javaScriptProxy({
          object: this.bridge,
          name: 'harmonyBridge',
          methodList: ['setAtHome', 'log'],
          controller: this.controller
        })
        .onPageEnd(() => {
          this.pageReady = true;
          hilog.info(DOMAIN, TAG, 'workbench page ready');
        })
        .onErrorReceive(() => {
          hilog.error(DOMAIN, TAG, 'web resource load error');
        })
        .onUrlLoadIntercept((event): boolean => {
          if (!event.data) {
            return false;
          }
          const url: string = event.data.getRequestUrl();
          if (url.startsWith('resource://') || url.startsWith('file://') ||
              url.startsWith('about:') || url.startsWith('data:')) {
            return false;   // 本地页面放行
          }
          // 外部链接交给系统浏览器，不在 Web 组件里跳出工作台
          try {
            const ctx = getContext(this) as common.UIAbilityContext;
            ctx.startAbility({
              action: 'ohos.want.action.viewData',
              entities: ['entity.system.default'],
              uri: url
            });
          } catch (e) {
            hilog.warn(DOMAIN, TAG, 'open external link failed');
          }
          return true;      // 拦截组件内部跳转
        });

      if (!this.pageReady) {
        Column() {
          Progress({ value: 0, type: ProgressType.Ring })
            .width(46).height(46).color('#FF0A59F7')
          Text('理财工作台')
            .fontSize(15).fontWeight(FontWeight.Medium)
            .fontColor('#FF586273').margin({ top: 14 })
          Text('正在加载本地页面…')
            .fontSize(11).fontColor('#FF9099A8').margin({ top: 5 })
        }
        .width('100%').height('100%')
        .justifyContent(FlexAlign.Center)
        .backgroundColor('#FFF4F6F9')
      }
    }
    .width('100%')
    .height('100%')
  }
}
