import { useId, useState, type FormEvent } from "react"
import { LoaderCircle, Save, X } from "lucide-react"

import type { ManagedModel, ModelAvailabilityReadback, ModelRegistry } from "../../data-sources/apim/types"
import {
  GatewayBrandLogo,
  ProviderBrandLogo,
  gatewayBrandFromIdentity,
  providerBrandFromMetadata,
} from "../brand-logos"
import { Button } from "../ui/button"
import { Checkbox } from "../ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog"
import { Input } from "../ui/input"
import { FieldHelp } from "./field-help"
import { ModelAvailabilityStatus, ModelAvailabilitySwitch, useModelAvailabilityCommand } from "./model-availability"
import {
  createModelEditDraft,
  modelEditHasChanges,
  modelEditPayload,
  modelEditRoleOptions,
  toggleModelSelection,
  validateModelEdit,
  type ModelEditDraft,
  type ModelEditError,
} from "./model-edit-form"
import { ModelPriceSourceFields } from "./model-price-source"
import { ModelVendorLogo, modelVendorFromMetadata, modelVendorLabel } from "./model-vendor-select"

const VALIDATION_MESSAGES: Record<ModelEditError, string> = {
  display_name: "显示名称须为 1–255 个字符。",
  context_window: "上下文窗口须为正整数，或留空。",
  prices: "单价须为非负有限数字，或留空。",
  price_reference: "跟随官方价时，必须先选定一个官方价基准。",
  context_pricing_preview: "当前后端尚不支持分档计费，暂不可保存或发布。",
  context_pricing_incomplete: "分档价格或阈值不完整，不能保存。",
  context_price_rule: "阈值须为正整数。",
  context_price_rule_preview: "当前后端尚不支持自定义阈值，暂不可保存或发布。",
  discount: "折扣须为 0–100 之间的数字，或留空以继承连接折扣。",
}

function EditNumberField({ id, label, unit, value, onChange, busy, integer, help }: {
  id: string
  label: string
  unit?: string
  value: string
  onChange: (value: string) => void
  busy: boolean
  integer?: boolean
  help?: string
}) {
  return <div className="registry-field">
    <span className="registry-field-label-row"><label htmlFor={id} className="registry-field-label">{label}</label>{unit && <span className="model-editor-unit" data-no-localize>{unit}</span>}{help && <FieldHelp>{help}</FieldHelp>}</span>
    <Input id={id} type="number" inputMode={integer ? "numeric" : "decimal"} min={integer ? 1 : 0} step={integer ? 1 : "any"} value={value} onChange={(event) => onChange(event.target.value)} disabled={busy} />
  </div>
}

