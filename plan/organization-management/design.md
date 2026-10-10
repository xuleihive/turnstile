# 组织管理功能设计

| 项目 | 内容 |
| --- | --- |
| 更新日期 / 基线 | 2026-10-11 / 当前源码 `ee27527`；原调查基线 `12dc1ee` |
| 状态 | P1及成员/范围权限/用量兼容已上线；最新应用 `9cc17fa`；P2真实同步和动态员工网关未验收，开关关闭 |
| 配套文档 | [需求分析](requirements.md) |
| 规范核查 | [项目规范核查记录](review.md)，为设计审查，不是已通过运行验证 |
| 分期 | P1 数据库目录与兼容适配；P2 Entra 同步与动态网关归因；P3 独立扩展 |

当前方案以下文为准；013/014赋组及调岗相关说明只作为明确标注的历史演进记录。
最新完整本地回归 1758 passed / 7 skipped、匹配三包和线上只读验证见[实施记录](implementation.md)。

## 1. 设计决策

| 决策 | 采用方案 | 原因 |
| --- | --- | --- |
| 前端入口范围 | APIM 平台管理下组织管理位于系统配置上方；固定菜单按来源/菜单权限生成 | 不依赖异步目录能力，Copilot保留独立企业团队 |
| 目录权威源 | PostgreSQL 主数据；观测信息进入候选队列 | 遥测不应自动修改任职，数据库故障不能回退示例数据 |
| 身份 | 内部人员 UUID + 不变治理 ID + 外部对象键 + 登录账号绑定 | 保留旧预算/账本，并允许邮箱变化和未来连接器 |
| 单位树 | 组织 -> 部门 -> 团队；单位存储预留父节点 | 不把团队强塞为现有预算 `department` 或遥测 `team` |
| 计费层级 | 继续组织 -> 部门 -> 人员，每人唯一主计费部门 | 不重复预算，不改变三层额度及证据协议 |
| 部门管理员 | 全局角色不变，另存部门范围授权 | 不让部门管理员获得全局模型/发布/配置权限 |
| 人员创建与成员关系 | 组织层级创建普通Member；部门/团队添加已有同组织人员 | 必填名称/邮箱/密码/部门，不允许嵌套团队，不改变计费锚点 |
| 菜单身份 | 当前kind/id的有效管理员任命投影为只读标签；全局菜单取并集 | 不编辑两套组，不推断父节点任命，不授APIM或Owner权限 |
| 报表与筛选 | AI FinOps固定两入口，内部报表目录；用量多选默认active | 历史查询兼容保留，未知数据不成为可管理组织 |
| 历史语义 | 当前任职、周期预算归属、请求发生时归属分别读取 | 调岗不能搬走旧账或重写历史请求 |
| 外部同步 | 指定租户、显式 Group 映射、只读、预览与冲突审核 | Group 不是组织树，外部成员不是本地授权 |
| 数据面 | P1 静态归因兼容并显示漂移；P2 版本化身份投影 | 页面保存不能假报 APIM 已更新，不逐请求访问 Graph |

新实体名称/业务编码均不是外键。组织与单位 ID 全局唯一且不可重用；跨组织不能仅以同名或同编码匹配。

## 2. 目标架构与职责

```text
APIM 组织管理页面 / 个人设置 / 预算 / 报表 / 助手 / 调用
                |
DirectoryService + DirectoryAccessPolicy
                |
目录仓储：PostgreSQL 主数据、身份映射、任职、授权、审计
                |
     兼容目录投影             目录变更 outbox
     EnterpriseEntityCatalog       |
     三层预算范围              网关映射 projector (P2)
                                   |
                           Table 身份快照 -> APIM

Graph Connector (P2) -> staging -> 差异预览/审核 -> DirectoryService
遥测发现任务         -> 待关联候选 ----------------------^

现有 BudgetService/账单证据/模型策略继续为治理权威源
```

### 2.1 代码落点

| 层 | 当前落点与责任 |
| --- | --- |
| 域 | `turnstile_core/domain/directory.py`：组织、单位、人员、任职与来源；预算范围仍用现有模型 |
| 仓储 | `turnstile_core/persistence/directory_store.py`；独立目录事务、principal和catalog，旧用量仍由原repository读取 |
| 共享服务 | `turnstile_core/services/directory_service.py`：CRUD、账号/成员/任命与状态；`directory_catalog.py`统一目录，`usage_directory.py`只读查询分类 |
| Web 服务 | `backend/http/organization_management.py`适配会话principal；`backend/services/user_settings_service.py`聚合本人信息；无独立Web目录服务副本 |
| 权限 | `backend/http/session.py` 获取会话上下文；核心 `DirectoryAccessPolicy` 采用独立 principal/能力模型，不依赖 FastAPI 或 `SessionIdentity` |
| HTTP | `backend/http/organization_management.py`；在 `backend/api.py` 注册；保留现有查询路径 |
| UI | `frontend/src/pages/organization-management-page.tsx`、`frontend/src/data-sources/apim/api/organization-management.ts` 与 query/组件；API 客户端沿用项目 APIM 来源边界，不 import React provider |
| 同步 P2 | `turnstile_core/integrations/` Graph 适配、`turnstile_core/services/` 独立任务 worker，接入现有 Control-plane Function 组合根而非新增未规划宿主 |
| 初始化/升级 | 现有 `backend.migrate/bootstrap/api` 入口与 `scripts/directory_upgrade.py`；001-017、并发锁和三包门禁已实现 |
| 契约 | `contracts/openapi/paths/organization-management.yaml` 与对应 schemas，由既有 OpenAPI 测试验证 |
| 测试 | 域与服务单元测试、真实 PostgreSQL 迁移/并发、API范围权限、浏览器及授权环境网关/Graph 验证 |

P1 的合理抽象是一个共享目录服务与范围策略，不先构建通用 HR 平台。连接器协议只在实际接入第二来源或 P2 Graph 时展开。

必须遵守现有架构测试：core 的 domain 不向上依赖，persistence 仅依赖 domain，integrations 不依赖 services，services 编排 domain/persistence/integrations。Graph 协议类型放 domain、实现放 integrations，sync 编排放 services；不能新增不在白名单中的 `turnstile_core/directory/` 层。运维 CLI 不增加 `backend` 根目录入口，不修改已固定的启动模块路径。

前端组织管理页面仅归属 APIM 工作区；底层目录与身份服务仍保持明确的共享职责。GitHub Copilot 的企业团队继续由其独立来源模块管理，本期不接入本地目录 CRUD。切换来源不会删除目录、停掉后台同步或更改账号权限。

### 2.2 当前目录调用点的替换

`budget_service._entities()`、`observability.get_enterprise_entities()`、`UserSettingsService.account()`、`AssistantService._catalog()`、`model_platform._bind_invocation_identity()` 统一读取目录服务。

`model_pricing.ModelPricingMatcher._judge()` 也需改为可信当前操作者/系统身份归因，不能仍取 `organizations[0]` 和种子目录。控制台调用前端同样不能默认第一组织；服务端校验/绑定组织与主部门，而不只绑定人员邮箱。

`TrafficGenerator` 刻意使用示例人员以免消耗真实人员预算，不能机械替换成生产目录。保留显式隔离的测试 fixture/专用测试身份，不在生产列表混入它们；现有项目/智能体目录的兼容依赖见第 7 节。

## 3. 数据模型

以下为逻辑 schema。生产迁移链为001-017；012目录、013/014兼容组、
015范围任命、016组织成员与旧工作流退役、017附加部门成员已应用；
实际落点与验证情况见实施记录。
本地目录表与外部同步能力分期开启，不改已应用迁移。

### 3.1 主数据表

| 表 | 关键字段 | 约束与语义 |
| --- | --- | --- |
| `directory_organization` | `id TEXT`、`code`、`name`、`description`、`status`、`revision`、时间/操作者 | ID 不变，规范化 code 全局唯一；管理状态 `active/archived`，读取兼容旧 `inactive` |
| `directory_unit` | `id TEXT`、`organization_id`、`parent_unit_id`、`kind`、`code`、`name`、`status`、`revision` | 组织内 code 唯一；`kind=department/team`；P1 部门无单位父节点、团队父节点为同组织部门 |
| `directory_person` | `id UUID`、`organization_id`（016）、稳定治理ID、资料、兼容job_title/013/014组字段、status/revision | 密码只存账号哈希；有效组来自范围任命与旧组兼容投影，资料表单不能改任命 |
| `directory_membership` | UUID、person/unit/organization、membership_kind、valid_from/to、source/revision | `primary_department/department/team`；唯一主计费部门；同组织可附加跨部门/团队协作，不改变预算父级 |
| `directory_account_link` | `person_id`、`app_user_id`、`verified_by/at`、`status` | P1 一对一有效绑定，UUID 外键；账号 enabled/role 仍由账号域管理 |
| `directory_department_grant` | `id`、`app_user_id`、`department_id`、`capabilities`、`valid_from/to`、授予者 | 权限白名单、有效任职与有效账号；不是第三种 `app_user.role` |
| `directory_menu_administrator` | 账号、组织、scope_kind、可选unit、有效时间、来源 | 015新增，组织/部门/团队精确任命；部门配置同步兼容grant，组织/团队不自动扩大部门数据范围 |
| `directory_change_audit` | `id`、`entity_type/id`、`action`、`actor_account_id`、`actor_person_id`、`actor_label`、来源、`before/after`、原因、时间、`request_id/job_id` | 追加写；同步 actor 可为空账号并记录服务身份；不序列化秘密 |
| `directory_outbox` | `id`、`entity_id`、`directory_version`、事件、状态、重试信息 | 与主数据事务提交；按版本投影、可重试，不把投影失败伪装为主数据保存失败 |

