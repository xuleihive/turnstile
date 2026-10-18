# AIGateway-Proxy 插件设计

整理日期：2026-10-17。按当前分支源码合并默认订阅、命名空间、可观测性和额度控制的有效契约，
替代早期“未实现用户版”“手选默认订阅”“UUID 文件名”和“只支持独立曲线”等旧设计。
源码版本常量目前为用户版 `0.5.1`、订阅版 `0.2.0`；这不是当前线上包的读回证明。
产品范围见 [需求](requirements.md)，完整预算升级见 [兼容方案](budget-auth-compatibility-upgrade-design.md)。

## 默认订阅与身份

用户版调用链为：个人可撤销授权 -> 平台身份/权限校验 -> 已绑定的真实应用订阅 -> APIM -> 模型。
订阅版直接持有其获准导出的 APIM Key，不冒充用户个人登录。

1. `ClientUserService.default_subscription()` 优先验证显式用户绑定。
2. 没有显式绑定时，复用平台实际调用网关和凭据优先级，以网关配置凭据为先，合法条件下才用 Dashboard 回退。
3. 服务端将实际凭据与有效订阅主/备用 Key 比对，必须唯一匹配；缺失/歧义/无模型时拒绝。
   不按应用名排序，不因为某个应用属于登录用户就选择它，不把部署中的某个订阅名称硬编码成规则。
4. 默认匹配缓存使用凭据 HMAC 指纹隔离，300 秒有效；配置变化使匹配失效，指纹和 Key 不进入用户包。
5. grant 保留 application/subscription/gateway 绑定。刷新模型保持绑定，重新登录才重解默认订阅。

用户 UUID、治理用户标识、浏览器登录方式和 APIM 订阅身份分开。账号展示已验证的姓名，
内部 uid 稳定；本插件仅在宿主锁下更新同 uid 自有槽位，不新增重复账号或恢复已退出账号。
服务端覆盖模型正文中不可信的用户归因，Anthropic 使用适配后的模型侧标识；预算仍用真实治理身份。
会话等追踪头由 `client_telemetry_headers()` 白名单处理，不能提升身份或伪造未提供的会话。

兼容软件的后端可信委托与旧 Token 上下文按已安装协议选择；安装 USD 的 APIM 变更是单独的
预算协议升级，不是为了解决默认订阅而取消订阅鉴权。完整条件见兼容方案。

## 接口与权限

| 接口/入口 | 职责 |
| --- | --- |
| `GET /api/v1/client-downloads/options`、`POST /api/v1/client-downloads/magpie` | 订阅版目录与下载，校验下载动作、应用归属、订阅和网关；下载时才取 Key |
| `GET /api/v1/client-downloads/user-options`、`POST /api/v1/client-downloads/magpie-user` | 用户版目录与下载；当月个人预算未配置则拒绝签发下载授权 |
| `/api/v1/client-auth/device-authorizations`、`verification`、`decision`、`token`、`revoke` | 设备确认、重登录、令牌轮换和撤销；浏览器会话与客户端令牌不互相替代 |
| `GET /api/v1/client-runtime/catalog`、`POST /api/v1/client-runtime/refresh-models` | 当前绑定的个人模型目录与显式刷新 |
| `GET /api/v1/client-runtime/usage` | 个人额度；普通缓存 60 秒，显式控制操作用 `fresh=true` 重读 |
| `/api/v1/client-runtime/image-capabilities`、原生协议代理 | 受限图片凭据与授权后的真实模型转发 |

API 技术路径保留 `magpie` 以兼容旧客户端，用户可见文案统一为“客户端”。
端点采用既有会话/Bearer 与跨站写保护；响应 no-store，错误不回显凭据。
模型、Key、网关 URL 均由平台解析，不接受浏览器任意指定目标。未知或跨网关的密钥回退拒绝。
下载矩阵及 CAS 更新详见 [下载权限](download-permissions-design.md)。

## 安装包、命名与升级

