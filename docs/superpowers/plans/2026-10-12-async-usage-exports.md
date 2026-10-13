# 异步分析报告与明细导出实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将大型请求分析报告和 CSV 导出改为按时间范围提交的异步任务，完成后由已授权用户从私有 Blob 下载。

**Architecture:** PostgreSQL 保存筛选快照、任务状态、租约和过期时间；Telemetry Azure Function 以 `SKIP LOCKED` 领取可重试任务，用现有数据库游标分批读取请求行，生成分析 Markdown 与 CSV ZIP 并上传到私有 Blob。API 使用会话检查用户范围后代理 Blob 下载，不暴露 SAS URL。

**Tech Stack:** PostgreSQL、FastAPI、现有 `QueryRepository.iter_usage_requests`、Azure Functions Timer Trigger、Azure Blob Storage SDK/Managed Identity、React、TanStack Query、OpenAPI YAML、pytest。

**Spec:** [用户、APIM Key、审计与报表设计](../specs/2026-10-12-user-access-billing-audit-design.md)

## Global Constraints

- 时间范围是半开区间 `[from,to)`；保留现有组织、部门、项目、智能体、模型、用户、Runtime、状态筛选。
- 输出为 `analysis.md` + `requests.csv` ZIP；CSV 只含已记录元数据、请求/关联 ID、token、状态、延迟、估算费用、准入与对账字段。
- 不导出 prompt、completion、messages、模型回复、密码、Cookie、Authorization 或 API Key；成本标明为价格目录估算值，不是 Azure 账单。
- 延续现有 CSV 列、数据库游标 `fetchmany(1000)` 和 `= + - @` 公式注入保护。
- Owner 可导出全企业；Member 的过滤范围由后端强制限制到本人/本人应用；前端过滤器不是授权边界。
- Blob 容器必须私有，默认 7 天过期；只能通过会话授权下载，不返回长期 SAS。

## Review Focus

- `from >= to`、超过最大区间、筛选不合法和超过导出行数须在任务入队前返回 422/413；由 Task 1 API tests 验证。
- 两个 Function 实例不能同时领取同一 job；worker crash 后租约过期可重试且已上传文件可安全重建；由 Task 2 concurrency tests 验证。
- Member 不能探测/下载别人的 Job ID 或跨用户导出；由 Task 3 API tests 验证。
- CSV 公式载荷（含前置空白的 `=`, `+`, `-`, `@`）保持为文本；由 Task 2 的 CSV tests 验证。
- Blob 缺失、下载过期、审计数据库或存储错误不能暴露内部路径、凭证、SAS、请求正文；由 Task 2/3 的 failure tests 验证。

---

### Task 1: 导出任务状态表、过滤校验与 API

**Files:**
- Create: `migrations/011_usage_export_jobs.up.sql`
- Modify: `tests/platform/contracts/test_migrations.py`
- Modify: `turnstile_core/config.py`
- Create: `turnstile_core/domain/usage_export.py`
- Create: `turnstile_core/persistence/export_jobs.py`
- Create: `turnstile_core/services/usage_export_jobs.py`
- Modify: `backend/http/observability.py`
- Modify: `backend/api.py`
- Create: `tests/platform/api/test_usage_export_jobs_api.py`
- Create: `tests/backend/persistence/test_export_jobs.py`
- Create: `contracts/openapi/paths/usage-exports.yaml`
- Create: `contracts/openapi/schemas/usage-exports.yaml`
- Modify: `contracts/openapi.yaml`
- Modify: `tests/platform/contracts/test_openapi.py`

**Interfaces:**
- `usage_export_job`: UUID、creator user ID、tenant、UTC time range、JSON filter snapshot、状态 `queued/running/ready/failed/expired`、attempt count、lease owner/until、row_count、private blob name、expires_at、sanitized error code、created/updated timestamps。
- 初始限额配置：`USAGE_EXPORT_MAX_RANGE_DAYS=366`、`USAGE_EXPORT_MAX_ROWS=1000000`、`USAGE_EXPORT_MAX_ACTIVE_PER_USER=2`、`USAGE_EXPORT_RETENTION_DAYS=7`；全局值可由 Settings 配置覆盖。
- `POST /api/v1/observability/export-jobs` 返回 202 与 job/status URL；`GET /api/v1/observability/export-jobs` 列出当前用户可见任务；`GET /api/v1/observability/export-jobs/{job_id}` 返回状态；Member 的 job 只能由其创建者读取，Owner 可查看全企业。
- 时间使用 `[from,to)`；重复 POST 以 `Idempotency-Key` 防止浏览器重试创建重复作业。下载路由在 Task 3 添加。
- 所有 job 过滤和行查询使用身份计划提供的 `scope_usage_filters(identity, filters)`；不在请求内生成整份 ZIP。

