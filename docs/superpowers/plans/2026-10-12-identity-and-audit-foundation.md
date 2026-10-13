# 身份、角色权限、组织与审计基础实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Turnstile 加入服务端 RBAC、用户/部门管理、单企业租户边界和可查询的安全审计，同时兼容现有 Owner/Member 登录。

**Architecture:** 通过追加式 PostgreSQL migration 种入 `owner`、`member` 和 permission mapping；登录会话携带从数据库读取的权限，所有后端授权依赖权限而不是只依赖前端隐藏按钮。安全事件使用独立追加接口写入，UI/API 不提供修改和删除操作。

**Tech Stack:** FastAPI、PostgreSQL、Pydantic、React、TanStack Query、OpenAPI YAML、pytest、Node frontend tests。

**Spec:** [用户、APIM Key、审计与报表设计](../specs/2026-10-12-user-access-billing-audit-design.md)

## Global Constraints

- 首期只启用 `owner`、`member` 两个内置角色；未来可增加角色和权限映射；内置角色不可删除，首期不提供角色编辑器。
- 首期权限代码固定为 `platform.manage`、`users.read`、`users.manage`、`org.read`、`org.manage`、`roles.read`、`applications.read.all`、`applications.read.own`、`keys.manage.all`、`keys.manage.own`、`usage.read.all`、`usage.read.own`、`reports.create.all`、`reports.create.own`、`security_audit.read`；Owner 得到全部，Member 只得到本人范围的读取/Key/报表权限。
- 首期单企业，数据库记录必须带稳定 tenant 边界；不实现跨租户登录或租户管理员。
- 不开放匿名密码注册；保留现有 Entra 登录和密码登录；管理员预建目录项不得在 Entra 首次登录时被重置角色、部门或停用状态。
- Member 只能读取本人/本人应用的资料、用量和 Key；所有授权必须由后端执行。
- 审计保留 365 天；不写请求/响应正文、密码、会话 Cookie、Bearer/API Key 或原始凭证；审计时间使用 UTC。
- 现有 `owner/member` 用户和会话必须兼容；迁移不得重写或删除历史用量记录。

## Review Focus

- Entra 同邮箱登录不得覆盖管理员预先指定的 Owner/Member、部门或禁用状态；由 Task 2 的 `test_entra_login_preserves_preprovisioned_access_state` 验证。
- 已禁用用户即使持有尚未过期的 Cookie 也必须立即收到 401；由 Task 2 的 `test_disabled_user_session_is_rejected_immediately` 验证。
- Member 通过直接构造 API 请求不能读写其他用户、角色或部门；由 Task 2 的 `test_member_cannot_access_admin_user_or_audit_routes` 验证。
- 不可信 `X-Forwarded-For` 前缀不能伪造审计源 IP；由 Task 3 的 `test_audit_ignores_untrusted_forwarded_for` 验证。
- 任意失败/成功审计事件中都不能出现密码、Cookie、Authorization、API Key 或请求正文；由 Task 3 的 `test_audit_allowlist_never_persists_secrets_or_bodies` 验证。

---

### Task 1: 追加角色、权限、租户、部门和用户归属结构

**Files:**
- Create: `migrations/009_user_access_and_audit_foundation.up.sql`
- Modify: `tests/platform/contracts/test_migrations.py`
- Modify: `tests/platform/infrastructure/test_infrastructure.py`

**Interfaces:**
- Produces: 数据库目录 `app_role(code, display_name, is_builtin)`、`app_permission(code, description)`、`app_role_permission(role_code, permission_code)`、单根 `tenant` 和有父子关系的 `organization_unit`。
- `app_user.role` 保持现有 `owner/member` 读取语义，但移除硬编码 CHECK 并增加到 `app_role.code` 的外键；`app_user` 增加 `tenant_id NOT NULL` 和 nullable `organization_unit_id`，既有用户归入唯一根 tenant。
- `gateway_application` 增加 `tenant_id NOT NULL` 和 nullable `owner_user_id`；既有应用归入唯一根 tenant，并通过大小写归一后的已有 `owner_id=email` 与 `app_user.email` 匹配可确认的旧归属；匹配不到的 user owner 保持 NULL，不猜测归属。
- 新增 `security_audit_event`：事件 UUID、UTC 时间、tenant、操作者 ID/email/role 快照、action、resource type/id、result、request/correlation ID、client IP、User-Agent、白名单 before/after JSON。

