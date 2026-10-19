# Windows 打包说明

## 目标

Windows 版交付物需要做到：用户不需要安装 Python，拿到包后可以直接运行或安装到当前用户目录。

主运行时使用 Qt WebEngine 加载 `UI-Flip-glass` 构建产物。构建机需要 Node.js 20+；客户机器不需要 Node.js。`build_windows.ps1` 会在 PyInstaller 之前自动运行 `scripts/build_webui.ps1`。

## 构建命令

```powershell
.\scripts\build_windows.ps1
```

默认构建 `enterprise` 完整离线包。产品档位控制内置词库与标点模型规格，模型 profile 控制安装包包含的识别能力。需要分别交付不同组合时，使用：

```powershell
.\scripts\build_windows.ps1 -ProductTier basic
.\scripts\build_windows.ps1 -ProductTier standard
.\scripts\build_windows.ps1 -ProductTier enterprise
.\scripts\build_windows.ps1 -ProductTier enterprise -ModelProfile streaming
.\scripts\build_windows.ps1 -ProductTier enterprise -ModelProfile full
```

- `-ModelProfile streaming`：轻量实时包，只包含 Paraformer 流式模型，支持麦克风实时转写；不包含离线复核、VAD、标点、音频导入模型和 FFmpeg。
- `-ModelProfile full`：完整离线包，包含流式、独立离线复核、VAD、标点与 FFmpeg，支持导入音频和结束后整段复核。
- 未指定时，`basic` 默认使用 `streaming`，`standard`/`enterprise` 默认使用 `full`。也可通过 `VOICE_INPUT_MODEL_PROFILE` 设置。

脚本会把档位写入 `assets/product.json`，运行时设置界面不展示“产品档位”，只展示该安装包实际允许的内置词库。
`enterprise/full` 沿用默认产物名；`basic`/`standard` 会保留档位后缀，显式选择非默认模型 profile 时还会增加 `-streaming` 或 `-full`，避免连续打包互相覆盖。例如企业轻量包为 `VoiceInputAssistant-streaming-portable.zip`。

脚本会自动安装/使用以下构建依赖：

- `pyinstaller`
- `sounddevice`
- `numpy`
- `pystray`
- `Pillow`
- `PySide6 QtWebEngine / QtWebChannel`

Qt WebEngine 会显著增加基础运行时体积。打包脚本会删除 WebEngine 调试资源，并仅保留简体中文、繁体中文和英文 locale；不要再删除 `Qt6Network.dll`、`Qt6OpenGL.dll`、`opengl32sw.dll`、`Qt6Qml*.dll`、`Qt6Quick*.dll`、`qml/`、`QtWebEngineProcess.exe` 或 WebEngine `.pak` 资源。

## 产物

- `dist/windows/VoiceInputAssistantSetup/VoiceInputAssistant-v0.1.0-Full-Setup.exe`
  - 单文件安装包。
  - 双击后安装到当前用户目录，不需要管理员权限。
  - 安装位置：`%LOCALAPPDATA%\Programs\VoiceInputAssistant`。
  - 会创建桌面快捷方式、开始菜单快捷方式和卸载入口。
  - 推荐作为客户 Windows 预览交付包。

- `dist/windows/VoiceInputAssistant-portable.zip`
  - 免安装版。
  - 解压后运行 `VoiceInputAssistant.exe`。
  - 诊断工具位于 `tools/VoiceInputDiagnose/VoiceInputDiagnose.exe`。

- `dist/windows/VoiceInputAssistant-installer.zip`
  - 安装包压缩版。
  - 内含 `VoiceInputAssistant-v0.1.0-Full-Setup.exe`，用于需要 zip 分发的场景。
  - 轻量实时包对应 `VoiceInputAssistant-v0.1.0-Streaming-Setup.exe`。安装器文件名包含版本和档位，用于避免 Windows Explorer 沿用旧版同名 EXE 的图标缓存。

## 真实 ASR 配置

轻量实时包只内置 FunASR Paraformer 流式模型，适合优先压缩首次交付体积；完整离线包额外内置离线高精度复核模型、FSMN-VAD 和 CT-Transformer 标点模型，安装后即可执行完整本地识别链路。

当前产品默认不向用户暴露模型切换入口；不同档位或不同模型组合应通过不同安装包交付。

## 已包含的产品能力

- Windows 全局快捷键，默认 `Ctrl+Shift+F8`。
- 关闭窗口时默认最小化到托盘，可在设置中关闭。
- Word、PDF、公文 Word 导出；完整离线包额外支持分段转写和音频文件识别。
- 内置政务/数字政府、公文规范、办公会议、财税/预算、工程/招投标五套词库，可在“设置 > 词库 > 管理词库”中启用或叠加自定义词库。
- FunASR 流式识别、词库热词、系统诊断入口和流式准确率评测工具；完整离线包额外支持结束后高精度复核和离线准确率评测。
- 运行日志，便于现场排查。
- 首次启动提示会明确显示当前是轻量实时包还是完整离线包；完整 profile 资源损坏时会引导运行系统诊断。

