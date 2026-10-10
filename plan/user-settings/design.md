# 用户设置功能设计

| 项目 | 内容 |
| --- | --- |
| 编写日期 | 2026-10-09 |
| 状态 | 已实现/上线，`9cc17fa`共享包保留并复验；上游PR #32 Open，待合并 |
| 最新核对 | 2026-10-11 / 源码 `ee27527`；最初main合并与历史独立候选见第10节 |
| 初始开发基线 | `712a1c9` |
| 上游提交基线 | `4e93935`；已独立整理，排除助手功能提交 |
| 需求依据 | [需求分析](requirements.md) |
| 视觉依据 | [图 1：用户菜单](assets/reference-user-menu.png)、[图 2：模块规范](assets/reference-budget-layout.png) |

## 1. 总体方案

新增平台级 `user-settings` 页面，复用应用壳、认证上下文和 FinOps 视觉语言。前端负责表单及只读展示；HTTP 层限定当前会话身份；账号写入由 `AuthStore` 完成；个人治理与统计由 web-only 聚合服务读取现有核心仓储。

```text
sidebar-account
  -> 用户设置 -> ?source=<保留原来源>&page=user-settings
  -> UserSettingsPage
       -> AuthProvider：基本资料、登录方式、头像
       -> 本人账户接口：资料更新、本地头像读取/更新、密码修改
       -> 本人只读接口：归属、预算、模型、统计
            -> UserSettingsService（backend/services）
            -> AuthStore / QueryRepository / 现有预算与模型规则
```

关键约束：

- 不使用 `OwnerSession` 限定个人设置；所有已认证 Owner/Member 均可访问本人。
- 密码/名称/头像写入还须验证当前会话为 `password`，不能信任请求中的登录方式；本地头像读取也限定 password 会话。
- 不让前端拉取完整预算/目录/注册表/组织统计后筛出本人；服务端投影后再返回。
- 不增加 `turnstile_core -> backend` 依赖；web-only 编排放在 `backend`，可复用领域逻辑放在 core。
- 本期不写 APIM 策略、不修改 Microsoft OAuth scopes、不改变推理链路。

## 2. 当前实现定位

以下路径为实现时的核查与修改入口，链接指向本仓库，不代表必须整文件重构。

| 文件 | 已有职责 | 本功能接入方式 |
| --- | --- | --- |
| [app.tsx](../../frontend/src/app.tsx) | 账号菜单、Page 枚举、URL 导航、页面渲染 | 增加全局页及菜单项；避免双重标题栏 |
| [apim/source.tsx](../../frontend/src/data-sources/apim/source.tsx) / [github-copilot/source.tsx](../../frontend/src/data-sources/github-copilot/source.tsx) | 数据源页面归一化和预取 | 全局页面在数据源归一化前处理，不触发对应数据源首页请求 |
| [auth-provider.tsx](../../frontend/src/providers/auth-provider.tsx) | 资料、头像、过期/退出、缓存清理 | 增加本人资料刷新、名称/头像保存后的上下文更新，按登录方式隔离头像来源 |
| [api/auth.ts](../../frontend/src/api/auth.ts) / [api/client.ts](../../frontend/src/api/client.ts) | 认证请求、API 错误及过期事件 | 增加个人 API；保留状态码，区别改密失败与会话过期 |
| [authentication.py](../../backend/http/authentication.py) / [session.py](../../backend/http/session.py) | 登录、资料、会话鉴权、写来源校验 | 复用依赖，新接口使用服务端身份 |
| [auth_store.py](../../turnstile_core/persistence/auth_store.py) | PostgreSQL 账号/会话 | 增加限定本人名称/头像读写、原子密码更新与全会话撤销 |
| [auth_service.py](../../backend/services/auth_service.py) | scrypt 密码与 token 工具 | 复用 `hash_password` / `verify_password`，不自建密码算法 |
| [enterprise.py](../../turnstile_core/domain/enterprise.py) | 目录及合并规则 | 复用归属解析，不建立新的目录数据源 |
| [budget_service.py](../../backend/services/budget_service.py) | 月预算、预警和预测 | 抽取/复用本人预算的计算规则，不调用整份 overview 后返回原响应 |
| [repository_budgets.py](../../turnstile_core/persistence/repository_budgets.py) | 预算证据、额度、人员模型策略 | 增加本人限定读取，保留现有证据选择与入账口径 |
| [runtime_service.py](../../backend/services/runtime_service.py) | 路由、模型/预算准入 | 复用或小范围提取纯授权判断，避免只读查询发起实际调用 |
| [repository.py](../../turnstile_core/persistence/repository.py) | APIM usage/trends/distribution | 服务端构造固定 `UsageFilters(user_id=...)` |
| [budget-page.tsx](../../frontend/src/data-sources/apim/pages/budget-page.tsx) / [finops.css](../../frontend/src/styles/finops.css) | 参考图 2 对应页面和样式 | 复用指标、表格、进度条语义，不复用管理操作 |
| [applications-page.tsx](../../frontend/src/pages/applications-page.tsx) / [application_access.py](../../turnstile_core/domain/application_access.py) | 应用头像压缩、base64/格式/64 KiB 校验 | 参考或小范围提取通用图片处理，不复用应用 Owner 权限或应用 ID |

## 3. 页面与视觉设计

### 3.1 布局

保留侧栏和内容区边界，页面标题为“用户设置”。不增加营销头图，不把全部模块包在一个大卡片中。表单、指标带和章节使用全宽平面布局；表格和图表可保留工具型边框，边框内不再嵌套卡片。

