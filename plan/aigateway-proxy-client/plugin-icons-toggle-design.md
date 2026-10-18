# 一键额度控制与预生成图标

本契约自用户版 0.4.3 / 订阅版 0.1.3 引入；版本号不代表当前运行版本。
有效额度行为同时见 [插件设计](design.md#个人额度与曲线)，历史发布见 [发布索引](release-history.md)。

## 范围

- 用户版“额度控制”保留按钮外观，点击一次直接切换本地开启/关闭，不再弹出选择框。
  不修改 magpie 源码；原生滑动开关、登录面板布局和持续状态标签仍由宿主决定。
- 平台强制 block 时只显示不可关闭说明，不修改本地偏好。保存前再次 fresh 读取平台。
- 一次点击捕获一次目标值，重复 callback 不会反向切回；取消/失败不写偏好。
- 不改变平台预算、APIM、身份、默认订阅、原始统计或其他供应商。
- 两版插件都带平台品牌图标。本次不实现企业图标上传页面、公开上传 API、数据库迁移
  或新的 Azure Storage 资源；预留后端服务接口供后续定制功能调用。

## 宿主图标协议

- auth.icon 优先，缺省读 package.json.magpie.icon；接受 https 或 data:image URI。
- 最大解码大小 1 MiB；没有协议强制的像素边长，本实现约定方形 128x128。
- 插件列表自身也读取 package.json.magpie.icon，因此两个位置都设置同一资源。
- 宿主只有一个图标字段，手动 dark/light 主题不保证向图片传递 color-scheme。
  不能只依靠 SVG prefers-color-scheme，亦不能声称支持 icon.light/icon.dark 字段。
- 默认图标直接派生自 frontend/public/turnstile.svg，保留平台蓝底白色转闸标志，
  在亮/暗主题均可辨认。不发明新 logo，不使用远程图标 URL 或 AI 生成。

## 生成与复用

默认资源：backend/services/client_assets/icons/default/，含 icon.svg、icon-light.svg、
icon-dark.svg 和 manifest.json。清单含 schema/generator 版本、尺寸、源摘要、内容版本、
文件 SHA-256 与主图标 data URI。默认 light/dark 相同是有意的主题安全单槽品牌图。

更新平台默认 SVG 后离线运行，测试检查源与生成资源一致：

```sh
python -m scripts.prepare_plugin_icons
```

API staging 复制完整 backend 树，预生成资源随包发布。两个下载 service 只读取、校验并
原样打包，不运行 Pillow、不缩放、不重新编码图片、不写缓存，也不在缺失时偷偷生成。
无定制 active 时使用内置包；已激活资源损坏返回 503。用户下载先检查资源，再创建 grant。

## 企业上传接口

服务接口：FilePluginIconStore.update_from_upload(light_bytes, dark_bytes=None, brand="platform")。

```python
from pathlib import Path
from backend.services.client_icons import FilePluginIconStore

# 未来处理器先完成管理员授权、企业范围及上传版本校验。
store = FilePluginIconStore(Path("/home/data/turnstile/plugin-icons"))
revision = store.update_from_upload(uploaded_bytes, optional_dark_bytes, brand="platform")
# 生成成功后未来品牌设置可记录 revision；失败保留旧 active。
```

开发/运维入口：

```sh
python -m scripts.prepare_plugin_icons --upload company.png \
  --dark-upload company-dark.png --store /path/to/shared/plugin-icons --brand platform
```

- 静态 PNG/JPEG/WebP，单文件最多 2 MiB、8-2048 像素；拒绝动画、全透明、压缩炸弹、
  无效图像及未经清洗 SVG。后续安全 SVG 栅格化可新增适配器。
- 按比例放入 112x112 内容区，输出 128x128 PNG，清除 EXIF/注释/配置文件元数据。
- 按前景亮度选择中性色底托，不反转企业色彩；可预生成不同 dark 源资源。
- 内容地址目录不可变；候选校验通过后原子更新 active.json。失败保留旧 active；
  重复内容复用同一版本，旧版本留存，不使历史下载包失效。
- CLIENT_PLUGIN_ICON_DIRECTORY 指向持久、实例间共享目录；下载直接读准备好的 active。
  不可指向 App Service 只读 wwwroot/挂载 ZIP，也不可依赖单实例临时缓存。
- brand 由服务器解析，不能从下载请求任意选择企业；当前使用 platform。
  load_plugin_icons(brand) 与生成分离，未来可替换为数据库/Blob 存储适配器。
- 未来异步消费者须校验上传版本，防止旧事件覆盖新图；Blob/数据库应使用 ETag/CAS。
  当前文件适配器提供原子完整发布，不声称实现跨系统事件排序。
- 图标只含公开品牌资料，ZIP 仍含用户/订阅凭据。旧安装不自动换图，重新下载才使用新资源。

## 兼容

两版命名空间和内部身份不变；老 cfg 没有 icon 时继续显示通用图标。现有本地额度偏好
保留。0.4.3 起点击“额度控制”代表一次切换，不再选择 on/off；强制模式不改变用户偏好。

## 验证记录（2026-10-13）

- 全量 Python：1884 passed、7 skipped，1 个既有 Starlette 弃用警告，374.41 秒。
- 定向 API/部署打包测试：38 passed；插件 JS 41 passed；Ruff、Mypy（5 个文件）、
  前端 TypeScript/Vite 构建和 diff 检查通过，保留既有大 chunk 提示。
- 实际 magpie 在隔离 HOME/XDG/端口中安装候选插件，用原生第 4 个方法直接点击式操作，
  没有选择框；audit 默认关闭 200，第一次操作后 429，第二次回到 200。
  平台 block 时只显示不可关闭说明，本地 false 偏好不能解除真实阻断。
- 原生额度历史与图表继续可用；实际 Bun 图片/MCP 超额阻断通过。两次模拟文本调用，
  无真实模型费用，不修改日常 Agent 配置。
- 实际 magpie 插件列表显示平台蓝色转闸图标，图片自然尺寸 128x128，亮/暗及移动视口
  检查通过；暗色测试保持 OS 为亮色，避免将 OS 自动配色误当作宿主手动主题适配。
- 图标 URI 与 ZIP 中预生成文件一致；连续下载不调用生成器、不改变已生成文件 mtime。
  企业上传模拟可发布新版本并读取；重复内容复用、损坏资源 503 且不创建 grant。
- 浏览器插件不在当前技能列表，使用已有 bundled Playwright。先关闭宿主 What's New
  对话框，再验证真实 Installed 页；未伪造 DOM 或修改宿主源码。
- 原生证据：`/var/folders/_n/vxzws45j78148dvlcyxmc7680000gn/T/ts-icon-toggle-native-0ouz7vvo/`。
  测试脚本 `/tmp/ts-icon-toggle-check-20261013/`；临时网关、图片助手、web GUI 和浏览器已停止。
- 本节为发布前的本地开发验收记录，不单独作为线上下载包已更新的证明。
  后续已提交并上线，实际包、原生图标及当前强制预算分支验收见
  [历史发布与恢复索引](release-history.md)。