日志默认位置：

```powershell
%APPDATA%\voice_input\logs\voice-input.log
```

现场反馈问题时建议同时收集：日志文件、系统诊断报告、目标输入软件名称、样本音频和复现步骤。

也可以用诊断工具先测：

```powershell
.\tools\VoiceInputDiagnose\VoiceInputDiagnose.exe
.\tools\VoiceInputHealth\VoiceInputHealth.exe --json --output "%APPDATA%\voice_input\health-report.json" --open
.\tools\VoiceInputEvaluate\VoiceInputEvaluate.exe samples.csv --json --output report.json
.\tools\VoiceInputStreamEvaluate\VoiceInputStreamEvaluate.exe samples.csv --json --output stream-report.json --suggestions-output lexicon-suggestions.csv
```

安装版诊断工具位于：

```powershell
%LOCALAPPDATA%\Programs\VoiceInputAssistant\tools\VoiceInputDiagnose\VoiceInputDiagnose.exe
```

免安装版诊断工具位于解压目录下：

```powershell
.\tools\VoiceInputDiagnose\VoiceInputDiagnose.exe
```

诊断工具返回 `engine=funasr` 和 `text=...` 表示后端最小调用已跑通；返回非 0 退出码表示 FunASR 模型、音频转码、麦克风权限或运行时依赖仍需现场处理。

## 安装包自检

安装包支持无污染检查：

```powershell
$setup = (Resolve-Path .\dist\windows\VoiceInputAssistantSetup\VoiceInputAssistant-v0.1.0-Full-Setup.exe).Path
Start-Process -FilePath $setup -ArgumentList @("--check") -Wait -PassThru -WindowStyle Hidden
Start-Process -FilePath $setup -ArgumentList @("--extract-only", (Resolve-Path .\tmp).Path + "\extract-check") -Wait -PassThru -WindowStyle Hidden
```

`--extract-only` 只解压 payload，不创建快捷方式、不写注册表。

## 说明

当前 Windows 安装包采用自研 PyInstaller bootstrap，不依赖 Inno Setup、NSIS 或管理员权限。后续如果需要更标准的企业安装向导，可以再接入 Inno Setup 或 NSIS。

## 开发者扩展词库

内置词库现在由 `assets/lexicons/catalog.json` 驱动，不再需要在运行时代码里硬编码每个词库。新增客户或行业词库时：

1. 将词库文件放入 `assets/lexicons/`，支持 `.json`、`.csv`、`.txt`，也可以放在子目录中，例如 `assets/lexicons/clients/city_government.csv`。
2. 在 `assets/lexicons/catalog.json` 的 `lexicons` 数组中追加一项，填写 `id`、`title`、`filename`、`default_enabled`、`min_tier` 和 `tags`。
3. 如果该安装包默认启用全部 `default_enabled=true` 的词库，直接运行打包命令即可。
4. 如果要给某个客户只启用指定词库，使用：

```powershell
.\scripts\build_windows.ps1 -ProductTier enterprise -EnabledLexicons "government_digital,official_document,client_government"
```

也可以使用环境变量 `VOICE_INPUT_ENABLED_LEXICONS`、`VOICE_INPUT_LEXICON_DIR`、`VOICE_INPUT_LEXICON_CATALOG` 接入外部词库目录或外部清单：

```powershell
$env:VOICE_INPUT_LEXICON_DIR = "D:\client-pack\lexicons"
$env:VOICE_INPUT_LEXICON_CATALOG = "D:\client-pack\lexicons\catalog.json"
$env:VOICE_INPUT_ENABLED_LEXICONS = "client_government,client_finance"
.\scripts\build_windows.ps1 -ProductTier enterprise
```

`catalog.json` 示例：

```json
{
  "schema_version": 1,
  "lexicons": [
    {
      "id": "client_government",
      "title": "某市政务专属",
      "description": "面向某市政府单位的专有术语和常见误识别纠错。",
      "filename": "clients/client_government.csv",
      "default_enabled": true,
      "min_tier": "enterprise",
      "tags": ["custom", "government"]
    }
  ]
}
```

打包脚本会把清单和被启用的词库复制进安装包，并写入 `assets/product.json`。安装器自检会按包内清单校验词库文件是否完整，用户界面只展示当前安装包实际包含的词库，不展示产品档位。

打包前建议先校验词库包：

```powershell
python .\scripts\validate_lexicon_catalog.py --lexicon-dir .\assets\lexicons --product-tier enterprise
python .\scripts\validate_lexicon_catalog.py --lexicon-dir .\assets\lexicons --product-tier enterprise --enabled "government_digital,client_government" --json
```

校验工具会检查 catalog 结构、词库文件是否存在、词库规则是否能解析、当前档位是否允许启用，并输出每个词库的规则数、有效规则数和热词数。
