# 用户、APIM Key、审计与报表实施路线图

> 本文是实施计划索引；详细步骤分别见下列三个计划。三个计划都实现同一份已批准设计稿，按列出的顺序执行。

**Goal:** 在单企业 Turnstile 实例中交付可授权的用户/部门管理、APIM 唯一 Key 生命周期、安全审计和受控的分析导出。

**Architecture:** 先建立稳定用户、角色权限、组织和审计写入基础；再把 APIM Subscription 关联到用户，让 Turnstile 仅作为 APIM Key 管理界面和模型调用代理；最后将大型报表移到有租约、重试、私有 Blob 与会话下载的异步任务中。APIM 继续作为实际凭证校验和用量计量的权威。

**Tech Stack:** FastAPI、PostgreSQL SQL migrations、Azure API Management、Azure Functions、Azure Blob Storage、React、TanStack Query、OpenAPI YAML、pytest、Vite/TypeScript。

**Spec:** [用户、APIM Key、审计与报表设计](../specs/2026-10-12-user-access-billing-audit-design.md)

## 实施计划

1. [身份、角色权限、组织与审计基础](2026-10-12-identity-and-audit-foundation.md)
2. [APIM Key 管理与 Turnstile 直连调用](2026-10-12-apim-key-management-and-invocation.md)（依赖 1）
3. [异步分析报告与明细导出](2026-10-12-async-usage-exports.md)（依赖 1；审计事件接入依赖 1）

每份子计划都列出文件边界、接口、测试与审查重点。执行阶段沿计划内的测试先行步骤逐段提交；任何 APIM 管理平面 Key 都不能写入日志、任务状态、数据库、审计事件或静态导出文件。

## 跨计划验收顺序

- 身份和授权：迁移现有 `owner/member` 用户后，Owner 可管理目录，Member 的 API 和用量查询都被服务端限制到本人及本人应用；停用账户后其已有会话立即失效。
- APIM Key：Turnstile 创建的 Subscription 由 APIM 生成密钥；Member 只能管理自己的 Subscription，Owner 可代管；调用入口转发原始 `Ocp-Apim-Subscription-Key` 并由 APIM 校验、策略控制、计量。无效 Key 不会触发 Dashboard Key 回退。
- 审计：记录指定登录、权限拒绝和管理操作的操作者、动作、UTC 时间、结果、请求 ID 和可信来源 IP；不记录正文、密码、Cookie 或 Key。
- 报表：同一 `[from,to)` 过滤条件产生一致的摘要与 CSV；Member 只能导出本人范围；超期任务与 Blob 在 7 天后回收，CSV 公式注入被转义。
- 部署：新迁移在生产启动入口自动应用；Function 和 API 使用托管身份及私有容器权限；部署包 staging、Bicep what-if、后端/前端/契约测试全部通过。

## 评审时请特别看

- APIM Key 模式依靠已通过 APIM 校验的 `context.Subscription.Id` 和本地归属投影确定用户；若映射缺失，APIM 必须拒绝请求，不能接受客户端自报身份。
- 设计只承诺应用级审计证据，不能据此宣称系统通过等保三级；正式定级和留存要求仍需组织与测评机构核定。
- 现有报表代码处于未提交状态。子计划会在该实现之上改造，并保留当前 CSV 字段、游标读取和公式注入保护，不会清理其他工作区改动。

