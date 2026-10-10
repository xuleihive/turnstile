# 组织管理设计规范核查

| 项目 | 内容 |
| --- | --- |
| 核查日期 / 源码基线 | 2026-10-10 / `12dc1ee` |
| 范围 | 当前需求、设计与根README/CONTRIBUTING、docs、实际初始化/迁移/部署/架构测试的静态对照 |
| 结论 | 原草案的分层和安装/升级缺口已在设计中补齐；本文保留设计核查基线。后续开发与真实验证见[实施记录](implementation.md)，不以静态核查代替整体验收 |
| 关联 | [需求分析](requirements.md) / [设计](design.md) |

当前更新：2026-10-11，源码核对 `ee27527`。第1-4节保留原始设计审查记录；
其中“未执行/实施时”仅描述当时，不代表P1目前尚未发布。当前规范落点与验收见第5节。

## 1. 主要发现与处理

| 优先级 | 原设计缺口 / 风险 | 本次修订 |
| --- | --- | --- |
| P1 | 目录核心逻辑集中 `backend/services`，worker复用会违反core不能import backend及Function打包边界 | shared CRUD/任职/策略移到core/services，Web只做会话适配与页面聚合，Graph integration不向上依赖 |
| P1 | 通用“种子回填”未区分新装和升级，新安装可能注入示例企业/人员，违背空业务库规范 | 新装只初始化平台状态，首个组织由Owner创建；旧实例经计划批准才回填原ID和来源 |
| P1 | 没明确启动/回填分工、迁移串行化与切换状态，可能多实例重复DDL或目录先切账务未切 | 原三条启动入口不变，补数据库迁移锁、私有staging/批次/active_version、plan/apply/verify与三包门禁 |
| P1 | 单纯部署新版包/改参数无法更新已有APIM；旧实例saved outputs跳过bootstrap | 明确P1 DB/包/settings与P2 Graph/RBAC/policy分开；P2使用独立版本化增量升级和readback |
| P1 | 简单回退种子程序会失去上线后新ID，回滚还可能恢复已撤销身份 | 只回滚已验证兼容包，保留目录/审计/账本；真正数据库恢复另行审批 |
| P2 | 新运维模块若放backend根会破坏架构入口白名单，且Functions不带该包 | 公共CLI放scripts，API精确打包必要文件；Functions只运行core与自己的入口 |
| P2 | 预算/模型可选委派与现有安全文档Owner-only原则存在扩展边界 | P1保持原治理写权限，委派默认关闭；目录范围写权限显式声明，后续委派先评审规范和测试 |
| P2 | Idempotency-Key/CORS、敏感验证错误、API层与cache未完整对应工程测试 | 补开发CORS、no-store/错误脱敏、前端API层及scope cache约束 |
| P2 | 参考截图含原环境地址/账号，不适合作为公开PR资料 | 文档截图副本脱敏；真实计划、目标和部署证据留私有，原附件保留 |

以上是**设计缺口**，不是对当前业务源码声称已经发现/修复运行Bug。设计修订不等于实现完成。

## 2. 规范追踪矩阵

| 规范来源 | 强制边界 | 设计落点 / 后续验证 |
| --- | --- | --- |
| [README](../../README.md) | PostgreSQL16+、既有migrate/accounts入口、锁定依赖、三包部署、完整检查命令 | 设计11.1/11.3/11.6/12；新装与升级独立真实验证 |
| [CONTRIBUTING](../../CONTRIBUTING.md) | main上的focused branch、每次行为变化有测试、已应用迁移不可改、infra reviewed what-if与授权 | 设计11.2/11.5/11.6；实施前重新核对上游及迁移序号，不混入无关提交 |
| [Architecture](../../docs/architecture.md) | core不import backend，Functions排除backend；APIM推理路径不依赖Web | 设计2.1/10.5/11.6；AST分层与staging索引回归 |
| [Configuration](../../docs/configuration.md) | env配置、Owner只在空账号表创建、feature gates独立、证据cutoff未来一次性 | 设计11.2/11.3；保留账号/镜像/ledger/cutoff，不把Group映射放环境常量 |
| [Deployment](../../docs/deployment.md) | 新装/升级分开、原state/outputs保留、无Delete、APIM增量、backup/rollback有边界 | 设计11.4-11.7；不会以清空state或重跑bootstrap“修复升级” |
| [Testing](../../docs/testing.md) | 无客户种子、真实数据库与浏览器验证、单元/源码检查不冒充E2E | 设计12与需求AC14-18；migration isolation、390px与候选结果记录 |
| [E2E Validation](../../docs/e2e-validation.md) | 真实网页工作流，deployment IDs和证据私有 | 设计11.7/12；通用公共文档不写实例部署历史 |
| [Security](../../docs/security.md) | Owner治理写、最小权限MI、敏感资料不日志化、公开提交不带秘密 | 设计5/8/10/11.6；scope授权、Graphconsent分离、截图脱敏与secret scan |
| [Troubleshooting](../../docs/troubleshooting.md) | startup首个错误、Function indexing、mounted asset读回而非盲目restart | 设计11.1/11.7；ready诊断与原包/hash保留 |
| [Architecture tests](../../tests/platform/architecture/test_architecture.py) | core层白名单、backend根入口、前端API层/no identity header | 设计2.1/8/11.6；不能为新功能绕开既有断言 |
| [Migration tests](../../tests/platform/contracts/test_migrations.py) | 精确迁移链/旧checksum、无down、初始空数据 | 新迁移编号加至清单，不更新旧hash、不把目录schema塞进001 |
| [Staging tests](../../tests/platform/deployment/test_deployment_artifacts.py) | API含编号链、Functions无backend且实际可索引 | API新增公共CLI范围精确，Function core依赖可用，未配Graph仍可索引 |

