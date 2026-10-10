import { useEffect, useState } from "react"
import { Tabs } from "@base-ui/react/tabs"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Check, Languages, Monitor, Moon, PanelLeft, RefreshCw, Save, Settings, Sparkles, Sun } from "lucide-react"

import { assistantApi } from "../components/assistant/api"
import { assistantSettingsKey, assistantSettingsQuery } from "../components/assistant/queries"
import { assistantSettingsChanged, assistantSettingsModel, assistantSettingsUpdate } from "../components/assistant/settings-update"
import type { AssistantApiFormat, AssistantSettingsWrite } from "../components/assistant/types"
import { CopilotLogo } from "../components/brand-logos"
import { CopilotSettingsSection } from "../data-sources/github-copilot/settings-section"
import { Button } from "../components/ui/button"
import { Switch } from "../components/ui/switch"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/ui/select"
import { getIntlLocale, type LocalePreference, useLocale } from "../locales/index"
import { type ThemePreference, useTheme } from "../providers/theme-provider"
import { useTimezone } from "../providers/timezone-provider"
import { useAuth } from "../providers/auth-provider"

const themes: Array<{
  value: ThemePreference
  label: string
  icon: typeof Sun
}> = [
  { value: "light", label: "浅色", icon: Sun },
  { value: "dark", label: "深色", icon: Moon },
  { value: "system", label: "跟随系统", icon: Monitor },
]

const languages: Array<{ value: LocalePreference; label: string }> = [
  { value: "en", label: "English" },
  { value: "zh-CN", label: "简体中文" },
  { value: "zh-TW", label: "繁體中文" },
  { value: "ko", label: "한국어" },
  { value: "ja", label: "日本語" },
]

type Pane = "preferences" | "assistant" | "copilot"

/** Null means automatic selection, which the Select has to carry as a real option rather
 *  than as an empty value: "let the registry decide" is a choice an administrator makes,
 *  not the absence of one, and Base UI reads an empty string as nothing selected. */
const AUTOMATIC = "automatic"
const API_LABELS: Record<AssistantApiFormat, string> = {
  openai_chat: "Chat Completions",
  openai_responses: "Responses",
  anthropic_messages: "Anthropic Messages",
}

function price(value: number | null | undefined) {
  return value === null || value === undefined ? "\u2014" : `$${value}`
}

function useAssistantEditor(enabled: boolean) {
  const queryClient = useQueryClient()
  const settings = useQuery({ ...assistantSettingsQuery(), enabled })
  const [draft, setDraft] = useState<AssistantSettingsWrite | null>(null)
  const save = useMutation({
    mutationFn: (body: AssistantSettingsWrite) =>
      assistantApi.saveSettings(body),
    onSuccess: (next) => {
      queryClient.setQueryData(assistantSettingsKey, next)
      setDraft(null)
    },
  })

  const value = settings.data
  const form = value ? assistantSettingsUpdate(value, draft ?? {}) : null
  const dirty = !!value && !!form && assistantSettingsChanged(value, form)

  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [dirty])

  function update(changes: Partial<AssistantSettingsWrite>) {
    if (!value || !form || save.isPending) return
    save.reset()
    setDraft(assistantSettingsUpdate(value, { ...form, ...changes }))
  }

  function submit() {
    if (!value || !form || !dirty || save.isPending) return
    save.mutate(assistantSettingsUpdate(value, form))
  }

  return { settings, save, value, form, dirty, update, submit }
}