`governance_user_id` 在 P1 是兼容技术键，不是可编辑邮箱字段。新手工人员采用已确认邮箱的规范化值生成一次；Graph 无可用治理映射的用户只能 staging/待关联。未来切换 UUID 治理需单独的双读/双写协议，不在本期批量替换字符串键。

`directory_person.status` 表示人员生命周期；“待关联”“同步冲突”“网关未生效”使用独立绑定/任务状态，不把全部状态混进一个枚举。

### 3.2 外部身份与候选

| 表 | 关键字段 | 规则 |
| --- | --- | --- |
| `directory_external_binding` | `id`、`provider`、`cloud`、`tenant_id`、`object_type/id`、`person_id` 或 `unit_id`、`connection_id`、`source_fields`、`source_disabled`、最后成功见到时间 | `(provider,cloud,tenant_id,object_type,object_id)` 唯一；恰有一个本地目标；不按 Group 名称绑定 |
| `directory_identity_alias` | `id`、`person_id`、`namespace`、`issuer_tenant`、`alias_type/value`、验证信息、有效时间 | 已验证别名唯一，保留大小写规范化值；未验证别名不可参与鉴权 |
| `app_user_external_identity` | `provider/cloud/tenant_id/object_id`、`app_user_id`、验证时间 | 唯一外部登录身份；与“组织人员资料来源”分离，目录导入不自动创建此绑定 |
| `directory_observation_candidate` | 原始 `user_id/org_id/department_id`、来源、首次/末次出现、状态、拟议映射 | 未知遥测只进入候选；批准才修改主数据，不猜部门或假邮箱 |

邮箱在 `app_user` 目前全局唯一，同邮箱多租户账号无法靠新增目录表自动解决。P2 碰撞时暂停激活并人工绑定；若确需两个不同账号，须先升级认证 schema 与登录协议。已登录密码账号与外部账户相同邮箱，不构成自动绑定的可信证明。

别名读取顺序：已验证 `(cloud,tid,oid)` -> 明确绑定的治理 ID -> 在允许签发租户内验证过的 UPN/mail 别名 -> 未关联。不得把任意请求 `x-user-id`、邮箱域或 Group 名称当作身份证明。

### 3.3 P2 同步表

| 表 | 字段/用途 |
| --- | --- |
| `directory_connection` | 本地组织/授权范围、provider、cloud、tenant、client ID、`credential_ref`、选择范围、定时配置、状态、revision；不存明文 secret |
| `directory_group_mapping` | connection、外部 Group ID、目标单位、direct/transitive、主部门规则、字段权威策略、状态/revision |
| `directory_sync_job` | 幂等键、连接/规则版本、`requested_by`、状态、开始/完成时间、预览/应用统计、错误摘要、lease |
| `directory_sync_stage` | job、外部键、选定资料、成员边、删除标记、数据完整性状态；短期保留，不缓存全量无关资料 |
| `directory_sync_checkpoint` | connection、资源、规则版本、next/delta link、成功轮次；敏感游标加密/受保护，不回传浏览器 |

状态机：`queued -> running -> awaiting_review -> applying -> succeeded`；另有 `failed/cancelled/conflicted`。自动同步可以只自动应用已批准规则下的低风险资料更新；涉及主部门、身份碰撞和大规模停用始终进入审核。

### 3.4 数据库约束与索引

- `unit.organization_id` 与父单位必须一致：组合外键；部门/团队种类限制由数据库约束触发器与服务共同验证，不能只靠 UI。
- 所有引用 `ON DELETE RESTRICT`；一般使用状态/有效区间，不级联删除历史任职、授权或预算。
- 主部门有效区间按 person 建 PostgreSQL exclusion constraint，避免两段区间重叠；可采用 `btree_gist`，在部署前确认扩展权限，否则用人员行锁 + 约束触发器，不把验证降为无锁查询。
- 团队成员相同 person/unit/source 的有效区间不重复；手工与外部来源分别保存来源，计算一次有效成员并允许本地抑制覆盖。
- 索引覆盖 organization/parent/status、membership(unit/person, valid_from/to)、grant(account,department)、外部键唯一查询、candidate 状态、job 状态/时间、outbox version。
- 人员检索通过规范化邮箱/员工号/名称索引；全文/模糊查询是否增加 `pg_trgm` 由实际性能评估决定。
- 归属涉及的预算写入、调岗、成员变更统一先锁 person，再按固定 `(period,scope_type,scope_id)` 顺序锁预算/部门，重试可识别的死锁；不能沿用各模块不一致的锁顺序。

### 3.5 字段校验与账号边界

| 字段 | 建议约束 |
| --- | --- |
| 名称 | trim 后 1-160 字符，允许中文，不含控制字符 |
| 业务编码 | trim 后 1-64 字符，ASCII 字母/数字/下划线/连字符，规范化后唯一；不等于实体 ID |
| 描述 | 最大 2,000 字符；不允许 HTML 注入或用描述字段存凭据 |
| 联系邮箱/UPN | 最大 320 字符，使用标准校验器，不仅检查 `@`；可空的外部 mail 与有效治理身份分开 |
| 治理 ID | trim、统一兼容大小写规则，最多 255 字符以兼容预算证据/账本限制；不符合条件不得激活为可计费人员 |
| 员工号/岗位 | 分别最大 64/160 字符；员工号在已确认组织内校验冲突，不作为认证依据 |
| 时间 | 持久化 TIMESTAMPTZ/UTC；显示时区不改变现有月度预算 UTC 边界 |
| 权限/字段写入 | capabilities 和可编辑字段白名单，禁止客户端直接写来源、版本、actor 或 password_hash |

账号绑定由 Owner 明确选择现有账号并审计，外部登录身份还需受信登录/管理员确认的对象证据。不能从姓名、邮箱相同或 Graph 导入自动确立登录身份所有权；人员资料更新也不覆盖现有密码会话头像、自助名称或 Microsoft 登录名称的同步规则。

## 4. 稳定 ID 与兼容投影

### 4.1 旧键保留

| 既有数据 | P1 兼容策略 |
| --- | --- |
| `token_budget.scope_id` | 组织/部门不改，user 继续使用 `governance_user_id` |
| `token_budget.parent_scope_id` | 周期归属，不从今天的 membership 自动覆盖 |
| `department_enforcement.department_id` | 使用旧部门 ID；新增部门默认 audit，但停用不能自动 audit |
| `user_model_policy/access.user_id` | 原键不变，策略未配置/显式空集合保持不同语义 |
| `token_usage.user_id`、预算证据、预留/恢复、账单请求 | 不改历史键、不重新创建请求、不重复计费；遵守迁移 004 的身份不可变保护 |
| Azure Table 人员/月分区 | 继续 `governance_user_id|YYYY-MM`；不因联系邮箱变化创建新预算分区 |
| 现有 user/department 查询参数与固定报表 scope | 原值继续解析，未知/归档原值可历史只读，不将其默认换成新实体 |

仅有治理记录而无目录的旧 ID，应回填为“待归属/历史实体”，不能删除或给一个看似合理的默认部门。重复旧身份合并需明确审批和可审计的查询映射；不能直接 UPDATE 受保护用量身份。

### 4.2 旧目录 API

保留 `/api/v1/enterprise/entities` 现有 arrays 和 `id/name/parent_id` 结构：

- organizations/departments 从权威目录投影，users.id 为 `governance_user_id`，users.parent_id 为主计费部门，团队不出现在 departments。
- 无有效主部门、停用人员不出现在新分配列表；历史查询使用另一个时间范围接口，不借此删除历史范围。
- `invocation_testers` 继续取显式 tester 配置与有效目录交集；测试身份不是任何真实人员都可代调用。
- projects/agents 保留现有 ID 和已确认部门关联，不能因新单位出现就为它生成虚构项目/智能体。
- 新管理 schema 独立返回内部人员 ID、状态、来源、账号和任职；不对现有 StrictModel 任意添加字段。
- 旧前端 `normalizeEnterpriseUsers` 只兼容旧 `user-XX`，不得用于重写新的 UUID/外部身份；迁移后该兼容项需有测试与退出条件。
- 目录版本可通过响应 header 暴露，不改变旧响应 body；缓存不能把一个管理员的受限目录提供给其他账号。

尚未绑定人员的旧账号，其个人页暂时保留“已验证会话邮箱的原有本人查询”兼容路径，并显示未关联；不能因此获得某个候选人员的目录归属。完成可信绑定后统一转到治理 ID 查询，读取历史时不接受客户端自行指定 alias/person ID。新账号绑定与既有自助资料的可编辑规则保持独立。

## 5. 权限设计

### 5.0 独立菜单权限组演进（013/014历史方案，当前以5.0.1为准）

013/014阶段按当时需求实现：不把组织管理发展为正式人事系统；前端“岗位”替换为
“菜单权限组”。新增 `013_menu_permission_groups`，只向 `directory_person` 追加
`menu_permission_group TEXT NOT NULL DEFAULT 'user'`，数据库约束四个枚举：
`user`、`organization_admin`、`department_admin`、`team_admin`。
随后追加 `014_multiple_menu_permission_groups`，以 `menu_permission_groups TEXT[]`
作为权威字段，保留 013 单组列兼容旧客户端；将其明确已指派组转为一元素数组。
三种管理员组允许同时存在，菜单取并集；没有管理员时保存 `['user']`。
新 API 规范化去重，禁止普通用户与管理员混填，同时提交新数组和旧单组字段也拒绝。
旧客户端单组写入仅替换为对应一元素数组，并执行同样的 Owner 检查。
不修改已应用的 012/013，不把旧岗位值或既有部门 grant 推断为菜单组。
`job_title` 与可选人员编号继续兼容旧 API/Graph 数据，前端不再编辑或展示岗位；
PATCH 未提供字段时保留原值，不能因为新表单不发送岗位而清空旧值。