- [x] **Step 1: 写任务表/服务/API 测试**，验证半开区间、时间顺序、366 天边界、并发数量、成员范围覆盖、Idempotency-Key 去重、对他人 Job 读回 404。
- [x] **Step 2: 运行 `uv run pytest tests/backend/persistence/test_export_jobs.py tests/platform/api/test_usage_export_jobs_api.py tests/platform/contracts/test_migrations.py -q`**，新增测试应先失败。
- [x] **Step 3: 新增 migration 和任务服务**；迁移追加在当前最新迁移之后，创建必要状态索引；任务查询/状态变更按用户范围和 tenant 条件执行。
- [x] **Step 4: 实现 API 入队与校验**；入队前验证筛选、范围、成员权限和活跃任务限额；响应只含状态和 ID。
- [x] **Step 5: 运行同一测试命令及 `uv run pytest tests/platform/contracts/test_openapi.py -q`**，确认 schema、接口和授权正确。

### Task 2: Function 租约工作器、CSV ZIP 和私有 Blob 存储

**Files:**
- Modify: `backend/services/usage_export.py`
- Create: `turnstile_core/services/usage_export_worker.py`
- Create: `turnstile_core/integrations/usage_export_blob.py`
- Modify: `functions/telemetry/function_app.py`
- Modify: `functions/telemetry/requirements.txt`
- Modify: `requirements.txt`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `turnstile_core/config.py`
- Modify: `infra/modules/data-plane.bicep`
- Modify: `tests/platform/deployment/test_function_app.py`
- Modify: `tests/platform/infrastructure/test_infrastructure.py`
- Modify: `tests/backend/persistence/test_export_jobs.py`
- Create: `tests/backend/services/test_usage_export_worker.py`
- Modify: `tests/platform/api/test_api_analytics.py`
- Modify: `tests/platform/deployment/test_deployment_artifacts.py`

**Interfaces:**
- `UsageExportWorker.run_once(worker_id: str, now: datetime) -> int` 用 PostgreSQL `FOR UPDATE SKIP LOCKED` 领取最多 1 个任务；租约 10 分钟，最多 3 次尝试；过期租约任务可重新入队。
- 数据库游标每处理一批 1000 行就续租；续租失败的 worker 停止生成/上传，不得将 Job 标成 ready。Blob 使用确定性 job 路径；worker 在上传成功、提交 ready 状态前崩溃时，重试可安全覆盖同一 job Blob。
- worker 从 `QueryRepository.iter_usage_requests` 用数据库游标每批 1000 行读取；摘要沿用当前汇总、分布、错误率和延迟分位数；详情仍通过 `CSV_COLUMNS` 写 `requests.csv`。
- ZIP 在 `SpooledTemporaryFile(max_size=8 * 1024 * 1024)` 上生成，超出内存阈值写本地临时盘；每行/列依旧过 `_safe_csv_value`；超过 1,000,000 行将 Job 标为 failed 并删除临时 Blob，不生成部分下载。
- `PrivateUsageExportBlob` 使用 Managed Identity `DefaultAzureCredential`；容器 `turnstile-usage-exports` 关闭 anonymous access；Function 只可写入、API 只读；Blob name 使用不可预测的 tenant/job UUID，不含邮箱或筛选字段。
- 依赖清单固定并同步 `azure-identity` 与 `azure-storage-blob` 到根 `pyproject.toml`、根 `requirements.txt`、`functions/telemetry/requirements.txt` 和 `uv.lock`，保证 API 与 Function 部署包都具有 Managed Identity 与 Blob SDK。
- 导出文件使用独立私有 Storage Account、Blob Private Endpoint 和现有私有 DNS 区；这样不会被 Function 运行时在宿主 Storage Account 上的 Blob Data Owner 权限扩大读取范围。Function Timer 使用 UTC cron `0 * * * * *` 每分钟轮询作业并清理 7 天过期 Blob，保留过期任务元数据供状态 API 返回 `expired`。
- 创建、完成、失败、过期和下载事件均调用计划 1 的 `SecurityAuditService.record`；事件只写 job ID、行数、状态和筛选摘要，不写 CSV 内容。

