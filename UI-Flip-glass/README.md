# UI-Flip-glass 0.2.0

Voice Input Assistant 专用的玻璃界面适配层，采用 GPL-3.0-only。

2026-10-19 重新实现：保留语音项目实际使用的 CSS 类、玻璃表面增强和提示消息接口。旧版本的音乐搜索、播放器、边缘浮层和 Web Components 注册接口不再提供；语音项目未使用这些接口。此版本不是通用的旧包替代品。

```js
import 'ui-flip-glass/auto';
import { enhanceGlass, showToast } from 'ui-flip-glass';
enhanceGlass(document);
showToast('设置已保存');
```

- `enhanceGlass(root = document)`：增强 root 内带 data-fg-glass 的元素，重复调用不重复添加滤镜；返回 surface 数组。
- `attachGlassSurface(element)`：返回 `{ update(), destroy() }`。销毁会断开尺寸观察并移除滤镜。
- `initFlipGlass(root = document)`：增强当前元素，返回 `{ destroy() }`。
- `showToast(message, duration = 2600)`：以纯文本显示提示，返回 `{ dismiss() }`。不会解释 HTML。
- `createGlassDisplacementMap(width, height, radius)`：来自固定上游提交的玻璃位移图函数。

自动入口只处理初始文档；新增元素需显式调用 enhanceGlass。CSS 使用 fg-shell、fg-card-panel、fg-icon-button、fg-glass-button、fg-toggle 等语音项目类名，主题变量由 webui 提供。不支持 SVG backdrop-filter 时保留 CSS 毛玻璃回退。

来源及修改记录见 [NOTICE.md](NOTICE.md)，上游历史声明见 [UPSTREAM-NOTICE.md](UPSTREAM-NOTICE.md)，许可全文见 [LICENSE](LICENSE)。

验证：`npm run check`、`npm run pack:dry`。原生浏览器演示见 demo/index.html。
