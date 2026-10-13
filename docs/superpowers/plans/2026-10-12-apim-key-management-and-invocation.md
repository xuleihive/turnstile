# APIM Key 管理与 Turnstile 直连调用实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在 Turnstile 中创建、查看、轮换和停用 APIM Subscription Key，并允许客户端携带 APIM Key 直接调用 Turnstile 的 `/api/v1/model-gateway/invoke`。

**Architecture:** APIM 是唯一 Key 签发、校验、策略和计量系统；Turnstile 经现有 ARM 身份创建无显式密钥值的 APIM Subscription，通过 `listSecrets` 临时揭示 Key。直连模型调用会把请求头原样转发到 APIM，并让 APIM 根据已验证 Subscription 与 Turnstile 归属投影计算应用/用户归属。

**Tech Stack:** FastAPI、Azure APIM ARM client、APIM policy/Bicep、PostgreSQL application access store、Azure Table admission map、React、OpenAPI、pytest。

**Spec:** [用户、APIM Key、审计与报表设计](../specs/2026-10-12-user-access-billing-audit-design.md)

## Global Constraints

- 只使用 APIM Subscription Key；不创建 Turnstile 原生 Key 表，不保存 APIM Key 明文或 SHA-256 摘要。
- 请求认证头固定为 `Ocp-Apim-Subscription-Key`；禁止 URL/query 参数传 Key。
- Member 只能管理自己的 Application/Subscription；Owner 可代表其他用户管理；系统级 Subscription 仍不可编辑。
- Key 揭示响应必须 `Cache-Control: no-store`；操作状态、审计、日志、数据库不含 Key。
- APIM 的 Subscription 才是计量归属；客户端 metadata 中的 `user_id` 不可信，不能作为 API Key 调用身份或费用归属。
- 无效 Key 必须由 APIM 返回 401；绝不能退回 `APIM_DASHBOARD_SUBSCRIPTION_KEY`。

## Review Focus

- 新建 Subscription 请求不应带 Turnstile 生成的 `primaryKey`/`secondaryKey`；由 Task 1 的 ARM client test 验证。
- APIM Key 直连必须穿过 APIM 策略和账本；无效 Key 不能回退 dashboard credential；由 Task 2 的 runtime/API 集成测试验证。
- 客户端伪造 `x-user-id` 不能影响 APIM usage 用户归属；只有经 APIM 验证的 `context.Subscription.Id` 映射能提供可信 owner；由 Task 2 的 APIM policy/attribution test 验证。
- Member 对其他用户的应用、Subscription、revealed Key 或 export 请求全部拒绝；由 Task 3 的 API 测试验证。
- 用户停用/Subscription 取消/归属未分配时，请求不能被错误计费到其他用户；由 Task 2 的 admission test 验证。

---

### Task 1: 改造 APIM Subscription 创建，不再由 Turnstile 产生 Key

**Files:**
- Modify: `turnstile_core/domain/control_plane.py`
- Modify: `turnstile_core/integrations/apim_control_plane_contract.py`
- Modify: `turnstile_core/integrations/apim_publisher_client.py`
- Modify: `turnstile_core/services/control_plane.py`
- Modify: `turnstile_core/services/gateway_release_operation_worker.py`
- Modify: `tests/backend/model_platform/test_application_provisioning.py`
- Modify: `tests/backend/model_platform/test_gateway_releases.py`
- Modify: `tests/platform/infrastructure/test_apim_policy.py`

