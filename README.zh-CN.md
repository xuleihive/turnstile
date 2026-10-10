<p align="right">
   <a href="README.md">English</a> | <strong>简体中文</strong>
</p>

<p align="center">
   <a href="https://turn-stile.com">
      <img src="frontend/public/turnstile.svg" width="96" height="96" alt="Turnstile 标志">
   </a>
</p>

<h1 align="center">Turnstile</h1>

<p align="center"><strong>通过一个自托管控制平面统一治理 AI 访问、支出与运营。</strong></p>

<p align="center">
   <a href="https://github.com/xuleihive/turnstile/releases/tag/v1.1"><img alt="Turnstile v1.1 版本" src="https://img.shields.io/badge/release-v1.1-2f6fdd.svg"></a>
   <a href="LICENSE"><img alt="MIT 许可证" src="https://img.shields.io/badge/license-MIT-2f6fdd.svg"></a>
   <a href="#前置条件"><img alt="Python 3.11 或更高版本" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&amp;logoColor=white"></a>
   <a href="#前置条件"><img alt="Node.js 20.19.x 或 22.12 及更高版本" src="https://img.shields.io/badge/Node.js-20.19.x%20%7C%2022.12%2B-339933?logo=node.js&amp;logoColor=white"></a>
   <a href="#部署"><img alt="基于 Azure 构建" src="https://img.shields.io/badge/Azure-API%20Management-0078D4?logo=microsoftazure&amp;logoColor=white"></a>
</p>

<p align="center">
   <a href="https://turn-stile.com">🌐 官网</a> ·
   <a href="https://turn-stile.com/docs">📚 文档</a> ·
   <a href="https://github.com/xuleihive/turnstile/releases">更新日志</a> ·
   <a href="#本地设置">🛠️ 本地设置</a> ·
   <a href="#部署">🚀 部署</a> ·
   <a href="CONTRIBUTING.md">🤝 参与贡献</a>
</p>

Turnstile 是一个用于治理 AI 模型访问的自托管控制平面。它整合了 Azure API Management 网关、Token 与成本遥测、预算、应用访问、模型接入、发布控制和运维 Web 控制台。

> [!NOTE]
> **Turnstile v1.1** 是当前公开版本。项目正在快速迭代，API、配置项和部署流程可能会随着项目成熟而变化。升级生产部署前，请仔细审阅相关变更。

> [!IMPORTANT]
> 本仓库包含的基础设施模板只部署 Turnstile 平台，不会创建 Azure AI Foundry 项目、部署提供商模型，也不会预置客户连接和模型。平台运行后，运维人员需要连接自己的 Foundry 或其他兼容提供商资源。

<p align="center">
   <a href="docs/assets/product-overview.png">
      <img src="docs/assets/product-overview.png" width="100%" alt="Turnstile 管理层概览，展示用量、成本、延迟与治理信号">
   </a>
</p>
<p align="center"><sub>涵盖用量、成本、可靠性与治理的管理层概览。此产品截图已经过脱敏，可用于公开发布。</sub></p>

## 核心能力

- 与提供商无关的模型、运行时与网关注册表
- 由 APIM 提供支持的 OpenAI 和 Anthropic 兼容端点
- 通过 Event Hub 摄取数据，并使用 PostgreSQL 分析用量与成本
- 月度预算、模型访问策略与应用归因
- 提供商连接管理与模型注册表 API
- 使用 APIM 原生后端池实现故障转移、均衡或加权分发
- 支持完整性检查、回滚和受保护发布的版本化网关发布流程
- 仅限所有者的应用订阅预配与密钥管理
- 基于密码的身份验证和 Microsoft Entra 身份验证
- 以已存储遥测数据为依据的 FinOps Assistant 与报告集
- GitHub Copilot 用量与治理集成（正在积极开发）