集中菜单矩阵位于 `turnstile_core/domain/menu_permissions.py`，不依赖 backend。
有效菜单组只取显式账号关联的启用人员和启用账号；未关联、停用、手工禁用或来源停用
均回落普通用户。账号仍是 Owner/Member；Owner 独立保留全部适用菜单。
不以岗位、Graph Group、团队成员关系或部门 grant 推断菜单组。
组的完整初始菜单矩阵见需求第 1.1 节。

登录响应及 `/auth/me` 返回 `menu_permission_groups`、服务端计算的 `menu_permissions`；
单数字段仅为兼容投影，不用于计算有效菜单。
后者包含稳定页面 ID，不是 API capabilities 或 APIM App Roles。
服务端只允许 Owner 改变人员菜单组；具备部门资料编辑能力的 Member 可以保持原组，
但不能自行升/降组，也不能创建带管理员菜单组的人员。人员资料与赋组在同一事务保存，
沿用人员 revision、审计和写源检查。菜单变更递增 permission revision；
仅改菜单组时不递增 APIM 目录版本、不插入投影 outbox，不触发网关身份重发布。
同步只写来源字段，不能覆盖本地菜单组或通过来源岗位提升权限。

前端仅消费服务端白名单：侧栏、命令搜索、浮动助手/报表、预取及快捷入口均校验。
刷新、来源切换、前进/后退、直达 URL 和现有会话降组也做路由归一化：
APIM 回到可访问的调用测试，否则回到用户设置。每 30 秒及窗口聚焦刷新 profile，
组/菜单变化清除旧私有缓存，渲染时先使用受控页面，不能短暂挂载被撤销的页面。
GitHub Copilot 继续隐藏组织管理，保留独立企业团队入口。

菜单控制**不是数据安全边界**。APIM 模型调用、预算限额、限流、订阅、已有 Owner 写入、
部门数据范围 SQL 均不读取本字段。后台接口继续由原授权体系控制，
菜单访问不自动扩张业务 API 权限，也不表示新增多租户隔离。
当时曾将管理员页签改称数据授权；015之后已取消重复入口，当前各节点统一“管理员”页签。

人员新增/编辑表单只以多个只读标签显示当前组，列表也逐组显示标签。
表单不保存组选择状态，不提供复选框或下拉编辑，提交资料时省略新旧菜单组字段，
防止普通资料修改覆盖独立管理员维护入口的指派。新人员只显示默认普通用户。
已有管理员维护入口保持不变；未指派管理员时显示普通用户。
初始化随既有迁移链追加 013/014，导入/新建人员使用数据库默认普通组。
已激活环境按兼容运行包更新流程备份和追加迁移，禁止重做目录切换/回填；
数据库迁移必须先于新 API 的 Member principal 查询。回滚保留新列与历史审计，
恢复上一组兼容三包/配置；不自动恢复数据库。验证应覆盖升级幂等、旧值保留、
赋组/撤销、角色/数据范围/模型授权不变，以及桌面/移动端菜单与直达路由。

### 5.0.1 管理员统一来源修正（2026-10-10）

此前独立人员菜单字段未关联部门指派、团队页复用父部门面板，且组织节点缺少配置入口。
追加 `015_scoped_menu_administrators`，新增 `directory_menu_administrator`
（账号、组织、可选单位、scope_kind、有效时间、操作者、来源）及有效身份视图。
组织无 unit_id；部门、团队必须匹配单位 kind 和所属组织，由约束触发器保护。
有效身份要求启用的显式关联账号/人员、当前主部门、启用层级以及团队实际成员关系。

人员与 profile 的 `menu_permission_groups` 统一为有效指派的去重投影；旧显式人员组值
兼容保留，新管理员配置以范围指派为来源，不再让用户维护两套 UI。Owner 保留平台能力，
但 profile 不再提前返回普通用户，仍报告其实际指派。
部门新旧写入口共用一条事务：验证候选人与 revision，维护范围指派及
`directory_department_grant` 兼容数据范围，审计并刷新权限版本。不新增 APIM outbox、
不提升 Owner、不更改模型/预算写权限。组织／团队不会被自动扩展为部门数据授权。

组织、部门、团队节点各显示“管理员”页签；参数必须为当前实际节点 kind/id。
候选搜索按组织/部门/团队成员过滤，既有未在当前分页的指派保留，
加载完成前不能保存空选择。移除重复“数据授权”编辑入口，人员弹窗继续只读标签。
新 API 为 `GET/PUT /organization-management/menu-administrators/{scope_kind}/{scope_id}`，
仅 Owner 维护；旧部门写 API 委托同一服务，不成为另一套来源。

015 只导入真实有效的旧部门管理员，并去重；不改旧部门 grant、人员原始资料、
账务/模型表或已应用迁移，不从普通团队成员关系猜团队管理员。
旧团队面板与部门完全相同的勾选不是独立团队指派证据，升级后须在团队实际节点明确配置。
迁移提高权限版本但不提高目录投影版本。发布需备份、隔离升级验证以及同核心版本三包，
不能只发布前端或直接改当前生产库来掩盖标签错误。此修正已随 `47b9c11` 上线，
生产迁移、合法旧部门指派导入及精确范围验收见[实施记录](implementation.md)。

### 最新成员流程修正（2026-10-10）

范围角色补充：在查询层返回有效 `menu_administrator_scopes`（kind、scope ID、
organization ID、name），人员列表服务派生当前节点 `scope_menu_permission_groups` 和
`scope_menu_administrator_scopes`；未任命时普通用户，父范围角色不自动作为下级任命。
列表/人员弹窗只用当前节点投影。Profile 保留全局菜单并集且公开本人精确任命，
core `is_menu_administrator`/`menu_permissions_for_scope` 和 frontend `isMenuAdministrator`
提供统一精确匹配接口，不比较显示名称。旧显式角色字段不生成任命或数据范围。
业务授权保留 Owner 及 department grant，每个部门能力单独映射并检查；编辑控件用同图，
防止多个部门能力的笛卡尔并集。此修正无新 schema、不改变原任命或模型/APIM策略。

最新要求覆盖本文件旧调岗/历史身份编辑设计：组织节点独占新增人员，必填名称、邮箱、
登录密码和启用的同组织归属部门。`OrganizationPersonCreate` 使用 SecretStr 接收密码，
共享 core scrypt 与现有认证格式保持一致；事务创建 Member、人员、primary_department、
account_link、audit/outbox。幂等摘要排除密码，重试从存储哈希验证，防止存储快速密码摘要；
重复邮箱返回冲突，不调用覆盖密码的账号 upsert。

部门/团队的“添加成员”使用目标单位 revision 和 UUID 列表，候选通过 available_unit_id
服务端限定组织、启用及尚未加入状态；追加关系和审计整体提交，坏成员导致整批回滚。
部门附加边使用新的 membership_kind=department；唯一 primary_department 始终负责
预算、调用归因和投影。017 扩展成员约束，不改变原主部门唯一/范围有效时间约束。
仅组织新增表单提供 required 的归属部门 Select，添加成员表单没有部门选择。
团队候选允许同组织跨部门协作，不能自动给候选人跨部门授权。

016 回填人员 organization_id 并取消未执行旧调岗；组织约束不可静默跨组织变更。
移除 transfer/identity-conflicts HTTP 端点、相关页签/客户端代码和 Telemetry 月度调岗调用，
保留旧内部历史结构、历史查询、账务和审计。旧6.3/6.4及相关接口/验收只作为历史设计，
不再作为新交付能力。已随 `47b9c11` 应用 016/017 并发布同核心版本兼容三包，
生产成员创建、登录、附加成员保持主部门及旧数据保护验收见[实施记录](implementation.md)。

### 5.1 平台角色与部门能力

保持 `SessionIdentity.role`、模型/runtime `allowed_roles` 和现有 DB CHECK 的 `owner/member`。添加独立服务端授权上下文：

```text
principal: 已认证 app_user UUID + 验证后的 person 绑定
mode: owner_global | department_scoped | legacy_member_read
allowed_departments: 明确非空集合，或明确 deny-all
capabilities: directory.read、directory.edit_people、
              directory.edit_teams、budget.allocate_users、model.assign_users
permission_revision: 最新授权版本
```

- Owner 优先全域；否则有部门管理授权的 Member 为受限模式，不能继承普通 Member 的全域读取绕过限制。
- 普通 Member 保持当前已有只读接口权限，以避免未经需求批准的大范围行为变更；这不是完整多租户隔离。未来全面收紧 Member 读取需单独评审。
- `directory.edit_people` 默认只含业务资料/团队关系，不含主部门/治理 ID/账号绑定/外部权威字段。
- 预算授权限于明确部门、预算周期、实际预算父级。新分配以当期有效主部门校验；历史修改以周期父级授权，不按今天任职决定。
- `model.assign_users` 为人员级跨周期策略，因此只允许对当前主部门且本人可管理的人员操作；不能以某个历史预算归属取得现今模型授权权。
- 权限解除立即写授权版本，服务端每次访问重验；缓存有短期上界。部门管理员停用/调出时 grant 自动失效，不产生孤立权限。

本期默认关闭预算/模型写入委派，现有 governance mutations 继续要求 Owner；P1 新增受限目录写能力属于新的显式例外，应同步说明于 `docs/security.md`，不是放宽已有预算/发布接口。未来开启两个可选能力前，需先评审并更新安全规范、HTTP 依赖和全部范围测试。

### 5.2 读路径必须覆盖

所有受限查询均 `用户过滤 ∩ 服务端授权范围`，不是信任 query parameter，也不是前端过滤结果。具体包括：