function AssistantSection({ editor, canEdit }: {
  editor: ReturnType<typeof useAssistantEditor>
  canEdit: boolean
}) {
  const { settings, save, value, form, update, submit } = editor
  const chosen = value?.available_models.find((model) => model.id === form?.model_id)
  const effective = value && form ? assistantSettingsModel(value, form) : undefined
  const apiFormats = effective?.api_formats
    ?? (effective?.id === value?.effective_model_id ? value?.available_api_formats : [])
    ?? []
  const disabled = !value || !canEdit || save.isPending
  const automaticLabel = "自动选择"

  return <form id="assistant-settings-form" onSubmit={(event) => {
    event.preventDefault()
    if (canEdit) submit()
  }} aria-busy={save.isPending}>
    <div className="settings-section">
      <div>
        <h2>助手模型</h2>
        <p>FinOps 助手回答问题时调用的模型。仅列出已启用且支持工具调用的模型。</p>
      </div>
      <Select
        items={[
          { value: AUTOMATIC, label: automaticLabel },
          ...(value?.available_models ?? []).map((model) => ({
            value: model.id, label: model.display_name,
          })),
        ]}
        value={form?.model_id ?? AUTOMATIC}
        disabled={disabled}
        onValueChange={(next) => next && update({
          model_id: next === AUTOMATIC ? null : String(next),
        })}
      >
        <SelectTrigger aria-label="助手模型" className="settings-select-trigger">
          <SelectValue>
            {!value && settings.isPending
              ? <span className="settings-inline-loading"><RefreshCw className="spin" size={13} />正在加载模型</span>
              : chosen ? chosen.display_name : automaticLabel}
          </SelectValue>
        </SelectTrigger>
        <SelectContent align="start" alignItemWithTrigger={false}>
          <SelectGroup>
          <SelectItem value={AUTOMATIC}>{automaticLabel}</SelectItem>
          {(value?.available_models ?? []).map((model) => <SelectItem key={model.id} value={model.id}>
            <span className="settings-model-option">
              <b>{model.display_name}</b>
              <small>
                {model.runtime_name} · 输入 {price(model.input_cost_per_million)} / 输出{" "}
                {price(model.output_cost_per_million)} 每百万 Token
              </small>
            </span>
          </SelectItem>)}
          </SelectGroup>
        </SelectContent>
      </Select>
      {value?.effective_model_name && <small>
        当前使用：<span data-no-localize>{value.effective_model_name}</span>
      </small>}
      {/* The reason is carried through rather than swallowed. A bare "could not be read"
          makes an undeployed endpoint look identical to a broken page, and this pane is
          exactly where someone lands when the API they are pointed at is older than the
          UI. */}
      {settings.isError && <p className="settings-warning">
        <AlertTriangle size={13} />
        <span>
          无法读取助手设置
          <br />
          <span data-no-localize>{settings.error instanceof Error ? settings.error.message : ""}</span>
        </span>
        <Button type="button" variant="ghost" size="icon-sm" title="重试" aria-label="重试"
          disabled={settings.isFetching} onClick={() => void settings.refetch()}>
          <RefreshCw />
        </Button>
      </p>}
      {/* Only the degraded cases are surfaced. Reporting a healthy pin as well would make
          this line permanent furniture and train the reader to skip it. */}
      {value && !value.model_available && <p className="settings-warning">
        <AlertTriangle size={13} />
        指定的模型已停用或其运行时不支持工具调用，助手已回退到自动选择。
      </p>}
      {value && value.available_models.length === 0 && <p className="settings-warning">
        <AlertTriangle size={13} />
        没有任何已启用的模型支持工具调用，助手无法回答问题。
      </p>}
    </div>
    <div className="settings-section compact">
      <h2>接口协议</h2>
      <Select
        items={[
          { value: AUTOMATIC, label: automaticLabel },
          ...apiFormats.map((api) => ({
            value: api, label: API_LABELS[api],
          })),
        ]}
        value={form?.api_format ?? AUTOMATIC}
        disabled={disabled || !value?.available_api_formats || !apiFormats.length}
        onValueChange={(next) => next && update({
          api_format: next === AUTOMATIC ? null : next as AssistantApiFormat,
        })}
      >
        <SelectTrigger aria-label="接口协议" className="settings-select-trigger">
          <SelectValue>
            {form?.api_format
              ? <span data-no-localize>{API_LABELS[form.api_format]}</span>
              : automaticLabel}
          </SelectValue>
        </SelectTrigger>
        <SelectContent align="start" alignItemWithTrigger={false}>
          <SelectGroup>
            <SelectItem value={AUTOMATIC}>{automaticLabel}</SelectItem>
            {apiFormats.map((api) => (
              <SelectItem key={api} value={api}>
                <span data-no-localize>{API_LABELS[api]}</span>
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
      {value?.effective_api_format && value.effective_api_path && <small>
        实际接口：<span data-no-localize>
          {API_LABELS[value.effective_api_format]} · POST {value.effective_api_path}
        </span>
      </small>}
      {value?.api_available === false && <p className="settings-warning" role="alert">
        <AlertTriangle size={13} />
        指定协议已不可用，当前使用自动匹配的接口。
      </p>}
      {save.isError && <p className="settings-warning" role="alert">
        <AlertTriangle size={13} />
        <span>
          助手设置保存失败
          <br />
          <span data-no-localize>{save.error instanceof Error ? save.error.message : ""}</span>
        </span>
      </p>}
    </div>
    <div className="settings-section compact">
      <div>
        <h2>自动生成对话标题</h2>
        <p>新对话产生首个回答后，额外调用一次模型为其命名。关闭后沿用首个问题作为标题。</p>
      </div>
      <label className="settings-switch">
        <Switch
          aria-label="自动生成对话标题"
          checked={form?.auto_title ?? false}
          disabled={disabled}
          onCheckedChange={(checked) => update({
            auto_title: checked === true,
          })}
        />
        <span>{form ? (form.auto_title ? "已开启" : "已关闭") : "\u2014"}</span>
      </label>
    </div>
  </form>
}

export function SettingsPage({ dataSource, onToggleSidebar }: {
  dataSource: "apim" | "github-copilot"
  onToggleSidebar: () => void
}) {
  const [pane, setPane] = useState<Pane>(() => dataSource === "apim" ? "preferences" : "copilot")
  useEffect(() => {
    setPane(dataSource === "apim" ? "preferences" : "copilot")
  }, [dataSource])
  const activePane = pane
  const editor = useAssistantEditor(dataSource === "apim" && activePane === "assistant")
  const { user } = useAuth()
  const canEdit = user?.role === "owner"
  const { preference, resolvedTheme, setPreference } = useTheme()
  const { locale, setLocale } = useLocale()
  const { preference: timezonePreference, browserTimezone, setPreference: setTimezonePreference } = useTimezone()
  const browserTimezoneLabel = `${browserTimezone}（浏览器）`
  const timezoneLabel = timezonePreference === "UTC" ? "UTC" : browserTimezoneLabel
  return <Tabs.Root className="smh-workspace settings-workspace" value={activePane}
    onValueChange={(next) => setPane(next as Pane)}>
    <header className="smh-page-header">
      <div>
        <Button type="button" variant="ghost" size="icon-sm" className="model-mobile-sidebar-toggle"
          aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}><PanelLeft size={16} /></Button>
        <span className="smh-header-icon"><Settings size={17} /></span><h1>系统配置</h1>
      </div>
      {activePane === "assistant" && <div className="smh-header-actions settings-save-actions">
        <span role="status" className="settings-save-status">
          {editor.dirty ? "未保存" : editor.save.isSuccess ? "配置已保存" : ""}
        </span>
        {canEdit && <Button type="submit" form="assistant-settings-form" variant="outline" size="sm"
          title="保存" aria-label="保存"
          disabled={!editor.dirty || !editor.value || editor.save.isPending || editor.settings.isError}>
          {editor.save.isPending
            ? <RefreshCw size={14} data-icon="inline-start" className="spin" />
            : <Save size={14} data-icon="inline-start" />}
          保存
        </Button>}
      </div>}
    </header>
    <div className="settings-toolbar">
      <Tabs.List className="settings-tabs usage-metric-segment" aria-label="系统配置">
        {dataSource === "github-copilot" && <Tabs.Tab value="copilot"><CopilotLogo size={14} />GitHub Copilot</Tabs.Tab>}
        <Tabs.Tab value="preferences"><Settings size={14} />偏好设置</Tabs.Tab>
        {dataSource === "apim" && <Tabs.Tab value="assistant"><Sparkles size={14} /><span data-no-localize>FinOps Assistant</span></Tabs.Tab>}
      </Tabs.List>
    </div>
    <div className="settings-workspace-scroll">
      {dataSource === "github-copilot" && <Tabs.Panel value="copilot" className="settings-content"><CopilotSettingsSection /></Tabs.Panel>}
      {dataSource === "apim" && <Tabs.Panel value="assistant" className="settings-content">
        <AssistantSection editor={editor} canEdit={canEdit} />
      </Tabs.Panel>}
      <Tabs.Panel value="preferences" className="settings-content">
      <div className="settings-section">
        <div><h2>主题</h2><p>选择 FinOps 工作台的显示外观。</p></div>
        <div className="theme-options">
          {themes.map(({ value, label, icon: Icon }) => <button key={value} type="button" aria-pressed={preference === value} className={preference === value ? "selected" : ""} onClick={() => setPreference(value)}><span className={`theme-preview ${value}`}><i className="preview-dots" /><i className="preview-sidebar" /><i className="preview-line one" /><i className="preview-line two" />{preference === value && <b><Check size={11} /></b>}</span><span><Icon size={13} />{label}</span></button>)}
        </div>
        <small>当前实际显示：{resolvedTheme === "dark" ? "深色" : "浅色"}</small>
      </div>
      <div className="settings-section compact">
        <div><h2>语言</h2><p>应用界面语言。</p></div>
        <div className="language-control">{languages.map(({ value, label }) => {
          const active = value === locale
          return <button key={value} type="button" aria-pressed={active} className={active ? "active" : ""} onClick={() => setLocale(value)}>{active && <Languages size={13} />}{label}</button>
        })}</div>
      </div>
      <div className="settings-section compact">
        <div><h2>查看时区</h2><p>用于用量活动图和热力图日期。</p></div>
        <Select value={timezonePreference} onValueChange={(value) => value && setTimezonePreference(value as "browser" | "UTC")}>
          <SelectTrigger aria-label="查看时区" className="settings-select-trigger">
            <SelectValue>{timezoneLabel}</SelectValue>
          </SelectTrigger>
          <SelectContent align="start" alignItemWithTrigger={false}>
            <SelectItem value="browser">{browserTimezoneLabel}</SelectItem>
            <SelectItem value="UTC">UTC</SelectItem>
          </SelectContent>
        </Select>
      </div>
      </Tabs.Panel>
    </div>
    <footer className="model-table-footer settings-audit-footer">
      {activePane === "assistant" && editor.value?.updated_by && editor.value.updated_at && <span>
        上次修改：<span data-no-localize>
          {new Date(editor.value.updated_at).toLocaleString(getIntlLocale())} · {editor.value.updated_by}
        </span>
      </span>}
    </footer>
  </Tabs.Root>
}