**Interfaces:**
- `ApimPublisherClient.ensure_application_subscription(spec) -> None` 创建 Subscription 时不发送 `primaryKey`/`secondaryKey`；发布操作的 `semantic_preview`、checkpoint 不包含秘密。
- 不再在 `GatewayControlPlaneService.request_application_subscription_provision` 生成随机 Key、加密存入 `gateway_release_operation_secret` 或在 `202` 响应中返回 Key。
- `gateway_release_operation_secret` 仍供其他发布操作使用；改造仅移除 `application_provision` 新任务的 Key payload。Worker 将旧的 Application Provision operation 按 APIM Subscription 状态恢复，并在该 operation 进入终态后删除其旧加密 Key payload；其它 operation secret 不动。
- 稳定 `apim_subscription_id` 和操作状态用于幂等重试；APIM 已有同 ID 但 scope/displayName/state 不匹配时返回冲突，不能覆盖其他 Subscription。
- Key 只在现有 API 通过 `AzureApimSubscriptionKeyClient.application_subscription_keys(apim_subscription_id)` 调用 `listSecrets` 时短暂读取。

- [x] **Step 1: 扩展 publisher contract tests**，断言 create body 缺少两个 key 属性、重试读取匹配资源不重复创建、同 ID 不匹配资源失败、操作 response/secret store 无 key 字符串。
- [x] **Step 2: 运行 `uv run pytest tests/backend/model_platform/test_application_provisioning.py tests/backend/model_platform/test_gateway_releases.py -q`**，新断言先失败。
- [x] **Step 3: 改造发布操作和 ARM publisher**；以 APIM 返回资源状态作为重试依据，移除依赖本地 Key 的 staged display name 标记和验证分支；保留 scope、Product、APIM policy 和 subscription 状态 readback。
- [x] **Step 4: 运行同一组测试**，确认重试仍幂等且发布完成状态不泄漏 Key。

### Task 2: Key 调用入口、安全归属与 APIM policy

**Files:**
- Modify: `backend/http/session.py`
- Modify: `backend/http/model_platform.py`
- Modify: `backend/api.py`
- Modify: `backend/services/runtime_service.py`
- Modify: `turnstile_core/integrations/gateway.py`
- Modify: `contracts/openapi/paths/model-platform.yaml`
- Modify: `contracts/openapi/schemas/model-platform.yaml`
- Modify: `contracts/openapi.yaml`
- Modify: `tests/platform/contracts/test_openapi.py`
- Modify: `infra/policies/foundry-finops-policy.xml`
- Modify: `infra/modules/apim-integration.bicep`
- Modify: `turnstile_core/persistence/repository_applications.py`
- Modify: `turnstile_core/persistence/in_memory_applications.py`
- Modify: `turnstile_core/services/application_access.py`
- Modify: `turnstile_core/persistence/repository_contract.py`
- Modify: `tests/backend/model_platform/test_model_runtime.py`
- Modify: `tests/platform/api/test_api_access.py`
- Modify: `tests/platform/infrastructure/test_apim_policy.py`
- Modify: `tests/platform/deployment/test_apim_upgrade.py`

**Interfaces:**
- 新增可选会话 resolver：`optional_authenticated_session(request, store, settings) -> SessionIdentity | None`。唯一模型调用路由由独立 router 注册，依赖 Origin 校验但不强制登录；其它管理/API 路由仍强制会话。
- `invoke_model(..., identity: SessionIdentity | None, apim_subscription_key: str | None = Header(alias="Ocp-Apim-Subscription-Key"))`：会话路径沿用现有 model/user policy 和 dashboard credential；Key 路径要求配置的 APIM gateway 与 Subscription admission map 存在。
- `ModelRuntimeService.invoke(..., gateway_credential_override: str | None = None, enforce_user_model_access: bool = True, enforce_local_user_budget: bool = True)`：override 只供本次调用使用；Key 路径传入两个 `enforce_*` false，由 APIM 执行模型准入与 application budget，不运行客户端 user-id 本地判断，也不触发 dashboard key fallback。
- 只要设置 `gateway_credential_override`，OpenAI-compatible APIM adapter 就必须把它写入固定 header `Ocp-Apim-Subscription-Key`，不得沿用 provider 的普通 `api-key` header 名，也不得重复带其它凭证。
- APIM application map 增加 `owner_user_id`、组织/部门快照；APIM policy 在 `context.Subscription != null` 后用已验证 Subscription ID 查该映射并覆盖 `x-user-id`/`x-user-name`，映射缺失或停用时 fail closed。不得信任请求传入的 `x-user-id`。
- 普通 APIM error status（包括 401）按现有 runtime 行为传播；Turnstile 自己不保存或比对 APIM Key。

