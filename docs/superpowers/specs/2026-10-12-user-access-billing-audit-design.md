# Turnstile 用户、APIM Key、审计与报表设计（评审稿）

状态：待老师评审。此文档明确一期边界；通过评审后再拆实施计划和修改功能代码。

## 一期目标

在单企业部署中，让管理员管理用户和部门，让用户在 Turnstile 中创建和管理自己的模型调用 Key，并让管理员代用户创建、轮换和导出 Key；调用量仍由 APIM 计量。提供按时间范围生成的分析报告与详细 CSV，并留下不含请求正文和密钥的安全审计记录。角色先支持 `owner`、`member`，权限以角色和权限映射实现，为以后添加审计员等角色留出空间。

当前工作区已经有一组未提交的报表导出修改（ZIP 分析报告和 CSV、时间范围、数据库迭代读取）。实施时会在这组改动上继续改进，不清理、不覆盖它们。现有导出会在大数据量下同步完成，因此需要升级为异步导出任务。

## 已由老师确认的决定

- 一期先实现 `owner`、`member`，并预留后续角色扩展。
- 一期只有一个企业租户，数据结构为未来多租户保留租户边界。
- Key 由 Turnstile 页面管理，但实际是 APIM Subscription；创建、轮换和管理都通过 Turnstile 操作。
- APIM Key 也可直接调用 Turnstile 的模型调用入口。
- 不保存请求正文；报表按时间范围导出“分析摘要 + 明细 CSV”。

## APIM Key 方案比较

| 方案 | 优点 | 代价/风险 | 结论 |
| --- | --- | --- | --- |
| Turnstile 调用 APIM 管理接口创建 Subscription，由 APIM 生成 Key；直连调用时把入站 Key 转发给实际 APIM，由 APIM 再校验和计量 | APIM 是唯一签发与校验权威；Turnstile 不存第二份明文或摘要；沿用 APIM 原有应用策略、预算和用量账 | Turnstile 是一层代理，调用多一次网络跳转；APIM 不可用时代理调用失败 | 采用，符合“Key 在 Turnstile 管理、实际由 APIM 创建”和“Key 可直连 Turnstile” |
| Turnstile 在本地保存 APIM Key 摘要并独立校验 | 可在转发前识别 Key 对应的用户/应用 | 要同步创建、轮换及 Azure 门户外部变更；形成第二份 Key 索引，可能与 APIM 状态漂移 | 不采用；本地身份用 APIM Subscription 计量归属，不用 Key 摘要维护 |
| 强制客户端直接调用 APIM，不提供 Turnstile 直连入口 | 调用路径最短、边界简单 | 不满足老师希望通过 Turnstile 入口调用的需求 | 不采用 |

## 代码事实与调用路径

- 登录账户保存在 PostgreSQL 的 `app_user`，目前角色被限制为 `owner/member`，并已有密码登录和 Entra 登录。
- Turnstile 已有 APIM Application/Subscription 的创建、密钥揭示、轮换和使用量关联能力；创建 APIM Subscription 的 API 目前只允许 Owner。当前新建流程先在 Turnstile 生成 primary/secondary 值再写入 APIM；一期将调整为由 APIM 生成实际 Key，避免把本地生成的秘密当作第二个 Key 权威。
- Turnstile 的模型调用入口是 `/api/v1/model-gateway/invoke`。生产环境调用解析出的 APIM 模型路由；若请求没有 APIM 凭据，服务会回退到服务器侧 dashboard subscription key。
- APIM Key 管理使用 Azure 管理接口 `listSecrets` 和 regenerate 操作。每个 APIM Subscription 可有 primary/secondary 两把可轮换密钥。
- 模型调用上的身份元数据会被写成发往 APIM 的 `x-user-id`、组织/部门等头；报表与 APIM usage ledger 以请求元数据、APIM Subscription 和数据库中的 application attribution 关联。
- 现有报表 ZIP 在响应开始前生成完整文件；数据量大时会占用请求线程、临时磁盘并受 App Service 请求时限影响。

## 身份与角色

1. 将角色和权限变成数据库目录：一期只种入 `owner`、`member` 两个内置角色。内置角色不可删除；一期不开放自定义角色编辑器，但所有 API 授权经权限依赖/策略检查，不再把业务权限散落为 `role == "owner"` 判断。
2. Owner 可创建、启用/停用用户，设置姓名、登录邮箱、部门和角色；Member 可查看自己的资料、用量和 Key。停用用户立即撤销其会话及用户自助管理权限。
3. 不开放匿名注册。沿用现有 Entra 域限制和自动开户；Owner 也可预先创建用户记录并分配角色。密码账号的初始凭证由管理员安全交付，界面不记录或审计密码。
4. 单企业一期以一个根组织和可维护的部门树承载用户归属。用户、APIM application、密钥、审计事件和导出任务都带租户/组织归属字段；一期只允许一个有效租户，暂不做跨租户登录和租户管理员。
5. 用户角色只影响 Turnstile 管理面权限。模型调用的授权和配额继续由 APIM Subscription、APIM policy、现有用户/模型策略和预算共同约束。

