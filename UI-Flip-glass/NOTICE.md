# 来源、许可与修改记录

本目录当前版本 0.2.0 按 GPL-3.0-only 提供，全文见 LICENSE。

## 上游代码

Copyright (C) 2026 XxHuberrr.

`src/mineradio-map.js` 中的 generateControlGlassDisplacementMap 函数逐字提取自 Mineradio 提交 `6b130103f759e5dcd1e133700071c8216b8fa5a6` 的 [public/index.html](https://github.com/XxHuberrr/Mineradio-paused/blob/6b130103f759e5dcd1e133700071c8216b8fa5a6/public/index.html)，仅添加 ES module export 和文件头。上游 [README](https://github.com/XxHuberrr/Mineradio-paused/blob/6b130103f759e5dcd1e133700071c8216b8fa5a6/README.md) 声明 GPL-3.0；[LICENSE](https://github.com/XxHuberrr/Mineradio-paused/blob/6b130103f759e5dcd1e133700071c8216b8fa5a6/LICENSE) 随本目录保留。

UPSTREAM-NOTICE.md 原样保留上游 NOTICE，包括对 emily 的致谢。它是历史来源记录，其中音乐服务和依赖清单不代表本组件使用了这些服务或全部依赖。Mineradio 名称、MR Logo 和原作者署名不归本项目所有；本组件不代表上游官方产品。

## 新实现

2026-10-19，应 Voice Input Assistant 维护者要求，由 Codex 辅助重新编写 src/index.js、auto.js、index.d.ts、style.css 和 demo/index.html，维护者已选择 GPL-3.0。新适配层围绕当前应用调用接口与页面布局编写，不继续携带来源未知的旧封装实现。package.json 与 README 同步更新。

旧封装仅在被忽略的本地 .verification/ui-rewrite-original 留作对照，不属于发布内容。新旧实现的视觉对照不等同于从未接触旧代码的净室开发；此处如实记录采用的固定上游代码和本次新编写部分。

完整溯源与验证见 ../docs/UI_FLIP_GLASS_PROVENANCE.md。