- [ ] **Step 1: 先扩展 migration 链测试**，验证将按序新增 `009_user_access_and_audit_foundation.up.sql`、旧角色记录映射成功、旧未匹配 Application 保持未分配，并断言 migration 不更新或删除 `token_usage`。
- [ ] **Step 2: 运行 `uv run pytest tests/platform/contracts/test_migrations.py tests/platform/infrastructure/test_infrastructure.py -q`**；新链断言应先失败。
- [ ] **Step 3: 编写 migration**，种入 `owner/member` 与所需权限；建立确定性的单根 tenant；保留既有 `role` 列语义并加外键；不给旧记录编造归属。
- [ ] **Step 4: 再运行同一测试命令**，确认 migration 顺序、向后兼容和 append-only 历史事实约束通过。

### Task 2: 服务端权限、用户和组织管理 API

**Files:**
- Create: `turnstile_core/domain/access_control.py`
- Create: `turnstile_core/services/access_control.py`
- Create: `turnstile_core/persistence/access_control_store.py`
- Modify: `turnstile_core/persistence/auth_store.py`
- Modify: `backend/http/session.py`
- Modify: `backend/http/authentication.py`
- Modify: `backend/http/observability.py`
- Create: `backend/http/user_management.py`
- Modify: `backend/api.py`
- Create: `tests/platform/api/test_user_management_api.py`
- Modify: `tests/platform/api/test_api_analytics.py`
- Modify: `tests/platform/api/api_support.py`
- Modify: `contracts/openapi/paths/authentication.yaml`
- Create: `contracts/openapi/paths/user-management.yaml`
- Create: `contracts/openapi/schemas/user-management.yaml`
- Modify: `contracts/openapi.yaml`
- Modify: `tests/platform/contracts/test_openapi.py`

**Interfaces:**
- `SessionIdentity` 增加 `permissions: frozenset[str]`，且 role 类型改为字符串；新增 `require_permission(code: str)` FastAPI dependency，现有 `OwnerSession` 兼容别名依赖 `platform.manage`，不直接硬编码角色判断。
- `GET /api/v1/auth/me` 返回当前用户 role code 与有效权限代码。
- `GET /api/v1/admin/users`（搜索、role、enabled、department 过滤）、`POST /api/v1/admin/users`（email、display name、role、department、optional initial password）、`PATCH /api/v1/admin/users/{user_id}`（启停、角色、部门、显示名）。不传密码即预建 Entra 目录项；传入密码则通过现有 `hash_password` 存储并由管理员在 Turnstile 外安全交付；响应和审计事件都不回显密码。现有密码账号的登录路径不变。
- `GET /api/v1/admin/organization-units`、`POST /api/v1/admin/organization-units`、`PATCH /api/v1/admin/organization-units/{id}`、`DELETE /api/v1/admin/organization-units/{id}`；不能删除仍有用户或子部门的节点。
- `GET /api/v1/admin/roles` 只读返回 Owner/Member 的权限矩阵；不暴露首期角色增删/编辑端点。
- `AccessControlStore.transaction() -> ContextManager[Connection]`、`update_user(connection, user_id, patch) -> dict | None` 和 `update_organization_unit(connection, unit_id, patch) -> dict | None`：服务层在同一连接/事务中更新目标、撤销相关会话并插入审计事件。
- 同一 PostgreSQL 事务内提交的用户/组织管理写入也必须提交对应审计事件；审计插入失败则回滚管理写入。登录/登出等没有业务行变更的事件单独追加，日志错误中不得包含凭证。
- 新增 `scope_usage_filters(identity, filters) -> UsageFilters`：Owner 原样使用允许的全企业筛选；Member 必须匹配本人 `user_id`，其它值返回 403；`backend/http/observability.py` 的 overview、distribution、requests、detail、trends、anomalies 和同步兼容 export 都使用该范围。