- [x] **Step 1: 写 invocation tests**：缺 Key 且无会话为 401；带无效 Key 时 adapter 收到的出站 header 与输入完全一致且 401 原样传回；配置 Dashboard Key 时仍不发生 fallback；Key 调用客户端伪造 user metadata 不改变 admission owner；没有归属投影时 fail closed。
- [x] **Step 2: 运行 `uv run pytest tests/backend/model_platform/test_model_runtime.py tests/platform/api/test_api_access.py tests/platform/infrastructure/test_apim_policy.py -q`**，新路径先失败。
- [x] **Step 3: 实现双认证入口和一次性 outbound credential override**；只把 `Ocp-Apim-Subscription-Key` 放到 APIM 请求 header，不记录、不持久化、不添加到错误信息；会话路径保持现有行为。
- [x] **Step 4: 更新 APIM 归属投影和 policy**，将 authenticated `context.Subscription.Id` 作为唯一 subscription identity，验证 Active/owner/tenant 映射后覆盖用户归属 header；拒绝未分配 subscription。
- [x] **Step 5: 更新 OpenAPI 安全说明和 header 参数，并同步更新 contract test 的 path/schema 数量预期；运行 `uv run pytest tests/backend/model_platform/test_model_runtime.py tests/platform/api/test_api_access.py tests/platform/infrastructure/test_apim_policy.py tests/platform/deployment/test_apim_upgrade.py tests/platform/contracts/test_openapi.py -q`**，确认匿名拒绝、Session 兼容、Key 转发、fail-closed 与策略快照测试通过。

### Task 3: 自助/管理员 Key 生命周期和管理页面

**Files:**
- Modify: `backend/http/application_access.py`
- Modify: `turnstile_core/services/application_access.py`
- Modify: `turnstile_core/domain/application_access.py`
- Modify: `turnstile_core/persistence/repository_contract.py`
- Modify: `turnstile_core/persistence/repository_applications.py`
- Modify: `turnstile_core/persistence/in_memory_applications.py`
- Modify: `turnstile_core/integrations/ledger.py`
- Modify: `turnstile_core/integrations/apim_subscription_key_client.py`
- Modify: `tests/platform/api/test_application_access_api.py`
- Modify: `tests/backend/model_platform/test_application_access.py`
- Modify: `contracts/openapi/paths/application-access.yaml`
- Modify: `contracts/openapi/schemas/application-access.yaml`
- Modify: `contracts/openapi.yaml`
- Create: `frontend/src/data-sources/apim/user-api-keys.ts`
- Create: `frontend/src/pages/my-api-keys-page.tsx`
- Modify: `frontend/src/pages/applications-page.tsx`
- Modify: `frontend/src/app.tsx`
- Modify: `frontend/src/data-sources/apim/source.tsx`
- Modify: `frontend/src/locales/en/phrases-core.ts`
- Modify: `frontend/src/locales/ja/phrases-core.ts`
- Modify: `frontend/src/locales/ko/phrases-core.ts`
- Modify: `tests/platform/frontend/test_application_access_source.py`