1. 目录树、人员、管理员和账号搜索，仅返回范围内且绑定所需的字段。
2. 预算概览的组织行对部门管理员返回显式“可见部门小计”，不暴露真实组织总额度/其他部门风险与审计；响应标明 coverage，不能伪装完整概览。
3. 用量 overview/distribution/trends、请求明细、异常规则/告警、原始 detail ID 查找、分页 total 与导出均约束部门归因。
4. 明细按请求发生时部门判定。人员今日调入不授予其此前其他部门的明细；账号个人页仍以本人稳定身份查询其本人历史。
5. 助手目录、工具参数、模型收到的工具结果、已保存图表重算、固定报表共享/导出必须带同一上下文。历史已保存的全域快照不能直接对部门管理员返回。
6. 没有部门范围返回 403/空投影；不能将 `allowed_departments=()` 传给当前 `_filter_sql`，因为空 tuple 当前会消除过滤。

建议用授权后的 repository facade 供助手/报表，底层 SQL 显式有 deny-all 状态和参数化 `ANY`，避免每个工具重复遗漏过滤。范围参数不允许模型自行覆盖。

GitHub Copilot 的企业/团队/成本中心 API没有本地部门语义。部门管理员进入该来源时，个人账号功能可沿用本人规则；企业治理与明细在缺少显式来源映射时拒绝，不将本地 department ID直接发给 GitHub，也不继承全域只读结果。普通 Member 的原有来源权限不在本次悄悄改变。后续映射需要明确来源授权与本地授权的交集。

### 5.3 写路径

- 新组织写接口采用 Owner / ScopedCapability 依赖与现有 `require_allowed_write_origin`；绑定、管理员、同步连接始终 Owner。
- 既有预算 Owner 路径保留；只有增加可选部门能力时才把特定人员分配路径改为 capability 检查，不扩大组织/部门总预算及 enforcement 权限。
- bulk 的 ids 和 all-matching 都先授权完整候选，再选择；混入任何越权 ID 整体拒绝，total 与历史不能泄漏。
- 版本冲突返回 409；跨范围未知 ID可一致返回 404，避免存在性泄漏。不能靠账户 display name 或客户端传的操作者字段进行审计。

## 6. 变更、归属与预算

### 6.1 一般保存与归档

组织、部门、团队和人员管理只使用启用/归档。旧 `inactive` 数据保持原值，
界面映射为已归档；人员归档查询包含 inactive/archived，写 DTO 将旧客户端
inactive 归一化保存为 archived，不修改已应用迁移或历史审计。
账号 enabled、manual_disabled、来源 source_disabled 保持原有独立语义。
组织/部门/团队归档不要求清空启用人员、子节点或业务引用，仅修改当前节点及
原有 revision/audit/outbox 元数据，不联动人员/账号/成员关系/子节点状态或历史业务配置。
组织内待执行同步、当前节点或下属团队映射的待执行同步、all_users 默认部门任务仍需
先完成或取消；权限、revision 和不可变树结构检查保留。
当前目录分配/调用及范围管理员有效性仍检查范围是否启用，不能将人员状态与范围权限混同。

组织目录“隐藏已归档”默认开启，仅影响导航，不更改 API 数据范围或授权。
归档组织及子树、归档部门及子树、归档团队隐藏；关闭开关可查看/恢复。
各级同级节点先按启用/归档分组，再用当前 locale 的 Intl.Collator（numeric、
sensitivity=base）按名称升序，同名按 ID 稳定排序。前端仅排序数组副本，
不改API缓存、后端顺序或成员关系；首个启用组织回退与显示顺序一致。

预算管理层级导航复用目录状态作只读展示：默认隐藏归档、组织折叠，同级启用/名称排序。
BudgetStatus 仍表示额度风险，不能用目录状态覆盖；KPI/风险/历史以及额度约束仍使用原始
预算响应，显示隐藏不得改统计、账本或拦截。组织/部门点击使用真实 kind/id 联动人员范围，
团队下拉仅包含当前范围团队；切范围重置团队/搜索选择状态，全部团队不排除无团队人员。
人员归属继续使用原月度预算 API 的 primary department，附加部门成员边不转换成预算归属。
前端分页读取已有部门预算接口，再聚合组织、与当前团队治理ID集合取交集、搜索/状态筛选、
按50条分页；跨页目录成员均读取，不只过滤当前页。并发部门读取限制4个，每次最多200条。
不新增后端查询参数或团队预算字段。无目录读取权限使用已有授权enterprise catalog，
不请求受保护目录列表；读取失败显示错误，不能伪装零数据。
批量编辑仍单一主部门，团队筛选发送明确人员ID；混合部门禁止提交，不自动拆成多次写入。
保留原500-ID限制、单部门完整未搜索结果的all_matching、预算编辑器和批量编辑器；
固定/均分/保留额度、上级与子级校验、Owner写权限、模型配置和存储都不改。
未来若需原生组织/团队预算接口或跨部门事务，必须先审批，不能用展示需求隐式引入。
选择回退采用真实 ID，团队到启用部门、部门到组织、组织到首个启用组织；
没有启用组织显示空态。未知/不可访问 ID 保留错误，不替换到其他范围。
切换隐藏前执行未保存离开保护，URL 清除已隐藏节点；归档保存后重新查询并应用同一规则。

数据库事务一次完成主数据、audit、outbox；返回保存后的 revision 与独立 `gateway_projection_state`。projector 失败可重试且有告警，用户不应因投影失败重复创建实体。

名称变更仅影响当前展示，不修改原用量名称。联系邮箱变更不改治理 ID；新 alias 先验证，再由受信身份映射将新 claim 解析到旧治理键。

人员归档流程必须分开：

1. 目录状态立即归档，管理/新预算分配不再接受；绑定账号是否停用须单独选择且 Owner 审批。
2. 如选择账号停用，沿用 `app_user.enabled` 与会话失效检查；不删除账号或密码。
3. 产生显式网关身份禁用投影，保留模型策略和预算。无预算/无模型策略的人也必须受禁用控制，不能只遍历预算记录。
4. P1 仍为静态员工 policy 时提示未完成网关撤销，要求授权的 App Role/访问撤销或网关升级；不能声称目录 status 自带 APIM 拒绝能力。
5. 恢复需检查 manual_disabled、source_disabled 和本地抑制，不能被一次同步重新开启。

### 6.2 周期归属

预算人员清单按指定周期生成：已有预算以 `token_budget.parent_scope_id` 为父级；未分配人员按该周期有效/预定任职产生候选。历史月份不可从今天的目录重新构造。

组织 -> 部门关系同样保持历史版本：P1 禁止有引用部门跨组织迁移，因此旧父关系稳定；P3 若支持迁移，需单位归属历史与预算周期快照，不能仅改 `directory_unit.organization_id`。

历史 use/confirmed tokens 仍由现有有效证据读取。新增目录不能把 `budget_evidence_selected` 或恢复量替换为纯日志求和。

### 6.3 已退役的调岗工作流

016保留内部历史记录并取消未执行旧计划；人员调岗菜单、transfer-preview/transfers端点、
历史身份编辑及Telemetry月度调岗执行均已移除。`can_schedule_transfers=false`，
旧内部服务存在不表示外部可用，不将本流程列为未完成P1验收。

### 6.4 未来计费归属转移边界

当前通用资料PATCH、添加成员或Graph更新均不得改变既有主计费部门。
未来需要转移时另行审批，先设计周期预算父级、预留请求、历史归属与授权撤销的原子语义；
不能通过只改membership或搬移整月预算实现。跨组织转移与团队原生预算不在本期。

## 7. 各功能兼容性矩阵

| 功能与源码 | 影响 / 必须适配 |
| --- | --- |
| `budget_service.py` | 注入统一目录；按周期归属构造列表与父子校验； inactive/历史预算仍展示且不可新分配 |
| `repository_budgets.py` | 原预算控制、存储、继承及证据保持不变；附加成员不改周期父级，无新增调岗写入 |
| `observability.py`、`repository.py` 及趋势仓储 | 范围授权进入 SQL；历史维度包含未知/归档；从 ID+名称分组迁到 ID 唯一分组 |
| `dashboard-page.tsx` | 五页面共用多选与archive筛选；范围级联、parent_ids及选择重算；默认active，清空其他字段不解除状态约束 |
| `user_settings_service.py` | 账号 UUID -> 已验证 person -> 稳定治理键；本人预算/模型/用量不再直接用可变登录邮箱；兼容旧 membership_source 并版本化新来源 |
| `auth_service.py`、`authentication.py`、`auth_store.py` | 保留既有密码/Microsoft 流程；P2 保存验证后的 cloud/tid/oid 并保护账号绑定，不因同步放宽登录门槛 |
| `model_platform._bind_invocation_identity()`、`dashboard-invocation.tsx` | 从可信目录确定组织和主部门；不默认首个组织；Owner 代调用仍显式授权与审计，未映射用户不伪造归属 |
| `assistant.py`、`assistant_tools.py` | 同一可见目录/授权 facade，工具与存储图表重新校验；防止自然语言绕过范围 |
| `anomaly-rule-management.tsx`、`anomaly_rule` | 原 scope ID 保留；归档范围可读不可新选；规则编辑按授权，未知 scope 不因目录迁移变全局 |
| 报表中心、图表、共享与导出 | `pinned-report`权限/旧chart链接保留，目录移入中心；快照/原查询和can_manage保留，不成为部门隔离旁路 |
| `user_model_policy/access`、runtime.allowed_roles | 不改 owner/member；停用仍显式拒绝，缺失策略与空策略不同；组同步不自动授模型 |
| `ledger.py` 与 APIM policy | governance ID及预留协议不变；identity outbox 独立版本；禁用覆盖无预算用户；目录/网关漂移可观测 |
| `gateway_application.department_id/owner_id`、`application_access` | 保留应用域预算/模型/订阅，不把人等同应用；部门归档保留引用，不清空应用，历史归因不重写 |
| `enterprise_catalog` 项目/智能体 | P1 把既有项目/智能体映射作为兼容数据持久化或保留受控只读配置，部门依赖有注册表；新增部门可无项目，调用需可用的显式上下文 |
| `TrafficGenerator`、健康/发布探针 | 测试种子与机器身份分离；不得转成真实人员、部门成员或重复消费人员预算 |
| `ModelPricingMatcher` | 改为操作者已验证目录或独立系统身份，不借固定组织归因实际调用 |
| `InMemoryRepository`、demo factory | 仅显式 demo/单元 fixture；生产不回退种子，内存实现也应遵守新约束；不作为持久化验收证据 |
| GitHub Copilot service/teams/cost centers | 保持 `usage_domain` 与外部 ID 空间及独立企业团队功能；该模式隐藏组织管理菜单/搜索，不接入本地目录 CRUD；将来通过显式 provider binding 连接 |
| 前端目录与个人/预算 query cache | 目录保存后同时失效 reference、finops、user-settings、application 引用；授权/用户切换时清理旧缓存，不能只 invalidate `["finops"]` |
| `app.tsx` 与各 source normalizer | 固定菜单按来源/白名单生成，不等待目录capabilities或报表；目录能力只约束页面操作，Copilot保持归一化 |

