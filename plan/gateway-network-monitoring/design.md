# 初始化私网、已有环境 hotfix 与请求监控

日期：2026-10-12。

## 需求与根因

1. 初始化时 Storage 已默认 Deny，但 APIM 未接入私网；初始化必须完整建立身份权限和网络路径。
2. 已发布环境不应重跑 main/bootstrap 覆盖模型策略，应有独立、可预览、可重复执行的网络修复工具。
3. 00:37 的 cc-switch 两次调用实际已写入 token_usage（62563、64259 Tokens，HTTP200）。相同时间范围无目录过滤返回记录，active 和 archived 均返回0。原因是应用订阅没有组织/部门身份，被默认“启用”筛选排除，不是这两次事件没有消费。
4. 请求列表原 staleTime=2分钟且没有定时刷新；流式详情缓存30分钟。监控不是 WebSocket/SSE 严格实时。

## 初始化网络

- 新 platform-network 模块先于 APIM 创建 VNet，单一拥有完整五个子网，避免两个模块相互依赖或重复 PUT VNet 清除兄弟子网。
- Developer 使用专用非委派子网、NSG、External VNet injection；StandardV2 使用专用 Microsoft.Web/serverFarms 委派子网及 outbound integration，保留公开网关入口。
- BasicV2 不支持所需 outbound 私网访问，模板和部署输入提前拒绝，不能悄悄升级计费 SKU 或把 Storage 改为 Allow。
- data-plane 只引用已有 VNet/子网，仍创建私有 Table endpoint、DNS、RBAC；不改存储 Deny、共享密钥禁用、月度预留/结算。
- provisionApimService=false 用于接管已有完整平台，要求其 VNet 已存在；网络接入缺失使用下述 hotfix，不重跑新建网络模块覆盖未知客户子网。
- StandardV2 要求参考 Microsoft 官方 outbound VNet 文档；本次不另建付费 APIM 实例，初始化模板验证范围为编译和自动化测试。

## 已有环境 hotfix

工具：scripts/apim_ledger_hotfix.py，公共 Azure、Python 3.11+、Azure CLI。默认只读规划；--apply 才允许修复。

```sh
python -m scripts.apim_ledger_hotfix \
  --apim-resource-id "$APIM_RESOURCE_ID" \
  --vnet-resource-id "$VNET_RESOURCE_ID" \
  --plan-file /tmp/turnstile-network-hotfix.json

# 相同参数加 --apply，已接入且子网一致时不写任何 Azure 资源。
python -m scripts.apim_ledger_hotfix \
  --apim-resource-id "$APIM_RESOURCE_ID" \
  --vnet-resource-id "$VNET_RESOURCE_ID" \
  --plan-file /tmp/turnstile-network-hotfix.json --apply
```

- 原平台的 Ledger 私有 endpoint、私有 DNS及 APIM Table Data Contributor 为前置条件；本工具不扩大身份权限、不修改 Storage firewall、订阅密钥、API、模型策略或应用配置。
- 发现实际 SKU，不升级、不改 capacity。校验同订阅/同区域、CIDR归属/大小/无重叠、子网委派、NSG以及既有网络；拒绝移动已经接入其他网络的实例或占用其他服务子网。
- 缺子网时只预览并创建专用 NSG/子网；what-if 只允许这两个目标 Create/Modify，拒绝 Delete 或其他资源修改。应用前再次检查 VNet/APIM 完整快照没有漂移。
- APIM 只 PATCH virtualNetworkType 和 subnetResourceId，不把完整 GET 配置写回。长操作地址仅存600私有文件，轮询完成、读回 SKU/identity/网络及其他关键配置；失败/超时不伪报成功、不自动删除资源或迁移到旧网络。
- 已有一致网络时幂等 no-op；仅在当前环境做 no-op 验证，不反复触发25分钟网络重配置。
- 可选真实 Responses 探测：--probe-url、--probe-model、--probe-key-env（默认 AIGATEWAY_PROBE_SUBSCRIPTION_KEY）、--minimum-remaining-tokens。密钥只从环境变量读取、不序列化，URL仅允许当前 APIM HTTPS默认域名；固定32输出Token的小请求，可能产生少量模型费用。最低剩余Token阈值可检测默认100000回退；仅连通性成功不等同所有预算策略已验收。
- 经典 APIM 首次接网可能有短暂中断；先安排维护窗口。若Azure长操作仍进行中，保留 operation 私有记录并查看服务状态，不盲目重复发起。

## 监控修复

- 前端“启用”转换成 active + unattributed，表示未绑定目录的应用调用不是已归档；归档选择仍只选 archived，不混入未知历史实体。
- API 的 active 分类契约保持原样。SQL/InMemory 分类先判断已归档组织/部门/人员，再判断未归因，防止“已归档但缺某个字段”的事件混入启用列表。
- 目录权限、部门/user data scope 仍先独立强制执行；包含未归因不意味着范围管理员可读取不属于其范围的数据。
- 仅请求追踪页对列表、总计、趋势、已选详情每10秒刷新，窗口结束时间每次请求重算；隐藏标签页停止定时轮询，回到页面按既有查询机制刷新。不让全平台所有查询都高频轮询。
- 页面明确显示“近实时，10秒刷新”；列表出现取决于 APIM 请求完成、Event Hub 异步消费、数据库可用和轮询时点。流式最终用量有可能还需日志入库、安全滞后和原10分钟校准任务，不改变保守预算结算。
- 请求追踪的日趋势原按UTC分桶、详情按浏览器时区显示，凌晨调用看上去属于“昨天”。改为使用已有带时区缓存key的usageActivity查询，桶标签、详情时间和图表说明都使用同一所选时区；不改事件本身的UTC时间。

## 验证与发布边界

- 新建/既有网络、重复执行、不同SKU、占用/重叠/错误网络、拒绝删除/越界、私有记录权限；Bicep main和独立hotfix编译。
- 真实目录及权限API测试：匿名订阅可见、归档优先、其他部门数据仍不可见；前端筛选和10秒刷新测试。
- 无新迁移；兼容 core 更新同时发布API、Telemetry、Control-plane，保留当前包及全量设置。只更新包及digest，不重发布APIM模型或全量基础设施。
- 发布前/后检查 PostgreSQL Ready、真实登录、事件消费、页面默认筛选出现00:37两条记录，以及新探测事件自动出现。