**Interfaces:**
- 新增 `POST /api/v1/application-access/me/subscriptions`：服务器生成不冲突 APIM Subscription ID，关联当前 `owner_user_id` 和用户 application，返回 202 operation，不返回 Key；`GET /api/v1/application-access/me/subscriptions` 仅返回本人资源。
- `POST /api/v1/admin/users/{user_id}/subscriptions` 和 `GET /api/v1/admin/users/{user_id}/subscriptions` 仅 Owner 可操作；Owner 可替用户创建并查看 Subscription。
- Owner 认领历史未分配 application 使用 `PUT /api/v1/application-access/applications/{application_id}/owner` body `{user_id: UUID | null}`；只改变后续归属，不重写历史 usage，并生成 application audit 与 security audit 事件。
- Owner 的批量导出路由为 `POST /api/v1/admin/subscriptions/keys/export`，可选 body 字段为 `user_ids`、`department_id`、`subscription_ids`；空筛选表示全企业。最多导出 1000 个 APIM Subscription（与部署中 Product 上限相同），管理平面 `listSecrets` 并发上限为 8；结果 CSV 含 user id/email/role、application、subscription id、primary/secondary key 和创建时间。
- 现有 reveal/rotate 路由改为基于 `owner_user_id` 检查：Owner 可管理全体，Member 只管理本人。新增 Owner-only Subscription disable/cancel 接口并同步 APIM；取消后的 key 不再被策略接受。
- 批量导出 API 为 Owner 专属，仅在请求时逐个调用 APIM `listSecrets`，响应 `no-store`，不给后台任务或持久化层保存 Key；超过 1000 条要求通过筛选分批导出。
- Key 页展示一次性 reveal 状态、备用 Key 轮换步骤、APIM Subscription 状态与用户归属；Key 由浏览器显式显示/下载，不缓存到 TanStack Query persistence。
- 自助或代建操作成功后，页面自动请求一次 `primary` reveal 并在当前交互中显示，保持用户一键创建并拿到 Key 的流程；后续揭示仍需明确点击。新建响应本身不携带 Key。
- 所有 create/reveal/rotate/cancel/admin export 操作调用计划 1 的 `SecurityAuditService.record`，action 记录 Key 类型与 Subscription ID，不写 Key 值。
- APIM ARM 调用与 PostgreSQL 不可原子提交：先写 `requested` 审计事件，再写 APIM 结果 `succeeded/failed` 事件，以 release operation/request ID 关联；审计错误不得把 Key/凭证写入错误日志。

- [x] **Step 1: 写 service/API tests**，覆盖自助创建、Owner 代建、Owner 显式认领未分配 application、Member 跨用户读/写/reveal 拒绝、Key reveal `no-store`、rotate/cancel 走 APIM client、批量导出筛选/1000 上限/最多 8 个并发且不落盘、APIM 403 错误不泄漏响应秘密，并验证每个敏感动作有审计元数据。
- [x] **Step 2: 运行 `uv run pytest tests/platform/api/test_application_access_api.py tests/backend/model_platform/test_application_access.py -q`**，测试先失败。
- [x] **Step 3: 实现用户到 Application 归属约束和 API**；生成 ID 的服务端逻辑不得允许调用方指定已有用户 ID；旧归属未知时仅 Owner 显式认领并留下审计事件。
- [x] **Step 4: 实现 APIM create/reveal/rotate/cancel 和 no-store 批量导出**；创建只返回 operation，等待 APIM 资源 ready 后才允许 reveal；错误响应经白名单转换。
- [x] **Step 5: 接入“我的 API Key”页面与管理页 Owner 功能**；应用 id、订阅 id、角色、创建人等关联关系可查询；Member 看不到其他用户对象。
- [x] **Step 6: 运行 `uv run pytest tests/platform/api/test_application_access_api.py tests/backend/model_platform/test_application_access.py tests/platform/contracts/test_openapi.py tests/platform/frontend/test_application_access_source.py -q` 和 `npm --prefix frontend run build`**。

## 结束条件

运行：`uv run pytest tests/backend/model_platform/test_application_provisioning.py tests/backend/model_platform/test_gateway_releases.py tests/backend/model_platform/test_model_runtime.py tests/platform/api/test_application_access_api.py tests/platform/api/test_api_access.py tests/platform/infrastructure/test_apim_policy.py tests/platform/deployment/test_apim_upgrade.py tests/platform/contracts/test_openapi.py -q`，并运行 `npm --prefix frontend run build`。

预期：所有创建、权限、凭证转发、策略、契约测试通过；无 Key 明文或摘要落入数据库；生产配置下不存在 Key 路径回退 dashboard credential。