- [ ] **Step 1: 写 worker 测试**：同一 job 只能被一个 worker lease；续租丢失后停止处理；崩溃后租约过期重试；上传后提交状态前崩溃可覆盖重试；连续失败三次后进入 failed；成功会将 analysis+csv ZIP 上传并进入 ready；7 天后过期；公式注入安全；超行数失败且无部分 blob。
- [ ] **Step 2: 运行 `uv run pytest tests/backend/services/test_usage_export_worker.py tests/platform/deployment/test_function_app.py tests/platform/infrastructure/test_infrastructure.py -q`**，新增行为先失败。
- [ ] **Step 3: 提取可注入 clock、Blob client 和 job store 的 `UsageExportWorker`**；复用现有 `backend/services/usage_export.py` 字段与报告生成，避免改写当前未提交 CSV 实现或破坏当前测试。
- [ ] **Step 4: 注册一分钟 Timer Trigger 与 managed identity Blob roles**；把 Function metadata test 的函数数从 5 更新到 6 并断言作业 Timer；给 telemetry Function `Storage Blob Data Contributor`、API `Storage Blob Data Reader`，权限限定在私有导出 container；已有 Storage Private Endpoint 已覆盖 Blob 服务，不开放公网匿名访问。
- [ ] **Step 5: 更新精确锁定 SDK 依赖与 deployment artifact 测试**；确保 Function 包中包含 worker 调用代码，API 包含 Blob download 所需代码。
- [ ] **Step 6: 运行 `uv run pytest tests/backend/services/test_usage_export_worker.py tests/platform/deployment/test_function_app.py tests/platform/infrastructure/test_infrastructure.py tests/platform/deployment/test_deployment_artifacts.py tests/platform/api/test_api_analytics.py -q`**。

### Task 3: 授权下载、任务 UI、旧导出兼容切换

**Files:**
- Modify: `backend/http/observability.py`
- Modify: `backend/services/usage_export.py`
- Modify: `frontend/src/data-sources/apim/api.ts`
- Modify: `frontend/src/data-sources/apim/queries.ts`
- Modify: `frontend/src/data-sources/apim/pages/dashboard-page.tsx`
- Modify: `frontend/src/locales/en/phrases-core.ts`
- Modify: `frontend/src/locales/ja/phrases-core.ts`
- Modify: `frontend/src/locales/ko/phrases-core.ts`
- Modify: `contracts/openapi/paths/usage-exports.yaml`
- Modify: `contracts/openapi.yaml`
- Modify: `tests/platform/api/test_usage_export_jobs_api.py`
- Modify: `tests/platform/api/test_api_analytics.py`
- Create: `tests/platform/frontend/test_usage_export_source.py`
- Modify: `tests/platform/frontend/test_locale_coverage.py`

**Interfaces:**
- `GET /api/v1/observability/export-jobs/{job_id}/download` 在每次请求时重新校验会话、tenant、Owner/creator 所属关系和 `ready` 状态，再由 API 流式读私有 Blob；headers 为 ZIP、attachment、`Cache-Control: private, no-store`，不返回 SAS/Blob URL。
- 旧 `GET /api/v1/observability/requests/export` 暂时保留以兼容脚本，但 UI 改用 Job API；旧路由最多接受 31 天及 10,000 行，超限提示用户提交异步任务；删除兼容路由留待后续版本。
- 页面在当前分析筛选下提交任务、轮询状态、显示 row count/过期时间、失败原因安全码和重试按钮；刷新不改变当前筛选；Ready 后下载 ZIP。

- [ ] **Step 1: 写 API 测试**：Owner 下载全企业 ready Job、Member 只下载本人 ready Job、伪造 job UUID/其他用户 job 返回 404、过期返回 410、Blob 不存在返回安全 503、响应没有 SAS URL；兼容路由范围限制通过。
- [ ] **Step 2: 写 UI 测试**，验证筛选快照入队、queued/running/ready/failed/expired 状态和重试行为。
- [ ] **Step 3: 运行 `uv run pytest tests/platform/api/test_usage_export_jobs_api.py tests/platform/api/test_api_analytics.py tests/platform/frontend/test_usage_export_source.py -q`**，新测试先失败。
- [ ] **Step 4: 实现 Blob 代理下载和兼容范围检查**；任何 Storage 错误响应不暴露内部 URL、文件路径、Azure token 或 SAS。
- [ ] **Step 5: 将现有分析页同步下载替换为异步 Job UI**；保留现有时间/组织/部门/项目/智能体/模型/用户/Runtime/状态筛选和摘要字段。
- [ ] **Step 6: 运行 `uv run pytest tests/platform/api/test_usage_export_jobs_api.py tests/platform/api/test_api_analytics.py tests/platform/contracts/test_openapi.py tests/platform/frontend/test_usage_export_source.py tests/platform/frontend/test_locale_coverage.py -q` 与 `npm --prefix frontend run build`**。

## 结束条件

运行：`uv run pytest tests/backend/services/test_usage_export_worker.py tests/backend/persistence/test_export_jobs.py tests/platform/api/test_usage_export_jobs_api.py tests/platform/api/test_api_analytics.py tests/platform/deployment/test_function_app.py tests/platform/infrastructure/test_infrastructure.py tests/platform/deployment/test_deployment_artifacts.py tests/platform/contracts/test_migrations.py tests/platform/contracts/test_openapi.py tests/platform/frontend/test_usage_export_source.py -q`，并运行 `npm --prefix frontend run build`。

预期：数据库 lease/重试、私有 Blob 生命周期、授权下载、CSV 防注入、元数据范围、部署 staging 与前端类型检查通过；同步兼容端点受到明确上限保护。

