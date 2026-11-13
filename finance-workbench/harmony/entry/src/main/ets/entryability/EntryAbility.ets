/**
 * 理财工作台 · 应用入口 Ability
 * 配置状态栏 / 导航栏配色，与 H5 的 theme-color 保持一致；
 * 默认不使用全屏沉浸式布局（避免 ArkWeb 内容被状态栏遮挡），
 * 如需沉浸式详见 harmony/README.md「可选：沉浸式」一节。
 */
import { AbilityConstant, ConfigurationConstant, UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import { BusinessError } from '@kit.BasicServicesKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const DOMAIN: number = 0x0000;
const TAG: string = 'FinanceWorkbench';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    try {
      this.context.getApplicationContext()
        .setColorMode(ConfigurationConstant.ColorMode.COLOR_MODE_LIGHT);
    } catch (e) {
      hilog.warn(DOMAIN, TAG, 'setColorMode skipped');
    }
    hilog.info(DOMAIN, TAG, 'EntryAbility onCreate');
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    windowStage.loadContent('pages/Index', (err: BusinessError) => {
      if (err.code) {
        hilog.error(DOMAIN, TAG, 'loadContent failed: %{public}d', err.code);
        return;
      }
      hilog.info(DOMAIN, TAG, 'loadContent succeeded');
    });

    try {
      const mainWindow: window.Window = windowStage.getMainWindowSync();
      // 状态栏品牌蓝 + 白色图标；导航栏浅色，贴合工作台底色
      mainWindow.setWindowSystemBarProperties({
        statusBarColor: '#FF0A59F7',
        statusBarContentColor: '#FFFFFF',
        navigationBarColor: '#FFF4F6F9',
        navigationBarContentColor: '#FF1D2430'
      }, (err: BusinessError) => {
        if (err.code) {
          hilog.warn(DOMAIN, TAG, 'setWindowSystemBarProperties: %{public}d', err.code);
        }
      });
    } catch (e) {
      hilog.warn(DOMAIN, TAG, 'system bar config skipped');
    }
  }

  onWindowStageDestroy(): void {
    hilog.info(DOMAIN, TAG, 'EntryAbility onWindowStageDestroy');
  }

  onForeground(): void {
    hilog.info(DOMAIN, TAG, 'EntryAbility onForeground');
  }

  onBackground(): void {
    hilog.info(DOMAIN, TAG, 'EntryAbility onBackground');
  }

  onDestroy(): void {
    hilog.info(DOMAIN, TAG, 'EntryAbility onDestroy');
  }
}
