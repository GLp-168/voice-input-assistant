# UI-Flip-glass 重建与来源核查

日期：2026-10-19。当前版本：0.2.0，GPL-3.0-only。旧封装来源无法证实，已按维护者要求替换，不再作为待发布代码。

## 来源边界

- 唯一保留的上游实现：`src/mineradio-map.js` 中的 generateControlGlassDisplacementMap，来自 [Mineradio 固定提交](https://github.com/XxHuberrr/Mineradio-paused/blob/6b130103f759e5dcd1e133700071c8216b8fa5a6/public/index.html)，Copyright (C) 2026 XxHuberrr。
- 函数正文与该提交逐字一致，仅新增 export 和许可头，已自动比对。上游 [README](https://github.com/XxHuberrr/Mineradio-paused/blob/6b130103f759e5dcd1e133700071c8216b8fa5a6/README.md) 和 [LICENSE](https://github.com/XxHuberrr/Mineradio-paused/blob/6b130103f759e5dcd1e133700071c8216b8fa5a6/LICENSE) 明确 GPL-3.0。
- index.js、auto.js、index.d.ts、style.css、demo/index.html 为本轮在维护者指示下由 Codex 辅助新编写。新封装依据当前应用实际使用的接口和布局；并非声称未接触旧代码的净室开发。
- 旧包包含的搜索、播放器、边缘浮层及 Web Components API 均不再实现，应用未使用它们。保留语音界面需要的玻璃、提示、基础 CSS。
- 完整 GPL v3 标准 LICENSE、上游原始 NOTICE、署名和修改记录随源码与 wheel 保存；未知封装旧文件及旧编译资源仅在被忽略的本地验证目录留作历史对照。

## 旧来源调查（历史）

旧副本与本地 Mineradio 未跟踪目录的四份 src 文件相同，但 git log --all 无提交记录，上游该提交完整目录树不含 UI-Flip-glass。公开搜索、npm 注册表及现有本机会话记录未找到独立封装作者证明，维护者也记不清。这些证据不足以给旧封装补授许可证，故采用替换方案。

## 当前源文件 SHA-256

| 文件 | SHA-256 |
| --- | --- |
| `src/auto.js` | `540fccdfce3967f15d5d43e2df716c9ee13f6102c7478256461b6cec45641d54` |
| `src/index.d.ts` | `f41ab487f7bc0df939f54c3d21939a9a7fa6b9422b5f46d38102b6c40542ebfc` |
| `src/index.js` | `e03ae9d904633fb4663e53ed631f309901ce099b6f35e14cf963343aee676e5e` |
| `src/mineradio-map.js` | `7ce643f67e47f61fe22bfbf832b67ef3c4d1a6ade797d5574c489ae47e9da45e` |
| `src/style.css` | `73a4d1124625d2b1ef976fa60d0d1f9b25438f8d814c78eab9e592f6245af2b6` |

## 验证

- Web 依赖安装、语法检查、Vite 构建及 integrity.json 重新生成通过。
- 现有 Python 测试：396 passed（56.16 秒）。
- 1280×800 的重建前后快照：42 个匹配的原页面 fg 元素位置和尺寸一致，截图已人工对照；背景细节与动画不保证逐像素相同。
- Qt WebEngine 实际加载：WebChannel 握手、转写文本、录音指令分发、设置修改保存、两个玻璃表面及 1000×680 布局通过。
- 提示消息中的 HTML 作为纯文本呈现，没有创建 img 元素。
- Qt offscreen 环境有 GPU 上下文告警；不替代真实桌面的 GPU 性能验收，未启用麦克风、模型识别、系统热键或跨应用粘贴。
- 全新 147 文件源码副本构建 sdist 和 wheel 成功；npm dry-run 为 0.2.0。三种包的许可/来源文件齐全，wheel 界面文件与本轮构建逐字节相同，sdist 不含本地备份或 node_modules；未创建仓库或上传。

其他依赖、模型及素材仍适用各自条款；本次解决的是 UI 封装来源问题，不是全面法律审计。

## 公开仓库复核更正（2026-10-19）

此前仅比对了本地 LICENSE 与上游副本的一致性，未发现上游文本缺少 GPL 第 5、6 节部分段落。现已将根和组件 LICENSE 更正为 SPDX 的完整 GPL-3.0-only 标准文本；上游原文件通过固定提交链接追溯，原始 NOTICE 和作者署名继续保留。此次不修改源代码、构建资源或所选许可版本。