```text
现有侧栏 | 用户设置                                  [刷新图标]
         | ---------------------------------------------------
         | 个人资料
         | [头像] 显示名称 / 账号名称             [登录方式标签]
         | 密码会话：头像处 [相机图标] [移除图标，仅已有头像]
         |           [名称输入] [取消] [保存] [修改密码]
         | Microsoft 会话：头像及名称只读，不显示上述修改命令
         | ---------------------------------------------------
         | 账号信息
         | 平台角色 Owner/Member    所属组织          所属部门
         | ---------------------------------------------------
         | [预算周期 月份选择器] [APIM] [执行模式只读标签]
         | [月度预算 | 已使用 | 剩余 | 月底预测 | 使用率]
         | 本人预算：实际数字、细进度条、正常/预警/超额状态
         | ---------------------------------------------------
         | 可用模型                              [搜索] [数量]
         | 名称 | 模型标识 | 提供方 | 上下文 | 授权来源 | 状态
         | ---------------------------------------------------
         | 使用统计
         | 请求数 / 总 Token / 错误率 / 费用估算
         | [用量趋势图，Token/费用，柱图/折线图]
         | 模型使用汇总表
```

账号信息、模型授权是“当前状态”；月份选择器只控制预算和使用统计，不回放历史角色、归属或模型授权。

### 3.2 图 2 规范映射

| 图 2 元素 | 本页应用 | 设计约束 |
| --- | --- | --- |
| 紧凑标题区 | “用户设置”及刷新图标 | 沿用 56px 工作区标题、14px 标题字；刷新按钮有 tooltip 和 aria-label |
| 月份工具条 | 预算周期及执行模式 | 复用 `MonthPicker`；工具条可换行，不能挤压正文 |
| 横向五项 KPI | 预算、已使用、剩余、预测、使用率 | 一个指标带及内部细分隔线，不使用五个漂浮卡片 |
| 章节标题 | 个人资料、账号信息、可用模型、使用统计 | 13-17px 层级，不使用巨型标题；章节之间 16-24px 间距 |
| 范围与层级 | 本人所属组织/部门 | 紧凑只读属性行，不展示整棵组织树 |
| 使用进度 | 本人月预算消耗 | 6px 细轨道、数字及真实百分比、文字状态；未知额度不画假进度 |
| 列表表头与行 | 模型列表、模型使用汇总 | 浅色表头、1px 分隔线、44px 表头、约 56-60px 行高 |
| 状态徽标 | 正常、预警、已超额、未分配、暂不可用 | 沿用成功/警告/危险/中性色 token；不只依赖颜色 |
| 管理按钮与开关 | 不迁移 | 不出现分配、编辑、批量勾选、启用、拦截开关 |

复用现有 `--background/--foreground/--border/--muted/--brand/--success/--warning/--destructive`，不另建主题。常规新边框圆角不超过 8px；不全局修改现有 9px FinOps 规则。正文 12-14px，次要信息 11-12px，KPI 数字 24px；固定字号、字距为 0。数字使用等宽数字，并提供精确值读取方式。

### 3.3 可视化组件

- 预算：复用 `BudgetProgress` 的语义与颜色规则，可小范围提取展示组件，不携带管理页状态。
- 趋势：Recharts 堆叠柱图为默认，折线图为可选；复用 `ChartModeToggle` 的图标方式、`ChartSeriesLegend` 和 `FinOpsChartTooltip`。
- 配色与已有用量图一致：输入 `--chart-4`、输出 `--chart-2`、缓存读取 `--chart-1`、缓存写入 `--chart-cache-write`。
- 总 Token 堆叠柱展示四个不重叠序列；折线使用实际桶间直线，不制造平滑插值或堆叠含义。
- 本期不直接复用 `UsageActivityCard` 的请求逻辑，因为它请求通用观测接口，且热力图固定扩展至 26 周。可复用展示层或提取接受数据的图表，所有数据从本人接口获取。
- 趋势默认按日；用户可选小时。图表高约 300-340px、最小宽约束为 0，容器宽度缩小时轴标签降密度，不缩字体。
- 按模型汇总默认按总 Token 降序；列为模型、请求数、输入、输出、缓存读取、缓存写入、总 Token、费用估算。
- 图 2 没有定义趋势图细节，以上属于沿用仓库现有可视化规范的补充设计，不应表述为图片已有组件。

### 3.4 响应式与无障碍

- 内容区 >= 1024px：资料/归属属性横向排列，预算指标五列；各数据模块纵向展开。
- 内容区 640-1023px：属性与 KPI 改为两列，最后指标自然占位，不产生水平页面滚动。
- 内容区 < 640px：属性单列，KPI 按可用空间两列或单列；表单宽度 100%；表格可内部横向滚动。
- 图标操作使用现有 `Button` 与 Lucide 图标；触摸区足够点击，不因 loading 或 hover 改变尺寸。
- 长邮箱/名称在资料区允许换行；表格可截断并提供完整值 tooltip/复制；不得用持续减小字号掩盖溢出。
- 密码对话框管理焦点，关闭后回触发按钮；字段具有 label、错误关联及适当 autocomplete。
- 头像使用固定 64x64px 圆形展示；密码会话提供 `Camera`/`Trash2` 图标和 tooltip，触屏入口持续可见，Microsoft 会话没有交互覆盖层。头像预览使用方形裁切区域并叠加圆形显示边界；确认/取消具有明确焦点顺序。
- 本人资料成功/失败采用就近状态消息；图表也提供可读汇总表，颜色不是唯一信息通道。
- 尊重减少动态效果设置；浅/深色及现有各语言均须验收。

## 4. 交互与前端状态

### 4.1 路由接入

新增 `Page = ... | "user-settings"`，加入 `pageIds`。将 `settings/user-settings` 等全局页明确置于数据源归一化之前，或使用一个最小公共全局页集合；不把个人设置注册为 APIM/Copilot 数据源专属页。

需覆盖初始 URL、`popstate`、`FINOPS_NAVIGATE_EVENT`、数据源切换和页面渲染。用户菜单调用现有 `navigatePage`；保留 `source`。在本页切换来源仍停留本页，账号信息中的 APIM 数据域不跟随来源混用。

