import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Check, Languages, Monitor, Moon, RefreshCw, Settings, Sparkles, Sun } from "lucide-react"

import { assistantApi } from "../components/assistant/api"
import { assistantSettingsKey, assistantSettingsQuery } from "../components/assistant/queries"
import { CopilotLogo } from "../components/brand-logos"
import { CopilotSettingsSection } from "../data-sources/github-copilot/settings-section"
import { Button } from "../components/ui/button"
import { Switch } from "../components/ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/ui/select"
import { getIntlLocale, type LocalePreference, useLocale } from "../locales/index"
import { type ThemePreference, useTheme } from "../providers/theme-provider"
import { useTimezone } from "../providers/timezone-provider"

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

function price(value: number | null | undefined) {
  return value === null || value === undefined ? "\u2014" : `$${value}`
}

function AssistantSection() {
  const queryClient = useQueryClient()
  const settings = useQuery(assistantSettingsQuery())
  const save = useMutation({
    mutationFn: (body: { model_id: string | null; auto_title: boolean }) =>
      assistantApi.saveSettings(body),
    // The response is the whole settings object, so the cache is written rather than
    // invalidated: there is nothing left to fetch.
    onSuccess: (next) => queryClient.setQueryData(assistantSettingsKey, next),
  })

  const value = settings.data
  // The pane's structure does not depend on the response -- two headings, a picker and a
  // switch, always. Only the picker's options and the switch's position do, so only those
  // wait. Blanking the whole pane behind one spinner made a 200 ms fetch look like a page
  // that had not loaded.
  const chosen = value?.available_models.find((model) => model.id === value.model_id)
  // Deliberately NOT `自动选择（当前为 ${name}）`. A phrase composed at runtime is invisible
  // to the static extractor that feeds the locale catalogs, so only the leading words got
  // translated and the label shipped as "Automatic (当前为 gpt-5.6-luna)". The model name is
  // its own line below instead, where it needs no sentence around it.
  const automaticLabel = "自动选择"

  return <>
    <div className="settings-section">
      <div>
        <h2>助手模型</h2>
        <p>FinOps 助手回答问题时调用的模型。仅列出已启用且支持工具调用的模型。</p>
      </div>
      <Select
        value={value?.model_id ?? AUTOMATIC}
        disabled={!value || save.isPending}
        onValueChange={(next) => next && value && save.mutate({
          model_id: next === AUTOMATIC ? null : String(next),
          auto_title: value.auto_title,
        })}
      >
        <SelectTrigger aria-label="助手模型" className="settings-select-trigger">
          <SelectValue>
            {!value && settings.isPending
              ? <span className="settings-inline-loading"><RefreshCw className="spin" size={13} />正在加载模型</span>
              : chosen ? chosen.display_name : value?.model_id ? "指定模型不可用" : automaticLabel}
          </SelectValue>
        </SelectTrigger>
        <SelectContent align="start" alignItemWithTrigger={false}>
          <SelectItem value={AUTOMATIC}>{automaticLabel}</SelectItem>
          {value?.model_id && !chosen && <SelectItem value={value.model_id} disabled>指定模型不可用</SelectItem>}
          {(value?.available_models ?? []).map((model) => <SelectItem key={model.id} value={model.id}>
            <span className="settings-model-option">
              <b>{model.display_name}</b>
              <small>
                {model.runtime_name} · 输入 {price(model.input_cost_per_million)} / 输出{" "}
                {price(model.output_cost_per_million)} 每百万 Token
              </small>
            </span>
          </SelectItem>)}
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
      </p>}
      {/* Only the degraded cases are surfaced. Reporting a healthy pin as well would make
          this line permanent furniture and train the reader to skip it. */}
      {value && !value.model_available && <p className="settings-warning">
        <AlertTriangle size={13} />
        指定或默认模型不可用，原配置已保留。
      </p>}
      {value && value.available_models.length === 0 && <p className="settings-warning">
        <AlertTriangle size={13} />
        没有任何已启用的模型支持工具调用，助手无法回答问题。
      </p>}
      {value?.updated_by && value.updated_at && <small>
        上次修改：{new Date(value.updated_at).toLocaleString(getIntlLocale())} · {value.updated_by}
      </small>}
    </div>
    <div className="settings-section compact">
      <div>
        <h2>自动生成对话标题</h2>
        <p>新对话产生首个回答后，额外调用一次模型为其命名。关闭后沿用首个问题作为标题。</p>
      </div>
      <label className="settings-switch">
        <Switch
          // Off, not on, while the real position is unknown: a switch that starts on and
          // flicks off is a worse lie than one that has not moved yet. Disabled until it
          // is real, so nobody toggles a value that is about to be replaced.
          checked={value?.auto_title ?? false}
          disabled={!value || save.isPending}
          onCheckedChange={(checked) => value && save.mutate({
            model_id: value.model_id,
            auto_title: checked === true,
          })}
        />
        <span>{value ? (value.auto_title ? "已开启" : "已关闭") : "\u2014"}</span>
      </label>
    </div>
  </>
}

export function SettingsPage({ dataSource }: { dataSource: "apim" | "github-copilot" }) {
  const [pane, setPane] = useState<Pane>(() => dataSource === "apim" ? "preferences" : "copilot")
  useEffect(() => {
    setPane(dataSource === "apim" ? "preferences" : "copilot")
  }, [dataSource])
  const activePane = pane
  const { preference, resolvedTheme, setPreference } = useTheme()
  const { locale, setLocale } = useLocale()
  const { preference: timezonePreference, browserTimezone, setPreference: setTimezonePreference } = useTimezone()
  const browserTimezoneLabel = `${browserTimezone}（浏览器）`
  const timezoneLabel = timezonePreference === "UTC" ? "UTC" : browserTimezoneLabel
  return <div className="settings-page">
    <div className="settings-sidebar">
      <h1>设置</h1>
      {dataSource === "github-copilot" && <button type="button" className={activePane === "copilot" ? "active" : ""} onClick={() => setPane("copilot")}><CopilotLogo size={14} />GitHub Copilot{activePane === "copilot" && <i />}</button>}
      <button type="button" className={activePane === "preferences" ? "active" : ""} onClick={() => setPane("preferences")}><Settings size={14} />偏好设置{activePane === "preferences" && <i />}</button>
      {/* The assistant's own name and its own mark, the same pair the composer chip uses.
          "AI 助手" named a category; this names the thing being configured, and a product
          name is not translated. */}
      {dataSource === "apim" && <button type="button" className={activePane === "assistant" ? "active" : ""} onClick={() => setPane("assistant")}><Sparkles size={14} /><span data-no-localize>FinOps Assistant</span>{activePane === "assistant" && <i />}</button>}
    </div>
    <section className="settings-content">
      {activePane === "copilot" ? <CopilotSettingsSection /> : activePane === "assistant" ? <AssistantSection /> : <>
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
          <SelectTrigger aria-label="查看时区" className="settings-select-trigger settings-timezone-trigger">
            <SelectValue>{timezoneLabel}</SelectValue>
          </SelectTrigger>
          <SelectContent align="start" alignItemWithTrigger={false}>
            <SelectItem value="browser">{browserTimezoneLabel}</SelectItem>
            <SelectItem value="UTC">UTC</SelectItem>
          </SelectContent>
        </Select>
      </div>
      </>}
    </section>
  </div>
}
