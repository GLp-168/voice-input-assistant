# 第三方内容清单

本清单依据当前本地源码、元数据与说明整理，不替代许可证审查。

| 内容 | 本地情况 | 本轮处理 |
| --- | --- | --- |
| UI-Flip-glass | 固定 Mineradio GPL-3.0 函数 + 本次新编写适配层 | 旧封装及旧构建产物已移出候选范围，许可与署名齐全，见 UI_FLIP_GLASS_PROVENANCE.md |
| FunASR / ModelScope / Torch | Python 依赖，通过 pyproject 与锁文件声明 | 不复制依赖到仓库；模型通过下载脚本获取 |
| src/funasr 代理入口 | 用于冻结打包的兼容层 | 保留源码供打包脚本使用，从通用 wheel 的 Python 包发现中排除，避免覆盖上游 funasr 包 |
| packaging/windows/funasr_patches | 冻结打包兼容补丁 | 保留并提示发布者核查上游代码来源与声明 |
| PySide6 / Qt WebEngine | 桌面运行时依赖 | 公开安装包前核查对应分发义务；本轮不制作安装包 |
| FFmpeg、whisper.cpp、sherpa-onnx | 本地模型或二进制资源，包含于 vendor 或安装产物 | 整个 vendor 排除源码提交；现有本地文件保留 |
| assets/icons、内置词库 | 项目图标和业务术语 JSON | 保留候选源码；发布者仍需确认素材来源权利 |
| 文档导出字体 | 代码引用系统字体名称 | 不将系统字体文件复制入仓库 |

## 模型获取

`scripts/download_funasr_models.ps1` 列出模型 ID 并从 ModelScope 下载，目标为 `vendor/funasr/models/`。脚本包含流式、离线复核、VAD 和标点模型，不包含可选的说话人模型；默认模型版本未固定为提交 SHA，因此下载结果不保证永久不变。

运行下载脚本前自行确认磁盘、网络和模型授权。模型缺失时干净源码不能直接进行真实 ASR；自动测试使用替身和临时文件验证行为。

本轮没有下载模型、运行真实录音、复制字体或上传任何内容。