export function ModelEditDialog({ registry, model, availability, availabilityLoading, busy, error, onClose, onSave }: {
  registry: ModelRegistry
  model: ManagedModel
  availability?: ModelAvailabilityReadback
  availabilityLoading?: boolean
  busy: boolean
  error: string | null
  onClose: () => void
  onSave: (value: ReturnType<typeof modelEditPayload>) => void
}) {
  const id = useId()
  const availabilityAction = useModelAvailabilityCommand(model.id, availability)
  const [initial] = useState(() => createModelEditDraft(model, registry.context_pricing_supported === true, registry.context_price_rules_supported === true))
  const [draft, setDraft] = useState<ModelEditDraft>(initial)
  const [submitted, setSubmitted] = useState(false)
  const runtime = registry.runtimes.find((item) => item.id === model.runtime_id)
  const provider = registry.providers.find((item) => item.id === model.provider_id)
  const gateway = registry.gateways.find((item) => item.id === runtime?.gateway_profile_id)
  const isOpenAICompatible = provider?.provider_kind === "openai_compatible"
  const imageGeneration = model.capabilities.includes("image_generation")
  const decision = model.capabilities.includes("decision")
  const vendor = runtime && isOpenAICompatible
    ? modelVendorFromMetadata(runtime.config, `${runtime.name} ${runtime.provider_name}`)
    : null
  const upstreamLabel = runtime?.brand_key === "microsoft_foundry" ? "Deployment Name"
    : runtime?.brand_key === "amazon_bedrock" ? "Model / Inference Profile ID" : "上游模型 ID"
  const roleOptions = modelEditRoleOptions(model)
  const dirty = modelEditHasChanges(initial, draft)
  const validation = validateModelEdit(draft)
  const followingListPrice = draft.priceSource !== "manual"
  const imagePriceMissing = imageGeneration && [draft.inputPrice, draft.cacheReadPrice, draft.outputPrice].some(value => !value.trim())
  const validationMessage = (dirty || submitted) ? imagePriceMissing ? "请填写文字输入、缓存文字和图像输出单价。" : validation && validation !== "context_pricing_preview" ? VALIDATION_MESSAGES[validation] : null : null
  const update = <K extends keyof ModelEditDraft>(key: K, value: ModelEditDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
  }
  const close = () => { if (!busy) onClose() }
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSubmitted(true)
    if (busy || !dirty || validation || imagePriceMissing) return
    onSave(modelEditPayload(model, draft))
  }

  return <Dialog open onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="registry-editor-dialog simple-model-dialog model-edit-dialog" finalFocus={false}>
      <form className="registry-editor simple-model-form" onSubmit={submit}>
        <DialogHeader className="registry-editor-header">
          <DialogTitle>编辑模型</DialogTitle>
          <DialogDescription>更新模型名称、价格和访问配置。连接与模型标识保持不变。</DialogDescription>
        </DialogHeader>
        <button type="button" className="registry-editor-close" onClick={close} disabled={busy} aria-label="关闭"><X size={16} /></button>

        <div className="registry-editor-body simple-model-body">
          <section className="simple-model-section" aria-labelledby={`${id}-identity`}>
            <div className="simple-section-title"><b id={`${id}-identity`}>模型身份</b><FieldHelp>连接和模型标识决定已发布路由，仅供查看；更换连接请走模型接入与发布流程。</FieldHelp></div>
            <dl className="model-editor-identity">
              <div><dt>目标网关</dt><dd className="registry-option">{gateway && <GatewayBrandLogo brand={gatewayBrandFromIdentity(`${gateway.name} ${gateway.implementation}`)} size={15} />}<span data-no-localize>{gateway?.name ?? runtime?.gateway_name ?? "—"}</span></dd></div>
              <div><dt>{vendor ? "API 服务商" : "接入类型"}</dt><dd className="registry-option">{vendor ? <ModelVendorLogo value={vendor} size={15} /> : <ProviderBrandLogo brand={providerBrandFromMetadata(runtime?.brand_key ?? provider?.brand_key, model.provider_name)} size={15} />}{vendor ? <span>{modelVendorLabel(vendor)}</span> : <span data-no-localize>{model.provider_name}</span>}</dd></div>
              <div className="model-editor-identity-wide"><dt>连接</dt><dd data-no-localize>{runtime?.name ?? model.runtime_name}</dd></div>
              <div><dt>模型别名</dt><dd><code>{model.model_key}</code></dd></div>
              <div><dt>{upstreamLabel}</dt><dd><code>{model.upstream_model_id ?? "—"}</code></dd></div>
            </dl>
          </section>

          <section className="simple-model-section simple-connection-section" aria-labelledby={`${id}-basic`}>
            <div className="simple-section-title"><b id={`${id}-basic`}>基本信息</b></div>
            <label className="registry-field"><span className="registry-field-label">显示名称</span><Input value={draft.displayName} onChange={(event) => update("displayName", event.target.value)} required maxLength={255} disabled={busy} aria-invalid={validation === "display_name" || undefined} /></label>
          </section>

          <section className="simple-model-section simple-connection-section" aria-labelledby={`${id}-pricing`}>
            <div className="publication-section-heading"><span className="simple-section-title"><b id={`${id}-pricing`}>价格与限制</b></span><span className="model-editor-unit" data-no-localize>USD / 1M Tokens</span></div>
            <ModelPriceSourceFields model={model} draft={draft} setDraft={setDraft}
              busy={busy} connectionDiscount={runtime?.price_discount_percent ?? null}
              imageGeneration={imageGeneration} showAnthropic={!imageGeneration && !decision} />
            <div className={imageGeneration || draft.contextPricingPreview ? "form-grid" : "form-grid three"}>
              {!imageGeneration && <EditNumberField id={`${id}-context`} label="上下文窗口" unit="Tokens" integer value={draft.contextWindow} onChange={(value) => update("contextWindow", value)} busy={busy} />}
              {!draft.contextPricingPreview && <EditNumberField id={`${id}-input`} label={imageGeneration ? "文字输入单价" : "输入单价"} value={draft.inputPrice} onChange={(value) => update("inputPrice", value)} busy={busy || followingListPrice} />}
              {!draft.contextPricingPreview && <EditNumberField id={`${id}-output`} label={imageGeneration ? "图像输出单价" : "输出单价"} value={draft.outputPrice} onChange={(value) => update("outputPrice", value)} busy={busy || followingListPrice} />}
            </div>
            {!draft.contextPricingPreview && <div className="form-grid">
              <EditNumberField id={`${id}-cache-read`} label={imageGeneration ? "缓存文字单价" : "缓存读取单价"} value={draft.cacheReadPrice} onChange={(value) => update("cacheReadPrice", value)} busy={busy || followingListPrice} help="留空按输入单价计费；填写 0 表示免费。" />
              {!imageGeneration && <EditNumberField id={`${id}-cache-write`} label="缓存写入单价" value={draft.cacheWritePrice} onChange={(value) => update("cacheWritePrice", value)} busy={busy || followingListPrice} help="留空按缓存读取单价计费；读取单价也未填写时按输入单价。填写 0 表示免费。" />}
            </div>}
            {!draft.contextPricingPreview && <p className="publication-form-note">{followingListPrice
              ? "跟随官方价时，这四个值由 官方价 × 折扣 自动算出，改折扣或换基准即可调整。"
              : "留空表示未配置，不等于 0。"}</p>}
          </section>

          <section className="simple-model-section simple-connection-section" aria-labelledby={`${id}-access`}>
            <div className="simple-section-title"><b id={`${id}-access`}>状态与访问</b></div>
            <div className="model-editor-state">
              <div className="model-availability-summary"><ModelAvailabilityStatus readback={availability} loading={availabilityLoading} pending={availabilityAction.submitting} requestedEnabled={availabilityAction.requestedEnabled} error={availabilityAction.error} /><ModelAvailabilitySwitch readback={availability} loading={availabilityLoading} registryEnabled={model.enabled} pending={availabilityAction.submitting} requestedEnabled={availabilityAction.requestedEnabled} disabled={busy} onChange={availabilityAction.change} /></div>
              <div className="registry-field-label-row"><label className="model-editor-checkbox"><Checkbox checked={draft.isDefault} disabled={busy || imageGeneration || decision} onCheckedChange={(checked) => update("isDefault", checked)} /><span>设为默认</span></label><FieldHelp>{decision ? "Decision 模型不能设为聊天默认" : imageGeneration ? "图片模型不能设为聊天默认" : "设为整个模型目录的默认模型，并将其连接设为默认；不改变网关路由优先级。"}</FieldHelp></div>
            </div>
            {availabilityAction.error && <div className="registry-error" role="alert">{availabilityAction.error}</div>}
            <details className="model-editor-advanced">
              <summary>高级访问限制</summary>
              <div className="model-editor-advanced-body">
                <fieldset className="model-editor-choices" disabled={busy}>
                  <legend className="registry-field-label">允许角色</legend>
                  <div className="model-editor-checkbox-grid">
                    {roleOptions.map((role) => <label key={role} className="model-editor-checkbox">
                      <Checkbox checked={draft.allowedRoles.includes(role)} disabled={busy} onCheckedChange={(checked) => update("allowedRoles", toggleModelSelection(draft.allowedRoles, role, checked))} />
                      <span data-no-localize>{role === "owner" ? "Owner" : role === "member" ? "Member" : role}</span>
                      {role !== "owner" && role !== "member" && <small>兼容角色</small>}
                    </label>)}
                  </div>
                </fieldset>
                <p className="publication-form-note">角色限制不替代人员模型分配，仍需满足连接和人员的访问策略。</p>
                {roleOptions.some((role) => role !== "owner" && role !== "member") && <p className="publication-form-note">当前应用登录角色为 Owner 和 Member；其他已有角色值按兼容配置保留。</p>}
                {draft.allowedRoles.length === 0 && <p className="model-editor-access-warning" role="status">未选择角色；此模型将无法通过应用的角色检查。</p>}
              </div>
            </details>
          </section>

          {(validationMessage || error) && <div className="registry-error" role="alert">{validationMessage ?? error}</div>}
        </div>

        <DialogFooter className="registry-editor-footer">
          <Button type="button" variant="outline" className="publication-dismiss" onClick={close} disabled={busy}>取消</Button>
          <Button type="submit" disabled={busy || !dirty || Boolean(validation) || imagePriceMissing}>{busy ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}{busy ? "正在保存" : "保存更改"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