- 用户版 ZIP 为 `aigateway-proxy-user-<用户名>.zip`；订阅版为 `aigateway-proxy-<订阅名>.zip`。
  名称清洗、长度限制和空/UUID 名称回退共用 `download_filename.py`，根目录由同一文件名去除 `.zip` 得到。
- providerId 分别为 `aigateway-user-<名称>-<12位摘要>` 和 `aigateway-subscription-<名称>-<12位摘要>`。
  名称规范化，非 ASCII 技术名采用 Punycode；显示名保留原文。摘要区分平台、种类及稳定身份，不含秘密。
- 模型完整标识使用 providerId 与平台模型 ID；请求到 APIM 时只发送平台模型 ID。
  package.name 保留 ASCII 类型前缀及摘要，内部 UUID 保留于私有配置，不放在下载名称中。
- 首次自动导入遵守宿主账号锁、备份及一次性标记；失败保留手动入口，不覆盖账号或其他供应商。
- 旧包继续使用原 providerId。显式更新命名空间后需重新选择 Agent 模型；不能把新旧目录当作无冲突的双安装。
- 用户 ZIP 包含 `index.mjs`、`helpers.mjs`、`images.mjs`、`native-images.mjs`、`quota.mjs`、
  公共 `pricing.mjs`、配置、README、package.json 及预生成图标。订阅版使用独立入口、
  `image-sync.mjs` 和配置，打包时复用同一响应适配器、图片桥接、原生登记和价格模块。
- 文件权限为私密插件 ZIP 的 0600；平台部署 ZIP 的 0644/0755 是另一套规则，不可混淆。

用户版配置含个人 access/refresh 凭据，不含 APIM Key、密码或浏览器 Cookie；订阅版含获准导出的 Key。
两者都不能公开。下载不触发安装脚本或秘密轮换；尚未实现的凭据加密不能写成安全承诺。

## 模型与价格

- 以有效发布声明的协议选择 SDK/端点，保持原生流式、取消及工具正文；不按模型名猜协议。
- 用户版可刷新已绑定范围的完整授权目录；订阅版发现更新限定于原快照交集，不通过 discovery 扩权。
- 价格复用平台模型计价读取，USD/百万 Token，含输入、输出、缓存读/写；零价与未知价格区分。
- 宿主通用适配器不能承载完整缓存价时，使用已有 `model price` CLI 导入本 provider/model。
  保留人工单模型、供应商通配和全局模型通配价格，不直接重写宿主 settings.json。
- 两版图片目录同样带平台有效 `cost`；只有成功登记且启用的自有图片供应商才同步价格，
  使用独立 `providerId-images/modelId`，不借用文本供应商价格或重命名 Foundry 模型 ID。
  冲突、人工修改或关闭的图片供应商保持不动；未知单价不伪造零价。
  旧插件需替换为新下载文件，刷新目录本身不会升级插件代码；宿主按新价重算已有 Token
  只是本地估算，不改写平台历史账单。
- 定价子进程不启动图片助手、创建账号或携带平台登录凭据；每批有界同步，未完成项通过后续刷新继续。
  原生完整价格导入失败不把缓存当免费；平台真实账单与宿主本地估算不是同一数据源。
- 订阅版增加独立“刷新模型”动作，保留原有导入/更新 Key 的位置；刷新不重新导入 Key，
  不轮换订阅或越过下载快照。成功的空发现及缩小后的交集持久化，断网不能从宽泛旧列表恢复。
  每次文本调用读取当前授权，别名转换复用用户版的精确匹配 JSON/SSE 适配器。

## 个人额度与曲线

只返回本人月度窗口，不返回共享应用、部门或组织窗口；应用统计和硬限制照常执行。
Token/USD 用量不能混算，历史采样按单位/周期分段。个人设置、额度接口和人员列表使用整月个人范围，
按部门筛选的是人员，不重写历史请求归属来强行对齐统计。

有效本地阻断 = 平台强制 block OR 用户显式开启本地阻断。