## 3. 实施时必须同步的公共文档

- `README.md` / `README.zh-CN.md`：组织能力、首个组织设置、既有安装升级入口及真实限制。
- `docs/architecture.md`：目录服务/任务所在层、APIM身份快照、旧账务边界不变。
- `docs/configuration.md`：实际新增变量、默认关闭与readiness条件，不宣传计划字段已支持。
- `docs/deployment.md`：schema与业务回填分离，新装/升级/中断续跑、独立P2升级、兼容回滚。
- `docs/testing.md` / `docs/e2e-validation.md`：新迁移链、初始化/并发/升级真实用例及两路径候选验收矩阵。
- `docs/security.md` / `docs/troubleshooting.md`：目录范围授权例外、角色不变、凭据/PII与升级状态诊断。
- OpenAPI schemas/paths、`.env.example` 与Bicep参数examples：按实际实现同步，禁止填真实组织、租户、账号或秘密。

这些文件本次只作为核查依据，未为未实现功能提前改写运维操作说明。实施PR需保持其与代码同候选一致。

## 4. 本次验证与剩余门禁

已完成：源码/文档静态对照、设计内一致性检查、Markdown本地引用与图片格式/脱敏区域检查。未执行：业务测试、DDL/回填、真实DB恢复、Azure what-if/发布、Graph连接及浏览器新功能E2E。

正式实现仍需：执行根README完整检查、架构/契约/staging测试、独立PostgreSQL升级与并发测试、真实浏览器和授权Azure验证、公开提交secret scan。P2的consent与投影/禁用传播另有门禁，不随P1默认开启。

当前未解决的业务选择（管理员委派、Guest/hidden Group策略、资料保留等）见设计第13节；它们不应通过放宽现有安全规范来省略。

## 5. 实现后规范复核（2026-10-11）

| 规范边界 | 当前落点 / 已有证据 | 状态 |
| --- | --- | --- |
| 不建设HR系统、精确权限范围 | 组织创建普通Member、附加成员、只读组标签、scope kind/id任命，调岗编辑退役 | 已实现/上线 |
| 菜单/平台角色/数据范围/APIM分离 | 固定菜单白名单、Owner写限制、department grant与原网关授权；旧岗位/Graph Group不提权 | 已实现/回归 |
| core/HTTP/Functions分层 | directory_store/services共享，HTTP适配会话，Functions不打包backend，前端客户端归APIM来源 | 1758项发布前完整回归含架构/staging检查 |
| 初始化与已应用迁移不可变 | migrate advisory lock、bootstrap幂等、001-017；最新两次生产空迁移 | 已验证，无新SQL或重做激活 |
| 三包/配置及恢复门禁 | 同core digest、限定appsettings what-if、实际包逐文件读回、触发器原状态恢复 | `9cc17fa`发布通过 |
| 预算/账务兼容 | 用量状态只读投影、团队只筛名单、周期父级与拦截存储不改；数据和APIM指纹保持一致 | 已验证 |
| 真实验证与私有证据 | 本地1758 passed / 7 skipped，线上Owner及Light/Dark桌面/390px；备份隔离恢复 | 通过项与skip/未执行明确区分 |
| Entra/P2安全性 | 代码和候选编译已验证；Graph/员工真实调用、动态切换/撤销仍未验收 | 未完成，不将模拟当作交付 |
| 可用性与性能 | 外部数据库停启、后续登录恢复已诊断；health不含依赖，池生命周期和压缩/缓存待改 | 待实现，不以Running或单次验收保证持续健康 |

该表同步当前设计，不新增云配置或修改业务行为。最新源码/部署/PR状态见
[规划索引](../README.md)；数据库停启不归入目录功能的自动修复。