### 一期权限矩阵

| 能力 | Owner | Member |
| --- | --- | --- |
| 查看全企业目录、用量和审计 | 是 | 否，仅本人/本人应用 |
| 创建、停用用户，分配部门和角色 | 是 | 否 |
| 查看/调整全体用户的 APIM application 和 Subscription | 是 | 否，仅自己的 |
| 创建、揭示、轮换、停用自己的 APIM Key | 是 | 是 |
| 代用户创建、揭示、轮换、导出 APIM Key | 是 | 否 |
| 创建和下载报表 | 全企业 | 本人/本人应用范围 |
| 编辑内置角色权限、增删角色 | 一期不开放 | 否 |

## APIM Key 与调用认证

1. 不实现第二套 Turnstile 原生 Key，也不在 Turnstile 数据库长期保存 APIM Key 明文或摘要。Turnstile 通过 APIM 管理接口创建 Subscription，让 APIM 生成 primary/secondary Key；如果当前管理 API 版本要求显式生成，则创建资源后调用 APIM regenerate 操作。APIM Subscription 是唯一 Key 的签发和校验来源；Turnstile 是它的管理界面。
2. 用户从“我的 API Key”创建 APIM Subscription。Turnstile 将 Subscription、APIM Product、application、用户归属关联起来；完成 APIM 的异步配置后，用户通过受权限保护的揭示接口从 APIM `listSecrets` 临时取得 Key。密钥仅在揭示/导出响应中返回，响应使用 `Cache-Control: no-store`；创建任务响应、日志和审计记录绝不携带密钥值。
3. Member 只能管理本人归属的 Subscription；Owner 可代用户操作。密钥轮换由 Turnstile 调用现有 APIM Management API；任一时刻的有效性以 APIM 返回结果为准。primary/secondary 支持先轮换备用 Key、客户端切换后再轮换另一把的零停机流程。
4. 对 `/api/v1/model-gateway/invoke` 的 APIM Key 调用，Turnstile 从请求头读取 `Ocp-Apim-Subscription-Key`，把该 Key 作为本次出站 APIM 模型请求的凭据转发；APIM 再校验 Key、执行 Product/API policy、预算/模型准入并记录真实 Subscription 用量。此路径不把客户端提交的 `user_id` 当作可信身份：去掉客户端自报的用户身份元数据，用户/应用费用归属以 APIM Subscription 与 Turnstile application 映射为准；业务用途、项目等可选标签只能作为未验证的筛选元数据。
5. 浏览器管理会话的推理检查仍可使用当前 dashboard 凭据与现有本地用户/模型策略。调用入口区分会话认证和 APIM Key 转发路径，避免 APIM Key 路径误用客户端 metadata 作为身份。
6. 上述直连是“客户端直接请求 Turnstile，再由 Turnstile 转发 APIM”，不是绕过 APIM 直接连模型提供方。若出站 APIM 不可用，请求失败；这保留了 APIM 的鉴权、策略与计量。接口只接受受支持的 APIM Key header，不把 Key 放到 URL/query string。

## 用户和 APIM application 的归属

- 为用户到 Turnstile application 建立显式关系，避免继续依赖自由文本 `owner_id` 推断身份。
- 用户首次创建 Key 时，由 Turnstile 在 APIM 中创建属于该用户的 application/subscription 关联；同一用户的多个 Key 可汇总到一个用户 application，统一预算和用量归属。
- Owner 可为指定用户创建相同类型的 Key，批量创建后的导出文件包含 Key、用户、邮箱、角色、APIM Subscription ID、创建时间等字段。该导出端点仅 Owner 可用，`no-store`，不落盘 Key 明文；再次导出时只在明确的 Owner 操作中通过 APIM `listSecrets` 临时读取。
- 组织、部门作为应用和用量筛选维度。变更归属时保留已有请求的历史快照，不回写旧用量记录。

## 报表导出

1. 页面提供起止时间和现有组织、部门、项目、用户、模型、状态筛选；时间区间采用 `[from, to)`。
2. 请求先创建 PostgreSQL `export_job` 记录并返回 Job ID。后台 Function 定时领取任务（数据库 lease/`SKIP LOCKED`，支持重试），从 PostgreSQL 按游标读取数据，生成 `analysis.md` 与 `requests.csv`，写入私有 Blob container。
3. 下载通过需要会话授权的 API 从私有 Blob 流式读取，不使用匿名 Blob、不把长期 SAS 暴露给浏览器。导出文件和任务默认保留 7 天，过期后自动删除；任务状态包括 queued/running/ready/failed/expired，页面可查看进度和重试失败任务。
4. 汇总与 CSV 只包含现有数据库中记录的元数据：请求/关联 ID、时间、组织与用户归属、模型/Runtime、token 分项、状态、延迟、估算价格/费用、预算与准入信息。请求正文、模型回复、Authorization、Cookie 和 API Key 一律不导出。
5. CSV 对以 `= + - @` 开头的字符串做公式注入保护；时间范围、最大导出跨度、并发任务数和行数上限配置化，防止导出任务耗尽数据库。
6. 成本注明为模型目录价格计算的估算值，不等同 Azure 实际账单。分析摘要继续显示数据区间、趋势、成功/错误率、延迟分位数和组织/部门/项目/用户/模型分布。