原有系统设置入口及行为不变；新页加入工作区布局判定，避免 App 通用 topbar 与页面自己的 header 重复。

### 4.2 数据加载与缓存

初始基本资料使用 `AuthProvider.user`，随后通过 `/auth/me` 刷新校验。治理数据分为 account、budget、models、usage 四个本人查询，避免一次失败导致整个页面空白。本地头像元数据随 Profile 返回，二进制由单独的本人接口读取，不放进治理查询响应。

建议 query keys：

```text
["user-settings", appUserId, "account"]
["user-settings", appUserId, "budget", period]
["user-settings", appUserId, "models"]
["user-settings", appUserId, "usage", period, interval, timezone]
```

- `appUserId` 只用于客户端缓存分区，不发作后端查询目标。
- `account/models` 可设短期 staleTime；`budget/usage` 初始为 30 秒，手动刷新失效当前用户的只读查询。
- 后端及 fetch 使用 `Cache-Control: no-store`/`cache: "no-store"`，不将个人信息放入共享缓存或 localStorage。
- 名称更新成功直接接收服务端 Profile，更新 AuthProvider 并使 account 查询失效；头像更新成功接收 `PersonalAvatar` 更新 `avatar_url`。两者均不复用会清空全部查询的登录 `adopt` 路径。
- 更新模块级 `identity` 缓存或提供统一 profile refresh，避免再次挂载返回旧名称/头像；不延长 `session_expires_at`。
- 月份切换的 query key 必须变化；前一月数据不可标成本月数据。模型列表不因月份切换重新解释授权。
- `AuthUser` 新增可选 `avatar_url: string | null`，旧 API 缺字段时回退首字母，不能把客户端预览当作已持久化头像。password 会话只请求本地 `avatar_url`，entra 会话只请求 Graph；Graph 缓存按账号与 method 隔离，不写入本地头像表。
- 头像更新后 URL 的 revision 变化，页面与侧栏读取同一已保存结果；本地头像 `photo` 不写 sessionStorage/localStorage/TanStack Query，使用认证上下文短期内存。失败显示回退头像，不阻塞页面。
- 账号或 method 变化时先清空 `photo`，取消旧请求或以身份版本校验结果，避免旧 Graph/本地头像的异步返回覆盖新用户。
- 头像预览仅在编辑器内显示；取消、完成、退出和卸载均撤销 object URL、清空 File/base64。头像提交与名称提交互不影响；密码字段只存在表单内存并在关闭/完成时清除。
- 账号切换、退出、过期或改密成功清除查询、照片缓存和身份解析缓存；持久化的本地头像不因退出或改密被删除。

### 4.3 状态表

| 状态 | 展示/行为 |
| --- | --- |
| 身份检查中 | 现有认证 skeleton，不请求个人统计 |
| 只读模块加载 | 固定尺寸 skeleton，基本资料可使用 |
| 资料编辑未改变 | 保存不可用 |
| 资料提交中 | 禁止重复提交，关闭/取消遵循现有表单约定 |
| 头像选择/预览 | 仅编辑器显示候选图；取消不写入、不影响已保存头像或名称草稿 |
| 头像处理/提交中 | 固定尺寸 loading，禁用重复上传/移除，不阻塞只读数据 |
| 头像保存失败 | 旧头像仍为已保存值，保留可重试的局部错误 |
| 头像移除成功 | 页面、侧栏及后续密码登录均回退首字母 |
| Microsoft 会话 | 普通文本展示资料，无资料修改命令 |
| 没有目录匹配 | 组织/部门显示未关联；其他本人数据仍独立读取 |
| 无预算记录 | 已使用可展示，预算/余额/比例为空；不画 0% 假进度 |
| 真实零用量 | 显示 0 及暂无记录，不显示接口错误 |
| 显式空模型策略 | “未获模型授权”，不能自动回退 |
| 只读模块失败 | 模块错误与重试；保留其他模块及资料编辑 |
| 刷新失败但已有旧数据 | 保留并标记过期/刷新失败，显示数据时间，不能伪装最新 |
| 会话过期 | 现有过期事件清理状态并显示登录页 |
| 改密成功 | 清理本地状态并返回登录；显示一次性“密码已更新，请重新登录”状态 |

## 5. 身份与只读投影

### 5.1 两种标识

服务端写入目标为 `SessionIdentity.id` 对应的 `app_user` UUID。人员预算/模型/遥测的查询键为服务端规范化邮箱，匹配目录中的人员 ID。

```text
account_id = SessionIdentity.id                 # 仅用于账号写入
person_key = SessionIdentity.email.strip().lower()
catalog_user = 当前合并目录中与 person_key 精确匹配的人员
```

不使用名称匹配、邮箱前缀匹配、域名推断、前端传入 UUID 或任意别名扩大查询。匹配规则与现有 APIM 标识约定一致；历史数据标识不一致时明确身份映射缺口，不通过模糊匹配合并他人用量。

### 5.2 归属和权限

复用 `enterprise_catalog -> merge_observed_users -> merge_application_owners` 的既有顺序。现有 seeded 人员不会被 observed 合并覆盖，不在本功能中悄然改为“最新遥测一定覆盖目录”。解析结果附带 `source`：

| source | 含义 |
| --- | --- |
| `catalog` | 固定目录人员及其归属 |
| `observed_usage` | 已观测人员，在已知部门中被合并 |
| `owner_default` | 没有已有目录记录的启用 Owner 默认归属 |
| `unlinked` | 没有可用匹配，不补默认部门 |

归属响应只返回本人组织/部门属性，不返回整个目录。权限组响应为 `[{key: role, name: 对应平台角色标签, source: "turnstile"}]`。不从当前 Microsoft 会话推断 Entra group IDs；如果后续产品需要这些组，应增加经验证的服务端数据来源及权限设计。