### 7.1 历史筛选与名称

已实现 `/enterprise/query-entities` 时间范围目录，返回独立 `UsageQueryEntityCatalog`，
含 `source/directory_status/parent_ids`；当前名称优先，多历史父边保留，不自动创建目录实体。
后端仍支持active/archived/historical/unattributed及无状态全量兼容；新UI只提供
全部/启用/已归档，默认active，全部显式发送active+archived，不再暴露兼容分类。
同字段多个ID取并集，跨字段及服务端授权范围取交集，候选与实际SQL使用一致状态口径。
归档子范围可保留启用祖先供级联导航，不能据此扩大实际查询。姓名中含“测试”不构成脏数据判据。
代码落点为 `usage_directory.py`、`repository_support.py`、`observability.py`、
`usage-filter-scope.ts`；本次不写原用量、预算或计费表，无新迁移。

聚合 SQL 以稳定原始 ID分组；展示名可用当前目录名称，找不到时选择范围内最近原始名称并稳定排序。详情保留原始名称。原先同 ID/多个名称的行合并后，tokens/count/cost 求和、延迟/成功率按原始请求重算，**不能把旧聚合百分比和 p95 平均**。排名变化不应改变整体总量。

P1 不将不同旧 ID自动 alias 合并为一个统计实体；该合并会改变历史分组与权限，需要另行确认映射及报表版本。历史报表重放至少记录目录命名策略，不把“显示新名”误当作“重写旧归属”。

## 8. API 设计

以下按当前代码列出对外接口，统一 `/api/v1`、cookie会话及写来源校验。列表使用cursor，
写请求含expected_revision；创建/任务的幂等与具体响应以现有OpenAPI为准。
P2端点已部署不等于真实同步开启或验收。

数据传输与工程约束：扩展 `backend/api.py` 的 CORS header 白名单以支持 `Idempotency-Key`，不能打开任意来源；同源生产与跨端口开发均要测试。人员/身份/同步错误不回显 Pydantic 原始 input、凭据引用内容或 Graph 原始响应，敏感响应 `Cache-Control: no-store`；普通目录只可进入当前账号范围内的前端缓存。组织请求沿用 `frontend/src/data-sources/apim/api/`，不得在页面里直接 fetch 或自报角色/人员身份 header。

| 方法 / 路径 | 功能与权限 |
| --- | --- |
| `GET /organization-management/capabilities` | 当前账号有效范围/能力，不返回隐藏部门名称 |
| `GET/POST /organization-management/organizations` | 列表按范围；创建 Owner |
| `GET/PATCH /organization-management/organizations/{id}` | 详情/修改；归档只检查并发同步，不要求清空成员或子节点，Owner |
| `GET/POST /organization-management/organizations/{id}/units` | 部门/团队列表；Owner建部门，授权管理员建本部门团队 |
| `PATCH /organization-management/units/{id}` | 单位资料/状态；详情从组织units列表读取，按能力限制字段 |
| `GET/POST /organization-management/people` | GET范围搜索；POST仅Owner组织层级创建，必填名称/邮箱/密码/启用部门 |
| `POST /organization-management/units/{id}/members` | 添加同组织已有启用人员，目标revision及person_ids整体校验，不改计费主部门 |
| `GET/PATCH /organization-management/people/{person_id}` | 业务资料编辑；治理 ID/主部门/账号不得通用 PATCH |
| `PUT /organization-management/people/{id}/teams` | 明确替换本地团队关系，验证同组织/source及权限，不改变主部门 |
| `POST /organization-management/people/{id}/account-link` | Owner验证绑定；不创建密码，不授 Owner |
| `POST /organization-management/people/{id}/status-preview` | 停用/恢复影响及网关当前状态 |
| `POST /organization-management/people/{id}/status-changes` | 显式选择人员状态、账号联动与网关撤销，Owner |
| `GET/PUT /organization-management/departments/{id}/administrators` | Owner设置/撤销授权；管理员仅读自身可见信息 |
| `GET/PUT /organization-management/menu-administrators/{kind}/{id}` | 当前组织/部门/团队范围任命，Owner写入；旧部门入口委托同一事务 |
| `GET /organization-management/audit` | 范围过滤的目录审计，不泄漏其他部门前后值 |
| `GET /enterprise/query-entities?from=...&to=...` | 多值ID/状态查询投影；无状态参数保持历史兼容，不供身份分配 |
| `GET/POST/PATCH /organization-management/connections[/{id}]` | P2，Owner配置指定租户，只回传 credential 状态 |
| `POST /organization-management/connections/{id}/validate` | P2，读取Graph检查权限并记录验证revision，不启动同步或授予consent |
| `GET/PUT /organization-management/connections/{id}/group-mappings` | P2，显式 Group 映射和规则版本 |
| `POST /organization-management/connections/{id}/sync-jobs` | P2，创建 preview/full/delta 任务，202 |
| `GET /organization-management/sync-jobs/{id}` | P2，进度、差异、失败与删除保护报告 |
| `POST /organization-management/sync-jobs/{id}/apply` | P2，Owner确认，版本过期先重新预览 |
| `POST /organization-management/sync-jobs/{id}/cancel` | P2，停止未提交部分；已提交结果不假称回滚 |

当前错误使用 `detail/code`，参数错误不回显敏感input；建议未来增加request_id与字段级冲突，
不能把其列为已实现字段。状态码及完整响应以OpenAPI/HTTP为准。

请求示例：人员业务 PATCH。

```json
{
  "expected_revision": 7,
  "display_name": "Example Person",
  "reason": "Approved profile correction"
}
```

主数据响应包含id/revision和资料；目录版本与 `gateway_projection_state` 由capabilities
单独返回，不将旧草案的directory_state字段当作已发布契约。保存成功不等于网关已生效。

## 9. 前端交互

- 页面URL不变；菜单/搜索条件为APIM来源及 `canAccessMenu(user,page)`，
  不等capabilities响应。后端及页面操作再检查具体范围/能力，菜单访问不等于目录数据权限。
- `organization-management` 只加入 APIM 的页面白名单；不加入 `normalizePageForSource` 中类似 `user-settings` 的跨源特例，也不加入 GitHub Copilot 的 `githubCopilotPageIds`。
- 选定 GitHub Copilot 时，组织管理页面按已有 `normalizeGithubCopilotPage` 规则转为 `finops-overview`（使用指标），URL 同步归一化。直接 URL、刷新及浏览器历史恢复也检查来源，不只处理菜单点击。
- 渲染和 query `enabled` 同时限制来源及能力；切离页面停止其前端预取/轮询，不返回或显示前次 APIM 页面数据。该限制不替代服务端授权，后台同步 worker 不受浏览器来源选择控制。
- GitHub Copilot 的 `copilot-enterprise-teams`、成本中心和未分配用户页面保持现有职责，不把本地组织管理重定向成企业团队、不改变其 API 或权限。切回 APIM 恢复组织管理菜单，重新进入时校验原组织选择仍有效；未保存表单切换前确认，取消则保留原来源/页面。
- 主工作区采用现有 FinOps header/toolbar/scroll 布局。左侧树固定受限宽度，右侧表格弹性区域；无权限树节点完全不返回。
- 搜索分为当前单位人员和 Owner全域目录；结果显示主部门、团队、来源、人员/账号状态、同步状态，不用邮箱域猜组织。
- 选节点后页签为人员、管理员、变更记录；Owner的同步页提供连接、Group映射、预览与任务记录。
- 新增部门/团队和组织人员用既有Dialog；添加成员独立选择已有人员。只有组织创建表单有密码/必选部门，资料编辑不提交组字段。
- 当前节点管理员搜索有效关联账号/人员，选择任命并保存；不额外提供能力编辑或两套授权界面。
  当前节点标签不继承父范围管理员，长标签单项不换行，组合自然换行。
- 外部资料只读显示同步来源；人工资料/覆盖独立编辑。成员来源为 Group的行不能伪装成可直接编辑成功的本地关系。
- 目录 tree 与人员表分别分页/按需展开；页面选择写 URL 参数，恢复不存在节点时显示不存在/失效，不静默改为其他组织。
- reference cache 以账号 UUID、permission_revision、目录版本和查询时间区分；目录变更失效 `finopsKeys.entities`，也失效相关预算、个人账号、规则和应用引用。
- 目录/用量/预算切范围按各自规则重算选择；无调岗入口，历史账务不随附加成员变化。
- 静态网关未对齐时显示真实状态与版本。只有受信 projector/readback 确认后才显示 ready。

## 10. Entra ID 同步扩展

### 10.1 租户与认证

连接固定 `cloud + tenant_id + client_id`，不可由前端输入任意 authority/Graph URL。P1 当前控制台登录允许域不代表授权目标租户同步；P2 另有登录租户绑定/允许策略，经评审迁移后启用，不能默默切成目录同步租户登录。

