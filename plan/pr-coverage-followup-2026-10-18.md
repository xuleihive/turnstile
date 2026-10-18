# 上游 PR 覆盖补齐

## 基线与目标

本次更新既有 PR，不创建重复 PR，不改写已发布提交，不部署生产。
比较基线为上游 main `e59177d` 与个人 main `697f43f`。

审计发现 main 相对上游有 150 个独有提交、497 个变更文件，
其中 144 个提交可从五个既有 PR 的 Head 到达。
剩余六个为五项 main 合并记录和一项发布文档提交，不能视为六项全新业务功能。
最终文件比较有 458 个文件与至少一个 PR Head 完全相同，39 个文件是合并、
兼容性与记录层面的组合差异；文件差异数量不等于遗漏功能数量。

## 本次处理

| PR | 本次更新与边界 |
| --- | --- |
| [#31](https://github.com/xuleihive/turnstile/pull/31) | 基于 `760f368` 补入报价发布幂等性修复 `040a14d`、回归测试及独立验证记录，不带入 main 的独立组织/插件功能 |
| [#35](https://github.com/xuleihive/turnstile/pull/35) | 原 Head `a18fca9` 已是 main 的祖先，先快进至 `697f43f`，再合并 #31 补丁，保留全部已有历史及主分支整合结果 |
| #30 / #32 / #34 | 保持原功能分支范围；其最新已提交 Head 和跨功能整合均由 #35 的提交历史覆盖，不反向混入整个 main |

#35 因此包含了此前仅在 main 的：

- `cdb9e9f`：用户设置与目录身份、USD 未知费用及前后端字段整合。
- `ef09fdd`：组织与个人设置兼容、语言及契约收尾。
- `3f2b974`：助手协议、Claude/Responses 调用、错误/追踪及契约整合。
- `2a9fe6b`：企业插件文档合并，原本没有额外人工冲突修改。
- `8e4b568`：定价兼容合并、重复报价修复及测试。
- `697f43f`：最新发布验收记录、补验结果和未完成边界。

合并 #31 补丁时，代码和测试与 main 已有版本相同，实际合并增量仅为补丁说明。
相关已有迁移、业务协议、锁文件和 CI 不因本次 PR 覆盖补齐而变化。

## 验证与发布边界

#31 补丁先在原候选复现失败，再完成定价、发布、合同及真实 PostgreSQL 专项
379 passed；Ruff 通过，Mypy 241 个源文件通过。
详见[补丁验证记录](model-pricing-sync/publication-idempotency-2026-10-18.md)。

### 最终集成回归

| 检查 | 实际结果 |
| --- | --- |
| 完整 Python 套件 | **4790 passed，0 failed，0 errors，0 skipped**；一次完整运行，不是累加专项 |
| 环境 | 本机已有 .NET SDK 8.0.426；自建仅 Unix socket 可达的 PostgreSQL，应用完整 40 项迁移，显式用户设置存储测试也实际执行 |
| Ruff / Mypy | 通过；Mypy 376 个源文件 |
| 助手设置、模型编辑与发布前端专项 | 75 passed，无失败或跳过 |
| TypeScript / Vite 构建 | 通过，保留大 bundle 提示 |
| OpenAPI / Bicep / APIM XML | 本地校验、编译、解析通过；OpenAPI 49 项、Bicep 3 项既有警告 |
| 运行代码对比 | backend/core/frontend/contracts/functions/infra/migrations/scripts/tests 与 main `697f43f` 无差异，核心摘要仍为 `f50ba232d015f0b700feaa6b4f01ee6c3ba37a26a4f625519ba6e6f9fa7271fb` |

Python 保留 13 条警告：Starlette 弃用 1 条、Pydantic 序列化 10 条，以及两项规模测试
使用 `record_property` 与本轮 JUnit xunit2 格式不兼容的报告警告。没有改动断言、
跳过 .NET 或弱化 CI。完整回归完成后再次验证文档链接。
本次临时 PostgreSQL 已停止并清理，未连接生产数据库。
GitHub CI 的审批与执行状态独立读取，不把本地结果视为上游 CI 已通过。

### 覆盖核对

本地已确认 `697f43f` 及 #31 的 `040a14d` 均是 #35 候选的祖先。
推送后重新读取 GitHub 实际 Head，核对工作区/远端一致、Ready for review 状态、
main 相对上游提交的 PR 覆盖，以及运行代码和迁移的最终差异。
该步骤不要求将公开 CI 标记为成功，也不绕过维护者对外部分支工作流的批准。

上次线上发布的代码来源仍为 `8e4b568`，详情见
[发布与验收日志](model-pricing-sync/release-2026-10-18.md)。
本次没有重新发布、连接生产数据库、改变预算/权限或发起付费推理。
新增模型发布原有 409 与 WorkBuddy 桌面交互未验收的边界没有因更新 PR 而消失。
私有凭据、配置、安装包、原始金融证据和恢复密钥不提交 Git。
