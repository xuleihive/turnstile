# 2026-10-16 上游 Main 合并日志

## 1. 合并信息

| 项目 | 记录 |
| --- | --- |
| 项目目录 | `/Users/edwin/Development/turnstile` |
| 合并分支 | 本地 `main`，跟踪 `origin/main` |
| 用户仓库 | `origin`: `https://github.com/EdwinDigital/turnstile.git` |
| 上游仓库 | `upstream`: `https://github.com/xuleihive/turnstile.git` |
| 合并前版本 / 第一父提交 | `2a58e152197fc377c48f5328df4c7169264607ea` |
| 本次拉取的上游版本 / 第二父提交 | `e59177d61c27d3641fff86caed8fe476bacf858e` |
| 合并提交 | `7115b4f4b0a1aa9d16fbc404e4fc8feccba54157` |
| 合并时间 | `2026-10-16 03:35:59 +0800` |
| 合并范围 | 引入上游独有的 50 个提交，保留原分支独有的 48 个提交 |
| 备份分支 | `codex/backup-main-before-upstream-20261016` |
| 冲突原则 | 定制功能冲突以用户分支为准，同时修复两边代码组合产生的集成问题 |
| 操作边界 | 仅本地代码合并和测试，没有推送、发布、生产迁移、Azure 资源修改或付费模型调用 |

功能清单以 `7115b4f^1..7115b4f` 的最终差异为准，而不是把上游现有功能全部计为新增。
整理本日志时，本地 `main` 工作区干净，领先 `origin/main` 51 个提交，即 50 个上游提交及 1 个合并提交。
该状态是整理日志前的检查结果；本日志本身是在合并完成后新增的文档。

## 2. 新增及扩展的功能

### F01. 金额预算与 Token 预算双模式

- 在原有 Token 预算基础上新增 `budget_mode="usd"`，保留默认的 `budget_mode="tokens"`。
- 组织、部门、人员预算，以及应用订阅预算的数据模型和管理流程扩展到美元金额。
- 新增美元额度、已用金额、剩余额度、预测金额、未定价请求数和费用可用性等字段。
- 批量分配、父子预算额度校验、预算变更审计和跨月处理适配预算单位；层级预算不能随意混用单位。
- 金额处理采用 `Decimal`；TPM 仍然是每分钟 Token 速率，不因月度预算切换到美元而变成金额速率。
- 未知费用或缺失价格有独立状态，不能直接当作免费请求或零花费。

依据：[预算领域模型](../turnstile_core/domain/models.py)、[预算服务](../backend/services/budget_service.py)、[应用预算模型](../turnstile_core/domain/application_access.py)。

### F02. 原子预算执行、预留和结算恢复

- 新增独立、默认关闭的预算执行协议，覆盖请求准入、额度预留、完成回执、结算和恢复。
- 执行账本围绕 Person 和 Application scope 处理；组织、部门的预算展示与分配不能等同于已经安装的 APIM 实时拦截能力。
- 新增执行 epoch、范围准备状态、租约任务、不可变证据和结算 outbox，避免不确定结果直接释放额度。
- 支持可信委托调用身份，以及明确选择人员、应用和模型的预算就绪检查与联合执行验证。
- 按协议分析请求；原生 Token 计数只有符合验证合同的上界结果才能降低预留值。
- Observer 可直接完成受条件写保护的结算；写入失败仍保留事件路径进行持久化恢复。
- 新增美元预留、预留解析和终态观察；修改额度或权限不应重置已确认用量及既有预留。

