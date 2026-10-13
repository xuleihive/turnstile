import { useEffect, useMemo, useRef, useState, type FormEvent } from "react"
import { AlertTriangle, CheckCircle2, Plus, RefreshCw, X } from "lucide-react"

import { ApiError, dataSource } from "../../data-sources/apim/api"
import type { GatewayApplicationList, GatewayApplicationSubscriptionCreate, GatewayProfile, GatewayReleaseOperation, GatewayReleaseOperationAccepted } from "../../data-sources/apim/types"
import { getIntlLocale } from "../../locales"
import { GatewayBrandLogo, gatewayBrandFromIdentity } from "../brand-logos"
import { FieldHelp } from "../model-management/field-help"
import { Button } from "../ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"
import { Textarea } from "../ui/textarea"
import { applicationCreateError, applicationOperationTerminal, applicationProvisionBlocker, applicationProvisionStage, applicationSubscriptionIdFromName, provisionedApplicationId } from "./application-create-form"

export function ApplicationCreateDialog({ gateways, inventory, inventoryError, applicationType, operation, operationError, refreshing, onRefresh, onClose, onAccepted, onOpenApplication }: {
  gateways: GatewayProfile[]
  inventory: GatewayApplicationList | undefined
  inventoryError: boolean
  applicationType: "service" | "agent"
  operation: GatewayReleaseOperation | undefined
  operationError: string | null
  refreshing: boolean
  onRefresh: () => void
  onClose: () => void
  onAccepted: (accepted: GatewayReleaseOperationAccepted) => void
  onOpenApplication: (id: string) => void
}) {
  const availableGateways = useMemo(() => gateways.filter((gateway) => gateway.enabled && gateway.implementation === "apim"), [gateways])
  const [gatewayId, setGatewayId] = useState(availableGateways.find((gateway) => gateway.is_default)?.id ?? availableGateways[0]?.id ?? "")
  const gateway = availableGateways.find((item) => item.id === gatewayId)
  const [displayName, setDisplayName] = useState("")
  const [subscriptionId, setSubscriptionId] = useState("")
  const [subscriptionIdEdited, setSubscriptionIdEdited] = useState(false)
  const [description, setDescription] = useState("")
  const [accepted, setAccepted] = useState<GatewayReleaseOperationAccepted | null>(null)
  const submittingRef = useRef(false)
  const mounted = useRef(true)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const currentOperation = accepted && operation?.id === accepted.operation.id ? operation : accepted?.operation
  const terminal = applicationOperationTerminal(currentOperation)
  const failed = terminal && currentOperation?.status !== "succeeded"
  const applicationId = provisionedApplicationId(currentOperation)
  const blocker = inventoryError ? "无法读取创建能力，请重试。" : applicationProvisionBlocker(inventory)
  const defaults = inventory?.provisioning_defaults

  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    if (!accepted && !availableGateways.some((item) => item.id === gatewayId)) {
      setGatewayId(availableGateways.find((item) => item.is_default)?.id ?? availableGateways[0]?.id ?? "")
    }
  }, [accepted, availableGateways, gatewayId])
  useEffect(() => {
    const protectPendingCreation = (event: BeforeUnloadEvent) => {
      if (!submittingRef.current) return
      event.preventDefault()
      event.returnValue = ""
    }
    window.addEventListener("beforeunload", protectPendingCreation)
    return () => window.removeEventListener("beforeunload", protectPendingCreation)
  }, [])

  const mayClose = () => {
    return !submittingRef.current
  }
  const close = () => { if (mayClose()) onClose() }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (submittingRef.current || accepted) return
    if (blocker) { setFormError(blocker); return }
    const value: GatewayApplicationSubscriptionCreate = {
      subscription_id: subscriptionId.trim().toLowerCase(), display_name: displayName.trim(),
      description: description.trim() || null, application_type: applicationType,
    }
    const validation = applicationCreateError(gateway, value, inventory?.items ?? [])
    if (validation || !gateway) { setFormError(validation); return }
    setFormError(null)
    submittingRef.current = true
    setSubmitting(true)
    try {
      const response = await dataSource.provisionGatewayApplicationSubscription(gateway.id, value)
      if (!mounted.current) return
      const safeAccepted = { operation: response.operation, status_url: response.status_url }
      setAccepted(safeAccepted)
      onAccepted(safeAccepted)
    } catch (error) {
      if (!mounted.current) return
      setFormError(error instanceof ApiError && [404, 405, 503].includes(error.status)
        ? "创建服务当前不可用。请刷新能力状态并检查后台配置。"
        : error instanceof ApiError ? error.message
        : "请求结果尚未确认。请先刷新库存和操作状态，再决定是否重试，避免创建重复订阅。")
    } finally {
      submittingRef.current = false
      if (mounted.current) setSubmitting(false)
    }
  }
  return <Dialog open onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="registry-editor-dialog application-create-dialog" finalFocus={false}>
      <form className="registry-editor application-create-form" onSubmit={(event) => void submit(event)}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{accepted ? "应用创建进度" : applicationType === "agent" ? "添加智能体" : "添加应用"}</DialogTitle>
          <DialogDescription>{accepted ? displayName : "创建独立的 APIM 订阅、调用密钥和应用预算，不是新增一个空白记录。"}</DialogDescription>
        </DialogHeader>
        <button type="button" className="registry-editor-close" onClick={close} disabled={submitting} aria-label="关闭"><X size={16} /></button>
        <div className="registry-editor-body application-create-body">
          {!accepted ? <>
            {blocker && <div className="application-create-unavailable" role="status"><b>当前环境尚未启用创建</b><span>{blocker}</span><p>可以填写配置，但后台就绪前不会提交创建请求。</p><Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={onRefresh}><RefreshCw size={13} className={refreshing ? "spin" : undefined} />重新检查</Button></div>}
            <div className="registry-field"><span className="registry-field-label">目标网关</span>
              <Select value={gateway?.id ?? null} items={availableGateways.map((item) => ({ value: item.id, label: item.name }))} disabled={submitting || availableGateways.length < 2} onValueChange={(value) => { if (value) { setGatewayId(value); setFormError(null) } }}>
                <SelectTrigger className="registry-select-trigger" aria-label="目标网关"><SelectValue>{gateway ? <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${gateway.name} ${gateway.implementation}`)} size={15} /><span data-no-localize>{gateway.name}</span></span> : "没有可用的 Azure API Management 网关。"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>{availableGateways.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <label className="registry-field"><span className="registry-field-label">{applicationType === "agent" ? "智能体名称" : "应用名称"}</span><Input value={displayName} onChange={(event) => { setDisplayName(event.target.value); if (!subscriptionIdEdited) setSubscriptionId(applicationSubscriptionIdFromName(event.target.value)); setFormError(null) }} disabled={submitting} maxLength={100} required placeholder={applicationType === "agent" ? "例如：Invoice Agent" : "例如：Invoice Service"} /></label>
            <div className="registry-field"><span className="registry-field-label-row"><label htmlFor="application-subscription-id" className="registry-field-label">订阅 ID</label><FieldHelp>网关内唯一的调用标识；只能使用小写字母、数字和连字符。名称为中文时请单独填写。</FieldHelp></span><Input id="application-subscription-id" value={subscriptionId} onChange={(event) => { setSubscriptionIdEdited(true); setSubscriptionId(event.target.value.toLowerCase()); setFormError(null) }} disabled={submitting} maxLength={127} required placeholder="invoice-service" spellCheck={false} autoCapitalize="none" data-no-localize /></div>
            <label className="registry-field"><span className="registry-field-label">说明</span><Textarea value={description} onChange={(event) => setDescription(event.target.value)} disabled={submitting} maxLength={1000} placeholder="可选" /></label>
            <dl className="application-create-defaults">
              <div><dt>创建内容</dt><dd>独立订阅与密钥</dd></div>
              <div><dt>初始模型访问</dt><dd>已发布模型，未限制</dd></div>
              <div><dt>初始月额度</dt><dd>{defaults ? new Intl.NumberFormat(getIntlLocale()).format(defaults.monthly_token_limit) : "使用平台默认值"}{defaults && <span data-no-localize> Tokens</span>}</dd></div>
              <div><dt>初始速率</dt><dd>{defaults ? new Intl.NumberFormat(getIntlLocale()).format(defaults.tokens_per_minute) : "使用平台默认值"}{defaults && <span data-no-localize> TPM</span>}</dd></div>
            </dl>
            <p className="application-create-note">创建后可在应用详情调整预算和模型访问。提交将写入共享 APIM 和应用数据库。</p>
          </> : <>
            <div className={`application-create-operation ${failed ? "failed" : currentOperation?.status}`} role="status"><span>{failed || currentOperation?.worker_available === false ? <AlertTriangle size={15} /> : terminal ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}</span><b>{applicationProvisionStage(currentOperation)}</b></div>
            <dl className="application-create-defaults"><div><dt>订阅 ID</dt><dd><code data-no-localize>{accepted.operation.semantic_preview.apim_subscription_id as string}</code></dd></div><div><dt>操作 ID</dt><dd><code data-no-localize>{accepted.operation.id}</code></dd></div></dl>
            {currentOperation?.error_message && <div className="registry-error" role="alert">{currentOperation.error_message}</div>}
            {failed && currentOperation?.checkpoint.apim_subscription_created === true && <p className="registry-error">部分资源已创建，但创建流程未完成。请检查现有订阅和操作记录，再决定是否重试。</p>}
            {currentOperation?.worker_available === false && !terminal && <div className="registry-error">{currentOperation.worker_unavailable_reason ?? "Release Worker 未部署。"}</div>}
            {operationError && <div className="registry-error" role="alert">{operationError}</div>}
            <Button type="button" variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}><RefreshCw size={13} />刷新进度</Button>
          </>}
          {formError && <div className="registry-error" role="alert">{formError}</div>}
        </div>
        <DialogFooter className="registry-editor-footer">
          <Button type="button" variant="outline" className="application-create-dismiss" onClick={close} disabled={submitting}>{accepted ? "关闭" : "取消"}</Button>
          {accepted ? applicationId && <Button type="button" onClick={() => { if (mayClose()) onOpenApplication(applicationId) }}>打开应用详情</Button>
            : <Button type="submit" disabled={submitting || Boolean(blocker) || !gateway || !displayName.trim() || !subscriptionId.trim()}>{submitting ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}{submitting ? "正在创建" : "创建应用"}</Button>}
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