后台使用 application permission + client credentials，Graph scope 为该云的 `/.default`；凭据优先证书/工作负载联合身份，Azure同租户适用时可用已授予 Graph app roles 的托管身份。跨租户必须在目标租户有应用 service principal 和管理员 consent，不能假设本服务托管身份天然可读其他租户。

API 只保存 Key Vault/受管秘密引用；凭据管理不在人员字段、应用配置响应或日志中出现。浏览器头像 `User.Read` 委托权限不用于同步。

### 10.2 Graph API 与权限范围

使用 v1.0；实施时再次核对目标云端点、具体字段及官方权限表，尤其新细分权限与 hidden membership。2026-10-10 核对的官方源码确认如下行为：

| 目的 | 拟议 API | 说明 |
| --- | --- | --- |
| Group 选择/验证 | `GET /groups`、`GET /groups/{id}` | 显式选择 IDs，不靠显示名称唯一性 |
| 直接成员 | `GET /groups/{id}/members` | 非递归，过滤 `@odata.type=#microsoft.graph.user`；完整遍历 nextLink |
| 用户资料 | `GET /users/{id}` 或 `GET /users?$select=...` | 请求 `id,displayName,userPrincipalName,mail,accountEnabled,userType,department,jobTitle` 等必要字段 |
| Group及成员增量 | `GET /groups/delta?$select=id,displayName,members` | 处理 `members@delta`，成员通常只返回 ID，需补用户资料；选定 ID过滤有数量限制，不能无限拼接 |
| 用户增量 | `GET /users/delta?$select=...` | 与 Group游标分开；字段集合变更需重新初始化 |
| 嵌套成员（可选） | `GET /groups/{id}/transitiveMembers` | 与直接成员语义分开；嵌套映射不等于部门树 |

权限不是任意写权限集合：读用户资料使用应用 `User.Read.All`；Group部分根据 selected fields 与成员/delta endpoint 采用只读 Group权限。首个完整连接器可用 `Group.Read.All`，上线前评估是否能缩为 `Group.ReadBasic.All` + `GroupMember.ReadBasic.All/GroupMember.Read.All`。不默认申请 `Directory.Read.All`、任何 ReadWrite权限或 Group管理员角色；hidden membership 只有业务明确需要时加 `Member.Read.Hidden` 并获 consent，否则报告不可完整同步。

不要声称选定 Group就将 Graph应用权限收窄到那几个 Group：这通常是本地同步范围，不等于租户级 app permission边界，管理员 consent 页面须说明实际读取权限。`department` 字符串属性仅作来源资料，不据此自动创建或匹配部门。

直接 members 返回还可能包含 group/device/contact/service principal；只接收 user，空/不完整对象进入异常列表，不伪装正常零成员。P2 默认不把 Guest或无可用治理身份的对象激活为可计费人员。

### 10.3 Group 映射与冲突

示例：外部 `group-A` 映射 `department-platform`，`group-B` 映射其 `team-runtime`，二者与 Graph名称变化无关。

- Team映射只添加协作关系，不能决定计费部门；Department映射产生主部门候选。
- 同一人员命中多个部门，需要显式规则或人工确认；默认保留现有主部门并标记 conflict。
- 与人工锁定归属冲突不覆盖；Group移除关闭该来源成员边，不删人工来源或其他 Group仍提供的成员边。
- Graph Group Owner不自动授予 `directory_department_grant`；如未来导入管理员也必须 Owner确认且绑定有效账号。
- 租户/object ID稳定，邮箱变更只更新资料/待验证 alias；不同租户同 mailbox不合并。新对象接管旧邮箱不能自动继承预算、账号和授权。
- 本地 `manual_disabled`、显式排除和外部 `source_disabled` 分开，effective active 要同时通过，不允许同步恢复人工禁用。

### 10.4 全量/增量任务

1. 拿连接 lease，记录规则版本与旧checkpoint；读取 Graph 必要对象，顺着官方 nextLink 完整遍历，不手动拼 `$skiptoken`。
2. 仅访问预配置 Graph host/云和 HTTPS；nextLink/deltaLink 验证 host，避免携带凭据跳转到任意地址。
3. 数据进入 staging，按稳定外部键幂等；一个对象多次出现合并更新/删除顺序，不能把“本页未出现”当删除。
4. 检查分页完整性、hidden member权限、对象类型、人员身份与主部门冲突、预算影响；生成预览。
5. 用 job/规则/目录版本批准。Apply前再次验证旧版本，陈旧预览返回409；Group变更需要新的预览。
6. 同一审核应用事务写外部资料、来源成员边和audit/outbox；既有主部门变化保留冲突待处理，
   不创建已退役transfer任务，不直接PATCH计费归属。
7. 新deltaLink只在该轮成功 apply/确认安全结果后推进；失败保留旧游标，重试不重复审计或预算分配。
8. 429/503 按 `Retry-After` 与抖动退避；401重新取得 token，403报告缺权限，游标失效安全全量重建；不清空目录。
9. 配置变化建立新checkpoint，旧范围“消失”不是用户离职；Group已删除与失去读取权限区分。
10. 全量缺失或删除标记先置待审核；建议超过 10% 或 50 人触发额外保护（任一达到，阈值可配），不能直接批量停用。单个人员真正外部禁用也必须遵守批准的停用策略。

Group delta用于直接成员变化。启用transitive时，对嵌套关联额外建立依赖/周期全量校验，不能认为某一个父Group delta必然覆盖所有深层成员变化。Graph最终一致性意味着一轮sync不必然是瞬时完整目录。

### 10.5 网关身份投影

P2新增独立身份映射分区，键由受信 token的 `(cloud,tid,oid)` 唯一确定，value包含治理ID、org/department IDs和显示名、effective status、mapping revision与生效时间。保留原预算 `Q/C/M/R` 协议，目录投影不能写预留或confirmed用量。

- APIM验证签名、issuer/audience及租户后，读取映射覆盖归因头；客户端无法用自报header改变主部门。
- 人员/月预算和模型查原稳定治理键；控制台/BFF 使用同一解析，不因 email与UPN差异产生第二个人。
- 显式已停用的映射始终拒绝；缺映射/读取失败不等于未配置模型策略。受管员工模式 fail-closed，对尚未迁移租户可以有明确版本化 legacy模式，不能在失败时隐式放行。
- unknown mapping 返回可识别错误并产生候选/告警，不使用任意 `groups[0]` 或第一个App Role替代。
- 投影版本单调、重放旧事件不覆盖新禁用；及时重试并读回记录版本。缓存TTL、紧急撤销路径、传播目标与实际 APIM限制一并验证。
- 已准入请求冻结当时身份/归属，后续流式响应与对账不能在目录变动后重新归因；identity版本可新增在请求旁表，不修改旧计费身份。
- P1静态policy兼容只显示漂移/待对齐；P2切换需授权环境 what-if、policy readback及回滚验证，不通过单元测试宣称已生效。

## 11. 迁移与发布

### 11.1 当前入口与真实约束

| 已有入口 | 当前行为 | 本功能需改造/保留 |
| --- | --- | --- |
| `backend.migrate` | 按文件名排序.up.sql、记录SHA-256；已应用checksum漂移失败，数据库advisory lock等待上限120秒 | 已实现/并发验证；保持唯一入口和失败事务语义，不做业务回填 |
| `backend.bootstrap` | 原子地仅在 `app_user` 为空时建立首个密码 Owner | 保留账号逻辑，不生成默认组织或重置账号；业务回填不挂到此处 |
| `infra/modules/data-plane.bicep` | `migrate && bootstrap && uvicorn`，失败阻断后续启动 | 保持三条入口路径；目录轻量 readiness 检查在 API lifespan，禁止启动时做长回填/Graph 同步 |
| `backend.api.lifespan` | 校验生产前端资源、repository 与加密配置 | 选择 database 目录时检查 schema/ready 状态与版本；legacy 显式模式不假读不存在新表 |
| `scripts.stage_deployment` | API 包含 backend/core/migrations/frontend；Functions 只有 core 与各自入口 | API 包含新迁移与受控公共升级 CLI；Functions 不包含 backend、迁移 runner 或私有部署工具 |
| `scripts.deploy` | 保留 state；新装构建三个包，重跑用 saved outputs 与 runtime-release，不重新 APIM bootstrap；验证固定 Functions 清单 | 配套目录配置/包版本/ready 验证，不自动回填客户数据、不改 state 密钥，不将新装 bootstrap 当升级 |
| `functions/telemetry.sync_budget_ledger` | 原月度预算继承在ledger flag前执行 | 月度调岗调用已移除，继承/证据逻辑保持；不访问Graph |
| `functions/control_plane` | 发布与 release 独立 timer，依赖 core | P2 加有独立门禁的 sync/projection timer，函数索引清单随包更新；不挂在 paid probe 流程 |

`backend.migrate`在检查/创建schema_migration之前取得数据库锁、连接关闭释放。
保持既有连接/transaction上下文，真实多pending文件失败与并发测试已覆盖；
不能把嵌套transaction误称每文件独立持久提交，或把迁移锁误称运行期健康/断线恢复机制。

### 11.2 Schema、业务回填与开关分离

初始设计基线最高编号011，P1及后续改造已追加并应用012-017，
包含目录、兼容组、范围任命、组织成员和附加部门成员；
以后重新核对迁移链，从下一可用编号追加 `.up.sql`，不改已应用迁移的内容或
checksum，不新增自动 down 脚本。

012已实现目录表、约束、索引及平台升级控制记录。`directory_control_state`记录协议、
权威源/active_version/phase；`directory_upgrade_run`和`directory_upgrade_stage`
记录批准计划、lease/checkpoint和物化结果，不是未实现的拟议表。
后续015-017回填/旧任务退役严格按已审查迁移执行，例行发布不得重放这些数据操作。