| 平台模式 | 本地选择 | `aside` | 原生趋势 |
| --- | --- | --- | --- |
| audit | 默认关闭/明确关闭 | true | 不新增原生采样 |
| audit | 明确开启 | false | 宿主真实采样；还需启用宿主走势设置 |
| block | 不可关闭 | false | 宿主真实采样；本地偏好不能解除平台限制 |

“额度控制”复用插件授权动作，一次点击捕获一次切换目标，重复 callback 不反向切回。
保存前 fresh 读取策略；平台改为 block 则拒绝关闭。偏好按内部用户和 provider 私有保存，
损坏/跨用户文件不覆盖。宿主按钮和布局不受插件控制，不能把它称为已实现的原生滑动开关。

`aside=false` 本身不保证指定模型被宿主拒绝；文本 loader 和图片助手还执行本地提前检查。
本地只是辅助，跨客户端并发仍靠平台准入。普通读数和进程缓存至多 60 秒，不增加后台查询循环。

独立“查看额度趋势”保留为补充，复用真实个人快照、去重并限制历史量；不写宿主 quota-history.json，
不补造安装前数据。关闭本地控制不删除宿主旧历史，所以旧曲线可能仍可见。
USD 未初始化、价格未知或策略同步中应明确不可用，不能显示假零或把旧 Token 数当美元。

## 图片、图标和界面

- 图片桥接仅监听 loopback，使用 image-only capability 和本机随机 Key；不向 Agent 下发 APIM Key 或 refresh token。
- 上一条的 image-only capability 属于用户版；订阅版桥接每次从精确自有账号槽读取当前
  APIM Key，仅发送到下载绑定的 HTTPS 网关，CLI、运行时描述和 MCP 配置只包含本机随机 Key。
  订阅版不调用个人 OAuth、个人额度或撤销接口，不制造订阅额度；APIM 继续执行实际准入。
- 原生图片登记通过宿主 CLI，仅管理有所有权证据的本插件供应商；无法安全定位配置目录则保留 MCP 回退。
- 路径、禁用/卸载/注销、同名冲突和手工修改保护见 [图片登记](native-image-registration-design.md)
  与 [跨平台生命周期](windows-image-paths.md)。
- 图标生成与下载分离，默认资源随 API 打包。企业上传的服务接口、共享目录和版本激活见
  [图标设计](plugin-icons-toggle-design.md)，不在下载时临时生成。
- 下载页采用平台现有 UI，不另造风格；权限、独立勾选、加载和错误态详见下载布局设计。
  客户端会话缺失或 Agent 图标关联缺失不能通过插件伪造修复。

订阅版 0.2.0 的逐项对照、可复用能力与不适用的个人能力见
[订阅版对齐记录](subscription-parity.md)。本地对齐不表示已部署或覆盖已安装包。

## 源码与验证入口

| 范围 | 主要位置 |
| --- | --- |
| 用户版授权与目录 | `backend/services/client_user.py`、`client_user_store.py`、`backend/http/client_user.py` |
| 订阅版下载 | `backend/services/client_downloads.py`、`backend/http/client_downloads.py` |
| 名称与图标 | `backend/services/client_namespace.py`、`download_filename.py`、`client_icons.py` |
| 插件运行时 | `backend/services/client_user_plugin/`、`backend/services/client_plugin/` |
| 追踪与权限 | `backend/services/client_telemetry.py`、`client_download_permissions.py` |
| 页面 | `frontend/src/pages/client-downloads-page.tsx`、`client-authorization-page.tsx` |

本地、模拟宿主、实际下载和真实付费调用分别验收，不复用旧版本测试作为当前通过证明。
常规软件包发布与 USD 金融安装分别执行；不再沿用早期“所有改动只发布 API、不涉及迁移或 Core”的统一结论。
恢复依据见 [历史与恢复索引](release-history.md)，最近金融安装结果见 [USD 验收记录](usd-installation.md)，
后续安装与恢复入口见[预算运维手册](operations.md)。
