# 功能规划

本目录保存功能的需求分析和设计方案。各功能的实现状态见下表及对应文档；拟议接口、字段和组件不代表已经实现。

更新日期：2026-10-11。最近已从用户仓库 `main` 提交 `22897a5` 构建并发布，
运行内容保留 `4c2dadc` 单菜单权限修复。组织/权限治理上游 PR #34 已提交，
main本地与线上验收通过，结果见实施记录；上游CI待维护者批准。
文档更新不代表重新发布，不将运维恢复等同于代码修复。

| 功能 | 需求分析 | 设计文档 | 状态 | 已提交 PR |
| --- | --- | --- | --- | --- |
| 模型定价同步 | [requirements.md](model-pricing-sync/requirements.md) | [design.md](model-pricing-sync/design.md) | 已实现、已上线；010不可变，后续共享包保留；原验收边界见验证记录 | [xuleihive/turnstile#31](https://github.com/xuleihive/turnstile/pull/31)，Open，待合并 |
| 用户设置 | [requirements.md](user-settings/requirements.md) | [design.md](user-settings/design.md) | 已实现、已上线；当前目录集成及本人只读接口已随 `9cc17fa` 复验；真实 Microsoft OAuth/Graph 未重新验收 | [xuleihive/turnstile#32](https://github.com/xuleihive/turnstile/pull/32)，Open，待合并 |
| 组织管理 | [requirements.md](organization-management/requirements.md) | [design.md](organization-management/design.md) | P1 已上线；001-018 已应用，成员/权限/预算与用量兼容已复验；P2 代码已部署，真实 Entra 与动态网关未验收且开关关闭 | [xuleihive/turnstile#34](https://github.com/xuleihive/turnstile/pull/34)，Open，待上游审查 |
| 权限管理 | [requirements.md](permission-management/requirements.md) | [design.md](permission-management/design.md) | 已上线：4c2dadc修复单菜单共享API 403，001-018；本地1774通过及另跑7项，线上普通用户四页面验收通过，见[实施记录](permission-management/implementation.md) | [xuleihive/turnstile#34](https://github.com/xuleihive/turnstile/pull/34)，Open，待上游审查 |
| AI FinOps 与报表中心 | [功能范围](ai-finops/design.md) | [design.md](ai-finops/design.md) | 已随 `9cc17fa` 上线；完整本地回归 1758 passed / 7 skipped，线上只读 API、桌面/手机验收通过 | [xuleihive/turnstile#34](https://github.com/xuleihive/turnstile/pull/34)，纳入组织/权限集成改造 |

每个功能使用独立子目录。需求和实现发生变化时，应同步更新对应文档；不得将计划中的能力描述成现有行为。

## 当前设计边界

- 平台管理按固定顺序显示组织管理、系统配置；模型平台按模型管理、订阅管理、负载均衡、网关发布排序，多语言一致。
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

最新发布保持schema 001-017、预算控制/存储与APIM revision；历史用量未删除。
所有真实备份、账号、资源与截图证据保留私有，生产验收未写业务数据或发起模型调用。

PR 列记录对应功能已提交的开发 PR；#31/#32 于 2026-10-11 只读核对仍为 Open，
与当前仓库的实现和部署状态分别记录。本地 1758 passed / 7 skipped 为 `9cc17fa`
发布前结果，本次仅更新文档，不冒称重新运行完整业务回归。