phase 建议为 `schema_only -> importing -> verified -> active`；全新空目录有独立 `fresh_empty_ready`，不能把它和未回填既有实例混为一谈。预览写独立staging，apply只在权威源仍为legacy且无已激活版本时首次物化目录表；重复批次不得克隆另一套同治理ID人员。旧读取继续用legacy，新表物化不等于切换。activate用数据库CAS设置active_version和权威源；所有目录服务检查相同版本，已激活后不允许初始化/升级工具重置人工维护数据，后续变更走正常版本化CRUD或独立评审迁移。

下列配置已在 `turnstile_core.config` 与公开example声明，实际运行值按环境传入：

| 配置变量 | 默认 / 启用规则 |
| --- | --- |
| `DIRECTORY_SOURCE` | Python 兼容默认 `legacy`；`database` 必须匹配数据库 active/empty-ready 状态，不允许异常后自动回退 |
| `DIRECTORY_EMPTY_INITIALIZATION_ALLOWED` | 默认 false；仅经确认的新装配置为 true，一次性初始化平台状态，不创建业务行 |
| `ORGANIZATION_MANAGEMENT_ENABLED` | 默认false；schema/目录就绪后开启功能capability；前端固定入口独立按APIM及菜单权限生成，页面处理功能不可用 |
| `DIRECTORY_SYNC_ENABLED` | P2，默认 false；schema、worker 包、连接、凭据/consent 验证后独立开启 |
| `DIRECTORY_IDENTITY_PROJECTION_ENABLED` | P2，默认 false；原 ledger 目标、身份 writer RBAC、policy 契约与读回验证后独立开启 |

不要用浏览器 `source`、`PRODUCTION` 或账号表是否为空代替初始化授权；环境变量中只放运行开关，企业组织和 Group 映射放数据库。新装 Bicep 可以显式 opt-in 空目录模式，升级沿用原 settings 并保持 legacy，直到完成第 11.5 节切换。

原有 `LEDGER_SYNC_ENABLED`、`CONTROL_PLANE_ENABLED`、发布/release 门禁、image flag、证据 v2 cutoff、bootstrap hash 与 credential encryption key 全部独立保留。关闭组织菜单不应停止既有月度预算继承，也不等于安全回滚。

### 11.3 全新安装

1. 使用新 prefix/私有 state，执行现有 what-if、批准与部署流程，迁移完整编号链。任何 DDL 失败不得继续 bootstrap/serve。
2. 现有 bootstrap 创建首个 Owner。不改邮箱/密码规范、不把 Owner 自动认作业务人员。
3. API lifespan 在显式空初始化授权下校验新 schema，并在数据库锁内验证无历史用量、预算、人员模型策略、客户应用归属及未完成升级；写一次 `fresh_empty_ready` 平台控制状态。已有 ready 状态重启只读校验，不能重置目录；并发启动不能分别初始化两个目录版本。
4. 此时健康、认证和空目录页面可用；不存在客户组织、部门、人员、模型和预算。未完成业务设置是正常空态，不等于允许伪造调用归属。
5. Owner 通过真实页面创建首个企业/部门/人员，显式绑定本人账号，按既有顺序配置预算和模型授权；无需要的真实项目时不能用生成的示例项目填充。
6. 重跑 deploy/启动后保留所有手工数据、改过的密码与头像；静态员工 APIM 归因需对齐并验证，不以 homepage health 代表治理已 ready。

本地手动安装使用相同 migrate/accounts/API 入口和显式新装空目录配置。演示 fixture 仅在现有显式 demo 隔离环境使用，不作为生产数据库回填来源。

### 11.4 业务回填 CLI 与幂等性

`scripts/directory_upgrade.py` 已有候选实现，公共工具不包含具体部署目标/SCM 凭据，
也不新增 `backend` 根入口；已完成P1生产plan/apply/verify/activate及兼容回滚验证。
已激活实例例行更新不得重跑回填或activate；最新 `9cc17fa` 只验证两次空迁移。完整约定见
[运维文档](../../docs/organization-management.md)：

```bash
uv run python -m scripts.directory_upgrade plan --output <private-plan-path>
uv run python -m scripts.directory_upgrade apply --plan <private-plan-path>
uv run python -m scripts.directory_upgrade verify --run-id <upgrade-run-id>
uv run python -m scripts.directory_upgrade activate --run-id <upgrade-run-id>
```

- 通过现有环境秘密获取 `DATABASE_URL`，不在命令行传连接串。运行于已授权且可达 DB 的运维环境；禁止为了命令临时打开 firewall/修改数据库 SKU。
- plan 是只读预览，不自动执行 migrate、bootstrap、Graph、云部署或赋权；只保存本次业务计划的私有文件（目录0700、文件0600），不是新的平台 secrets/state。
- plan 绑定数据库实例标识摘要、schema checksum 集、候选代码/protocol hash、源目录配置/业务引用摘要及规则版本。apply 前重读，漂移则拒绝并重新预览。
- source 使用当前受控旧目录与数据库实际引用的并集，不读取网上默认目录或把全新实例20个测试人员当员工。确认的旧组织/部门保留 ID；未确认的历史scope进入候选，清理种子要单独批准。
- apply 要显式确认，数据库 lease/CAS、稳定外部/治理键、批次 checkpoint 保证重跑幂等；重复请求不新建人员、不重复审计、不重置人工修改、预算或授权。
- 数据库写入与升级阶段一同提交。中断保留staging与已确认物化checkpoint，续跑同计划；旧权威源不变，失去 lease 不能推进状态。SQL failure不能留下“已完成”的 marker。
- verify 检查主部门约束、目录/预算父级、孤儿 ID、模型策略、账单/证据计数和各周期额度/用量，生成 pass/conflict 报告；显式批准的目录差异不能掩盖计费差异。
- activate 不是任意新配置 writer：先核对运维排他锁、匹配包/protocol、完成回填和影子结果，CAS 激活已验证directory version；环境开关变更由既有部署/settings 流程完成，不把 Azure 全量部署塞进 CLI。

最新遥测只证明某次请求归属，历史有效区间不能由最新部门倒推。已存周期预算父级优先保留，不为“全库 ready”捏造完整 HR 历史。

### 11.5 既有安装升级顺序

1. 记录原包与 SHA-256、mounted asset、运行/停止状态、migration checksum、预算/usage/模型授权基线、当前 APIM revision 和私有 state/outputs。确认备份可恢复；Burstable 不承诺可创建 on-demand backup，采用已有自动备份能力或授权环境 `pg_dump`，恢复测试另行批准。
2. 保持现有权威目录及新写入口/worker关闭，排除管理员配置写入，安排维护窗口。旧消费/发布与新目录切换关系逐项确认，不为本功能任意丢弃 Event Hub checkpoint。
3. 在授权 DB 环境先运行现有 `backend.migrate`，新增编号 chain 必须 additive；不直接执行 SQL，不改变001-011 checksum。schema_only 不代表可自动切换。
4. 首次旧目录升级发布兼容过渡三包，保持明确legacy来源、新能力关闭；检测protocol/digest，
   不混用核心不匹配的Telemetry。已激活实例按后述兼容更新，不再回到legacy流程。
5. 运行 plan，批准来源和候选，apply/verify；在受控影子读取中对比目录、周期预算、个人页、模型策略、助手/历史报表。冲突未解不得 activate。
6. 首次切换时暂停三类目录/治理写者，合并匹配source/digest后CAS激活已验证版本，
   恢复原状态并检查ready。未active/核心不匹配时拒绝目录读写；APIM兼容单独确认。
   最新例行更新仅暂停触发器、发布匹配三包并读回，不重新激活或恢复退役调岗。
7. 恢复记录的运行状态和写入，验证 API capability、真实登录/UI、原有查询/预算/模型、月度继承与新目录 CRUD；重新运行 migrate/CLI 应无重复副作用。
8. P2 的 Graph 和 identity projection 另走独立可批准步骤：先部署 worker/RBAC/凭据，确认映射及增量 policy/readback，然后逐一开启开关。不得因 P1上线自动启动它们。

未经目录切换的 legacy 模式必须维持现有行为；一旦新目录写入真实数据，不能通过简单切回legacy抹掉它们。只要必要包/DB/验证门禁不完整，部署结果记录为 blocked/未验收，而非“新前端已经上线所以完成”。

### 11.6 脚本、基础设施与打包改造清单

| 文件 / 契约 | 当前改造 / 保持的边界 |
| --- | --- |
| `backend/migrate.py` | 全局数据库迁移锁、有限等待、失败一致性；仍处理 `.up.sql` 与旧checksum，不进行业务回填 |
| `backend/bootstrap.py` | 保持首个Owner幂等创建；测试证明组织升级/重启不能重置账号；不挂Graph或大批数据迁移 |
| `backend/api.py` | lifespan轻量schema/readiness/版本校验、能力/状态响应和CORS/敏感错误；初始化仅受控平台状态，不隐式修业务行 |
| `scripts/directory_upgrade.py` | 受控业务plan/apply/verify/activate；核心服务复用，目标与计划敏感数据私有 |
| `scripts/stage_deployment.py` | API精确纳入公共CLI与必要schema文件，不复制整个私有运维目录；Functions带core服务，仍排除backend与SQL runner |
| `scripts/deploy.py` | 新装空目录opt-in、既有升级前置状态门禁、三包匹配/capability验证；P2维护 `EXPECTED_FUNCTIONS`，保留state、原密码验证与无Delete规则 |
| `infra/main.bicep`、data/control-plane modules、examples | 新装运行开关及必要身份设置，默认sync/projection关闭；不创建客户Entra或Foundry资源 |
| `infra/runtime-release.bicep` | 原例行发布受digest/目录权威源门禁，不能代替激活目录的三包维护流程 |
| `infra/directory-settings-upgrade.bicep` | 已实现三appsettings精确快照合并、source/digest及独立开关；what-if仅允许授权配置，不重建ledger或bootstrap |
| `scripts/apim_upgrade.py` 与版本化升级契约（P2） | 独立directory identity版本，不复用或改写images-v2成功日志；结构化转换、候选revision、完整readback、漂移拒绝与显式回滚 |
| `functions/telemetry/function_app.py` | 保留原继承与账本协议，月度调岗执行退役；不访问Graph，池生命周期优化另列待办 |
| `functions/control_plane/function_app.py` | directory sync/projection独立timer与lease；host索引不得因没配Graph就失败，disabled时无网络副作用 |
| config、API/core/Functions requirements与lockfiles | 仅需要的新依赖精确锁定，Linux x86-64/Python3.11三包一致；不依赖开发机临时库 |
| 公共docs与OpenAPI | 配置、升级/回滚、威胁边界、真实验证与初始化路径同步更新，中英文README保持一致 |

