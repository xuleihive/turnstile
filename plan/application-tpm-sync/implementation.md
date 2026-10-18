# 实施记录

日期：2026-10-12。完整链路修复已实现、已上线，正式网关真实限流回归完成。

- 最终完整本地回归：1805 passed / 1 warning，无跳过；包含独立 PostgreSQL 16 用户持久化测试。Ruff、Mypy 290 源文件、frontend TypeScript/Vite 构建通过；独立网络模板 Bicep build 通过。
- 生产数据库已只读备份；隔离 PostgreSQL 16 恢复后 106 表在两次迁移前后 hash 完全相同，001-018 checksum 验证，两次 migrate 返回空。
- 隔离恢复数据库真实保存 TPM 为 11000000、12000000，返回值正确且 updated_at 单调递增。
- 最终 APIM what-if 仅创建候选 turnstile-tpm-9e80afc7ae5ec57a 与其 parent policy；promote 前真实调用验证，正式切换后读回一致。源修订 turnstile-9-bda0c63f 保留供回滚，7个 operation 及其策略不变；两次早期候选仅保留审计，没有 promote。
- 三包兼容更新 what-if 只修改三个现有 appsettings，无删除。此时没有待处理控制平面 operation/publication。
- Function 包备份及新包读回使用临时容器级 Blob Data Reader：只读、两容器、独立 role ID，结束时两项临时权限已移除并读回404。未放宽存储网络，未启用共享密钥。

候选真实验证补充：APIM 能执行动态表达式，但应用账本读取返回 application_map_unavailable。只在自有非当前候选暂加诊断头并恢复后，确认 bootstrap 将带尾斜杠的 Table endpoint 再拼接斜杠，导致 //TurnstileLedger。追加修复：新建基础设施先规范化 endpoint，已有策略窄转换只规范化已知 ledger origin；不改其他 URL。旧候选不 promote，重新生成候选与兼容包后验证。

网络实测补充：规范化地址后仍有 Table 403 AuthorizationFailure。存储防火墙保持 Deny；已存在 Table Data Contributor，新增 APIM resource-instance rule 仍无效。确认实际 SKU=classic Developer 后，新增专用子网 snet-apim-ledger (10.42.4.0/24) 和 NSG，以稀疏 PATCH 切换 External VNet；长操作成功，SKU/capacity/identity 不变。候选读回 Table 200，识别 cc-switch 与账本 tokensPerMinute=10000000。

第二个候选边界：未限制模型时 applicationAdmission=application_policy_unconfigured，原 TPM 选择错误要求 admission=ok，仍回退100000。修复为独立可信映射 + 配置账本判断，保留授权逻辑；新增早期候选升级及拒绝未知自定义表达式测试。此前候选均未切换为正式修订。

## 正式发布与验收

- 线上运行代码提交为 217fbd2，兼容 core digest 为 f33f12036ee072fbedecfc457f0503e7ab6ff65ffb75201816c32faee397c0db。三个真实已部署包逐文件比对构建目录；API 的实际挂载包、线上 HTML 及入口 JS/CSS 也逐字节一致。
- API、Telemetry、Control-plane 全量原设置已恢复，仅 expected core digest 更新。发布工作进程恢复启用，临时触发器禁用设置已移除；Telemetry 四个函数、Control-plane 四个函数索引及同步验证通过。无数据库新迁移。
- cc-switch 保持月度100000000、TPM10000000、enforce=true、预警80。真实同值保存 HTTP200，立即返回 projected；再次详情读取时 budget.updated_at 与 Q.ConfigurationUpdatedAt 相同。候选剩余9999978、正式路径剩余9999980，真实 GPT Responses 返回200，证明不再使用旧默认100000。
- 通过原异步工作流创建独立 turnstile-tpm-verification-20261011 测试订阅，月度1000000。保存TPM1000后相同1800余Token请求返回429与 Retry-After=60；保存TPM1000000后相同请求返回200、剩余998180。两次均立即投影，无 APIM 策略发布或订阅发现同步，证明保存即可生效。
- 测试完成仅暂停自有 APIM 测试订阅，并完成原库存刷新、读回 state=suspended；保留应用、用量及审计，不删除用户实体。cc-switch 额度未改变。移除无效自有 resource-instance rule，保留原 scanner rule、Deny 与禁用共享密钥。
- Edge 线上页面身份、非空/无错误覆盖层、保存对话框关闭与投影状态、订阅列表“刷新 APIM 订阅”入口通过；控制台无相关 error/warn。桌面与390x844移动布局检查，视口已恢复。私有截图和发布证据位于 /tmp/ts-tpm-release，不进入Git。隔离本地 PostgreSQL 已停止。
- 线上付费推理验证限于 GPT Responses；文本/图像策略分支及员工语义本地回归通过，本次没有进行真实图像生成或重配其他Agent。

## 数据库外部停机记录

- 2026-10-12 00:05:24 (Asia/Shanghai) 另一个服务身份 da847d8c-ee71-44de-b462-6775e7d649a7 发起 PostgreSQL stop/action，00:09:32完成。不是此次部署脚本的停机操作；活动日志证明服务身份操作，未识别具体调度器，不能仅凭时间宣称确定来自某个自动关停任务。
- 停机导致启动后登录/数据库复验超时，已按本次上线测试授权启动原数据库，不修改规格或数据；随后 Ready、/health=200、PostgreSQL-backed 登录及应用业务接口通过。没有修改或停用未知关停调度，后续仍需单独处理该运行环境问题。
- 完整主模板不是这个实例的无条件更新路径；当前 External VNet 私有账本网络升级是独立模板和稀疏PATCH。未来全量基础设施重部署必须预览并保留已接入网络，避免退回无私网的旧 bootstrap 配置。
