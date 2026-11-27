import { useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { useMutation, useQuery } from "@tanstack/react-query"
import { ArrowUpRight, Download, Play, Plus, Sparkles } from "lucide-react"
import { apiFetch, RUN_LABELS, SOURCE_LABELS } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/controls"
import { MarkdownView } from "@/components/domain/MarkdownView"

export function BriefingPage() {
  const { activeTopics, topicId, refetchTopics, askCopilot } = useApp()
  const navigate = useNavigate()
  const [selectedDay, setSelectedDay] = useState("")
  const [showMarkdown, setShowMarkdown] = useState(false)

  const overviewQuery = useQuery({
    queryKey: ["overview", topicId],
    queryFn: () => apiFetch<any>(`/api/topics/${topicId}/overview`),
    enabled: Boolean(topicId),
  })

  const reportsQuery = useQuery({
    queryKey: ["reports-daily", topicId],
    queryFn: () => apiFetch<{ reports: any[] }>(`/api/reports?topic_id=${encodeURIComponent(topicId)}&type=daily`),
    enabled: Boolean(topicId),
  })

  const seedMut = useMutation({
    mutationFn: () => apiFetch("/api/topics/sample", { method: "POST" }),
    onSuccess: () => refetchTopics(),
  })

  const runMut = useMutation({
    mutationFn: () => apiFetch(`/api/topics/${topicId}/runs`, { method: "POST" }),
    onSuccess: () => overviewQuery.refetch(),
  })

  if (activeTopics.length === 0) {
    return (
      <div className="mx-auto max-w-2xl px-6 py-16">
        <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6">
          <h1 className="font-serif-editorial text-2xl font-semibold">欢迎使用 ResearchRadar</h1>
          <p className="mt-2 text-xs text-[var(--muted-foreground)]">
            添加一个研究主题即可开启每日论文追踪、自动中文卡片提炼与科研热点事件监测。
          </p>
          <div className="mt-4 flex items-center gap-2">
            <Button onClick={() => navigate("/settings?tab=topics")}>
              <Plus className="h-3.5 w-3.5" />
              新建研究主题
            </Button>
            <Button variant="outline" disabled={seedMut.isPending} onClick={() => seedMut.mutate()}>
              载入示例主题
            </Button>
          </div>
        </div>
      </div>
    )
  }

  const data = overviewQuery.data
  const latestRun = data?.latest_run
  const reports = reportsQuery.data?.reports || []
  const pickedReport = reports.find((r) => r.local_day === selectedDay) || reports[0]
  const papers = (pickedReport?.result?.recommendations || data?.latest_report?.papers || []) as any[]
  const events = (data?.events || []) as any[]
  const eventLabels = (data?.event_labels || {}) as Record<string, string>
  const activeJobs = (data?.active_jobs || []) as any[]

  const downloadMd = () => {
    if (!pickedReport?.markdown) return
    const blob = new Blob([pickedReport.markdown], { type: "text/markdown;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `${topicId}-${pickedReport.local_day}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6 space-y-6">
      {/* Header Row */}
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wider text-[var(--muted-foreground)]">
            今日简报
          </div>
          <h1 className="font-serif-editorial mt-0.5 text-2xl font-semibold tracking-tight">
            {data?.topic?.name || "加载中…"}
          </h1>
          {data?.topic?.description && (
            <p className="mt-1 max-w-2xl text-xs text-[var(--muted-foreground)]">{data.topic.description}</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {reports.length > 0 && (
            <Select value={pickedReport?.local_day || ""} onValueChange={setSelectedDay}>
              <SelectTrigger className="w-36">
                <SelectValue placeholder="日报日期" />
              </SelectTrigger>
              <SelectContent>
                {reports.map((r) => (
                  <SelectItem key={r.id} value={r.local_day}>
                    {r.local_day}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {pickedReport?.markdown && (
            <>
              <Button size="sm" variant="outline" onClick={() => setShowMarkdown((v) => !v)}>
                {showMarkdown ? "卡片视图" : "Markdown 原文"}
              </Button>
              <Button size="sm" variant="outline" onClick={downloadMd}>
                <Download className="h-3.5 w-3.5" />
                导出
              </Button>
            </>
          )}
          <Button size="sm" disabled={runMut.isPending} onClick={() => runMut.mutate()}>
            <Play className="h-3 w-3" />
            立即检索
          </Button>
        </div>
      </div>

      {/* Compact Metric Strip */}
      <div className="grid grid-cols-3 divide-x divide-[var(--border)] rounded-md border border-[var(--border)] bg-[var(--surface)]">
        <div className="px-4 py-3">
          <div className="text-[11px] text-[var(--muted-foreground)]">最近一次检索</div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {latestRun ? RUN_LABELS[latestRun.status] || latestRun.status : "尚未运行"}
          </div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[11px] text-[var(--muted-foreground)]">当前查看日报</div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {pickedReport?.local_day || "待生成"}
          </div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[11px] text-[var(--muted-foreground)]">重点推荐论文</div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">{papers.length} 篇</div>
        </div>
      </div>

      {/* 7:3 Split Main Grid */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-10">
        <section className="lg:col-span-7 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
              重点论文推荐
            </h2>
            <Link to="/workbench" className="text-xs text-[var(--accent)] hover:underline">
              进入文献工作台 →
            </Link>
          </div>

          {showMarkdown && pickedReport?.markdown ? (
            <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-5">
              <MarkdownView content={pickedReport.markdown} />
            </div>
          ) : papers.length > 0 ? (
            <div className="divide-y divide-[var(--border)] rounded-md border border-[var(--border)] bg-[var(--surface)]">
              {papers.map((paper, idx) => {
                const links = paper.links || []
                const analysis = paper.card?.analysis || {}
                const summary = analysis.summary?.statement || ""
                const inference = analysis.inference || ""
                const rel = paper.components?.relevance
                return (
                  <article key={paper.paper_id ?? idx} className="p-4 hover:bg-[var(--muted)]/30 transition-colors">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <Link
                          to={`/workbench?paper=${paper.paper_id}`}
                          className="font-serif-editorial text-base font-semibold leading-snug text-[var(--foreground)] hover:text-[var(--accent)] hover:underline"
                        >
                          {paper.title}
                        </Link>
                        <div className="mt-1 text-[11px] text-[var(--muted-foreground)]">
                          {(paper.authors || []).slice(0, 5).join("、")}
                          {paper.published ? ` · ${paper.published}` : ""}
                        </div>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-1">
                        <Badge variant={paper.status === "published" ? "success" : paper.status === "accepted" ? "accent" : "outline"}>
                          {paper.venue || paper.status}
                        </Badge>
                        {rel !== undefined && rel !== null && (
                          <span className="text-[11px] tabular-nums text-[var(--muted-foreground)]">
                            相关性 {Math.round(rel * 100)}%
                          </span>
                        )}
                      </div>
                    </div>

                    {summary && <p className="mt-2 text-xs leading-relaxed text-[var(--foreground)]">{summary}</p>}
                    {inference && (
                      <div className="mt-2 border-l-2 border-[var(--primary)] pl-2.5 text-[11px] text-[var(--muted-foreground)]">
                        {inference}
                      </div>
                    )}

                    <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap gap-1.5">
                        {links.slice(0, 4).map((lk: any, li: number) => (
                          <a
                            key={li}
                            href={lk.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-1.5 py-0.5 text-[11px] text-[var(--muted-foreground)] hover:text-[var(--accent)]"
                          >
                            {SOURCE_LABELS[lk.source] || lk.source}
                            <ArrowUpRight className="h-2.5 w-2.5" />
                          </a>
                        ))}
                      </div>
                      <button
                        type="button"
                        onClick={() => askCopilot(`请结合我当前的主题背景，解读论文《${paper.title}》的核心方法与适用边界。`)}
                        className="inline-flex items-center gap-1 text-[11px] text-[var(--accent)] hover:underline cursor-pointer"
                      >
                        <Sparkles className="h-3 w-3" />
                        向 Copilot 提问
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
          ) : (
            <div className="rounded-md border border-dashed border-[var(--border)] p-8 text-center text-xs text-[var(--muted-foreground)]">
              暂无重点论文日报。点击右上角「立即检索」生成首份推荐。
            </div>
          )}
        </section>

        <aside className="lg:col-span-3 space-y-4">
          <div>
            <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
              科研事件提醒
            </h2>
            {events.length > 0 ? (
              <div className="divide-y divide-[var(--border)] rounded-md border border-[var(--border)] bg-[var(--surface)]">
                {events.slice(0, 8).map((ev) => (
                  <div key={ev.id} className="p-3 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <Badge variant="accent">{eventLabels[ev.event_type] || ev.event_type}</Badge>
                      <span className="text-[10px] tabular-nums text-[var(--muted-foreground)]">
                        {ev.created_at?.slice(0, 10)}
                      </span>
                    </div>
                    <div className="mt-1.5 font-medium text-[var(--foreground)]">
                      {ev.payload?.title || ev.payload?.label || "新动态"}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-3 text-xs text-[var(--muted-foreground)]">
                暂无新科研事件（代码开源、顶会录用、热点升温会自动汇总在此）。
              </div>
            )}
          </div>

          {activeJobs.length > 0 && (
            <div>
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
                进行中的专题研究
              </h2>
              <div className="space-y-2">
                {activeJobs.map((j) => (
                  <Link
                    key={j.id}
                    to="/research"
                    className="block rounded-md border border-[var(--border)] bg-[var(--surface)] p-3 text-xs hover:border-[var(--accent)]"
                  >
                    <div className="flex items-center justify-between">
                      <Badge variant="accent">{RUN_LABELS[j.status] || j.status}</Badge>
                      <span className="text-[11px] text-[var(--muted-foreground)]">候选 {j.candidate_count}</span>
                    </div>
                    <p className="mt-1.5 line-clamp-2 font-medium">{j.question}</p>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
