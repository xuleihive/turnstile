import { useId, useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { ArrowLeft, CheckCircle2, ChevronRight, Circle, CircleAlert, Link2, Play, RefreshCw, X } from "lucide-react"

import { dataSource } from "../../data-sources/apim/api"
import { ApiError } from "../../api/client"
import type { ApimOnboardingPlan, ApimOnboardingRequest, GatewayReleaseOperation } from "../../data-sources/apim/types"
import { apimOnboardingFormPayload, apimOnboardingFormValid } from "../../lib/gateway-forms"
import { apimOnboardingDetails, apimOnboardingOperationName } from "../../lib/gateway-topology"
import { ApimLogo } from "../brand-logos"
import { Button } from "../ui/button"
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"

const steps: Record<string, string> = {
  validate_target: "检查实例",
  create_api: "创建专用 API",
  configure_governance: "配置鉴权与计量",
  create_subscriptions: "创建系统订阅",
  project_access: "初始化额度",
  verify_managed_gateway: "验证 Managed Gateway",
  activate_target: "完成接入",
}
const blockers: Record<string, string> = {
  apim_not_ready: "APIM 实例尚未就绪",
  managed_gateway_tier_unsupported: "当前 APIM 层不支持平台所需的策略",
  dedicated_api_conflict: "专用 API 标识已被其他配置占用",
  dedicated_api_path_conflict: "专用 API 路径已被其他配置占用",
  system_subscription_conflict: "系统订阅标识已被其他配置占用",
  budget_execution_not_ready: "平台预算执行服务尚未就绪",
  apim_already_registered: "此 APIM 实例已接入",
}

export function pendingApimOnboarding(operation: GatewayReleaseOperation) {
  return !["succeeded", "failed", "restored"].includes(operation.status)
}

const TENANT_MISMATCH = "The tenant ID does not match the tenant of the Turnstile management identity"

export function ApimOnboardingDialog({ available, onClose, onAdopt, onAccepted, onSettled }: {
  available: boolean
  onClose: () => void
  onAdopt: () => void
  onAccepted: (operation: GatewayReleaseOperation) => void
  onSettled: () => void
}) {
  const [value, setValue] = useState<ApimOnboardingRequest>({ tenant_id: "", resource_id: "", name: "" })
  const [plan, setPlan] = useState<ApimOnboardingPlan | null>(null)
  const preview = useMutation({ mutationFn: dataSource.previewApimOnboarding, onSuccess: setPlan })
  const start = useMutation({
    mutationFn: dataSource.startApimOnboarding,
    onSuccess: (accepted) => onAccepted(accepted.operation),
    onSettled,
  })
  const busy = preview.isPending || start.isPending
  const update = (field: keyof ApimOnboardingRequest, next: string) => {
    setValue(current => ({ ...current, [field]: next }))
    setPlan(null)
    preview.reset()
    start.reset()
  }
  const error = preview.error ?? start.error
  const valid = apimOnboardingFormValid(value)
  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}>
    <DialogContent className="registry-editor-dialog infrastructure-editor-dialog apim-onboarding-dialog" finalFocus={false}>
      <form className="registry-editor" onSubmit={event => {
        event.preventDefault()
        if (busy || !available || !valid) return
        if (plan?.ready) start.mutate({ request: plan.request, confirmation_sha256: plan.confirmation_sha256 })
        else preview.mutate(apimOnboardingFormPayload(value))
      }}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>添加 APIM 实例</DialogTitle>
          <DialogDescription>Managed Gateway 初始化</DialogDescription>
        </DialogHeader>
        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" disabled={busy} />}><X size={16} /><span className="sr-only">关闭</span></DialogClose>
        <div className="registry-editor-body infrastructure-editor-body">
          {!plan && <>
            <label className="registry-field"><span className="registry-field-label">Turnstile 显示名称（可选）</span><Input value={value.name ?? ""} maxLength={120} disabled={busy} onChange={event => update("name", event.target.value)} autoFocus /></label>
            <label className="registry-field"><span className="registry-field-label">Tenant ID</span><Input value={value.tenant_id} disabled={busy} onChange={event => update("tenant_id", event.target.value)} /></label>
            <label className="registry-field"><span className="registry-field-label">APIM 资源 ID</span><Input value={value.resource_id} maxLength={1024} disabled={busy} onChange={event => update("resource_id", event.target.value)} placeholder="/subscriptions/.../resourceGroups/.../providers/Microsoft.ApiManagement/service/..." /></label>
            <details className="runtime-diagnostic-access" open>
              <summary>部署授权详情</summary>
              <dl className="runtime-diagnostic-facts">
                <div><dt>所需权限</dt><dd><code data-no-localize>API Management Service Contributor (312a565d-c81f-4fd8-895a-4e21e48d571c)</code></dd></div>
                <div><dt>平台资源</dt><dd>Ledger、路由表、Event Hub 与 Key Vault 权限由平台自动授权</dd></div>
                <div><dt>授权说明</dt><dd>同订阅 APIM 已自动具备权限；跨订阅 APIM 请在该实例 IAM 中将上述角色授予平台 API 与 Control Plane 托管身份</dd></div>
                <div><dt>部署模板</dt><dd><code data-no-localize>infra/modules/control-plane-apim-rbac.bicep</code></dd></div>
              </dl>
            </details>
          </>}
          {plan && <section className="apim-onboarding-plan" aria-label="APIM 初始化计划">
            <div className="apim-onboarding-target"><ApimLogo size={20} /><strong data-no-localize>{plan.request.name ?? plan.service_name}</strong><span data-no-localize>{plan.service_tier} · {plan.location}</span></div>
            <dl>
              <dt>Azure 资源名称</dt><dd><code data-no-localize>{plan.service_name}</code></dd>
              <dt>入口类型</dt><dd>Managed Gateway</dd>
              <dt>专用 API</dt><dd><code data-no-localize>{plan.api_id}</code></dd>
              <dt>调用地址</dt><dd><code data-no-localize>{`${plan.managed_gateway_origin.replace(/\/$/, "")}/${plan.api_path}`}</code></dd>
            </dl>
            <section className="apim-onboarding-group" aria-label="Dashboard 调用应用">
              <h3>Dashboard 调用应用</h3>
              <dl>
                <dt>Dashboard 订阅</dt><dd><code data-no-localize>{plan.dashboard_subscription_id}</code></dd>
                <dt>共享月度 Token 上限</dt><dd>{plan.initial_monthly_token_limit.toLocaleString()} Tokens</dd>
                <dt>共享每分钟 Token 上限 (TPM)</dt><dd>{plan.initial_tokens_per_minute.toLocaleString()}</dd>
              </dl>
            </section>
            <section className="apim-onboarding-group" aria-label="Probe 系统验证">
              <h3>Probe 系统验证</h3>
              <dl>
                <dt>Probe 订阅</dt><dd><code data-no-localize>{plan.probe_subscription_id}</code></dd>
              </dl>
            </section>
            <ol className="apim-onboarding-steps">{plan.steps.map(step => <li key={step}><Circle size={13} /><span>{steps[step] ?? step}</span></li>)}</ol>
            {plan.blockers.length > 0 && <ul className="registry-error apim-onboarding-blockers" role="alert">{plan.blockers.map(blocker => <li key={blocker}>{blockers[blocker] ?? blocker}</li>)}</ul>}
          </section>}
          {!available && <div className="infrastructure-warning" role="status"><CircleAlert size={15} /><span>平台初始化依赖尚未就绪</span></div>}
          {error && <div className="registry-error" role="alert">{error instanceof ApiError && error.status === 409 && error.detail === TENANT_MISMATCH ? "租户 ID 与 Turnstile 管理身份所在的租户不一致，不能添加其他租户中的 APIM。" : error.message}</div>}
        </div>
        <DialogFooter className="registry-editor-footer apim-onboarding-footer">
          {plan ? <Button type="button" variant="ghost" disabled={busy} onClick={() => { setPlan(null); preview.reset(); start.reset() }}><ArrowLeft size={14} />返回修改</Button>
            : <Button type="button" variant="ghost" disabled={busy} onClick={onAdopt}><Link2 size={14} />关联已有配置</Button>}
          <DialogClose render={<Button type="button" variant="outline" disabled={busy} />}>取消</DialogClose>
          <Button type="submit" disabled={busy || !available || !valid}>{busy ? <RefreshCw size={14} className="spin" /> : plan?.ready ? <Play size={14} /> : <CheckCircle2 size={14} />}{start.isPending ? "正在提交" : preview.isPending ? "正在检查" : plan?.ready ? "开始初始化" : plan ? "重新检查" : "检查资源"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function ApimOnboardingRow({ operation, available, retryingId, onRetry }: {
  operation: GatewayReleaseOperation
  available: boolean
  retryingId: string | null
  onRetry: (id: string) => void
}) {
  const [expanded, setExpanded] = useState(true)
  const detailsId = useId()
  const succeeded = operation.status === "succeeded"
  const failed = operation.status === "failed"
  const completed = new Set(succeeded ? Object.keys(steps) : Array.isArray(operation.checkpoint.completed_steps)
    ? operation.checkpoint.completed_steps.filter((value): value is string => typeof value === "string") : [])
  const name = apimOnboardingOperationName(operation)
  const details = apimOnboardingDetails(operation)
  const resourceLabel = [details.serviceName, details.resourceGroup].filter(Boolean).join(" · ")
  const activeStep = Object.keys(steps).find(step => !completed.has(step))
  const status = failed ? "初始化失败" : succeeded ? "初始化完成" : operation.worker_available ? "正在初始化" : "初始化已排队，Worker 当前不可用"
  const disclosureLabel = expanded ? "收起初始化进度" : "展开初始化进度"
  return <section className="connection-provider-group apim-target-group apim-onboarding-row" role="rowgroup" aria-label={name} data-status={operation.status}>
    <article className="connection-provider-heading apim-target-heading" role="row">
      <div className="connection-provider-identity" role="cell">
        <span className="model-list-icon" data-model-family="apim"><ApimLogo size={16} /></span>
        <span><b title={name} data-no-localize>{name}</b><small className="apim-target-resource" title={resourceLabel} data-no-localize>{resourceLabel}</small></span>
      </div>
      <span className="connection-provider-summary" role="cell"><b title={details.endpoint ?? undefined} data-no-localize>{details.endpoint ?? "-"}</b><small>计划调用地址</small></span>
      <span className="connection-provider-summary" role="cell"><b>Managed Gateway</b><small title={details.apiId} data-no-localize>{details.apiId || "-"}</small></span>
      <div className="model-status-cell" role="cell"><span className={`gateway-readiness apim-onboarding-status ${failed ? "unavailable" : succeeded ? "ready" : "warning"}`} role="status">
        <span>{failed ? <CircleAlert size={13} /> : succeeded ? <CheckCircle2 size={13} /> : <RefreshCw size={13} className={operation.worker_available ? "spin" : ""} />}{status}</span>
        <small>{activeStep ? steps[activeStep] : "完成接入"}</small>
      </span></div>
      <div className="model-row-actions infrastructure-row-actions" role="cell">
        <button type="button" className="connection-provider-chevron apim-row-chevron" title={disclosureLabel} aria-label={disclosureLabel} aria-expanded={expanded} aria-controls={detailsId} onClick={() => setExpanded(current => !current)}><ChevronRight size={15} /></button>
      </div>
    </article>
    <div className="apim-onboarding-details" role="row" hidden={!expanded && !failed && !operation.error_message}>
      <div role="cell" aria-colspan={5}>
        <div className="apim-onboarding-progress" id={detailsId} hidden={!expanded}>
          <dl className="apim-onboarding-mobile-summary">
            <dt>计划调用地址</dt><dd data-no-localize>{details.endpoint ?? "-"}</dd>
            <dt>专用 API</dt><dd data-no-localize>{details.apiId || "-"}</dd>
          </dl>
          <ol className="apim-onboarding-steps">{Object.entries(steps).map(([step, label]) => <li key={step} data-complete={completed.has(step)} data-failed={failed && step === activeStep} aria-current={step === activeStep ? "step" : undefined}>{completed.has(step) ? <CheckCircle2 size={13} /> : failed && step === activeStep ? <CircleAlert size={13} /> : <Circle size={13} />}<span>{label}</span></li>)}</ol>
        </div>
        {(failed || operation.error_message) && <div className="registry-error apim-onboarding-error">
          <p role="alert">{operation.error_message || "初始化失败"}</p>
          {failed && <Button type="button" size="sm" variant="outline" className="apim-onboarding-retry" disabled={!available || retryingId !== null} onClick={() => onRetry(operation.id)}><RefreshCw size={14} className={retryingId === operation.id ? "spin" : ""} /><span>重试初始化</span></Button>}
        </div>}
      </div>
    </div>
  </section>
}