依据：[原子预算配置](../docs/configuration.md#atomic-budget-execution)、[账本集成](../turnstile_core/integrations/atomic_ledger.py)、[结算服务](../turnstile_core/services/budget_settlement.py)、[切换流程](aigateway-proxy-client/operations.md#budget-execution-cutover)。

### F03. 上下文分档定价及历史费用快照

- 在已有手工价格、公开定价同步和折扣管理之上，新增短/长上下文等完整分档计价规则。
- 支持按实际输入 Token 区间选档，保存完整档位引用、价格、边界及来源信息。
- 支持自定义上下文阈值和规则来源；不能将一个分档条目误当成完整平价价格覆盖模型。
- 请求历史保存适用的输入、输出、缓存读写单价以及上下文档位快照，避免后续改价改变历史计价依据。
- 价格同步扩展到分档规则和已接受基线，继续保留大幅变价复核、并发配置变更保护及手工定价保护。

依据：[分档定价解析](../turnstile_core/pricing/context.py)、[同步规则](../turnstile_core/pricing/sync.py)、[价格编辑组件](../frontend/src/components/model-management/model-price-source.tsx)。

### F04. 多 APIM 目标注册与网关拓扑

- 从原有全局单目标配置扩展为可注册多个既有 APIM 实例，分别保存 Azure 身份、API、端点及订阅配置。
- 新增 ARM 清单发现、网关部署记录、Managed/Self-hosted 关系及模型路由绑定。
- 模型调用和发布可明确选择 APIM 与 Gateway；不能将不匹配或不可用的明确选择静默替换为默认目标。
- 模型管理在能力就绪时提供模型、连接、Gateway、APIM 四个工作区；旧后端保留兼容视图。
- 凭据、Observer、探测和运行时绑定按目标隔离，避免跨 APIM 继承全局配置。

依据：[注册目标文档](../docs/configuration.md#registered-apim-targets)、[目标服务](../backend/services/gateway_targets.py)、[网关工作区](../frontend/src/components/model-management/apim-gateway-workspace.tsx)。

### F05. 既有 APIM 的可恢复初始化

- 新增独立的 APIM 初始化流程：预览、确认计划摘要、异步执行、进度查询及失败重试。
- 初始化任务可跨页面刷新持久保留；重复操作使用幂等身份、原始计划和加密临时凭据。
- 检查目标所有权、配置和权限，创建受管 API、固定协议操作、Logger 和系统订阅等初始化内容。
- 系统订阅先保持挂起，完成策略、账本及目录校验后才激活；匿名和错误密钥拒绝也需要验证。
- 包含受资源范围限制的权限委托和 ETag 保护，不应覆盖外部所有的 API 或已有预算防护。
- 初始化使用非推理目录校验；该能力不负责创建 APIM 服务、资源组或上游模型部署。

依据：[初始化文档](../docs/configuration.md#initialize-an-existing-apim)、[初始化服务](../turnstile_core/services/apim_onboarding.py)、[初始化对话框](../frontend/src/components/model-management/apim-onboarding-dialog.tsx)。

### F06. Self-hosted Gateway 生命周期与启动配置

- 新增自托管网关的关联、清单同步、名称修改、连接验证、重新关联和退役等管理流程。
- 在符合条件的既有 APIM 上创建新的 Gateway 配置，生成原生网关 ID、关联 API 并跟踪异步任务。
- 提供短期 Gateway 专属令牌、环境文件和 Docker 启动命令；敏感启动信息不进入审计、URL 或查询缓存。
- 需要操作者在自己的服务器启动容器并配置 HTTPS；“配置创建成功”只代表等待容器，不代表连通性验证成功。
- 当前创建流程仅接受已验证支持自托管的 classic Developer/Premium APIM；不承诺所有 APIM SKU 都支持。
- 平台移除或退役记录不等于删除 Azure Gateway 配置、用户服务器或容器。

依据：[创建自托管网关](../docs/configuration.md#create-a-self-hosted-gateway-from-turnstile)、[自托管集成](../turnstile_core/integrations/apim_self_hosted_gateway.py)、[启动配置组件](../frontend/src/components/model-management/self-hosted-gateway-dialog.tsx)。

### F07. 多目标模型发布 Rollout

- 新增一个逻辑模型向 2 至 20 个不同 APIM 目标发起发布的 API 和客户端支持。
- 父任务、子发布及 outbox 在同一事务中排队，统一逻辑模型身份和完整价格快照。
- 每个目标保留自己的上游模型 ID、连接、凭据、策略及探测配置，分别推进执行状态。
- 支持排队、执行中、部分成功等聚合状态；已成功目标不会因为其他目标失败而被自动重放或回滚。
- 移除一个目标路由时，其他启用路由仍可保留逻辑模型、价格和访问配置。
- 限制：当前新增模型界面仍是单目标流程；多目标能力主要通过 API/客户端使用，也不提供跨 APIM 同时切换的原子保证。

依据：[多目标发布文档](../docs/configuration.md#multi-target-model-publication)、[Rollout 服务](../turnstile_core/services/route_publication.py)、[HTTP 接口](../backend/http/model_platform.py)。

### F08. 版本化模型启用/停用控制

- 新增独立的模型 availability 命令，使用 `expected_revision` 和 `idempotency_key` 控制并发和重复提交。
- UI 分开显示期望状态、已验证生效状态、版本及各目标结果；`202` 只表示请求已接受。
- 状态需要覆盖相关 Gateway、保留的 API Revision 及已登记的自托管实例，不只更新数据库里的 `enabled`。
- 普通模型元数据编辑不再直接修改启停状态；缺少迁移、权限、策略或有效覆盖证据时不可假定开关已生效。
- APIM 对已禁用模型返回 `403 model_disabled`，不可验证的状态使用独立不可用错误。
- 停用不取消此前已经准入的在途请求，也不修改价格、预算及人员分配。

依据：[模型可用性配置](../docs/configuration.md#versioned-model-availability)、[可用性服务](../turnstile_core/services/model_availability.py)、[独立安装工具](../scripts/install_model_availability.py)。

### F09. 连接诊断与预算验证入口

- 新增连接诊断计划和执行接口，Owner 可明确选择模型与 Gateway，先查看缺失权限和执行条件。
- 连接诊断使用目标专属的系统 Probe Application，保留结果及配置指纹，区分预算/权限拦截、上游失败和配置已变化。
- 历史诊断结果是历史证据，不会自然演变成当前在线/离线结论。
- 发布管理新增预算就绪检查与隔离执行验证，分别展示配置/授权就绪与结算是否确认。
- 限制：连接诊断及预算执行验证在显式执行时可能产生模型费用；本次合并没有执行真实收费验证，也没有自动补授 Azure 权限。

依据：[连接诊断](../docs/configuration.md#connection-diagnostics)、[预算验证](aigateway-proxy-client/operations.md#budget-verification)、[诊断服务](../backend/services/runtime_diagnostics.py)。

### F10. 调用尝试追踪与网关归因

- 新增平台调用前的非计费尝试记录和调用后的结果记录，失败请求在正式用量尚未到达时也可追踪。
- 区分调用方 request ID、APIM correlation ID 和上游 request ID，保留平台超时与网关实际结果的差异。
- 新增 APIM 目标、Gateway deployment、原生实例、区域和 API Revision 等归因字段。
- 只有身份和关联证据足够明确时，才把平台尝试与正式遥测合并；未知用量或费用不展示为已测得的零。
- 增强流式完成证据、不可变费用字段和结算回执关联；追踪记录本身不作为正式计费用量。

依据：[平台调用追踪文档](../docs/configuration.md#platform-invocation-trace)、[运行时模型](../turnstile_core/domain/runtime_models.py)、[遥测处理](../turnstile_core/ingestion/processor.py)。

### F11. 模型目录筛选、关系图与模型用量详情

- 新增模型家族、接入平台、能力、连接、APIM/网关和状态等维度的组合筛选。
- 新增模型、连接、Gateway 的关系图与关联选择，辅助排查模型实际发布到哪里。
- 新增模型专属用量详情页，支持固定时间窗口的活动、费用及相关统计查询。
- 编辑表单、诊断历史、失败发布、状态确认和目标网关信息更细分；本次合并同时修复固定时间窗口和共享 Registry 缓存的组合问题。

依据：[模型筛选](../frontend/src/lib/model-inventory-filters.ts)、[关系图](../frontend/src/components/model-management/model-inventory-graph.tsx)、[用量详情](../frontend/src/components/model-management/model-usage-detail.tsx)。

### F12. 订阅创建、组织归属、TPM 和调用示例增强

- 在已有应用订阅管理基础上，创建时可以携带初始 Token/USD 预算、TPM 和标签。
- 扩展组织、部门及人员归属配置与能力声明，校验所选人员和部门所属关系。
- TPM 从平台保存值投影到既有准入账本，详情区分 `projected`、`pending`、`unavailable`，避免把数据库保存成功当作网关已生效。
- 应用详情增加执行账本来源、当前余额、待结算预留和执行状态，历史汇总不替代当前可验证余额。
- 新增 Bash/curl、Python、JavaScript 调用样例，覆盖目录、Chat、Responses、Messages、计数、图片及 Decision 等已安装协议操作。
- 订阅创建和密钥管理按选定 APIM 目标执行；继续保持一次性密钥及敏感信息保护。

依据：[应用创建文档](../docs/configuration.md#application-creation)、[订阅领域模型](../turnstile_core/domain/application_access.py)、[客户端示例](../frontend/src/lib/application-client-samples.ts)。

### F13. Microsoft Decision 原生模型操作

- 新增 Decision 模型能力、请求/响应结构、网关适配、调用入口及发布校验。
- 可提交状态和结构化问题，返回 Decision 结果，而不是将其包装为普通聊天调用。
- 复用人员/应用治理、预算与 Trace 路径；模型用途与 API 路径需要匹配，Decision 不作为默认聊天模型。
- 需要相应 Foundry 部署及已支持 Decision 的网关程序，普通发布不会无条件补建缺失协议操作。

依据：[运行时服务](../backend/services/runtime_service.py)、[网关适配器](../turnstile_core/integrations/gateway.py)、[发布领域模型](../turnstile_core/domain/control_plane.py)。

### F14. 发布与基础设施配套增强

- 完善目标范围内的权限预检、一次性凭据恢复、失败发布规格留存、重试和回滚确认。
- 新增路由 Table 投影、独立运行时凭据 Key Vault、自托管管理授权及 Foundry 托管身份授权模板。
- Control-plane Storage 配套私有端点和网络升级模板；角色委托限定资源、角色及主体类型，不能理解为应用天然拥有全局 Azure 管理权限。
- 同时包含静态资源压缩、托管身份令牌复用边界、连接池释放、表格拖动稳定性及开发代理缓存隔离等修复。

依据：[部署文档](../docs/deployment.md)、[Storage 路由发布](../turnstile_core/services/storage_route_publisher.py)、[Control-plane 模板](../infra/modules/control-plane-function.bicep)。

## 3. 保留的原有定制能力

以下不是本次首次新增，而是在合并时继续保留并适配的功能：

- 数据库组织目录、组织/部门/团队成员关系及人员归属。
- 菜单权限组、限定范围的管理员任命、数据可见范围及权限管理。
- 账号资料、用户设置、密码登录与已有身份绑定流程。
- `models.dev` 默认公开定价、批量同步、明确手工价格选择及旧数据兼容。
- Claude 原生工具、流式内容及 Responses 协议兼容处理。
- 用户分支的导航命名、多值用量过滤及原有组织数据隔离逻辑。
- 图片生成继续由 `IMAGE_GENERATION_ENABLED` 显式启用，默认仍为 `false`。

注意：图形化网关拓扑、多目标发布、APIM 原生后端池不等于 Model Intelligent Router。
README 明确本次公共版本不包含该智能路由功能。

## 4. 生效条件与默认开关

本次仅合并代码，未检查或修改线上配置。下面列的是代码默认值，不代表当前 Azure 配置值。

| 配置项 | 代码默认值 | 含义 |
| --- | --- | --- |
| `APIM_TARGET_REGISTRY_ENABLED` | `false` | 多 APIM 目标注册与拓扑 |
| `SELF_HOSTED_GATEWAY_ENABLED` | `false` | 自托管网关能力 |
| `SELF_HOSTED_GATEWAY_PROVISIONING_ENABLED` | `false` | 从平台创建 Gateway 配置 |
| `MULTI_APIM_TARGET_ROLLOUT_ENABLED` | `false` | 多目标发布 API |
| `BUDGET_EXECUTION_ENABLED` | `false` | 新预算协议的事件读取和执行余额查询入口，不等于已完成切换 |
| `BUDGET_EXECUTION_TARGET_IDS` | 空列表 | 经确认启用执行协议的目标范围 |
| `BUDGET_SETTLEMENT_ENABLED` | `false` | 新协议结算和恢复 |
| `BUDGET_RESERVATION_RESOLUTION_ENABLED` | `false` | 预留解析与处理能力 |
| `IMAGE_GENERATION_ENABLED` | `false` | 保留原分支的图片显式启用约束 |

`CONTROL_PLANE_ENABLED`、发布 Worker、Release Worker 等开关同样需要配套环境准备。
版本化模型启停不能仅靠修改一个布尔配置启用，还需要迁移、所有保留 Revision 的兼容策略、Table 权限及覆盖证据。
不能通过上传应用 ZIP 来替代网关协议安装、Azure RBAC、账本准备和受控切换。

依据：[实际默认配置](../turnstile_core/config.py)、[v1.2 升级边界](upstream-main-merge-2026-10-16.md#prepare-for-v12)。

## 5. 数据库迁移记录

本次新增 20 个上游迁移文件。原分支 18 个迁移与上游 28 个迁移中的 8 个公共文件去重后，完整目录共 38 个文件。
两边已有文件均与对应父提交逐字节相同，没有修改已应用迁移。
相同数字前缀下的不同迁移共存，执行器以完整文件名生成版本标识，不按数字前缀去重。

| 新增迁移文件 | 主要用途 |
| --- | --- |
| `009_context_tier_pricing.up.sql` | 分档价格及请求价格档位快照 |
| `010_budget_execution_protocol.up.sql` | 预算执行 rollout、scope、证据和结算任务 |
| `011_gateway_targets.up.sql` | 注册 APIM、Gateway 部署及模型目标绑定 |
| `012_apim_onboarding.up.sql` | 扩展发布操作类型约束，允许 `apim_onboarding` 初始化任务 |
| `013_multi_target_rollout.up.sql` | 多目标发布父子任务 |
| `014_self_hosted_gateway_provisioning.up.sql` | 自托管 Gateway 创建 |
| `015_platform_invocation_trace.up.sql` | 平台尝试及正式遥测匹配 |
| `016_model_availability.up.sql` | 版本化启停状态与证据 |
| `017_budget_terminal_observation.up.sql` | 预算终态观察 |
| `018_budget_reservation_resolution.up.sql` | 预算预留解析 |
| `019_runtime_display_name.up.sql` | 删除运行时名称的全局唯一约束 `model_runtime_name_key`，不新增元数据字段 |
| `020_failed_publication_spec.up.sql` | 重建发布规格唯一索引，排除 failed、superseded、rolled_back 历史记录，允许相同规格重新发布 |
| `021_usd_budget_reservations.up.sql` | 美元预算、预留及相关费用处理 |
| `022_subscription_tags.up.sql` | 订阅标签 |
| `023_usd_reservation_resolution.up.sql` | 美元预留解析 |
| `024_gateway_usage_attribution.up.sql` | 请求网关归因 |
| `025_usage_cost_component_prices.up.sql` | 替换 `budget_usage_usd_cost` 函数：非估算用量优先使用已存美元费用，否则仅要求非零用量项具有对应价格；估算或缺少必要价格时保持未知，不新增价格列 |
| `026_invocation_trace_service_identity.up.sql` | 服务调用身份追踪 |
| `027_product_schema_alignment.up.sql` | 新增 Application 用量恢复、账本终结状态和 Gateway 创建关联表，对齐预留终结及调用追踪视图 |
| `028_product_index_alignment.up.sql` | 新增预留终结 scope/period 索引和自托管 Gateway 创建请求唯一索引 |

这些迁移只在本地隔离 PostgreSQL 中验证，未对生产数据库执行。
完整 38 项有序清单及各项实际作用见 [迁移清单](upstream-main-merge-2026-10-16.md#migration-inventory)。
各功能说明中的首次引入迁移不是可选子集；当前候选版本应通过统一执行器应用全部待执行迁移，保留已应用版本及校验值。

## 6. 本次合并验证结果

以下引用合并完成时的验证记录；本次日志整理不等于重新执行全部测试。

| 验证项 | 记录结果及范围 |
| --- | --- |
| 最终单元回归 | 3892 通过、7 跳过；109 项本地 PostgreSQL 测试与 137 项 .NET 执行测试另行统计，共 246 项未纳入该轮单元回归 |
| 隔离 PostgreSQL 关键验证 | 8 通过：全新安装、从原分支 18 个迁移升级、数据/校验值保留、重复运行、价格同步并发保护 |
| 前端/部署/目录策略回归 | 298 通过 |
| 编辑表单与模型状态组件 | 53 通过 |
| 发布/连接组件 | 189 通过 |
| 公开价格发布集成用例 | 3 通过 |
| Ruff / Mypy | 通过，Mypy 检查 317 个源文件 |
| TypeScript / Vite | 构建通过，仍有大体积 bundle 警告 |
| OpenAPI | 校验通过，38 个警告 |
| Bicep | 本地编译通过，3 个警告；没有执行 Azure 部署 |
| APIM XML | 语法解析通过 |
| 模拟浏览器 | 1440x1000、390x844 通过；模型目录、打开编辑器、切换手工定价、修改名称；无未捕获异常、未发送保存请求 |
| 迁移文件及 Git 差异 | 两边旧迁移字节一致，`git diff --check` 通过 |

上述测试集合存在覆盖关系，不能相加后宣称为去重测试总数。

未解决的验证边界：

- 本机缺少 .NET SDK，137 项可执行策略测试未验证，不能宣称无筛选完整测试全部通过。
- 独立 Node 总套件最终为 406 通过、80 失败；剩余失败均能在相同运行时/依赖下的原样上游复现。
  原样上游对照为 91 失败。本次不扩展为全面修复上游旧测试断言或测试辅助代码的任务。
- 模拟接口和浏览器检查不构成真实 Azure、APIM、Provider、认证、生产并发或财务结算验收。
- 需要上线时，应另行执行备份、迁移、协议安装、权限、实际链路和回滚验证。

原始汇总：[本地合并与验证记录](upstream-main-merge-2026-10-16.md)。

## 7. 复核方法

```bash
git show --no-patch --format=fuller 7115b4f
git diff --stat 7115b4f^1 7115b4f
git log --reverse --oneline 7115b4f^1..7115b4f^2
git diff --name-status 7115b4f^1 7115b4f -- migrations
git merge-base --is-ancestor 7115b4f^1 7115b4f
git merge-base --is-ancestor 7115b4f^2 7115b4f
```

初次日志整理仅新增此文档，未修改业务代码、改变已完成的合并提交，也未执行推送或发布。

## 8. 迁移说明修正与回归验证

2026-10-16 在迁移一致性复核后修正文档，未修改任何历史迁移 SQL 或应用运行逻辑：

- `docs/testing.md` 新增完整 38 项有序清单，标明来源和实际作用，替换过时的 11 项安装清单。
- 说明定价侧表的 SQL 默认仍为 manual；显式手工配置不变，缺失配置由新读取器按公共目录默认解析，迁移不回填或改写计费价格。
- 部署、E2E 和组织管理说明统一要求执行兼容候选版本的全部待执行迁移，不再按重复数字前缀选择升级子集。
- 补全预算切换的后续 USD、证据和费用计算依赖，保留开关、协议安装及实际验收的独立边界。
- 修正图片生成默认关闭的描述，以及本日志中 012、019、020、025、027、028 的实际 SQL 作用。
- 新增 4 项文档回归测试，并扩展图片开关检查；迁移清单按真实执行器校验数量及顺序。

本次重新执行的验证：

```bash
PATH=/opt/homebrew/opt/postgresql@16/bin:$PATH .venv/bin/python -m pytest -q \
  tests/platform/contracts \
  tests/backend/persistence/test_upstream_merge_migration.py --tb=short
.venv/bin/ruff check tests/platform/contracts/test_schema_documentation.py
.venv/bin/mypy tests/platform/contracts/test_schema_documentation.py
git diff --check
```

结果为 46 项测试通过、无跳过，含独立本地 PostgreSQL 的全新安装和原分支升级验证；
仅一条 FastAPI/Starlette 测试客户端依赖弃用警告。Ruff、Mypy、差异格式检查均通过。
另行逐字节核对原分支 18 个与上游 28 个迁移均未改变，38 个 SQL 文件全部通过
`pglast` PostgreSQL 语法解析；清单来源及日志中的 20 项新增文件与合并父提交核对一致。
本次未重新执行前端、.NET 或完整应用测试，未连接生产数据库、推送、发布或变更 Azure 资源。
