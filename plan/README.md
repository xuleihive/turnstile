# 功能规划

本目录保存功能的需求分析和设计方案。各功能的实现状态见下表及对应文档；拟议接口、字段和组件不代表已经实现。

2026-10-18 已补齐上游 PR 覆盖：纯定价幂等补丁更新 #31，main 的跨功能整合和发布记录更新
#35；#30/#32/#34 保持各自范围。提交覆盖与验证边界见
[PR 覆盖补齐记录](pr-coverage-followup-2026-10-18.md)。已提交不代表已合入上游或 CI 已通过。

2026-10-18 已合并组织/权限分支至本地 main；PR 专用分支的上游同步与验证记录见
[PR #34 同步记录](upstream-main-sync-2026-10-18.md)。该记录的 38 条迁移仅对应 PR 候选版本，
当前 main 保留完整 40 条迁移及后续 Client/预算能力。本次合并不代表部署上线。

客户端文档统一从 [AIGateway-Proxy 入口](aigateway-proxy-client/README.md) 阅读。
[预算兼容设计](aigateway-proxy-client/budget-auth-compatibility-upgrade-design.md)覆盖自主组织/密码账号、
受信后端委托和双单位预算；通用安装与恢复入口见[预算运维手册](aigateway-proxy-client/operations.md)。
2026-10-18 已验收核心链路、组织 100 USD / 部门 70 USD 及真实结算，
观察时间和限制只维护在 [USD 验收记录](aigateway-proxy-client/usd-installation.md)，不是实时健康保证。

## 文档职责与入口

`docs/` 原有上游文件的字节内容保持 `main` 基线 `e59177d` 一致；本分支的设计、配置、升级和验收说明维护在对应 `plan/` 文档。
用户另行要求的终端用户与管理员使用文档放在 [docs/enterprise-plugin](../docs/enterprise-plugin/README.md)，附真实脱敏截图，不承载阶段运维日志。
上游通用手册不等于当前分支的实际开关或完整迁移链，操作当前分支时同时阅读以下补充：

| 范围 | 当前分支入口 |
| --- | --- |
| 组织目录、兼容三包发布、身份同步 | [组织运维](organization-management/operations.md) |
| USD 安装、结算、恢复与运行开关 | [预算运维](aigateway-proxy-client/operations.md) |
| 插件后续能力：TPM/RPM 感知与请求调度 | [插件 Roadmap](aigateway-proxy-client/roadmap.md)，规划未实现 |
| 用户设置、登录与个人配置升级 | [用户设置设计](user-settings/design.md#当前分支配置与验证补充2026-10-18) |
| 角色、菜单及安全边界 | [权限实施说明](permission-management/implementation.md#当前分支配置与验证补充2026-10-18) |
| FinOps 助手协议与验证 | [FinOps 设计](ai-finops/design.md#当前分支配置与验证补充2026-10-18) |
| 完整 40 条迁移及跨功能升级验证 | [合并兼容说明](upstream-main-merge-2026-10-16.md#当前分支配置与验证补充2026-10-18) |
| 本次真实 USD 用户验收 | [验收记录](aigateway-proxy-client/usd-installation.md) |

文档契约测试同时校验原上游文件哈希、企业插件专区清单和 `plan/` 分支说明。
未来更新上游时，须审查并同步基线哈希；不将阶段日志或内部运维配置混入使用指南。

以下运行包与验证数字是 2026-10-12 的历史状态快照，不代表上述新方案已交付；
表中 PR 列已更新至 2026-10-18，不能将历史验证数字视为当前 PR 的重新验收：
当时运行包来自 `92378f7`（兼容核心）和 `47595da`（请求追踪时区前端），监控修复已验收。
应用TPM修复此前从功能分支提交 `217fbd2` 构建并发布，真实限流回归完成。
此前从用户仓库 `main` 提交 `22897a5` 构建并发布，
运行内容保留 `4c2dadc` 单菜单权限修复。组织/权限治理上游 PR #34 已提交，
main本地与线上验收通过，结果见实施记录；上游CI待维护者批准。
文档更新不代表重新发布，不将运维恢复等同于代码修复。

| 功能 | 需求分析 | 设计文档 | 状态 | 已提交 PR |
| --- | --- | --- | --- | --- |
| 初始化私网与请求监控 | [需求与设计](gateway-network-monitoring/design.md) | [design.md](gateway-network-monitoring/design.md) | 已实现；监控已上线，1812项Python/157项前端通过，现网hotfix no-op及00:37记录/自动刷新/时区已验收，见[实施记录](gateway-network-monitoring/implementation.md) | [#35](https://github.com/xuleihive/turnstile/pull/35)，相关提交随集成 PR 提交 |
| 应用 TPM 与额度同步 | [设计与需求](application-tpm-sync/design.md) | [design.md](application-tpm-sync/design.md) | 已上线；1805项本地通过，保存即时投影、正式低TPM 429/提高TPM 200已验证，见[实施记录](application-tpm-sync/implementation.md) | [#35](https://github.com/xuleihive/turnstile/pull/35)，相关提交随集成 PR 提交 |
| AIGateway-Proxy 客户端 | [requirements.md](aigateway-proxy-client/requirements.md) | [design.md](aigateway-proxy-client/design.md) | 双版插件、下载权限和个人预算下载门槛已有实现；历史发布见[索引](aigateway-proxy-client/release-history.md)，当前行为不沿用早期 cc-switch/Owner-only 草案 | [#35](https://github.com/xuleihive/turnstile/pull/35)，Open，待上游审查 |
| 客户端鉴权与 Token/USD 兼容升级 | [身份与边界](aigateway-proxy-client/budget-auth-compatibility-upgrade-design.md#身份与兼容边界) | [现行设计](aigateway-proxy-client/budget-auth-compatibility-upgrade-design.md)、[运维手册](aigateway-proxy-client/operations.md) | 核心链路、目标预算及普通用户 USD 单次调用/结算已验收；并发、流式及额度耗尽未全部覆盖，见[验收记录](aigateway-proxy-client/usd-installation.md) | [#35](https://github.com/xuleihive/turnstile/pull/35)，发布事实与 PR 验证分开记录 |
| 模型定价同步 | [requirements.md](model-pricing-sync/requirements.md) | [design.md](model-pricing-sync/design.md) | 已实现、已上线；010不可变，后续共享包保留；原验收边界见验证记录 | [xuleihive/turnstile#31](https://github.com/xuleihive/turnstile/pull/31)，Open，待合并 |
| 用户设置 | [requirements.md](user-settings/requirements.md) | [design.md](user-settings/design.md) | 已实现、已上线；当前目录集成及本人只读接口已随 `9cc17fa` 复验；真实 Microsoft OAuth/Graph 未重新验收 | [xuleihive/turnstile#32](https://github.com/xuleihive/turnstile/pull/32)，Open，待合并 |
| 组织管理 | [requirements.md](organization-management/requirements.md) | [design.md](organization-management/design.md) | P1 已上线；001-018 已应用，成员/权限/预算与用量兼容已复验；P2 代码已部署，真实 Entra 与动态网关未验收且开关关闭 | [xuleihive/turnstile#34](https://github.com/xuleihive/turnstile/pull/34)，Open，待上游审查 |
| 权限管理 | [requirements.md](permission-management/requirements.md) | [design.md](permission-management/design.md) | 已上线：4c2dadc修复单菜单共享API 403，001-018；本地1774通过及另跑7项，线上普通用户四页面验收通过，见[实施记录](permission-management/implementation.md) | [xuleihive/turnstile#34](https://github.com/xuleihive/turnstile/pull/34)，Open，待上游审查 |
| AI FinOps 与报表中心 | [功能范围](ai-finops/design.md) | [design.md](ai-finops/design.md) | 已随 `9cc17fa` 上线；完整本地回归 1758 passed / 7 skipped，线上只读 API、桌面/手机验收通过 | [xuleihive/turnstile#34](https://github.com/xuleihive/turnstile/pull/34)，纳入组织/权限集成改造 |

每个功能使用独立子目录。需求和实现发生变化时，应同步更新对应文档；不得将计划中的能力描述成现有行为。

## 当前设计边界

- 平台管理按固定顺序显示组织管理、权限管理、系统配置；模型平台按模型管理、订阅管理、负载均衡、网关发布排序，多语言一致。
- AI FinOps 只显示 FinOps助手、报表中心；报表列表在报表中心内部加载，不再生成动态侧栏入口。
- 人员只在组织层级新增；名称、邮箱、登录密码、归属部门必填。部门/团队添加已有成员，不允许嵌套团队。
- 范围管理员配置是标签和菜单身份的统一来源，按当前 kind/id 精确匹配；平台角色、部门数据范围和 APIM 权限分别控制。
- 组织/部门/团队/人员仅启用和归档；默认隐藏归档、启用优先/名称排序。调岗计划及历史身份编辑已退役，不是待完成交付项。
- 用量治理五页面共用多选下拉，归档状态默认启用；全部显式查询 active+archived。后端保留历史/未归属兼容读取，不删除历史数据。
- 预算层级保持组织/部门/人员和原存储/拦截逻辑；团队只筛选名单，未来控制或存储变更必须先审批。

## 待验收与待改造

| 项目 | 当前状态 | 后续边界 |
| --- | --- | --- |
| 指定 Entra 同步、控制台登录与员工网关身份 | 代码已实现；真实租户/权限/令牌验收未完成 | 三项分别配置与验收；同步不会自动完成登录配置或授模型权限 |
| 动态 APIM 身份策略 | 独立候选编译及读回通过，未 promote | 授权后验证真实员工调用、撤销传播与切换/回滚 |
| 数据库自动停启 | 已确认外部治理应用停止、用户启动及恢复；未改治理规则 | 在线依赖的自动关停豁免由资源治理方审批，不在部署脚本中绕过 |
| 断线恢复、依赖健康与静态加载 | 已诊断，代码改造未实现 | 连接池生命周期/重连、就绪与错误提示、压缩/哈希资源缓存分别验证 |

恢复事件与待改造设计见 [运行恢复边界](ai-finops/design.md#availability-and-recovery-2026-10-11)。
资源 Running/Ready、历史验收通过或当前查询恢复，都不等于持续可用性已获保证。

## 发布追踪

| 应用提交 | 已发布范围 | 验证 / 记录 |
| --- | --- | --- |
| `47b9c11` | 015范围管理员统一来源、016/017组织人员与附加成员，调岗编辑退役 | 真实成员/权限与兼容三包验证，见[实施记录](organization-management/implementation.md) |
| `1d82983` | 目录状态/排序、人员弹窗、预算层级/团队筛选及平台菜单多语言 | 当次1748 passed / 7 skipped；备份/隔离恢复、SCM失败恢复后重试通过 |
| `9cc17fa` | 用量多选/归档分类、固定AI FinOps及报表中心 | 1758 passed / 7 skipped、三包/配置/备份及线上只读验证，见[发布记录](ai-finops/design.md#production-release-2026-10-11) |

以上三次历史发布当时保持 schema 001-017、预算控制/存储与 APIM revision，未写业务数据或发起模型调用。
后续 main 合并及 USD 安装已扩展迁移链并进行获批的真实预算配置和推理验收，不能沿用这段旧快照判断现状。
所有真实备份、账号、资源与原始证据继续保留私有。

PR 列记录对应功能已提交的开发 PR；#30/#31/#32/#34/#35 于 2026-10-18 核对均为 Open、非 Draft，
与当前仓库的实现和部署状态分别记录。本地 1758 passed / 7 skipped 为 `9cc17fa`
发布前结果，本次仅更新文档，不冒称重新运行完整业务回归。