### 5.3 预算

- 默认月份由服务端 UTC 时间决定，`period` 为真实 `YYYY-MM`，允许当前月及前 11 个月；月份上限/下限由响应提供给 MonthPicker。
- 增加本人限定预算读取和证据聚合参数/方法，在仓储层限定 `scope_type=user`、`scope_id=person_key`。复用 `token_usage_by_budget_scope` 的既有证据/入账语义，不能只 SUM 原始 token_usage。
- 对没有目录或没有预算的账号仍独立读取本人用量；缺归属不导致查询退化为所有用户。
- 复用 `TokenBudgetService._forecast_tokens` 对所选月预测，并复用状态计算；必要时小范围提取纯函数，不复制一套不同阈值。
- 预算使用率 `used / limit * 100`；余额 `limit - used` 可负；预警取实际使用或月底预测达到已配置阈值。无额度时百分比和余额为 `null`。
- 已使用是预算证据口径，实际趋势是原始遥测统计口径；返回 `basis` 与 `generated_at`。本期不将余额标为实时 APIM 可准入余额，不把待结算预留当作精确费用。
- 部门模式优先按本人该月预算记录的 `parent_scope_id` 解析；没有预算时使用当前目录部门，缺部门则模式为 `null`；有已知部门但无配置沿用 `audit`。模式归属与当前目录不一致时保留来源，不默默替换。
- 响应不带组织/部门额度、他人 rows、updated_by 邮箱或全局 history。

### 5.4 模型授权和运行状态

个人模型查询使用 `list_user_model_policies([person_key])`，并只在服务端读取注册表、有效路由和运行状态。返回脱敏的模型展示字段，不返回连接配置、凭据 hint、密钥、后端地址或未授权模型清单。

| 人员策略 | 授权候选 |
| --- | --- |
| 无 policy row | 仅 `assignment_required=false` 的兼容模型 |
| 有 policy row，列表为空 | 空列表，显式禁止全部 |
| 有 policy row，列表非空 | 显式模型列表，不额外放开兼容模型 |

候选再与模型/运行时 `allowed_roles`、启用状态、有效路由、生产 APIM 限制及图像功能开关取交集。纯读取不能调用 `_assert_model_allowed` 的失败路径，因为它会记录拒绝调用；可提取无副作用判定供查询和调用复用。

模型的 `access_source` 为 `explicit/legacy_compatibility`，`runtime_status` 为 `available/unavailable/unknown`。健康故障不抹除已获得授权：显示暂不可用；`unknown` 不呈现成已确认健康。无有效发布/路由或已停用的模型不作为可用模型返回。

预算风险单独展示，不以预算剩余 > 0 作为模型授权筛选条件。列表表示当前授权和运行快照，不保证一次真实调用必定被网关放行；网关最终策略和投影延迟保持原边界。

### 5.5 使用统计

由服务端构造 `UsageFilters(user_id=person_key)`，固定 APIM 数据域，对整个月调用现有 overview/trends/distribution 仓储方法。不暴露可修改的人员、部门或组织过滤参数，也不能用当前部门过滤本人跨部门历史记录。

- 使用 UTC 月窗 `[from, to)`，时区仅影响趋势分桶和标签；验证 IANA 时区，默认沿用应用时区。
- 支持 `interval=day/hour`，只有合法月份窗口，不开放任意无限时间范围。
- 复用既有聚合、价格快照与缓存字段，不用按请求 `limit` 截断的数据推算 KPI。
- 总量及按模型汇总覆盖所有模型；如果响应需要分页，KPI 仍针对完整时间窗，汇总表必须完整分页，不能把 Top N 当总计。
- 费用有数据完整性状态 `priced/partial/unpriced`；无价格显示未知或部分已计价金额，不以 0 冒充免费。
- 使用 `cache_read_tokens/cache_write_tokens` 显式字段；遇到旧字段按仓库已有兼容规则拆分，不将 `cached_tokens` 再加一遍。
- 空窗成功返回 0 请求/0 Token，错误率及平均时延为 `null`，而不是把无样本转换为 100% 成功率。
- 返回 `generated_at`、`last_observed_at`、`usage_domain=apim`，说明快照时间，不承诺实时性。
- 不返回提示词、生成内容、原始令牌或他人请求；本期不新增请求详情钻取入口。

## 6. API 契约

下列新增路由已按本设计实现。复用 Cookie 会话、`CurrentSession` 和现有 HTTP router 注册方式，所有响应 `Cache-Control: no-store`；写入复用 `require_allowed_write_origin`。请求模型禁止未知字段。

| 方法与路径 | 请求 | 响应 | 访问限制 |
| --- | --- | --- | --- |
| `GET /api/v1/auth/me` | 已有 | Profile，拟新增可选 avatar_url | 本人，用于更新基本资料 |
| `PATCH /api/v1/user-settings/me/profile` | `{display_name: string \| null}` | 同上扩展 Profile | 本人且当前为 password 会话 |
| `GET /api/v1/user-settings/me/avatar` | `v?`，仅用于图片 revision | PNG/JPEG/WebP 二进制，未设置为 404 | 本人且当前为 password 会话 |
| `PUT /api/v1/user-settings/me/avatar` | `{avatar_data_url: string \| null}`，null 表示移除 | `PersonalAvatar` | 本人且当前为 password 会话 |
| `POST /api/v1/user-settings/me/password` | `{current_password, new_password, confirm_password}` | `204`，删除 cookie | 本人且当前为 password 会话 |
| `GET /api/v1/user-settings/me/account` | 无 | `AccountInformation` | 本人 |
| `GET /api/v1/user-settings/me/budget` | `period?` | `PersonalBudget` | 本人 |
| `GET /api/v1/user-settings/me/models` | 无 | `PersonalModelList` | 本人 |
| `GET /api/v1/user-settings/me/usage` | `period?`, `interval=day/hour`, `timezone?` | `PersonalUsage` | 本人 |

