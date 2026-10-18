import { useMemo, useState } from "react"
import { Save, X } from "lucide-react"
import type { ChartSpec, PinnedReport } from "./types"
import { Button } from "../ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog"
import { Input } from "../ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
import { Textarea } from "../ui/textarea"

const NEW_REPORT = "__new__"

/** Same identity the server's unique index uses: the tool plus its validated arguments. */
const queryIdentity = (chart: ChartSpec) => JSON.stringify(chart.query)

export function PinDialog({
  chart,
  question,
  reports,
  busy,
  onClose,
  onConfirm,
}: {
  chart: ChartSpec
  question: string
  reports: PinnedReport[]
  busy: boolean
  onClose: () => void
  onConfirm: (
    target: { reportId?: string; title?: string; description?: string },
  ) => void | Promise<void>
}) {
  // Where this chart already lives, if anywhere. Adding it to that report again would
  // land on the unique index and merely refresh the card, so the dialog says so instead
  // of letting the person discover it after saving.
  const alreadyIn = useMemo(() => {
    const identity = queryIdentity(chart)
    return new Set(
      reports
        .filter((report) => report.charts.some((item) => queryIdentity(item.chart) === identity))
        .map((report) => report.id),
    )
  }, [chart, reports])

  const [target, setTarget] = useState(NEW_REPORT)
  const [title, setTitle] = useState(chart.title)
  const [description, setDescription] = useState("")

  const creating = target === NEW_REPORT
  const selected = reports.find((report) => report.id === target && report.can_manage)
  const canSave = busy ? false : creating ? Boolean(title.trim()) : Boolean(selected)

  return <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
    <DialogContent className="registry-editor-dialog assistant-pin-dialog" finalFocus={false}>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          if (!canSave) return
          void onConfirm(creating
            ? { title: title.trim(), description: description.trim() }
            : { reportId: target })
        }}
      >
        <DialogHeader className="registry-editor-header">
          <DialogTitle>固定为报表</DialogTitle>
          <DialogDescription>
            保存到报表中心。
          </DialogDescription>
        </DialogHeader>

        <div className="registry-editor-body assistant-pin-body">
          <label className="registry-field">
            <span className="registry-field-label">固定到</span>
            <Select value={target} onValueChange={(value) => { if (value) setTarget(value) }}>
              <SelectTrigger aria-label="固定到" className="registry-select-trigger">
                <SelectValue>{creating ? "新建报表" : selected?.title ?? "新建报表"}</SelectValue>
              </SelectTrigger>
              <SelectContent align="start" alignItemWithTrigger={false}>
                <SelectGroup>
                <SelectItem value={NEW_REPORT}>新建报表</SelectItem>
                {reports.filter((report) => report.can_manage).map((report) => <SelectItem key={report.id} value={report.id}>
                  <span data-no-localize>{report.title}</span>
                  <span className="assistant-pin-option-meta">
                    {alreadyIn.has(report.id) ? "已包含此图表" : `${report.charts.length} 个图表`}
                  </span>
                </SelectItem>)}
                </SelectGroup>
              </SelectContent>
            </Select>
          </label>

          {creating
            ? <>
              <label className="registry-field">
                <span className="registry-field-label">报表名称</span>
                <Input
                  value={title}
                  maxLength={120}
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
              <label className="registry-field">
                <span className="registry-field-label">描述（可选）</span>
                <Textarea
                  rows={2}
                  maxLength={500}
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
            </>
            // Deliberately no name field here: the report already has one, and letting
            // this rename it would make adding a chart a destructive act.
            : <p className="assistant-pin-note">
              {alreadyIn.has(target)
                ? `「${selected?.title ?? ""}」已包含此图表，保存会用最新数据刷新它。`
                : `将作为第 ${(selected?.charts.length ?? 0) + 1} 个图表加入`
                  + `「${selected?.title ?? ""}」，报表名称保持不变。`}
            </p>}

          {/* Shown rather than hidden: this is what the report will re-run, so the person
              pinning it should be able to see the scope they are saving. */}
          <dl className="assistant-pin-summary">
            <div><dt>原始问题</dt><dd data-no-localize>{question}</dd></div>
            <div><dt>时间范围</dt><dd>{chart.time_range_label}</dd></div>
            <div><dt>数据口径</dt><dd>{chart.basis}</dd></div>
            <div><dt>刷新方式</dt><dd>打开时重新执行原始查询</dd></div>
          </dl>
        </div>

        <DialogClose render={<Button type="button" variant="ghost" size="icon-sm" className="registry-editor-close" />}>
          <X size={16} /><span className="sr-only">关闭</span>
        </DialogClose>
        <DialogFooter className="registry-editor-footer">
          <DialogClose render={<Button type="button" variant="outline" />}>取消</DialogClose>
          <Button type="submit" disabled={!canSave}>
            <Save size={14} />{creating ? "保存报表" : "加入该报表"}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