## 安全审计

- 新增独立追加式安全事件记录：事件 ID、UTC 时间、租户、操作者 ID/邮箱/角色快照、动作、目标类型和 ID、成功/失败、请求/关联 ID、源 IP、User-Agent，以及经过白名单过滤的 before/after 元数据。
- 至少记录登录成功/失败、登出、用户新增/停用/角色/部门变更、Key 创建/揭示/轮换/停用/导出、权限拒绝、审计/报表导出和组织变更。高频模型调用仍用 APIM usage ledger 记录请求 ID、订阅归属、模型、Token、状态与延迟，避免同一调用在两处重复记账。
- 永不记录密码、会话 Cookie、Bearer/API Key、请求/响应正文或原始凭证；记录密钥事件时只写 APIM Subscription ID 与 Key 类型。
- 审计事件在 UI/API 中不可编辑或删除；默认在线保留 365 天，按时间查询，Owner 可导出。管理面管理事件的 IP 从 Azure ingress 可信头中提取；只信任部署入口注入的转发头，不信任任意客户端自报身份头。
- 这会补上应用级审计证据，但不单独构成“等保三级通过”。正式定级/测评还需结合组织制度、人员、边界防护、主机与网络日志、备份、密钥管理、渗透测试和测评机构要求；当前 Azure Monitor/Log Analytics 30 天保留也需纳入整体保留策略评估。

## 页面与 API 范围

- `用户管理`：用户列表、搜索、启停、部门和角色分配、用户详情及关联应用/Key。
- `角色与权限`：一期展示内置 Owner/Member 的权限说明，不允许删除/编辑；权限表由后端策略驱动，后续增设审计员或自定义角色时不改用户表结构。
- `我的 API Key`：创建 APIM Subscription、揭示/轮换/停用自己的 Key；显示创建状态、APIM ID、最近更新时间及一次性密钥结果。
- `审计记录`：Owner 按时间、用户、动作、资源筛选并导出；Member 不开放全局审计。
- `分析报告`：日期范围、筛选条件、异步任务状态、分析摘要和 CSV ZIP 下载。
- 所有写操作记录安全审计事件，并使用后端权限检查；前端隐藏按钮只改善体验，不承担安全边界。

## 兼容、迁移和回滚

- 保留既有 `owner/member` 用户语义，迁移时将其映射到两个内置角色；现有 Owner 会话不丢失。
- 现有 APIM applications/subscriptions 保留；首次同步补齐用户归属时，对无法确定的旧记录标为“未分配”，由 Owner 在 UI 中认领，不猜测归属。
- 当前同步 ZIP 导出 API 可在 UI 切换到异步 Job 后短期保留兼容，后续再移除。
- 先增加新表/列与兼容 API，再逐步切换 UI；回滚时可以恢复旧 UI 路径。任何 Key 轮换仍以 APIM 为准，不从数据库恢复密钥。
- 需为数据库迁移、APIM 用户 Key 创建/轮换、Member 越权、直连 Turnstile 到 APIM 的同 Key 转发、审计脱敏、异步导出重试/过期编写集成测试。

## 风险和验证重点

1. 只有 APIM 能验证 APIM Key。Turnstile 直连入口必须把密钥原样交给配置中的 APIM gateway 验证，不能用客户端 `user_id` 代替验证；应验证无效 Key 返回 401 且 APIM usage 不被记为有效调用。
2. APIM application 的数据库归属必须与实际 APIM Subscription ID 一致；通过创建和轮换后的 APIM usage telemetry 验证用户/应用费用归属。
3. 旧 Subscription 的 Owner 归属可能不完整；不能在同步时猜测，应提供显式认领和审计记录。
4. 密钥揭示/导出 API 会读取 Azure 管理平面秘密，RBAC 继续遵循最小权限，只让 Turnstile 管理身份调用 `listSecrets` 和 rotate 操作。
5. 等保三级保留时限按一期 365 天实现；正式要求仍以适用标准版本和测评机构的确认意见为准。

## 评审后实施顺序

1. RBAC/用户/部门数据模型与 Owner/Member 管理 API、审计事件基础表。
2. APIM application 用户归属、自助/管理员创建及 Key 权限；扩展 `/model-gateway/invoke` 为 APIM Key 透传到 APIM，同时维持会话推理路径。
3. 安全审计全覆盖与审计 UI。
4. 后台导出 Job、私有 Blob 存储、任务状态/下载 API、报表页面。
5. 前端路由和 Owner/Member 权限矩阵打通，运行迁移、后端/前端/契约和 Azure 模板测试。
