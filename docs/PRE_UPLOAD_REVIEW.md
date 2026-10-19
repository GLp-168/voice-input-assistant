# Voice Input Assistant 整理与验证结果

日期：2026-10-19。计划公开仓库名：`voice-input-assistant`。

## 已完成

- Python 发行包改名为 voice-input-assistant，Web 包改名为 voice-input-assistant-web；更新锁文件元数据与页面标题。
- 新增 voice-input-assistant 命令，保留 voice-input 命令和 voice_input 导入名、现有用户配置位置。本地目录仍为 zyb_yuyin。
- 更新 README 中过时的骨架描述，说明真实功能、模型获取、运行环境、测试和授权边界。
- 根忽略规则排除 vendor 模型与二进制、安装包、依赖、日志、录音、环境配置、缓存和临时验证目录，本地原件保留。
- 重新构建 Web UI 并生成 integrity.json，解决已有界面产物缺少完整性清单的问题。
- 补齐 sdist 中的本地 UI 组件、图标、冻结打包代理及 Web 完整性清单。
- 通用 wheel 只安装 voice_input 包，避免本地 funasr 代理覆盖上游依赖；冻结打包脚本仍保留显式使用代理的路径。
- wheel 包含内置词库与图标，并支持安装后的资源定位；增加两项回归测试。
- check.ps1 使用新发行包名，并在导入检查或测试失败时明确退出失败。
- 没有初始化 Git、提交、创建远程仓库或上传。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 整理前自动测试 | 374 通过、20 失败；失败原因均为 Web integrity.json 缺失 |
| 干净源码副本自动测试 | 396 通过，无 vendor 模型、原依赖目录或本地缓存混入 |
| Web npm ci、语法检查与 Vite 构建 | 通过 |
| UI-Flip-glass JavaScript 语法检查 | 通过 |
| Web 完整性清单校验 | 通过 |
| 内置词库校验 | 5 套词库、664 条有效规则、169 个热词，通过 |
| 修改的 Python 文件 Ruff 检查 | 通过 |
| 从干净副本构建 sdist，再构建 wheel | 成功，发行名为 voice_input_assistant-0.1.0 |
| 全新虚拟环境安装 wheel（--no-deps） | 成功；发行元数据、两个命令入口、5 套词库、图标及未覆盖 funasr 均验证通过 |
| 实际 Qt WebEngine 冒烟验证 | 页面加载、WebChannel 握手、模拟转写文本显示通过，并保存截图 |

测试环境：Windows、Python 3.12、Node.js 24、PySide6 6.11.1。完整测试复用了本机已有 Python 运行依赖；“干净”指模型与本地数据均未混入源码副本，不表示从空环境重新安装了全部 Torch/FunASR 依赖。

Qt offscreen 验证出现 GPU 上下文初始化告警，但页面完成实际加载与通信，没有触发旧界面回退；此结果不能替代用户桌面 GPU 性能验收。

## 本轮未验证

- 没有启用真实麦克风、真实模型推理、跨应用粘贴或修改用户启动项。
- 没有制作或安装新的 Windows 安装包，也未验证非 Windows 平台。
- 没有进行完整依赖漏洞审计或模型识别准确率评估。
- 未重新下载模型或验证所有模型下载地址的当前可用性。

## 候选内容检查

候选文件中未发现大于 10 MiB 的单文件或常见私钥/令牌特征；提交范围不含模型、二进制安装包、录音、数据库和环境配置。该检查不替代人工隐私与授权审查。

小型 Web 构建产物保留在 src/voice_input/web_dist，以便源码直接加载界面；所有生成测试产物、截图和打包文件保留在被忽略的 .verification 下。

## 公开上传前待办

1. 已按维护者选择将主项目自有代码改为 GPL-3.0-only，补齐许可证与打包规则。
2. UI-Flip-glass 0.2.0 已重新实现并固定 GPL 上游函数，原封装来源缺口通过替换解决。构建产物已重新生成；见 UI_FLIP_GLASS_PROVENANCE.md。
3. 发布安装包时另外核查模型、Qt、FFmpeg 等第三方分发条件；当前只准备源码候选。

UI 组件来源问题已通过替换解决。本报告不代表全部第三方资产和安装包的全面审查；其他依赖分发条件见 THIRD_PARTY.md 与 NOTICE.md。本轮不上传。

## UI 封装重建补充（2026-10-19）

UI-Flip-glass 更新为 0.2.0，保留固定 GPL 上游位移图函数，重写应用所需封装和样式。396 项测试通过；实际 WebChannel 与设置保存验证通过，42 个主页面元素布局尺寸与原版一致。全新源码包、wheel、npm dry-run 许可检查通过。详见 UI_FLIP_GLASS_PROVENANCE.md。真实麦克风、模型识别与 GPU 性能不在本轮验证范围。
