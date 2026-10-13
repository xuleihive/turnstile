import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertTriangle, Download, FileSearch, LoaderCircle, ShieldCheck } from "lucide-react"

import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { ResizableTable } from "../components/ui/resizable-table"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select"
import {
  downloadSecurityAuditCsv,
  exportSecurityAuditEvents,
  listSecurityAuditEvents,
  type SecurityAuditEvent,
  type SecurityAuditFilters,
  type SecurityAuditResult,
} from "../data-sources/apim/security-audit"
import { getIntlLocale } from "../locales"

const RESULT_ALL = "__all__"

function localInputValue(date: Date) {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return shifted.toISOString().slice(0, 16)
}

function dateFilter(value: string) {
  if (!value) return undefined
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? undefined : parsed.toISOString()
}

function eventTime(value: string) {
  return new Intl.DateTimeFormat(getIntlLocale(), {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value))
}

function metadata(value: Record<string, unknown>) {
  return Object.keys(value).length ? JSON.stringify(value, null, 2) : "—"
}

function AuditRow({ event }: { event: SecurityAuditEvent }) {
  return (
    <tr>
      <td className="security-audit-time">{eventTime(event.created_at)}</td>
      <td>
        <span>{event.actor_email ?? "系统 / 未认证"}</span>
        {event.actor_role && <small>{event.actor_role}</small>}
        {event.actor_user_id && <code>{event.actor_user_id}</code>}
      </td>
      <td><code>{event.action}</code></td>
      <td>
        <span>{event.resource_type}</span>
        {event.resource_id && <code>{event.resource_id}</code>}
      </td>
      <td><span className={`security-audit-result ${event.result}`}>{event.result}</span></td>
      <td>{event.client_ip ?? "—"}</td>
      <td>
        <details>
          <summary>查看安全元数据</summary>
          <dl>
            <dt>Request ID</dt><dd>{event.request_id ?? "—"}</dd>
            <dt>Correlation ID</dt><dd>{event.correlation_id ?? "—"}</dd>
            <dt>User-Agent</dt><dd>{event.user_agent ?? "—"}</dd>
            <dt>变更前</dt><dd><pre>{metadata(event.before_data)}</pre></dd>
            <dt>变更后</dt><dd><pre>{metadata(event.after_data)}</pre></dd>
          </dl>
        </details>
      </td>
    </tr>
  )
}

export function SecurityAuditPage() {
  const now = useMemo(() => new Date(), [])
  const initialFrom = localInputValue(new Date(now.getTime() - 7 * 24 * 60 * 60_000))
  const initialTo = localInputValue(now)
  const [from, setFrom] = useState(initialFrom)
  const [to, setTo] = useState(initialTo)
  const [actorId, setActorId] = useState("")
  const [action, setAction] = useState("")
  const [result, setResult] = useState<SecurityAuditResult | typeof RESULT_ALL>(RESULT_ALL)
  const [filters, setFilters] = useState<SecurityAuditFilters>(() => ({
    from: dateFilter(initialFrom),
    to: dateFilter(initialTo),
  }))
  const [cursor, setCursor] = useState<string | null>(null)
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([])
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  const events = useQuery({
    queryKey: ["security-audit", filters, cursor],
    queryFn: () => listSecurityAuditEvents(filters, cursor),
    retry: false,
  })

  function applyFilters() {
    const next: SecurityAuditFilters = {
      from: dateFilter(from),
      to: dateFilter(to),
      actor_id: actorId.trim() || undefined,
      action: action.trim() || undefined,
      result: result === RESULT_ALL ? undefined : result,
    }
    setFilters(next)
    setCursor(null)
    setCursorHistory([])
  }

  async function exportCsv() {
    setExporting(true)
    setExportError(null)
    try {
      downloadSecurityAuditCsv(await exportSecurityAuditEvents(filters))
    } catch (error) {
      setExportError(error instanceof Error ? error.message : String(error))
    } finally {
      setExporting(false)
    }
  }

  function nextPage() {
    const next = events.data?.next_cursor
    if (!next) return
    setCursorHistory((current) => [...current, cursor])
    setCursor(next)
  }

  function previousPage() {
    if (!cursorHistory.length) return
    setCursor(cursorHistory[cursorHistory.length - 1] ?? null)
    setCursorHistory((current) => current.slice(0, -1))
  }

  const items = events.data?.items ?? []

  return (
    <section className="security-audit-page">
      <header className="security-audit-heading">
        <div className="security-audit-title-icon"><ShieldCheck size={19} /></div>
        <div>
          <h1>安全审计</h1>
          <p>查询用户登录、权限拒绝和管理操作的审计元数据。请求正文、模型内容和凭证不会写入此处。</p>
        </div>
      </header>

      <section className="security-audit-filters" aria-label="审计筛选">
        <label><span>开始时间</span><Input type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} /></label>
        <label><span>结束时间</span><Input type="datetime-local" value={to} onChange={(event) => setTo(event.target.value)} /></label>
        <label><span>操作者 ID</span><Input value={actorId} onChange={(event) => setActorId(event.target.value)} placeholder="用户 UUID" /></label>
        <label><span>操作</span><Input value={action} onChange={(event) => setAction(event.target.value)} placeholder="如 user.updated" /></label>
        <label><span>结果</span>
          <Select value={result} onValueChange={(value) => setResult((value ?? RESULT_ALL) as SecurityAuditResult | typeof RESULT_ALL)}>
            <SelectTrigger aria-label="审计结果"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={RESULT_ALL}>全部</SelectItem>
              <SelectItem value="success">成功</SelectItem>
              <SelectItem value="failure">失败</SelectItem>
              <SelectItem value="denied">拒绝</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <div className="security-audit-filter-actions">
          <Button type="button" variant="outline" onClick={applyFilters}>应用筛选</Button>
          <Button type="button" variant="outline" disabled={exporting} onClick={() => void exportCsv()}>
            {exporting ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
            {exporting ? "正在导出" : "导出 CSV"}
          </Button>
        </div>
      </section>

      {exportError && <p className="security-audit-error" role="alert">导出失败：{exportError}</p>}
      {events.isLoading && <div className="security-audit-state"><LoaderCircle className="spin" size={18} />正在加载审计记录</div>}
      {events.isError && <div className="security-audit-state error" role="alert"><AlertTriangle size={18} /><span>审计记录读取失败：{events.error.message}</span></div>}
      {events.isSuccess && items.length === 0 && <div className="security-audit-state"><FileSearch size={19} />当前筛选没有审计记录</div>}
      {items.length > 0 && (
        <div className="security-audit-table-wrap">
          <ResizableTable className="security-audit-table" minWidths={[170, 180, 120, 150, 90, 130, 260]}>
            <thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>资源</th><th>结果</th><th>来源 IP</th><th>详情</th></tr></thead>
            <tbody>{items.map((event) => <AuditRow key={event.id} event={event} />)}</tbody>
          </ResizableTable>
        </div>
      )}
      <footer className="security-audit-pagination">
        <span>{items.length} 条记录{events.data?.next_cursor ? " · 还有下一页" : ""}</span>
        <div>
          <Button type="button" variant="outline" size="sm" disabled={!cursorHistory.length || events.isFetching} onClick={previousPage}>上一页</Button>
          <Button type="button" variant="outline" size="sm" disabled={!events.data?.next_cursor || events.isFetching} onClick={nextPage}>下一页</Button>
        </div>
      </footer>
    </section>
  )
}