不提供目标用户 path 参数；额外传 `user_id/email/role/method` 等拒绝为 422，不覆盖服务端会话。只读 GET 的 query 参数也采用白名单校验。头像 `v` 不是账号 ID，也不允许用它读取历史/其他账号头像；修订不匹配时返回 404，前端刷新本人元数据后再读取。

复用应用头像的 JSON base64 data URL 请求模式，`avatar_data_url` 字段必须显式提供，缺字段返回 422，避免空对象意外删除头像。base64 字符串上限 90,000 字符，整份请求 body 最大 96 KiB；读取 body 时流式计数并在超限时终止，不能只信任 Content-Length，也不能等完整 JSON 入内存后才检查。

Profile 在现有字段基础上新增可选 `avatar_url: string | null`，包含登录响应、`/auth/me` 和名称更新响应。password 会话有本地头像时返回 `/api/v1/user-settings/me/avatar?v=<revision>`，未设置为 null；entra 会话固定为 null，Graph 照片继续由 AuthProvider 读取，不接受本地头像替代。

核心响应字段草案：

```text
AccountInformation
  account_id: UUID
  permission_groups: [{key, name, source: "turnstile"}]
  organization: {id, name} | null
  department: {id, name} | null
  membership_source: catalog | observed_usage | owner_default | unlinked
  editable: {avatar: bool, display_name: bool, password: bool}
  generated_at: ISO datetime

PersonalAvatar
  avatar_url: string | null
  updated_at: ISO datetime | null

PersonalBudget
  period, period_start, period_end, min_period, max_period
  token_limit: int | null
  used_tokens: int
  remaining_tokens: int | null
  warning_threshold_percent: int | null
  usage_percent: number | null
  forecast_tokens: int
  forecast_percent: number | null
  status: unallocated | healthy | warning | exceeded
  enforcement_mode: audit | block | null
  enforcement_department_id: string | null
  basis: "budget_evidence"
  generated_at

PersonalModelList
  policy_state: unconfigured | assigned | deny_all
  items: [{id, name, model_key, provider_name, context_window,
           capabilities, access_source, runtime_status}]
  generated_at

PersonalUsage
  period, from, to, interval, timezone, usage_domain: "apim"
  totals: {requests, input_tokens, output_tokens, cache_read_tokens,
           cache_write_tokens, total_tokens, error_rate, average_latency_ms,
           estimated_cost_usd, cost_state}
  points: [{bucket_start, totals}]
  models: [{model_id, model_name, totals}]
  generated_at, last_observed_at
```

`editable` 为展示辅助，不是前端授权凭证；每次写入仍重新校验会话。`context_window`、名称和时间等字段允许缺失时必须显式 `null`，不使用图 1/图 2 的模型名称或数字填充。

错误约定：

| HTTP | 场景 | 前端处理 |
| --- | --- | --- |
| 400 | 当前密码不正确 | 对话框内错误，不触发过期或清空会话 |
| 401 | 会话缺失、停用、过期或已撤销 | 复用现有过期事件，返回登录 |
| 403 | Microsoft 会话资料写入/本地头像读取、非法写来源 | 显示操作不允许，不自动跳登录 |
| 404 | 本人未设置本地头像或头像 revision 已变化 | 无头像时回退首字母；旧 revision 则刷新本人资料，不当作登录过期 |
| 409 | 并发凭据变化或账号状态变化 | 要求刷新/重新认证，不自动重放密码提交 |
| 413 | 头像 JSON 请求体超过 96 KiB | 编辑器内报错，保留旧头像 |
| 422 | 名称/密码、头像编码/格式/尺寸/解码、月份、时区或未知字段非法 | 定位字段错误，不将原始密码或头像 base64 回显到响应 |
| 429 | 密码验证超限 | 就近错误，响应含 Retry-After |
| 5xx | 存储或只读模块失败 | 局部重试，不伪装无数据 |

校验错误响应须剔除敏感字段的 `input`，不能直接使用可能回显密码或头像 base64 的默认校验错误详情。不自动重试密码写入；网络结果不确定时先重新认证/检查会话，不能宣称修改失败后立即重放。

## 7. 账号更新与安全设计

### 7.1 显示名称

1. 解析当前会话，确认 `method=password`。
2. 规范化并验证名称，空字符串统一为 `NULL`，仅允许 `display_name`。
3. `AuthStore` 事务内更新 `WHERE id=session.id AND enabled`，写成功审计；不调用 `create_password_user`，避免更新角色或清空会话。
4. 返回当前账号 Profile 和原会话到期时间；不重签会话或延长 TTL。
5. 更新前端认证资料及侧栏。

此修改仅更新 `app_user.display_name`，不重写历史 token_usage.user_ref。由固定目录维护的人员名称也不被用户设置写入覆盖。

### 7.2 本地头像处理、存储与读取

参考现有应用头像的 10 MiB 原图限制、客户端居中裁切/压缩、JSON data URL 和数据库 bytea 存储；个人头像使用独立本人接口与账号权限，不调用应用头像的 Owner-only 管理接口。

