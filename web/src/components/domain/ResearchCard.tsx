import { useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { Download, SlidersHorizontal, Square } from "lucide-react"
import { apiFetch, ResearchJobItem, RUN_LABELS, STAGE_LABELS } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { MarkdownView } from "./MarkdownView"
import { TimelineAccordion } from "./TimelineDrawer"

export function ResearchCard({
  job,
  onChanged,
}: {
  job: ResearchJobItem
  onChanged: () => void
}) {
  const [showAdjust, setShowAdjust] = useState(false)
  const [focus, setFocus] = useState("")
  const [venues, setVenues] = useState("")
  const [paper, setPaper] = useState("")
  const [err, setErr] = useState("")

  const cancelMut = useMutation({
    mutationFn: () =>
      apiFetch(`/api/research/jobs/${job.id}/cancel`, {
        method: "POST",
        body: JSON.stringify({ conversation_id: job.conversation_id || "" }),
      }),
    onSuccess: () => onChanged(),
  })

  const adjustMut = useMutation({
    mutationFn: () =>
      apiFetch(`/api/research/jobs/${job.id}/adjust`, {
        method: "POST",
        body: JSON.stringify({
          conversation_id: job.conversation_id || "",
          focus: focus || null,
          venues: venues.trim() ? venues.replace(/，/g, ",").split(",").map((s) => s.trim()).filter(Boolean) : null,
          paper: paper || null,
        }),
      }),
    onSuccess: () => {
      setFocus("")
      setVenues("")
      setPaper("")
      setShowAdjust(false)
      setErr("")
      onChanged()
    },
    onError: (e: any) => setErr(e?.message || "调整失败"),
  })

  const active = job.status === "queued" || job.status === "running"
  const downloadMd = () => {
    if (!job.markdown) return
    const blob = new Blob([job.markdown], { type: "text/markdown;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `research-${job.id.slice(0, 10)}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="mt-2 rounded-md border border-[var(--border)] bg-[var(--surface)] p-3 text-xs">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="font-semibold text-[var(--foreground)]">{job.question}</div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-[var(--muted-foreground)] tabular-nums">
            <span>{job.after} 至 {job.before}</span>
            <span>·</span>
            <span>{STAGE_LABELS[job.stage] || job.stage}</span>
            <span>·</span>
            <span>候选 {job.candidate_count} 篇</span>
            <span>·</span>
            <span>全文 {job.pdf_count} 篇</span>
          </div>
        </div>
        <Badge variant={job.status === "succeeded" ? "success" : active ? "accent" : "outline"}>
          {RUN_LABELS[job.status] || job.status}
        </Badge>
      </div>

      {active && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          {!job.cancel_requested ? (
            <>
              <Button size="sm" variant="outline" onClick={() => setShowAdjust((v) => !v)}>
                <SlidersHorizontal className="h-3.5 w-3.5" />
                调整专题方向
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={cancelMut.isPending}
                onClick={() => cancelMut.mutate()}
              >
                <Square className="h-3 w-3" />
                取消专题
              </Button>
            </>
          ) : (
            <span className="text-[11px] text-[var(--warning)]">正在停止，当前步骤结束后生效…</span>
          )}
        </div>
      )}

      {showAdjust && active && (
        <div className="mt-2.5 space-y-2 rounded border border-[var(--border)] bg-[var(--background)] p-2.5">
          <Input placeholder="优先查证的问题（可选）" value={focus} onChange={(e) => setFocus(e.target.value)} />
          <Input placeholder="会议范围，逗号分隔（如 ICML, NeurIPS）" value={venues} onChange={(e) => setVenues(e.target.value)} />
          <Input placeholder="优先读取的 DOI 或 arXiv ID" value={paper} onChange={(e) => setPaper(e.target.value)} />
          {err && <div className="text-[var(--destructive)]">{err}</div>}
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setShowAdjust(false)}>收起</Button>
            <Button size="sm" disabled={adjustMut.isPending} onClick={() => adjustMut.mutate()}>提交调整</Button>
          </div>
        </div>
      )}

      <TimelineAccordion events={job.timeline || []} defaultOpen={active} />

      {job.markdown && (
        <div className="mt-3 border-t border-[var(--border)] pt-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="font-semibold">专题研究报告</span>
            <Button size="sm" variant="outline" onClick={downloadMd}>
              <Download className="h-3.5 w-3.5" />
              下载 Markdown
            </Button>
          </div>
          <MarkdownView content={job.markdown} />
        </div>
      )}
    </div>
  )
}
