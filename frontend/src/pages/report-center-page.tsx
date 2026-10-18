import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, FileSpreadsheet, Loader2, PanelLeft, RefreshCw, Trash2 } from "lucide-react"
import { assistantApi } from "../components/assistant/api"
import { pinnedChartsKey, pinnedChartsQuery } from "../components/assistant/queries"
import type { PinnedReport } from "../components/assistant/types"
import { Alert, AlertTitle } from "../components/ui/alert"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogMedia, AlertDialogTitle,
} from "../components/ui/alert-dialog"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import { Empty, EmptyContent, EmptyTitle } from "../components/ui/empty"
import { Input } from "../components/ui/input"
import { FINOPS_NAVIGATE_EVENT } from "../lib/navigation"
import { reportCenterUrl, reportDirectory, selectedReport } from "../lib/report-directory"
import { useAuth } from "../providers/auth-provider"
import { PinnedReportPage } from "./pinned-report-page"

const reportIdFromUrl = () => new URLSearchParams(window.location.search).get("chart")

export function ReportCenterPage({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const { user } = useAuth()
  const owner = user?.email ?? ""
  const queryClient = useQueryClient()
  const reports = useQuery(pinnedChartsQuery(owner))
  const [requestedId, setRequestedId] = useState(reportIdFromUrl)
  const [search, setSearch] = useState("")
  const [deleting, setDeleting] = useState<PinnedReport | null>(null)
  const directory = useMemo(() => reportDirectory(reports.data ?? []), [reports.data])
  const visible = useMemo(() => reportDirectory(directory, search), [directory, search])
  const selected = selectedReport(directory, requestedId)

  useEffect(() => {
    const syncSelection = () => setRequestedId(reportIdFromUrl())
    window.addEventListener("popstate", syncSelection)
    window.addEventListener(FINOPS_NAVIGATE_EVENT, syncSelection)
    return () => {
      window.removeEventListener("popstate", syncSelection)
      window.removeEventListener(FINOPS_NAVIGATE_EVENT, syncSelection)
    }
  }, [])

  const select = (id: string | null, replace = false) => {
    const url = reportCenterUrl(window.location.href, id)
    if (!replace && url.href === window.location.href) return
    if (replace) window.history.replaceState(null, "", url)
    else window.history.pushState(null, "", url)
    setRequestedId(id)
    window.dispatchEvent(new Event(FINOPS_NAVIGATE_EVENT))
  }

  useEffect(() => {
    if (requestedId === null && selected) select(selected.id, true)
  }, [requestedId, selected?.id])

  const forget = (id: string) => {
    const current = reportDirectory(queryClient.getQueryData<PinnedReport[]>(pinnedChartsKey(owner)) ?? [])
    const remaining = current.filter((report) => report.id !== id)
    const deletingSelected = selectedReport(current, reportIdFromUrl())?.id === id
    queryClient.setQueryData<PinnedReport[]>(pinnedChartsKey(owner), (current) =>
      current?.filter((report) => report.id !== id))
    if (deletingSelected) select(remaining[0]?.id ?? null, true)
  }
  const remove = useMutation({
    mutationFn: (report: PinnedReport) => {
      if (!report.can_manage) throw new Error("Report is read-only")
      return assistantApi.unpin(report.id)
    },
    onSuccess: (_result, report) => {
      forget(report.id)
      setDeleting(null)
    },
  })

  return <div className="finops-workspace report-center-workspace">
    <header className="finops-header">
      <div>
        <Button variant="ghost" size="icon-sm" className="finops-sidebar-trigger"
          aria-label="切换导航栏" title="切换导航栏" onClick={onToggleSidebar}>
          <PanelLeft data-icon="inline-start" />
        </Button>
        <span className="finops-header-icon"><FileSpreadsheet size={17} /></span>
        <h1>报表中心</h1>
      </div>
      <Button variant="ghost" size="icon-sm" className="finops-header-refresh"
        aria-label="刷新报表目录" title="刷新报表目录" disabled={reports.isFetching}
        onClick={() => void reports.refetch()}>
        <RefreshCw data-icon="inline-start" className={reports.isFetching ? "spin" : undefined} />
      </Button>
    </header>
    <div className="report-center-layout">
      <section className="report-directory" aria-label="报表目录">
        <div className="report-directory-head">
          <h2>报表目录</h2>
          <Badge variant="secondary">{directory.length}</Badge>
        </div>
        <Input type="search" aria-label="搜索报表" placeholder="搜索报表"
          value={search} onChange={(event) => setSearch(event.target.value)} />
        <nav className="report-directory-list" aria-label="报表列表">
          {reports.isLoading && <div className="report-directory-loading" role="status">
            <Loader2 size={15} className="spin" /><span>正在加载报表</span>
          </div>}
          {!reports.isLoading && visible.length === 0 && !reports.isError && <Empty>
            <EmptyTitle>{search.trim() ? "没有匹配的报表" : "暂无报表"}</EmptyTitle>
          </Empty>}
          {visible.map((report) => <div className="report-directory-row"
            data-current={selected?.id === report.id || undefined} key={report.id}>
            <button type="button" className="report-directory-select"
              aria-current={selected?.id === report.id ? "page" : undefined}
              title={report.title} data-no-localize onClick={() => select(report.id)}>
              <FileSpreadsheet size={15} />
              <span>{report.title}</span>
              {report.charts.length > 1 && <Badge variant="secondary">{report.charts.length}</Badge>}
            </button>
            {report.can_manage && <Button variant="ghost" size="icon-sm"
              className="report-directory-delete" aria-label="删除报表" title="删除报表"
              disabled={remove.isPending} onClick={() => { remove.reset(); setDeleting(report) }}>
              <Trash2 data-icon="inline-start" />
            </Button>}
          </div>)}
        </nav>
      </section>
      <section className="report-center-detail" aria-label="报表内容">
        {reports.isError && <Alert>
          <AlertTriangle size={16} />
          <AlertTitle>无法加载报表</AlertTitle>
          <Button variant="outline" size="sm" disabled={reports.isFetching}
            onClick={() => void reports.refetch()}><RefreshCw data-icon="inline-start" />重试</Button>
        </Alert>}
        {selected ? <PinnedReportPage key={selected.id} chartId={selected.id}
          embedded onToggleSidebar={onToggleSidebar} onDeleted={forget} />
          : reports.isLoading ? <Empty role="status"><EmptyTitle>正在加载报表</EmptyTitle></Empty>
            : !reports.isError && <Empty>
              <FileSpreadsheet size={24} aria-hidden="true" />
              <EmptyTitle>{requestedId !== null ? "报表不存在或无权访问" : "暂无报表"}</EmptyTitle>
              {requestedId !== null && directory.length > 0 && <EmptyContent>
                <Button variant="outline" size="sm" onClick={() => select(directory[0].id)}>查看报表</Button>
              </EmptyContent>}
            </Empty>}
      </section>
    </div>
    <AlertDialog open={Boolean(deleting)} onOpenChange={(open) => {
      if (!open && !remove.isPending) setDeleting(null)
    }}>
      <AlertDialogContent className="report-delete-dialog" size="sm">
        <AlertDialogHeader>
          <AlertDialogMedia><Trash2 /></AlertDialogMedia>
          <AlertDialogTitle>删除报表</AlertDialogTitle>
          <AlertDialogDescription>
            <span data-no-localize>{deleting?.title}</span>
            <span>删除后无法恢复。</span>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {remove.isError && <Alert><AlertTitle>删除报表失败</AlertTitle></Alert>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={remove.isPending}>取消</AlertDialogCancel>
          <AlertDialogAction variant="destructive" disabled={remove.isPending}
            onClick={() => { if (deleting?.can_manage) remove.mutate(deleting) }}>
            {remove.isPending ? <Loader2 data-icon="inline-start" className="spin" />
              : <Trash2 data-icon="inline-start" />}删除
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
}
