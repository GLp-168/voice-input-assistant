# Voice Input Assistant

`voice-input-assistant` 是面向桌面端的离线语音输入助手，包含麦克风录音、FunASR 流式识别、离线复核、词库、历史记录、导出和剪贴板输入。界面采用 PySide6 / Qt WebEngine 与本地 Web Components，也保留 Qt Widgets 兼容界面。

公开源码不包含模型权重、录音、安装包或 vendor 二进制。真实识别需另行准备模型；功能验证范围和授权待办见 [整理报告](docs/PRE_UPLOAD_REVIEW.md) 与 [第三方清单](docs/THIRD_PARTY.md)。

本地目录保留 `zyb_yuyin`，Python 导入名仍为 `voice_input`；发行包及计划仓库名统一为 `voice-input-assistant`，新增同名命令并保留旧 `voice-input` 命令兼容。用户配置目录不迁移。

## 环境要求

- Python 3.10+
- Node.js 20.19+ 或 22.12+（仅开发或重新构建玻璃运行时界面时需要，最终安装包不需要）
- 当前以 Windows 为主要目标；其他平台的安装、运行与输入注入尚需独立验证

## 安装

创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
```

安装基础包：

```powershell
python -m pip install -e .
```

按需安装可选能力：

```powershell
python -m pip install -e ".[audio]"
python -m pip install -e ".[clipboard]"
python -m pip install -e ".[dev]"
```

可选依赖说明：

- `audio`：包含 `sounddevice` 和 `numpy`，用于麦克风采集和音频缓冲处理。
- `clipboard`：包含 `pyperclip`，用于剪贴板式文本输出。
- `tray`：包含 `pystray` 和 `Pillow`，用于系统托盘常驻。
- `dev`：包含 `pytest`，用于本地测试。

基础依赖已包含本地识别所需的 FunASR、ModelScope、Torch、torchaudio、sounddevice 和音频处理组件。首次打包前请先运行 `scripts/download_funasr_models.ps1` 下载内置模型。

## 运行

源码运行入口：

```powershell
python -m voice_input
voice-input
python -m voice_input --legacy-ui
```

运行后会打开桌面窗口。点击“开始”开始录音，点击“结束”停止录音并整理最终文本。单次录制最长 2 小时；默认启用“低延迟流式输出”，录音时会用本地 FunASR Paraformer 流式模型实时刷新转写文本。完整离线包会在录音结束后使用离线 Paraformer + VAD + 标点 + 词库热词做高精度复核；轻量实时包不提供导入音频和结束后复核，并会在首次启动与功能入口给出说明。

### 性能与资源档位

资源档位控制模型的加载、保留和回收时机，不会改变安装包实际包含的模型，也不承诺单靠档位变化就提高识别准确率。当前应用固定使用 `auto`，在启动时自动选择 `constrained`、`balanced` 或 `high_quality`：

| 档位 | 模型生命周期 | 适合场景 | 主要代价 |
| --- | --- | --- | --- |
| `auto`（自动） | 按当前机器能力选择合适策略，并在本次应用会话内保持不变 | 当前默认且唯一的用户模式 | 实际内存占用和等待时间取决于自动选中的策略 |
| `constrained`（节省资源） | 同一阶段只保留必要 worker，使用后尽快回收 | 内存较小或同时运行较多应用 | 后续录音或复核可能需要重新加载模型，等待更明显 |
| `balanced`（均衡） | 优先保证当前录音流程，录音和复核结束后回收 worker | 大多数办公电脑 | 兼顾内存占用，但下一次使用仍可能重新加载 |
| `high_quality`（高质量） | 允许在线和离线两个模型 worker 同时驻留 | 内存充足、连续多次使用 | 常驻内存最高；“双模型驻留”不代表并行推理 |

无论选择哪个档位，程序都会先让在线模型就绪，以缩短开始录音前的等待。离线复核模型在第一次确实需要复核时才加载，因此完整离线包的首次复核可能出现明显等待，具体取决于 CPU、磁盘和录音长度；后续是否需要重新加载则由档位决定。`balanced` 和 `constrained` 会在录音及必要复核完成后回收识别 worker，降低空闲占用；`high_quality` 可以保留两个模型 worker，但在线转写和离线复核仍串行调度，不会同时推理并争抢 CPU 与内存带宽。

`auto` 的选择在一次应用会话内是 sticky 的：机器短时负载变化不会导致录音途中切换档位。重新启动应用后，运行时才会重新评估。

运行时界面由 PySide6 Qt WebEngine 承载，并直接使用仓库内 `UI-Flip-glass` Web Components 包。修改 `webui/` 或 `UI-Flip-glass/` 后执行：

```powershell
.\scripts\build_webui.ps1
```

构建结果会写入 `src/voice_input/web_dist`。如果 Web 资源缺失、WebEngine 窗口构造失败或页面加载失败，程序会自动回退到旧 Qt Widgets 界面；也可以使用 `--legacy-ui` 强制启动旧界面，便于现场排障。

## Windows 产品包

在 Windows 11 构建：

```powershell
.\scripts\build_windows.ps1
.\scripts\build_windows.ps1 -ProductTier enterprise -ModelProfile streaming
.\scripts\build_windows.ps1 -ProductTier enterprise -ModelProfile full
```

`streaming` 是轻量实时 profile，仅打包 Paraformer 流式模型并移除不需要的 FFmpeg 运行时；`full` 是完整离线 profile，包含流式、离线复核、VAD、标点和音频导入能力。`basic` 默认使用 `streaming`，`standard`/`enterprise` 默认使用 `full`，也可以显式覆盖；非默认组合会在产物名中增加 `-streaming` 或 `-full`，避免互相覆盖。

正式 Windows 构建使用 `requirements/windows-build.lock.txt` 中经过验证的精确依赖版本；更新锁文件后应重新执行完整打包验收。构建脚本会在 PyInstaller 前后校验 `web_dist/index.html` 及其引用的 JavaScript、CSS 资源，资源缺失或为空时立即终止。

构建完成后会生成：

- `dist/windows/VoiceInputAssistantSetup/VoiceInputAssistant-v0.1.0-Full-Setup.exe`：完整版安装包，双击安装到当前用户目录并创建桌面/开始菜单快捷方式。文件名包含版本和档位，避免 Windows 沿用旧安装器图标缓存。
- `dist/windows/VoiceInputAssistant-portable.zip`：免安装版，解压后双击 `VoiceInputAssistant.exe`。
- `dist/windows/VoiceInputAssistant-installer.zip`：安装包压缩版，内含带版本号的 `VoiceInputAssistant-v0.1.0-Full-Setup.exe`，便于通过文件系统或即时通讯工具分发。

完整离线包可验证窗口、麦克风实时转写、音频导入和离线识别全流程；轻量实时包用于较小体积的实时录音转写交付，不包含离线复核、VAD、标点和导入音频能力。

Windows 包已包含：

- 全局快捷键，默认 `Ctrl+Shift+F8`
- 关闭窗口时最小化到系统托盘
- 系统诊断入口；完整离线包额外包含结束后离线复核和音频文件识别
- 低延迟中文流式输出，基于内置 FunASR Paraformer streaming 模型
- 实时麦克风电平和设备状态，直接复用送入识别引擎的 PCM 数据，不重复打开录音设备
- 完整离线包提供录音结束后的高精度复核，基于 FunASR 离线 Paraformer + FSMN-VAD + CT-Transformer 标点
- 词库优化会作为 FunASR 原生热词传入复核模型，并继续应用到实时字幕预览和最终文本整理
- 可用 `tools/VoiceInputEvaluate/VoiceInputEvaluate.exe samples.csv --json --output report.json` 对最终识别做 CER 验收；可用 `tools/VoiceInputStreamEvaluate/VoiceInputStreamEvaluate.exe samples.csv --json --output stream-report.json --suggestions-output lexicon-suggestions.csv` 对流式输出、最终复核、热词召回和候选词库做验收。样本列为 `audio,reference,language,hotwords`；候选词库 CSV 需要复核后再导入，单字等高风险候选会默认禁用。
- 完整离线包的音频导入支持 WAV、FLAC、OGG 直接识别，并通过内置 FFmpeg 转码组件兼容 MP3、M4A、AAC、WMA、OPUS、WEBM、MP4、MOV、MKV、AVI、MPEG、MPG、M4V、AMR、3GP、AIFF、AIF、CAF
- Word、PDF、公文 Word 导出
- 内置政务/数字政府、公文规范、办公会议、财税/预算、工程/招投标五套词库，并支持叠加自定义词库
- 运行日志位于用户配置目录
- 本地历史会话、段落与时间段；可在设置文件中配置本地 `funasr_speaker_model` 启用 FunASR 说话人区分，未配置时不会生成虚假标签
- 首次启动提示

日志文件默认位于 `%APPDATA%\voice_input\logs\voice-input.log`。现场排查时建议同时提供该日志、系统诊断报告、目标输入软件名称和复现步骤。

## 客户预览边界

当前 Windows 包可用于客户预览以下内容：

- 安装包安装、桌面快捷方式、开始菜单入口和卸载入口。
- 程序启动、窗口操作、首次启动提示、运行日志。
- Dummy ASR 演示闭环，用于确认界面、热键、托盘和文本展示流程。
- FunASR 模型、词库、音频转码和诊断工具入口。

录制和音频导入边界：

- 单次麦克风录制最长 2 小时。
- 默认启用“低延迟流式输出”后，主录音持续进行，后台按 FunASR 流式 chunk 实时刷新文本；暂停期间不向识别器喂入新音频，结束录制后会用高精度模型整理最终文本。
- 默认流式链路会把已经发送给在线模型的 PCM 同步暂存到临时 WAV，结束录制后整段交给独立离线进程复核；正常完成、取消或失败时都会清理临时文件。旧版“分段转写”仅用于关闭低延迟流式输出后的兼容链路。
- 导入音频文件最长支持 2 小时；长文件识别时间取决于模型、CPU 和音频长度。
- WAV、FLAC、OGG 可直接进入 FunASR；MP3 和其他常见音频/视频容器通过 FFmpeg 转码为 16k 单声道 WAV 后识别。

仍需在客户真实环境验收：

- 目标 Windows 机器上的真实麦克风录音、FunASR 模型兼容性、识别速度和准确率。
- 客户业务系统中的文本注入兼容性。
- 麒麟/龙芯等目标国产系统的安装包、运行时依赖、模型二进制和输入注入适配。
- 现场噪声、静音、权限受限、模型加载失败等异常场景的体验调优。

## FunASR 诊断

命令行诊断：

```powershell
python -m voice_input.diagnose_asr
python -m voice_input.healthcheck --json
```

诊断命令会检查 FunASR 模型、VAD、标点、词库、音频转码组件和一次最小转写调用是否能跑通。

## 脚本

```powershell
.\scripts\bootstrap.ps1
.\scripts\check.ps1
.\scripts\package.ps1
```

- `bootstrap.ps1`：创建 `.venv` 并安装开发依赖。
- `check.ps1`：运行基础导入检查和 pytest，不启动桌面运行时。
- `build_webui.ps1`：安装本地前端依赖，检查 JavaScript，并把 UI-Flip-glass 运行时构建到 Python 包目录。
- `package.ps1`：构建源码包和 wheel。
- `build_windows.ps1`：构建 Windows 可执行程序和安装包 zip。

## 当前 MVP 限制

- 已接入 FunASR 流式 + VAD + 标点 + 热词 + 结束后高精度复核；真实准确率、噪声鲁棒性和首次加载耗时仍需要用客户真机样本验证。
- 已实现 Windows 全局热键和可选系统托盘；其他平台需按系统适配。
- 跨应用输入注入当前采用剪贴板 + 粘贴热键方案，复杂目标软件仍需现场适配。
- 噪声抑制、VAD、标点恢复等体验细节仍需继续增强。
- Windows 已提供单文件安装包和免安装包；其他平台安装包仍需按目标系统补齐。

## 后续方向

1. 验证不同硬件上的识别准确率、延迟与资源占用。
2. 完善跨应用输入注入及非 Windows 平台适配。
3. 核实第三方组件与模型的再分发授权后，再准备公开安装包。

## 公开源码验证

```powershell
python -m pip install -e ".[dev]"
.\scripts\build_webui.ps1
python -m pytest -q
python scripts/validate_web_dist.py src/voice_input/web_dist --require-manifest
python scripts/validate_lexicon_catalog.py --lexicon-dir assets/lexicons
```

自动测试使用模拟音频和临时资源，不能替代真实麦克风、模型推理和跨应用粘贴验收。无须为了运行这些测试下载模型。

`src/voice_input/web_dist` 保留经校验的小型界面产物，以便源码可直接加载界面；模型和 Windows 安装产物不进入提交清单。主项目自有代码已选择 `GPL-3.0-only`，全文见 [LICENSE](LICENSE)。UI-Flip-glass 已重建为来源明确的 GPL 适配层，旧封装不进入发布内容，见 [NOTICE.md](NOTICE.md) 与 [来源核查](docs/UI_FLIP_GLASS_PROVENANCE.md)。
