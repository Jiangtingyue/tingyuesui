# JTYHome v8.9.8 Desktop Slim

本包按提供的 `cache-hit-fix.zip` 两个补丁原样应用，不额外改缓存策略。

已精简：
- 合并原多层 CSS 为 `static/css/desktop-bundle.css`，保持原加载顺序。
- 移除 `reader-mode.css`、`dwell-reader.css`、`reader-coherence.css`（手机/阅读模式样式）。
- 移除通用“系统诊断/深度诊断”前端入口与 Dx 快捷按钮；后端运行所需的内部模块未硬删，避免破坏主程序依赖。
- 保留缓存命中/API 用量相关界面与缓存链路。
- 保留 Service Worker，因为桌面 Web Push 仍依赖它；缓存清单已改为精简后的单 CSS。

缓存修复来自提供的补丁：
1. keepalive 保留主请求 thinking shape，并把 keepalive continuity diagnostics 放入独立 lane。
2. embodied prelude 继承主请求 thinking shape，只覆盖输出 token 上限。

启动方式保持原项目不变：Windows 用 `start_jtyhome.bat`，macOS 用 `start_jtyhome.command`，Linux/终端可用 `start_jtyhome.sh`。

## 首次安装
- Windows：双击 `install_windows.bat`
- macOS：双击 `install_mac.command`
- 已装过依赖：继续使用原来的启动脚本即可。