- [ ] **Step 1: 写 API 测试**：权限拒绝、最后一个启用 Owner 不可被禁用/降权、Entra 对预建账户只更新允许更新的显示名、初始密码以 hash 存储且不出现在响应/审计中、禁用后 Cookie 立刻失效、部门非空不可删除、Member 不能通过 query 参数读取别人的 usage、未认证为 401。
- [ ] **Step 2: 运行 `uv run pytest tests/platform/api/test_user_management_api.py tests/platform/api/test_auth_session.py tests/platform/api/test_api_analytics.py -q`**；确认新增行为缺失时失败。
- [ ] **Step 3: 实现 permission lookup 和上述 API**；查询权限不得依赖 UI 声明；邮箱规范为小写；初始密码仅调用现有 hash routine 并从输出/审计 allowlist 排除；Owner 更新使用事务检查“至少一个启用 Owner”。
- [ ] **Step 4: 运行 `uv run pytest tests/platform/api/test_user_management_api.py tests/platform/api/test_auth_session.py tests/platform/api/test_api_analytics.py tests/platform/contracts/test_openapi.py -q`**，确认响应和 OpenAPI 路径一致。

### Task 3: 安全审计写入、查询、留存与 UI

**Files:**
- Create: `turnstile_core/domain/security_audit.py`
- Create: `turnstile_core/persistence/audit_store.py`
- Create: `turnstile_core/services/security_audit.py`
- Modify: `backend/http/authentication.py`
- Modify: `backend/http/user_management.py`
- Modify: `turnstile_core/config.py`
- Modify: `infra/modules/data-plane.bicep`
- Modify: `tests/platform/infrastructure/test_infrastructure.py`
- Create: `backend/http/security_audit.py`
- Modify: `backend/api.py`
- Modify: `functions/telemetry/function_app.py`
- Modify: `frontend/src/app.tsx`
- Modify: `frontend/src/data-sources/apim/source.tsx`
- Create: `frontend/src/data-sources/apim/security-audit.ts`
- Create: `frontend/src/pages/security-audit-page.tsx`
- Create: `contracts/openapi/paths/security-audit.yaml`
- Create: `contracts/openapi/schemas/security-audit.yaml`
- Modify: `contracts/openapi.yaml`
- Create: `tests/platform/api/test_security_audit_api.py`
- Create: `tests/backend/security/test_security_audit.py`
- Modify: `tests/platform/deployment/test_function_app.py`

**Interfaces:**
- `AuditEventWrite(action, result, resource_type, resource_id, actor_id, actor_email, actor_role, request_id, client_ip, user_agent, before, after)`；`SecurityAuditService.record(event, connection: Connection | None = None)` 和 `SecurityAuditService.list_events(filters, cursor, limit)`。提供 connection 时，审计 insert 不自行 commit。
- 仅 Owner 可访问 `GET /api/v1/security/audit-events?from=&to=&actor_id=&action=&result=&cursor=&limit=`；`GET /api/v1/security/audit-events/export` 导出筛选后的元数据 CSV；没有 PATCH/DELETE。
- 新增 `TRUST_AZURE_ARR_CLIENT_IP`（App Service Bicep 设置为 `true`）与 `TRUSTED_PROXY_CIDRS`（默认空）；仅显式启用 Azure App Service 信任时读取平台覆盖的 `X-ARR-ClientIP`，其他反向代理只有 peer IP 命中配置 CIDR 才解析转发链；否则回退 socket peer IP。任意客户端 `X-Forwarded-For` 不可信。
- 记录成功/失败登录、登出、用户和部门变更、禁用/权限拒绝；后续 Key 与导出计划调用相同 `record` 接口。
- Telemetry Function 每日维护保留策略，仅清理 `created_at < now() - 365 days` 的在线审计事件；管理 API 不提供清理能力。文档必须说明这不是防篡改归档或等保三级认证。