> [!WARNING]
> GitHub 数据源和 GitHub Copilot 集成功能仍在积极开发中，尚未达到生产就绪状态。请谨慎启用，并在将相关用量、成本或治理数据用于运营或财务决策前进行独立核验。如果你只需要 GitHub Copilot FinOps 功能，请参考 [OctoFinance](https://github.com/satomic/OctoFinance)。

Model Intelligent Router 未包含在此版本中，未来可能作为可选扩展重新加入。

## 架构

Turnstile 将推理数据平面与管理平面分离。客户端调用 Azure API Management，由其执行认证、预算和模型访问策略并选择提供商路由。响应感知 observer 只把元数据用量事件发送到 Event Hubs，Azure Functions 再将其持久化并对账到 PostgreSQL。FastAPI 应用和 React 控制台管理注册表、治理与发布状态，但不位于提供商请求路径。详见[架构文档](docs/architecture.md)。

## 仓库结构

```text
backend/          FastAPI 入口、HTTP 路由与 Web 专属服务
turnstile_core/   共享领域模型、持久化、集成与工作进程服务
frontend/         React、TypeScript、Vite Web 应用
functions/        用于遥测和控制平面工作的 Azure Functions
infra/            Bicep 模块、APIM 策略与部署模板
migrations/       PostgreSQL 数据库迁移；001 是全新部署使用的初始架构
contracts/        OpenAPI 与 Event Hub 契约
scripts/          构建与部署暂存工具
tests/            单元、契约、API、基础设施与源码测试
docs/             维护人员与运维人员文档
```

## 前置条件

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20.19.x 或 22.12+，以及 npm
- PostgreSQL 16+
- 已安装 Bicep 的 Azure CLI
- 一个可以创建平台资源的 Azure 订阅
- 可选：多租户 Microsoft Entra SPA 应用注册
- 可选：现有的 Azure AI Foundry 项目或其他受支持的提供商（部署后接入）

## 本地设置

1. 创建本地配置：

   ```bash
   cp .env.example .env
   cp frontend/.env.example frontend/.env.local
   ```

2. 设置 `DATABASE_URL`，为 `CREDENTIAL_ENCRYPTION_KEY` 和 `MANAGEMENT_API_KEY` 分别生成独立值，并按照[配置文档](docs/configuration.md)设置身份验证。

3. 安装依赖：

   ```bash
   uv sync --frozen
   npm --prefix frontend ci
   ```

4. 在空的 PostgreSQL 16+ 数据库中初始化数据库架构：

   ```bash
   uv run python -m backend.migrate
   ```

5. 如果使用密码身份验证，请创建第一个 Owner 账号。该命令会提示输入至少 12 个字符的密码：

   ```bash
   uv run python -m backend.accounts owner@example.com --role owner
   ```

6. 在两个终端中分别启动 API 和前端：

   ```bash
   uv run uvicorn backend.api:app --host 127.0.0.1 --port 8000
   npm --prefix frontend run dev -- --host localhost --port 5173
   ```

7. 打开 <http://localhost:5173>。

Vite 服务器会将 `/api` 和 `/health` 代理到 `API_PROXY_TARGET`，其默认值为 `http://127.0.0.1:8000`。

## 配置

所有运行时配置均通过环境变量提供。真实 `.env` 文件、部署参数文件、密钥、日志、软件包和生成的输出均被 Git 忽略。

- 根目录模板：[`.env.example`](.env.example)
- 前端模板：[`frontend/.env.example`](frontend/.env.example)
- 完整参考：[配置文档](docs/configuration.md)

切勿在不同环境之间复用密钥。在完成对应 Azure 身份和最小权限角色的部署之前，请保持 `CONTROL_PLANE_ENABLED`、发布工作进程和密钥管理功能处于禁用状态。

## 开发与测试

```bash
uv run ruff check backend turnstile_core scripts tests functions/telemetry/function_app.py functions/control_plane/function_app.py
uv run mypy backend turnstile_core scripts tests
uv run pytest -q
npm --prefix frontend run build
az bicep build --file infra/main.bicep
xmllint --noout infra/policies/foundry-finops-policy.xml
npx --yes @redocly/cli@2.38.0 lint contracts/openapi.yaml
git diff --check
```

内存仓库仅用于隔离的单元测试。集成与端到端验证必须使用真实 Turnstile 部署及其配置的 Azure 服务。详情请参阅[测试文档](docs/testing.md)与[端到端验证](docs/e2e-validation.md)。

## 部署

Bicep 模板会创建 PostgreSQL、Event Hubs、Storage、Key Vault、Application Insights、Web App、Functions 和 APIM 集成等 Turnstile 平台资源，但不会创建 Azure AI Foundry 项目或提供商模型部署。

对于 **Turnstile v1.1**，请先选择对应路径：

- **已安装 v1.0：**按照[升级指南](docs/deployment.md#upgrade-v10-to-v11)操作。保留原部署状态、数据库、账本、APIM、身份和历史；除应用包外，还需补齐必要的基础设施配置。
- **完全从零安装：**按照[全新部署指南](docs/deployment.md#deploy-v11-from-zero)操作。使用独立前缀和新状态，默认新建 StandardV2 APIM；提供商模型与凭据需要另行准备。

以下快速步骤用于新安装。已有安装不能用新生成的文件替换原部署状态。

1. 将公开参数样例复制到Git忽略目录，填写资源名称、区域、发布者邮箱和 `bootstrapOwnerEmail` 等非机密参数：

```bash
mkdir -p .turnstile
cp infra/main.parameters.example.json .turnstile/main.parameters.json
```

2. 登录Azure并运行仓库自带的部署命令：

```bash
az login
uv sync --frozen
uv run python -m scripts.deploy deploy \
   --subscription <subscription-id> \
   --parameters .turnstile/main.parameters.json
```

脚本会交互读取首个Owner密码，先执行subscription-scope what-if，并要求输入精确确认词 `deploy`；任何Delete变更都会终止。随后它会部署平台与observer、构建Linux x86-64不可变包、部署API和两个Functions、在observer就绪后启用worker，并验证health、Function索引和Owner密码登录。生成的secret只保存在权限为 `0600` 的 `.turnstile/deployments/<resource-group>.json`，该文件被Git忽略，应备份到获批的secret store。

只预览不创建资源时使用 `scripts.deploy plan`。重跑和恢复说明见[部署文档](docs/deployment.md)。

## 部署后模型接入

全新 Turnstile 部署不包含提供商连接、运行时或业务模型。

1. 使用部署脚本创建的密码Owner登录。
2. 在Model Management中添加指向现有Foundry项目或受支持提供商的连接，再通过Add Model选择已有的provider deployment。
3. 根据需要配置 APIM 原生后端池，以实现故障转移、均衡或加权路由。
4. 发布并等待candidate验证。同tenant的Foundry managed identity若缺少授权，发布对话框会显示准确的APIM principal、`Cognitive Services User`角色和Foundry资源scope；由有权限的Azure用户完成IAM授权后，在对话框中继续验证。
5. 执行一次小规模、带归因信息的调用，并确认其遥测记录。

Turnstile不会创建或修改客户的Foundry项目；执行IAM授权的Azure用户必须原本就对该资源有权限。

## 安全

- 遥测数据中不会持久化任何提示词或补全内容。
- 凭据在存入数据库前会进行加密。
- Azure 托管组件会尽可能使用托管身份。
- APIM 角色会限制在实际可行的最小资源与操作范围内。
- 包含密钥的响应使用 `Cache-Control: no-store`，且绝不进行查询缓存。
- 生产环境 Cookie 启用 `HttpOnly` 和 `Secure` 属性，并绑定角色。
- 本地配置与生成的配置均被 `.gitignore` 排除。

部署前请查看[安全文档](docs/security.md)。

## 已知限制

- 必须使用 PostgreSQL；不支持将本地或嵌入式数据存储用于生产环境。
- APIM 与 Azure 资源预配可能需要数分钟。
- 对于 APIM 策略无法捕获用量明细的流式响应，Token 与成本记录最初并不完整。当网关日志可用时，对账流程会补全提示词和补全 Token 数，但无法恢复单请求缓存用量，因此相应成本可能仍只是下限估算。
- 此版本不包含 Model Intelligent Router。

## 故障排除

启动、认证、数据库、APIM、observer 和 telemetry 诊断见[故障排除](docs/troubleshooting.md)。部署失败可以续跑：修复脚本报告的原因后，使用原 private state file 重新执行同一条 `scripts.deploy deploy` 命令。

## 文档

| 指南 | 内容 |
| --- | --- |
| [文档索引](docs/README.md) | 运维人员和维护人员的入口 |
| [组织管理](docs/organization-management.md) | 数据库组织目录、部门授权、Entra 同步审核、初始化与升级流程 |
| [架构](docs/architecture.md) | 运行时边界、组件归属与数据流 |
| [配置](docs/configuration.md) | 环境变量、身份验证与功能开关 |
| [部署](docs/deployment.md) | Azure 预配、打包、发布与验证 |
| [测试](docs/testing.md) | 本地、契约、基础设施与端到端验证 |
| [安全](docs/security.md) | 威胁边界、密钥处理与部署控制 |
| [故障排除](docs/troubleshooting.md) | 数据库、身份验证、APIM、遥测与启动检查 |

## 参与贡献

贡献应保持聚焦、经过测试，且不得包含部署密钥或客户数据。开发流程请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)，负责任的漏洞报告方式请参阅 [SECURITY.md](SECURITY.md)。

## 许可证

Turnstile 根据 [MIT 许可证](LICENSE) 发布。文档或截图中出现的第三方产品名称与商标均归各自所有者所有，其出现不代表任何形式的认可或背书。