1. password 会话选择 PNG/JPEG/WebP，浏览器先校验原图大小；解码后显示裁切预览，默认居中正方形裁切，不提供远程 URL 下载。
2. 用户确认后参考 `prepareApplicationAvatar`，输出 192/160/128px 正方形，优先 WebP，必要时 JPEG，降低质量直到解码后字节 <= 64 KiB；无法达到限制时提示更换图片，不上传原始 10 MiB 文件。
3. 后端校验会话/来源和 96 KiB 请求体上限，再对显式 `avatar_data_url` 校验严格 base64、PNG/JPEG/WebP 实际签名、非空及解码后 <= 64 KiB。
4. 使用仓库已依赖的 Pillow 检查格式、可解码性、单帧和正方形尺寸；上传结果边长不得超过 192px。解码前检查尺寸/像素限制，将解压炸弹告警视为错误，不仅做文件签名检查。
5. 重新编码为静态标准图像，移除 EXIF、注释等原始元数据，再确认最终内容 <= 64 KiB。不改用 MIME 猜测或原样保存上传内容。
6. `AuthStore` 事务内锁定本人 app_user 行，重新确认 enabled 和会话有效，仅 upsert 本人头像，生成新的 revision、写成功审计。任一步失败不替换已有图像；不改变 role/name/password 或会话 TTL。
7. 返回新的 `PersonalAvatar`，前端更新认证资料及页面/侧栏已保存头像；移除通过显式 null 删除本人头像行并返回 null。无头像时重复移除保持幂等。

拟议存储表 `app_user_avatar`：

| 字段 | 约束 |
| --- | --- |
| `app_user_id UUID` | 主键，外键关联 app_user(id)，账号删除时级联清理 |
| `media_type TEXT` | 仅 image/png、image/jpeg、image/webp |
| `image_bytes BYTEA` | 服务端标准化后内容，长度 1-65536 字节 |
| `revision UUID` | 每次实际替换生成新值，便于失效旧图片 URL |
| `updated_at TIMESTAMPTZ` | 保存时间，服务端生成 |

每个账号最多一行，不保存原图、文件名、远程 URL、裁切草稿或历史图像，不新增 Blob Storage 或公开静态文件目录。读取通过当前会话 UUID 定位，本人 GET 返回真实媒体类型及 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`，不沿用应用头像的长期 immutable 缓存。由相同有效会话验证保护 `<img>` 请求；前端 API 若跨源，使用 credentials fetch + 内存 object URL，不把头像端点误接为无凭据外链。

同邮箱的 Microsoft 登录不读写此表，只读取 Graph 照片；本地头像在 Microsoft 登录期间仍保留，下一次密码登录恢复。两种来源都不能从上一账号的照片缓存回退。头像与审计同事务，读取/上传不发送图像到遥测或日志，不新增密码再认证要求。

### 7.3 密码修改与并发

继续使用现有 scrypt 哈希、新随机盐和常量时间验证。账号表行锁用于协调同一账号改密及密码登录签发；没有客户端目标账号字段。

```text
校验会话、来源、长度/确认密码及限流
  -> 读取并验证当前密码，计算新哈希（不写日志）
  -> 数据库事务锁定 app_user 本人行
  -> 重新确认 enabled、当前会话仍有效、旧 hash 未变
  -> 更新 password_hash
  -> DELETE user_session WHERE user_id = 本人
  -> 记录成功审计
  -> commit
  -> 删除当前 cookie，204
  -> 前端清除会话/照片/查询状态，返回登录页