- [ ] **Step 1: 写审计服务/路由测试**，验证 Owner 过滤查询、Member/未登录拒绝、可信 IP 解析、JSON 白名单和正文/密钥不入库；加入 365 天边界清理测试。
- [ ] **Step 2: 运行 `uv run pytest tests/platform/api/test_security_audit_api.py tests/backend/security/test_security_audit.py -q`**；新测试先失败。
- [ ] **Step 3: 实现事务性审计写入与查询**；失败登录在认证失败路径写入不含凭证的事件；用户/部门管理成功动作与目标变更在同一 PostgreSQL 事务内提交。审计写入失败时回滚敏感管理操作；认证事件日志失败只输出无凭证的错误码。
- [ ] **Step 4: 实现审计路由和每天 01:00 UTC 的 timer 清理**；更新 Function metadata test，断言注册的函数从 4 个增至 5 个且审计清理为 Timer Trigger；UI 只展示白名单元数据并提供时间、操作者、动作、结果筛选和导出。
- [ ] **Step 5: 运行 `uv run pytest tests/platform/api/test_security_audit_api.py tests/backend/security/test_security_audit.py tests/platform/deployment/test_function_app.py tests/platform/infrastructure/test_infrastructure.py tests/platform/contracts/test_openapi.py -q`**，确认可查询、可信来源 IP、保留任务已注册且合同完整。

### Task 4: 用户管理与审计页面接线

**Files:**
- Create: `frontend/src/data-sources/apim/user-management.ts`
- Create: `frontend/src/pages/user-management-page.tsx`
- Modify: `frontend/src/app.tsx`
- Modify: `frontend/src/data-sources/apim/source.tsx`
- Modify: `frontend/src/providers/auth-provider.tsx`
- Modify: `frontend/src/locales/en/phrases-core.ts`
- Modify: `frontend/src/locales/ja/phrases-core.ts`
- Modify: `frontend/src/locales/ko/phrases-core.ts`
- Modify: `tests/platform/frontend/test_locale_coverage.py`
- Create: `tests/platform/frontend/test_user_management_source.py`

- [ ] **Step 1: 写前端源代码测试**，断言 Owner 菜单包含用户/组织管理和审计；Member 不显示管理入口；按钮操作请求权限保护 API；用户列表覆盖空、加载、403、成功状态。
- [ ] **Step 2: 运行 `uv run pytest tests/platform/frontend/test_user_management_source.py tests/platform/frontend/test_locale_coverage.py -q`**，先确认缺页面/文案时失败。
- [ ] **Step 3: 创建 Users/Organizations 与 Roles 展示页**，Owner 可搜索、预建 Entra 用户或输入初始密码创建本地账号、更新/停用用户并维护部门树；Roles tab 只读展示两内置角色权限；Member 只显示其可访问的个人入口。
- [ ] **Step 4: 接入 Audit 页面、权限导航和英文/日文/韩文文案**；所有安全边界仍由后端执行。
- [ ] **Step 5: 运行 `npm --prefix frontend run build` 与 `uv run pytest tests/platform/frontend/test_user_management_source.py tests/platform/frontend/test_locale_coverage.py -q`**，确认类型和语言覆盖通过。

## 结束条件

运行：`uv run pytest tests/platform/contracts/test_migrations.py tests/platform/contracts/test_openapi.py tests/platform/api/test_user_management_api.py tests/platform/api/test_auth_session.py tests/platform/api/test_api_analytics.py tests/platform/api/test_security_audit_api.py tests/backend/security/test_security_audit.py tests/platform/infrastructure/test_infrastructure.py -q`。

预期：migration 链追加成功；用户访问、登录兼容、审计权限/脱敏/留存和 OpenAPI 测试全部通过；`npm --prefix frontend run build` 成功。

