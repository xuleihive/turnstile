import { useEffect, useMemo, useRef, useState, type FormEvent } from "react"
import { AlertTriangle, CheckCircle2, Copy, KeyRound, Plus, RefreshCw, Trash2, X } from "lucide-react"

import { ApiError, dataSource } from "../../data-sources/apim/api"
import type { BudgetMode, GatewayApplicationList, GatewayApplicationSubscriptionCreate, GatewayProfile, GatewayReleaseOperation, GatewayReleaseOperationAccepted } from "../../data-sources/apim/types"
import { GatewayBrandLogo, gatewayBrandFromIdentity } from "../brand-logos"
import { FieldHelp } from "../model-management/field-help"
import { Button } from "../ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select"
import { Textarea } from "../ui/textarea"
import {
  applicationCreateError,
  applicationOperationTerminal,
  applicationProvisionBlocker,
  applicationProvisionStage,
  applicationRetryDetail,
  applicationSubscriptionIdFromName,
  applicationTagsError,
  provisionedApplicationId,
} from "./application-create-form"

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
  const [budgetMode, setBudgetMode] = useState<BudgetMode>("tokens")
  const [tokenLimit, setTokenLimit] = useState(String(inventory?.provisioning_defaults?.monthly_token_limit ?? ""))
  const [usdLimit, setUsdLimit] = useState("")
  const [tokensPerMinute, setTokensPerMinute] = useState(String(inventory?.provisioning_defaults?.tokens_per_minute ?? ""))
  const [tags, setTags] = useState<Array<{ id: number; key: string; value: string }>>([])
  const nextTagId = useRef(0)
  const [accepted, setAccepted] = useState<GatewayReleaseOperationAccepted | null>(null)
  // Never render the key or return it through a React Query mutation/cache.
  const primaryKey = useRef<string | null>(null)
  const submittingRef = useRef(false)
  const mounted = useRef(true)
  const [submitting, setSubmitting] = useState(false)
  const [copying, setCopying] = useState(false)
  const [copied, setCopied] = useState(false)
  const [closeWarning, setCloseWarning] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const currentOperation = accepted && operation?.id === accepted.operation.id ? operation : accepted?.operation
  const terminal = applicationOperationTerminal(currentOperation)
  const failed = terminal && currentOperation?.status !== "succeeded"
  const retryDetail = applicationRetryDetail(currentOperation)
  const applicationId = provisionedApplicationId(currentOperation)
  const blocker = inventoryError ? "无法读取创建能力，请重试。" : applicationProvisionBlocker(inventory)
  const draft: GatewayApplicationSubscriptionCreate = {
    subscription_id: subscriptionId.trim().toLowerCase(),
    display_name: displayName.trim(),
    description: description.trim() || null,
    application_type: applicationType,
    budget_mode: budgetMode,
    token_limit: budgetMode === "tokens" ? Number(tokenLimit) : null,
    usd_limit: budgetMode === "usd" ? usdLimit.trim() : null,
    tokens_per_minute: Number(tokensPerMinute),
    tags: Object.fromEntries(tags.map(tag => [tag.key.trim(), tag.value])),
  }
  const validation = applicationTagsError(tags) || applicationCreateError(gateway, draft, inventory?.items ?? [])

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; primaryKey.current = null } }, [])
  useEffect(() => {
    if (!accepted && !availableGateways.some((item) => item.id === gatewayId)) {
      setGatewayId(availableGateways.find((item) => item.is_default)?.id ?? availableGateways[0]?.id ?? "")
    }
  }, [accepted, availableGateways, gatewayId])
  useEffect(() => {
    const protectUnsavedKey = (event: BeforeUnloadEvent) => {
      if (!submittingRef.current && (!primaryKey.current || copied)) return
      event.preventDefault()
      event.returnValue = ""
    }
    window.addEventListener("beforeunload", protectUnsavedKey)
    return () => window.removeEventListener("beforeunload", protectUnsavedKey)
  }, [copied])
  useEffect(() => {
    if (failed) primaryKey.current = null
  }, [failed])

  const close = () => {
    if (submittingRef.current || copying) return
    if (primaryKey.current && !copied && !closeWarning) { setCloseWarning(true); return }
    primaryKey.current = null
    onClose()
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (submittingRef.current || accepted) return
    if (blocker) { setFormError(blocker); return }
    if (validation || !gateway) { setFormError(validation); return }
    setFormError(null)
    submittingRef.current = true
    setSubmitting(true)
    try {
      const response = await dataSource.provisionGatewayApplicationSubscription(gateway.id, draft)
      if (!mounted.current) return
      primaryKey.current = response.primary_key
      const safeAccepted = { operation: response.operation, status_url: response.status_url }
      setAccepted(safeAccepted)
      onAccepted(safeAccepted)
    } catch (error) {
      if (!mounted.current) return
      setFormError(error instanceof ApiError && [404, 405, 503].includes(error.status)
        ? "创建服务当前不可用。请刷新能力状态并检查后台配置。"
        : error instanceof ApiError ? error.message
        : "请求结果尚未确认。请先刷新库存，避免重复创建；已创建订阅的密钥可在详情中按需复制。")
    } finally {
      submittingRef.current = false
      if (mounted.current) setSubmitting(false)
    }
  }
  const copyKey = async () => {
    if (!primaryKey.current || copying || failed) return
    setCopying(true)
    setFormError(null)
    try {
      await navigator.clipboard.writeText(primaryKey.current)
      if (mounted.current) { setCopied(true); setCloseWarning(false) }
    } catch {
      if (mounted.current) setFormError("密钥复制失败。请允许剪贴板访问后重试，不要关闭此窗口。")
    } finally { if (mounted.current) setCopying(false) }
  }

  return <Dialog open onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="registry-editor-dialog application-create-dialog" finalFocus={false}>
      <form className="registry-editor application-create-form" onSubmit={(event) => void submit(event)}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>{accepted ? "订阅创建进度" : "添加订阅"}</DialogTitle>
          <DialogDescription>{accepted ? displayName : "独立的 APIM 订阅、调用密钥和预算。"}</DialogDescription>
        </DialogHeader>
        <button type="button" className="registry-editor-close" onClick={close} disabled={submitting || copying} aria-label="关闭"><X size={16} /></button>

        <div className="registry-editor-body application-create-body">
          {!accepted ? <>
            {blocker && <div className="application-create-unavailable" role="status"><b>当前环境尚未启用创建</b><span>{blocker}</span><p>可以填写配置，但后台就绪前不会提交创建请求。</p><Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={onRefresh}><RefreshCw size={13} className={refreshing ? "spin" : undefined} />重新检查</Button></div>}
            <div className="registry-field"><span className="registry-field-label">目标网关</span>
              <Select value={gateway?.id ?? null} items={availableGateways.map((item) => ({ value: item.id, label: item.name }))} disabled={submitting || availableGateways.length < 2} onValueChange={(value) => { if (value) { setGatewayId(value); setFormError(null) } }}>
                <SelectTrigger className="registry-select-trigger" aria-label="目标网关"><SelectValue>{gateway ? <span className="registry-option"><GatewayBrandLogo brand={gatewayBrandFromIdentity(`${gateway.name} ${gateway.implementation}`)} size={15} /><span data-no-localize>{gateway.name}</span></span> : "没有可用的 Azure API Management 网关。"}</SelectValue></SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>{availableGateways.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <label className="registry-field"><span className="registry-field-label">订阅名称</span><Input value={displayName} onChange={(event) => { setDisplayName(event.target.value); if (!subscriptionIdEdited) setSubscriptionId(applicationSubscriptionIdFromName(event.target.value)); setFormError(null) }} disabled={submitting} maxLength={100} required placeholder="例如：Invoice Service" /></label>
            <div className="registry-field"><span className="registry-field-label-row"><label htmlFor="application-subscription-id" className="registry-field-label">订阅 ID</label><FieldHelp>网关内唯一的调用标识；只能使用小写字母、数字和连字符。名称为中文时请单独填写。</FieldHelp></span><Input id="application-subscription-id" value={subscriptionId} onChange={(event) => { setSubscriptionIdEdited(true); setSubscriptionId(event.target.value.toLowerCase()); setFormError(null) }} disabled={submitting} maxLength={127} required placeholder="invoice-service" spellCheck={false} autoCapitalize="none" data-no-localize /></div>
            <label className="registry-field"><span className="registry-field-label">说明</span><Textarea value={description} onChange={(event) => setDescription(event.target.value)} disabled={submitting} maxLength={1000} placeholder="可选" /></label>
            <fieldset className="application-create-budget">
              <legend>初始预算</legend>
              <div className="registry-field"><span className="registry-field-label">预算单位</span><Select value={budgetMode} items={[{ value: "tokens", label: "Token" }, { value: "usd", label: "USD" }]} disabled={submitting} onValueChange={value => { if (value === "tokens" || value === "usd") { setBudgetMode(value); setFormError(null) } }}><SelectTrigger aria-label="预算单位"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="tokens">Token</SelectItem><SelectItem value="usd">USD</SelectItem></SelectContent></Select></div>
              <div className="application-create-budget-fields">
                <label className="registry-field"><span className="registry-field-label">{budgetMode === "usd" ? "月度金额上限（USD）" : "月度 Token 上限"}</span><Input name={budgetMode === "usd" ? "usd_limit" : "token_limit"} type={budgetMode === "usd" ? "text" : "number"} inputMode={budgetMode === "usd" ? "decimal" : "numeric"} min={budgetMode === "tokens" ? 1 : undefined} step={budgetMode === "tokens" ? 1 : undefined} value={budgetMode === "usd" ? usdLimit : tokenLimit} onChange={event => { if (budgetMode === "usd") setUsdLimit(event.target.value); else setTokenLimit(event.target.value); setFormError(null) }} disabled={submitting} required /></label>
                <label className="registry-field"><span className="registry-field-label">每分钟 Token 上限</span><Input name="tokens_per_minute" type="number" inputMode="numeric" min={1} max={2_147_483_647} step={1} value={tokensPerMinute} onChange={event => { setTokensPerMinute(event.target.value); setFormError(null) }} disabled={submitting} required /></label>
              </div>
            </fieldset>
            <section className="application-create-tags" aria-labelledby="application-create-tags-title">
              <div className="application-tags-header">
                <h3 id="application-create-tags-title">标签</h3>
                <Button type="button" variant="ghost" size="icon-sm" className="application-tag-add" aria-label="添加标签" title="添加标签" disabled={submitting || tags.length >= 32} onClick={() => setTags(current => [...current, { id: nextTagId.current++, key: "", value: "" }])}><Plus size={14} /></Button>
              </div>
              {tags.length > 0 && <div className="application-tag-columns">
                <span className="registry-field-label" id="application-tag-key-label">标签键</span>
                <span className="registry-field-label" id="application-tag-value-label">标签值</span>
              </div>}
              {tags.map(tag => <div className="application-tag-row" key={tag.id}>
                <div className="registry-field"><Input aria-labelledby="application-tag-key-label" value={tag.key} maxLength={128} placeholder="team" disabled={submitting} required onChange={event => { setTags(current => current.map(item => item.id === tag.id ? { ...item, key: event.target.value } : item)); setFormError(null) }} /></div>
                <div className="registry-field"><Input aria-labelledby="application-tag-value-label" value={tag.value} maxLength={256} placeholder="finance" disabled={submitting} onChange={event => { setTags(current => current.map(item => item.id === tag.id ? { ...item, value: event.target.value } : item)); setFormError(null) }} /></div>
                <Button type="button" variant="ghost" size="icon-sm" aria-label="移除标签" title="移除标签" disabled={submitting} onClick={() => { setTags(current => current.filter(item => item.id !== tag.id)); setFormError(null) }}><Trash2 size={14} /></Button>
              </div>)}
            </section>
            <dl className="application-create-defaults">
              <div><dt>创建内容</dt><dd>独立订阅与密钥</dd></div>
              <div><dt>初始模型访问</dt><dd>已发布模型，未限制</dd></div>
            </dl>
            <p className="application-create-note">创建后可在订阅详情调整预算和模型访问。提交将写入共享 APIM 和应用数据库。</p>
          </> : <>
            <div className={`application-create-operation ${failed ? "failed" : currentOperation?.status}`} role="status"><span>{failed || currentOperation?.worker_available === false ? <AlertTriangle size={15} /> : terminal ? <CheckCircle2 size={15} /> : <RefreshCw className="spin" size={15} />}</span><b>{applicationProvisionStage(currentOperation)}</b></div>
            <dl className="application-create-defaults"><div><dt>订阅 ID</dt><dd><code>{accepted.operation.semantic_preview.apim_subscription_id as string}</code></dd></div><div><dt>操作 ID</dt><dd><code>{accepted.operation.id}</code></dd></div></dl>
            {failed && currentOperation?.error_message && <div className="registry-error" role="alert">{currentOperation.error_message}</div>}
            {!terminal && !failed && <p className="application-create-note" role="status">新订阅通常需要 2–3 分钟生效，期间会自动重试，无需重复提交。</p>}
            {!terminal && currentOperation?.error_message && <p className="application-create-note" role="status">上次检查尚未通过，系统正在自动重试。</p>}
            {retryDetail && <p className="application-create-note">{retryDetail}</p>}
            {failed && currentOperation?.checkpoint.apim_subscription_created === true && <p className="registry-error">部分资源已创建，但创建流程未完成。请检查现有订阅和操作记录；不会自动删除资源或重复生成密钥。</p>}
            {currentOperation?.worker_available === false && !terminal && <div className="registry-error">{currentOperation.worker_unavailable_reason ?? "Release Worker 未部署。"}</div>}
            {operationError && <div className="registry-error" role="alert">{operationError}</div>}
            {!failed && primaryKey.current && <>
              <div className="application-key-warning"><KeyRound size={16} /><div><b>请妥善保存调用密钥</b><span>密钥只保留在此窗口，复制后存入安全位置。创建成功前请勿使用。</span></div></div>
              <div className="application-key-value"><code aria-label="订阅密钥已隐藏">••••••••••••••••••••••••••••••••</code><Button type="button" variant="outline" size="icon-sm" disabled={copying} onClick={() => void copyKey()} aria-label="复制订阅密钥" title="复制订阅密钥"><Copy size={14} /></Button></div>
            </>}
            {copied && !failed && <p className="application-key-copied" role="status">已复制</p>}
            {closeWarning && <p className="registry-error" role="alert">尚未复制密钥。再次关闭将丢弃此窗口中的密钥，创建任务不会取消。</p>}
            <Button type="button" variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}><RefreshCw size={13} />刷新进度</Button>
          </>}
          {formError && <div className="registry-error" role="alert">{formError}</div>}
        </div>

        <DialogFooter className="registry-editor-footer">
          <Button type="button" variant="outline" className="application-create-dismiss" onClick={close} disabled={submitting || copying}>{accepted ? "关闭" : "取消"}</Button>
          {accepted ? applicationId && <Button type="button" onClick={() => { if (primaryKey.current && !copied && !closeWarning) { setCloseWarning(true); return } primaryKey.current = null; onOpenApplication(applicationId) }}>打开订阅详情</Button>
            : <Button type="submit" disabled={submitting || Boolean(blocker) || Boolean(validation)}>{submitting ? <RefreshCw className="spin" size={14} /> : <Plus size={14} />}{submitting ? "正在创建" : "创建并生成密钥"}</Button>}
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}