```

- 若哈希计算期间账号凭据已变，不基于旧快照覆盖新密码；返回 409 或会话已撤销的 401。
- 密码登录签发也须在同一账号行锁下确认验证过的 hash 仍为当前值，再创建 session；否则并发旧密码登录可能在撤销之后创建新会话。
- Entra 登录签发与改密的会话创建需要在同一账号锁下串行；在改密之后完成且经 Microsoft 重新认证的新会话可存在，不是被撤销的旧会话。
- 不依赖单实例进程锁；多实例和命令行更新都要遵循相同账号凭据更新/会话签发约定。
- 对话框关闭、成功及过期立即清空密码字段；`autocomplete=current-password/new-password`，允许密码管理器和粘贴。
- 改密成功只撤销 Turnstile sessions，不调用 Microsoft 全局退出、重置密码或 Graph 写 API。

### 7.4 限流与审计

本期建议采用 PostgreSQL 持久化的账号安全事件，避免多实例限流只存在于内存。默认每账号 15 分钟内最多 5 次错误当前密码；与账号行锁协调计数并返回 429/Retry-After，限流针对本自助验证流程，不改已有登录准入。

新增 `account_security_event` 建议字段：`id`、`app_user_id`、`action`、`outcome`、`created_at`、`request_id`。动作包括 `display_name_updated/avatar_updated/avatar_removed/password_changed/password_verification_failed`；不包含任何密码、哈希、cookie、token、姓名旧值、新值、头像字节/base64、原文件名/EXIF 或请求 body。

成功更新和成功审计同事务；错误密码事件单独提交，不因后续 HTTP 错误回滚而丢失计数。索引为 `(app_user_id, action, created_at)`，审计仅系统/管理员后台使用，不在本人页暴露他人数据。保留策略沿用部署的审计管理，不在功能发布时擅自执行数据清理。

不在通用 API 请求日志、错误详情、APM traces 或前端 analytics 采集凭据字段或头像内容；必须增加自动化回显/日志检查用例。

## 8. 数据库、模块与交付边界

`app_user.display_name/password_hash/role` 与 `user_session` 已存在，名称/密码修改不需要新增这些列；头像修改需新增独立 `app_user_avatar` 表，由 AuthStore 管理，不在 app_user 行中混入图片大字段，也不新增组织或部门列。

头像表及持久化审计/限流通过 `011_user_settings_profile.up.sql` 建立。上线前发现当前 repo 的定价功能已应用 `010_models_dev_pricing`，因此将未上线的个人设置迁移顺延为 `011`；不修改已应用文件。本迁移不依赖 `009/010`，可从上游 `001-008` 独立升级，也可在当前 repo 的完整链后应用。迁移带文件类型/字节长度和账号外键约束；不将默认头像图片或实际用户图片写入迁移。部署顺序、执行入口及应用回滚边界见[升级说明](../../docs/deployment.md#user-settings-database-upgrade)。

建议新增文件：

| 文件 | 职责 |
| --- | --- |
| `frontend/src/pages/user-settings-page.tsx` | 平台个人设置页 |
| `frontend/src/api/user-settings.ts` | 请求及响应类型 |
| `frontend/src/components/user-settings/` | 头像选择/预览编辑器、名称表单、改密对话框、预算与模型/统计展示，按真实复杂度拆分 |
| `frontend/src/styles/user-settings.css` | 局部布局，复用主题和 FinOps token |
| `backend/http/user_settings.py` | 本人路由与参数校验 |
| `backend/services/user_settings_service.py` | 本人限定只读聚合与投影 |
| `backend/services/user_avatar_service.py` | 本地头像格式/解码/标准化及本人操作编排，可按复杂度决定是否单独拆分 |
| `tests/platform/api/test_user_settings_api.py` | 权限、参数、身份及接口契约 |
| `tests/backend/` 下对应测试 | 账号事务、预算模型口径及统计服务 |

必要配套修改：`backend/api.py` 路由注册、仓储协议及 PostgreSQL/demo 对应只读实现、OpenAPI 路径/模型分片及 `contracts/openapi.yaml` 索引、locale 字典、文档索引/配置说明。`contracts/openapi.yaml` 是规范契约入口，按现有 `contracts/README.md` 更新并验证，必须与运行时 Pydantic 模型同步。

认证继续只使用真实 `AuthStore`，不为 demo 增加“可编辑假账号”。demo 只读数据必须明确来自 demo，不能混入真实 APIM 或宣称是真实用户预算。

实现已新增业务代码及 `011_user_settings_profile.up.sql`，未调整依赖锁文件、部署参数或 CI 配置；该迁移上线前只在隔离的本地 PostgreSQL 中执行。

## 9. 实施顺序与验证

1. 确认本草案的范围决策，先建立本人接口模型、身份解析和隔离测试。
2. 增加限定本人只读仓储/服务，验证预算证据口径、空策略与模型动态分配规则。
3. 增加名称/头像写入、图片校验与标准化、密码事务、会话签发并发保护、限流与审计；完成真实 PostgreSQL 事务测试。
4. 增加全局路由与账号菜单，按图 2 完成资料、只读账号模块及图表。
5. 增加语言、错误和空状态，更新契约/文档，再进行浏览器和回归验证。

验证矩阵：

| 层级 | 必测内容 |
| --- | --- |
| 单元 | 名称 trim/null/字符限制、头像 base64/签名/尺寸/单帧/解码/EXIF 清理/64 KiB 限制、密码边界、归属来源、预测、策略为空、缓存不重复累加 |
| API | password/entra x owner/member、头像只读来源、未知字段、PUT 缺字段与 null 区分、96 KiB 流式 body 限制、来源拒绝、仅本人查询、无敏感字段、401 与错误当前密码区分 |
| PostgreSQL | 头像 upsert/移除/外键清理、图片与审计原子性、失败不替换旧图、名称/头像/改密并发、全会话撤销、双改密、旧密码登录竞态、并发多实例限流 |
| 契约 | OpenAPI 的请求/响应/错误、Profile avatar_url 向后兼容、二进制媒体类型、nosniff/no-store、新增迁移顺序 |
| 前端 | 全局页归一化、刷新/后退、身份缓存同步、头像预览取消/错误/版本更新/object URL 回收/旧身份异步回调、月份隔离、密码字段清除 |
| 浏览器 | 两种登录方式、真实改头像/改名/改密、头像侧栏同步/持久化/移除/Graph 回退、账号和 method 切换、移动端、键盘焦点、所有治理模块无修改入口 |
| 数据 | UTC 月边界与时区、调部门历史统计、未计价/部分计价、预算证据与遥测差异 |
| 视觉 | 375/768/1440/1920px、浅/深色、长名称邮箱、大数字、表格滚动和图表非空 |

实施阶段按仓库指南执行 `uv run ruff check ...`、`uv run mypy ...`、`uv run pytest -q`、`npm --prefix frontend run build`。浏览器截图与交互测试不能用构建成功替代；事务/锁测试不能用 SQL 字符串断言替代。云部署与迁移执行须另获环境授权。

## 10. 实现与验证记录

- 已实现用户菜单入口、平台级路由、名称/头像/密码自助更新及本人限定只读账号模块。
- 已实现头像标准化、独立私有存储、变更审计、密码错误限流、改密撤销全部会话及登录签发的账号行锁保护。
- 初始开发基线完整测试通过 1,562 项，包括真实 PostgreSQL 的持久化、回滚及并发验证；Ruff、Mypy、前端类型检查和生产构建通过。
- 浏览器使用 Playwright 与本机 Chrome 验证菜单、改名、头像保存/移除、错误密码不退出、改密重新登录、只读会话及响应式布局。
- Microsoft 只读验证使用隔离数据库中的 `entra` 方法会话夹具，不代表完成真实 Microsoft OAuth、租户准入或 Graph 头像验证。
- 本地验证不发起模型推理或云部署；部署前仍须在获授权的目标数据库应用新迁移并验证真实身份集成。

### 10.1 上游提交前复验

- 将用户设置提交独立整理到上游 `main` 的 `4e93935`，未包含助手相关的 5 个既有提交。
- 独立候选完整测试通过 1,462 项；数量变化来自排除助手功能测试，并非跳过本功能测试。真实 PostgreSQL 持久化、回滚、限流与并发用例均执行。
- 在两个新建的隔离 PostgreSQL 数据库验证 `001-008 -> 010` 升级、新库安装和重复执行；账号、会话、预算、用量及既有迁移校验和保持不变，无须 `009`。
- Ruff、Mypy（184 个源文件）、前端类型检查/生产构建、Bicep 编译、APIM XML、OpenAPI 校验及差异空白检查通过。
- 保留既有 Starlette 弃用提示、前端大包提示、Bicep/OpenAPI 警告；未调整无关配置来消除这些警告。
- 数据库升级只在本地测试环境执行；真实 Microsoft OAuth/Graph、云端升级和模型调用仍未验证。

### 10.2 当前仓库合并与发布准备

- 上游 PR 创建后，按用户要求合并至当前 repo `main` 并发布；保留已有助手与定价功能。
- 发现定价迁移 `010_models_dev_pricing` 已应用，未上线的用户设置迁移重命名为 `011_user_settings_profile`；同步更新 PR、契约测试及升级文档。
- 第 10.1 节的迁移验证记录对应更名前的候选；实际发布以合并后完整迁移链的复验及私有发布证据为准。
- 合并后的当前 repo 完整回归通过 1,607 项；Ruff、Mypy（198 个源文件）、前端类型检查/构建、OpenAPI 与差异空白检查通过。
- 在新建隔离数据库验证完整 `001-010 -> 011` 升级、`001-011` 新库安装及重复执行；既有账号、会话、预算、用量和迁移校验和均保留。此记录不代表线上迁移或上线验收。

### 10.3 实际发布与 PR 补充

- 当前 repo `main` 的发布源为 `540956d94dd0678b5c879824e63701b3f31542e9`，2026-10-09 23:22（Asia/Shanghai）完成线上验收。该版本包含原有助手和定价功能，不能将其 1,607 项回归结果冒充独立上游候选的测试数量。
- `011_user_settings_profile` 已通过原有 `backend.migrate` 在线上应用；旧 10 个迁移校验和保留，显式重跑无新增应用。已应用文件的 SHA-256 为 `c830385df79834f2730450365cc2fe3b590f325ea2ca550fd0906c4579747fa5`，不再修改该 SQL。
- 当前 repo 升级为 `001-010 -> 011`；独立上游候选仍支持 `001-008 -> 011`。无需另一份建表脚本、修改初始 schema、自动降级脚本或将私有 Azure 发布助手纳入 PR。
- 发布前保留原挂载 API 包和新加密逻辑备份。Azure PostgreSQL Burstable 拒绝按需备份，因此使用临时官方 PostgreSQL 16 客户端导出并验证归档目录；未调整规格、网络或常驻软件。归档目录校验不等于完成完整恢复演练。
- 新挂载 API 包及前端资源校验通过；健康、真实密码 Owner 登录、本人账号/预算/模型/用量查询均返回 200，个人响应禁止缓存。运行设置、权限、Control-plane 部署及业务配置指纹保持不变。
- 线上未修改已有用户的头像、名称或密码，未发起推理；真实 Microsoft OAuth/Graph 未重新验证。首次上线读回未通过，后续 SCM 状态和只读复验通过，没有重复上传。
- PR 补充通用备份与执行边界、实际发布记录，以及可复现的本地 PostgreSQL 迁移测试；测试会覆盖新装、既有数据保留、幂等重跑、校验和拒绝和失败回滚，不连接线上环境。
- 本次补充后的独立上游候选完整回归通过 1,465 项；Ruff、Mypy（185 个源文件）及差异空白检查通过。新增 3 个真实 PostgreSQL 16 迁移用例均实际执行，未跳过；部署的 SQL 与迁移入口没有变化，不需要重新执行线上迁移或重发应用包。

### 10.4 初始化部署脚本检查

- 现有 API 打包逻辑复制全部编号迁移，包含 `011`，无需固定迁移计数或新版本开关。启动命令按 `backend.migrate && backend.bootstrap && uvicorn` 顺序执行；迁移失败阻止 Owner 创建及 API 启动，首次安装流程无需重写。
- 已有 secret state 的部署验证接受当前 Owner 密码，而不重写 bootstrap 哈希或平台密钥。仅将“初始 Owner 验证密码”提示改为中性的“Owner 验证密码”，CLI 帮助和文档说明改密后需更新私有验证凭据。
- 增强部署包测试，确认 bootstrap、用户设置路由和原样 `011` SQL 均被打包；新增 secret state 验证用例，确保改密后的验证密码只保留在内存。
- 新增真实 PostgreSQL 首次安装用例：迁移、创建 Owner、签发可读取空头像的会话、改名/改密、模拟重启重跑；确认新名称和新密码不被旧 bootstrap 哈希覆盖。
- 初始化部署相关 102 项检查、完整独立候选 1,467 项回归、Ruff、Mypy（185 文件）和差异空白检查通过。未修改 Bicep、已应用迁移或运行时业务代码，未重新执行 Azure 部署。

### 10.5 当前目录兼容与运行边界（2026-10-11）

- 本人归属通过统一database目录读取，显式关联使用稳定治理ID；
  未关联账号保持受信会话本人兼容路径，不因同邮箱自动绑定人员。
- 平台角色、菜单权限组、部门数据范围和模型/APIM授权分开；
  用户设置不提供管理员赋组、目录成员编辑或预算/模型写入。
- 001-017保持已应用checksum，011的账号/头像/安全事件数据不重做初始化；
  `9cc17fa`匹配三包与真实Owner本人账号/模型/预算/用量只读复验通过。
- 最新1758 passed / 7 skipped为综合应用发布前回归，不替代早期独立上游候选或
  真实Microsoft OAuth/Graph验收。PR #32仍Open，本次未重新测试OAuth/改密/头像写入。
- 最近登录30秒500发生于外部数据库停启后的应用连接路径；随后复测恢复，
  具体额外恢复机制未证实。本次未改变AuthStore或认证门槛，
  重连/依赖就绪与错误提示优化见[待改造设计](../ai-finops/design.md#availability-and-recovery-2026-10-11)。

## 权限管理兼容说明

2026-10-11的[权限管理](../permission-management/design.md)保留用户设置为不可取消的
个人入口；本人资料、预算、模型和用量仍使用当前账号的自助读取逻辑。
普通用户的其他目录/治理页面即使被勾选开放，也只读取本人数据。