云settings/RBAC/policy变更必须有 reviewed what-if、精确scope、无Delete和授权。使用已有managed identity/Key Vault机制；Graph consent不是 `az deployment` 自动获得的权限，P1不得默默授目录读取权。

### 11.7 回滚、恢复与ready判定

- schema回滚默认不降级/删表；停止新增写入口与独立worker，保留目录、升级批次、audit与迁移记录。将数据库真正恢复到备份是独立恢复事件，不是包rollback。
- 新目录上线前保留匹配的过渡版本包，确保可读取原治理键与新目录结构。新ID进入生产后，只有已验证能读该active_version与schema的包才允许回滚；“给旧种子程序导出快照”并非现有受支持能力。
- mounted asset/hash、API/core版本、schema ledger、source/active_version、各worker索引/版本、数据基线共同判定ready；`/health` 200或OneDeploy成功不能代替这些结果。
- 恢复记录的运行状态，不无条件启动原本停止的应用。lease/中断job/未决请求依旧可诊断；不能删除journal强行跳过冲突。
- 恢复APIM旧revision前检查后续发布与禁用变化；不得重新开启已撤销身份，不移动证据cutoff、不清空pending reservation或confirmed量。
- 备份、计划、真实资源IDs、readback证据与凭据均保留私有；P1发布及早期兼容回滚已验证，
  `9cc17fa`本轮未重复回滚演练，生产数据库恢复未执行，不能与隔离恢复混同。

## 12. 验证计划

| 层 | 必须覆盖的场景 | 不能据此声称 |
| --- | --- | --- |
| 域/服务单元 | 树约束、稳定键、成员去重、唯一主部门、归档/授权撤销和退役工作流拒绝 | 真实DB锁与网关已更新 |
| 真实PostgreSQL | 从011升级至017、重跑、旧checksum/账务保持、membership约束、成员/授权并发与旧计划退役 | Graph授权与最终一致性 |
| API权限 | Owner/授权部门Member/普通Member、篡改ID、bulk、detail、total、history、个人页、助手及固定报表范围；admin/service不得被当作第三类登录角色 | 浏览器布局正确 |
| 契约/前端 | 旧enterprise/budget契约；APIM 菜单/搜索可见、GitHub Copilot 隐藏；来源切换/直达/历史恢复归一化、停止页面预取/轮询、企业团队回归、cache清理、长文本、多语言、键盘、移动端 | 已完成生产目录迁移 |
| 历史回归 | UI管理状态与旧API历史兼容、ID改名总量/原名称保持、旧周期父级、无策略/空策略；不验收退役调岗 | 所有旧别名已正确合并 |
| Graph mock | next/delta link、重复页、multi-group冲突、Guest、无mail、hidden、403/429/失效游标、删除阈值 | 目标租户consent已授予 |
| Graph授权环境 | 指定租户、选定Groups、应用权限/字段、跨租户隔离、改名/移组/禁用、全量/增量收敛 | APIM映射自动生效 |
| 网关授权环境 | 可信tid/oid映射、header伪造、旧/新邮箱同一账本、缓存TTL、禁用无预算人员、版本回放、未决请求归属不变 | 本地测试足以证明计费 |

新增初始化/升级覆盖：空业务库与首次Owner真实登录、bootstrap重跑保持改过密码/资料、多个migration并发、已应用checksum拒绝、pending多文件失败回滚、plan目标漂移、apply中断续跑、staging/未激活目录不影响旧读取、重复批次不克隆同ID、三包不匹配拒绝activate、迁移后旧预算/usage/策略/账号保持、回滚新ID仍可读。

复用 `test_user_settings_migration.py` 的私有Unix socket/本地PostgreSQL16+隔离模式，新增组织迁移套件，不接入生产 `DATABASE_URL`；工具缺失/版本不符明确skip，不能计作migration pass。core/backend/Functions分层和不含backend的真实staging索引测试必须加入。

重点回归包括 `tests/platform/architecture/test_architecture.py`、`tests/platform/contracts/test_migrations.py`、`tests/platform/deployment/test_deploy_script.py`、`test_deployment_artifacts.py`、`tests/backend/test_bootstrap.py`，以及原预算/API/ledger/助手/应用测试。

实施时运行根README完整命令集：ruff、mypy、pytest、frontend build、Bicep build、APIM XML、OpenAPI lint、diff检查；frontend typecheck额外补充。新模板/Function入口须追加编译和索引验证，不能仅测试原main。E2E按同候选完成新装与升级两条路径、桌面和390px移动端、真实Owner/部门Member会话。每项记录 passed/failed/blocked/not exercised；单元mock与源码检查不替代真实数据库/Azure/浏览器。文档阶段不执行云同步、付费探针或上线迁移。

## 13. 当前实现状态与退出条件

| 能力 | 实现/部署 | 验证边界 |
| --- | --- | --- |
| 数据库目录、组织人员与成员、精确范围任命 | 已上线，001-017 | 新装/升级及真实API/浏览器通过；不提供正式HR调岗流程 |
| 目录状态排序、人员弹窗、预算层级与团队筛选 | 已上线 `1d82983`，`9cc17fa`复验 | 原预算控制/存储/模型授权不变 |
| 用量多选与查询目录、AI FinOps报表中心 | 已上线 `9cc17fa` | 1758 passed / 7 skipped及线上只读复验；兼容未知数据未删除 |
| 同步、身份绑定/投影、独立APIM工具 | 代码已部署，真实开关关闭 | 仅Graph夹具/数据库、候选Azure编译与读回通过，真实租户/员工调用未验收 |
| 停启后就绪、连接池恢复与静态资源优化 | 尚未实现改造 | 运行事件与设计缺口见[运行恢复边界](../ai-finops/design.md#availability-and-recovery-2026-10-11) |

- 确认是否需要部门管理员预算/模型可选授权；默认关闭，启用前必须完成全部服务端范围路径。
- 确认目录人员停用是否要求同时禁用登录账号；分别存状态，但Owner可批准联动。
- 多组织当前支持目录与查询，不宣称所有现有接口已经具备tenant隔离；任何仍依赖第一组织的生产调用在启用第二组织前必须修复。
- 明确P1测试项目/智能体兼容来源及调用无project的语义；不得因组织CRUD做完就宣布调用流程完整。
- Graph云、Guest、隐藏Group、嵌套展开及资料保留策略需目标租户验证。
- P1上线退出条件：空目录新装与既有数据升级分别验证；迁移串行化/幂等、三包/状态门禁、兼容回滚通过；APIM入口与GitHub模式隐藏/归一化、独立企业团队回归通过；共享目录切换、旧治理键、历史查询/权限、静态网关对齐与生产映射均有对应证据。
- P2退出条件：可信外部身份/账号绑定、Graph任务安全性、动态网关映射及停用传播全部在授权环境验证；不能仅以同步任务返回200作为交付。
- 持续可用性不能沿用单次发布验收结论；数据库外部自动关停、重连耗时与health依赖缺口需独立处理，
  未经授权不得停启真实服务做故障测试或自行豁免治理策略。

## 14. 官方参考

2026-10-10 核对了官方文档源码中的用户列表、Group直接成员、Group delta及对应权限表；未进行目标租户调用。其他接口与实际组合权限在实施前仍需核对。

- [Microsoft Graph：List group members](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-1.0)：非递归、对象类型、分页、隐藏成员权限。
- [Microsoft Graph：List users](https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0)：字段选择、应用权限、最终一致性。
- [Microsoft Graph：Group delta](https://learn.microsoft.com/en-us/graph/api/group-delta?view=graph-rest-1.0)：members变化、nextLink/deltaLink及有限的ID过滤。
- [Microsoft Graph：User delta](https://learn.microsoft.com/en-us/graph/api/user-delta?view=graph-rest-1.0)。
- [Microsoft Graph：Transitive group members](https://learn.microsoft.com/en-us/graph/api/group-list-transitivemembers?view=graph-rest-1.0)。
- [Microsoft Graph：Permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)。
- [Microsoft identity：Client credentials flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow)。
- [Microsoft Graph：Paging](https://learn.microsoft.com/en-us/graph/paging) / [Throttling](https://learn.microsoft.com/en-us/graph/throttling) / [Delta overview](https://learn.microsoft.com/en-us/graph/delta-query-overview)。

当前源码事实及可点击文件依据见[需求分析第9节](requirements.md#9-源码依据)。

## 权限管理后续实现说明

2026-10-11新增[权限管理设计](../permission-management/design.md)：
菜单配置从固定四组矩阵扩展为Owner持久化配置；目录模式Member读取由有效管理员任命/
同级成员关系自动派生人员范围，普通用户仅本人。本文早期“普通Member保留全域读取”
或“组只控制导航”的描述仅为018以前的基线，不再作为新版本权限规范。
管理员指派继续留在组织管理，权限管理不重复提供指派/数据范围配置。
