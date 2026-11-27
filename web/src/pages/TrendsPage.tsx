import { useState } from "react"
import { Link } from "react-router-dom"
import { useMutation, useQuery } from "@tanstack/react-query"
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip as RechartsTooltip, XAxis, YAxis,
} from "recharts"
import {
  ArrowRight, BookOpen, ChevronDown, ChevronUp, Download, Flame, Layers, RefreshCw, Sparkles, TrendingUp,
} from "lucide-react"
import { apiFetch } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch } from "@/components/ui/controls"
import { MarkdownView } from "@/components/domain/MarkdownView"

const SERIES_COLORS = ["#1E3A5F", "#2563EB", "#15803D", "#B45309", "#DC2626", "#64748B"]
const SERIES_DASHES = ["", "5 4", "2 2", "8 3 2 3", "4 4", "1 3"]

export function TrendsPage() {
  const { topicId, currentTopic, askCopilot } = useApp()
  const [windowDays, setWindowDays] = useState<7 | 30 | 90 | 180>(30)
  const [expandedClusters, setExpandedClusters] = useState<Record<string, boolean>>({})
  const [periodType, setPeriodType] = useState<"weekly" | "monthly">("weekly")
  const [selectedPeriodKey, setSelectedPeriodKey] = useState("")
  const [includeAiInReport, setIncludeAiInReport] = useState(false)
  const [eventFilter, setEventFilter] = useState<"all" | "code_released" | "venue_accepted" | "version_updated">("all")
  const [notice, setNotice] = useState("")

  const trendsQuery = useQuery({
    queryKey: ["trends", topicId, windowDays],
    queryFn: () =>
      apiFetch<{
        fixed_trends: any[]
        dynamic_clusters: { mode: string; reason?: string; as_of?: string; clusters: any[] }
        live_trends?: Record<string, { total: number; subtopics: string[]; series: any[]; bars: Array<{ label: string; count: number }> }>
        insights?: any
        latest_synthesis?: any
        events?: any[]
        event_labels?: Record<string, string>
        cluster_labels?: Record<string, string>
      }>(`/api/topics/${topicId}/trends?window_days=${windowDays}`),
    enabled: Boolean(topicId),
  })

  const periodQuery = useQuery({
    queryKey: ["period-reports", topicId, periodType],
    queryFn: () => apiFetch<{ reports: any[] }>(`/api/reports?topic_id=${encodeURIComponent(topicId)}&type=${periodType}`),
    enabled: Boolean(topicId),
  })

  const refreshClustersMut = useMutation({
    mutationFn: () => apiFetch(`/api/topics/${topicId}/trends?window_days=${windowDays}&refresh_clusters=true`),
    onSuccess: () => {
      trendsQuery.refetch()
      setNotice("本地语义热点聚类与科研事件已刷新（未消耗大模型 Token）")
    },
  })

  const synthesisMut = useMutation({
    mutationFn: (force: boolean) =>
      apiFetch<{ available: boolean; reason?: string; cached?: boolean }>(`/api/topics/${topicId}/synthesis`, {
        method: "POST",
        body: JSON.stringify({ window_days: windowDays, force }),
      }),
    onSuccess: (res) => {
      if (res?.available) {
        trendsQuery.refetch()
        setNotice(res.cached ? "已加载缓存的 AI 跨文献整理" : "AI 跨文献热点整理已完成并缓存")
      } else {
        setNotice(res?.reason || "暂未能生成 AI 整理总结")
      }
    },
    onError: (e: any) => setNotice(e?.message || "生成失败"),
  })

  const genPeriodMut = useMutation({
    mutationFn: (type: "weekly" | "monthly") =>
      apiFetch<any>("/api/reports/period", {
        method: "POST",
        body: JSON.stringify({ topic_id: topicId, report_type: type, force: true, include_ai: includeAiInReport }),
      }),
    onSuccess: (rep) => {
      periodQuery.refetch()
      trendsQuery.refetch()
      if (rep?.period_key) setSelectedPeriodKey(rep.period_key)
      setNotice(includeAiInReport ? "已生成含 AI 综述的周期报告" : "已生成统计型周期报告（0 Token）")
    },
  })

  if (!topicId) {
    return <div className="p-8 text-xs text-[var(--muted-foreground)]">请先选择或创建研究主题。</div>
  }

  const data = trendsQuery.data
  const insights = data?.insights
  const dynClusters = data?.dynamic_clusters?.clusters || []
  const clusterLabels = data?.cluster_labels || {}
  const eventLabels = data?.event_labels || {}
  const trendEvents = (data?.events || []).filter((ev) => eventFilter === "all" || ev.event_type === eventFilter)
  const synthesisWrap = data?.latest_synthesis
  const synthesis = synthesisWrap?.synthesis

  // Build Recharts dataset from fixed_trends, falling back to live_trends
  const snapshots = data?.fixed_trends || []
  const latestSnap = snapshots[0]
  const completeLabels: string[] =
    latestSnap && windowDays !== 180
      ? Array.from(new Set(latestSnap.windows.filter((w: any) => w.days === windowDays && w.label !== "全部").map((w: any) => w.label)))
      : []
  const completeChartData =
    windowDays !== 180
      ? [...snapshots]
          .reverse()
          .map((snap: any) => {
            const row: Record<string, any> = { as_of: snap.as_of }
            let hasComplete = false
            for (const w of snap.windows || []) {
              if (w.days === windowDays && w.coverage_complete && w.label !== "全部") {
                row[w.label] = w.count
                hasComplete = true
              }
            }
            return hasComplete ? row : null
          })
          .filter(Boolean)
      : []
  const liveWindow = data?.live_trends?.[String(windowDays)]
  const usingLiveFallback = completeChartData.length < 2
  const chartData = usingLiveFallback ? liveWindow?.series || [] : (completeChartData as any[])
  const subLabels = usingLiveFallback ? liveWindow?.subtopics || [] : completeLabels
  const liveBars = liveWindow?.bars || []

  const reports = periodQuery.data?.reports || []
  const activeReport = reports.find((r) => r.period_key === selectedPeriodKey) || reports[0]

  const downloadReport = () => {
    if (!activeReport?.markdown) return
    const blob = new Blob([activeReport.markdown], { type: "text/markdown;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `${topicId}-${activeReport.period_key}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6 space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-[var(--border)] pb-4">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wider text-[var(--muted-foreground)]">
            研究趋势与热点洞察
          </div>
          <h1 className="font-serif-editorial mt-0.5 text-2xl font-semibold">
            {currentTopic?.name || "主题趋势"} · 文献脉络与热点整理
          </h1>
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            先用本地统计与语义聚类看清“大家在做什么”，需要深度梳理时再按需点击生成 AI 综述（默认零被动 Token 消耗）。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--surface)] p-0.5">
            {([7, 30, 90, 180] as const).map((d) => (
              <Button
                key={d}
                size="sm"
                variant={windowDays === d ? "default" : "ghost"}
                onClick={() => setWindowDays(d)}
              >
                近 {d} 天
              </Button>
            ))}
          </div>
          <Button size="sm" variant="outline" disabled={refreshClustersMut.isPending} onClick={() => refreshClustersMut.mutate()}>
            <RefreshCw className={`h-3.5 w-3.5 ${refreshClustersMut.isPending ? "animate-spin" : ""}`} />
            {refreshClustersMut.isPending ? "聚类计算中…" : "刷新本地热点"}
          </Button>
        </div>
      </div>

      {notice && (
        <div className="flex items-center justify-between rounded-md border border-[var(--border)] bg-[var(--surface)] px-3.5 py-2 text-xs">
          <span>{notice}</span>
          <button type="button" onClick={() => setNotice("")} className="text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] cursor-pointer">
            关闭
          </button>
        </div>
      )}

      {/* Layer 1: Local Zero-Token Pulse Overview */}
      <section className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-3.5">
            <div className="text-[11px] text-[var(--muted-foreground)]">近 {windowDays} 天收录文献</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="text-2xl font-semibold tabular-nums">{insights?.total ?? liveWindow?.total ?? 0}</span>
              <span className="text-xs text-[var(--muted-foreground)]">篇</span>
              {insights?.growth_pct !== null && insights?.growth_pct !== undefined && (
                <Badge variant={insights.delta >= 0 ? "success" : "outline"}>
                  {insights.delta >= 0 ? "+" : ""}{insights.growth_pct}% ({insights.delta >= 0 ? "+" : ""}{insights.delta})
                </Badge>
              )}
            </div>
            <div className="mt-1 text-[11px] text-[var(--muted-foreground)]">
              前 {windowDays} 天对照基线：{insights?.prev_total ?? 0} 篇
            </div>
          </div>

          <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-3.5">
            <div className="text-[11px] text-[var(--muted-foreground)]">正式发表 / 顶会成熟度</div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className="text-2xl font-semibold tabular-nums">{insights?.formal_share ?? 0}%</span>
              <span className="text-xs text-[var(--muted-foreground)]">已发表或录用</span>
            </div>
            <div className="mt-1 text-[11px] text-[var(--muted-foreground)]">
              正式 {insights?.formal_count ?? 0} 篇 · 预印本 {insights?.preprint_count ?? 0} 篇
            </div>
          </div>

          <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-3.5">
            <div className="text-[11px] text-[var(--muted-foreground)]">最集中研究方向</div>
            <div className="mt-1 truncate text-sm font-semibold" title={insights?.top_cluster?.label || ""}>
              {insights?.top_cluster?.label || "待聚类识别"}
            </div>
            <div className="mt-1.5 text-[11px] text-[var(--muted-foreground)]">
              {insights?.top_cluster
                ? `${insights.top_cluster.count} 篇（占窗口 ${insights.top_cluster.share}%）`
                : "点击右上角「刷新本地热点」划分方向"}
            </div>
          </div>

          <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-3.5">
            <div className="text-[11px] text-[var(--muted-foreground)]">科研进展信号（近 {windowDays} 天）</div>
            <div className="mt-1 flex items-baseline gap-3">
              <span className="text-sm font-semibold tabular-nums">开源 {insights?.code_released_count ?? 0} 项</span>
              <span className="text-sm font-semibold tabular-nums">录用 {insights?.venue_accepted_count ?? 0} 篇</span>
            </div>
            <div className="mt-1.5 text-[11px] text-[var(--muted-foreground)]">
              高频源：{(insights?.top_venues || []).slice(0, 2).map((v: any) => v.venue).join(" · ") || "arXiv / OpenAlex"}
            </div>
          </div>
        </div>

        {insights?.headline && (
          <div className="rounded-md border-l-4 border-[var(--accent)] bg-[var(--surface)] px-4 py-3 text-xs leading-relaxed">
            <span className="font-semibold text-[var(--foreground)]">本地态势速览：</span>
            <span className="text-[var(--foreground)]">{insights.headline}</span>
          </div>
        )}
      </section>

      {/* Layer 2: Drill-Down Hotspot Sub-Directions */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Flame className="h-4 w-4 text-[var(--accent)]" />
            <h2 className="text-sm font-semibold">当前细分研究热点与代表文献</h2>
            <Badge variant="outline">本地语义划分 · 0 Token</Badge>
          </div>
          {data?.dynamic_clusters?.as_of && (
            <span className="text-[11px] text-[var(--muted-foreground)]">最近聚类快照：{data.dynamic_clusters.as_of}</span>
          )}
        </div>

        {dynClusters.length > 0 ? (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {dynClusters.map((c: any) => {
              const m = c.metrics || {}
              const score = m.heat_score ?? (m.hotspot_index != null ? Math.round(m.hotspot_index * 100) : null)
              const delta = m.count_delta
              const reps = (c.representative_papers || []) as any[]
              const isExpanded = Boolean(expandedClusters[c.cluster_id])
              const visibleReps = isExpanded ? reps : reps.slice(0, 3)
              const pubPct = m.published_share != null ? Math.round(m.published_share * 100) : null

              return (
                <div key={c.cluster_id} className="flex flex-col justify-between rounded-md border border-[var(--border)] bg-[var(--surface)] p-4 space-y-3">
                  <div className="space-y-2">
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <h3 className="text-sm font-semibold leading-snug text-[var(--foreground)]">{c.label}</h3>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--muted-foreground)] tabular-nums">
                          <span>本期 <strong className="text-[var(--foreground)]">{m.count}</strong> 篇（占 {(Number(m.share || 0) * 100).toFixed(0)}%）</span>
                          <span>·</span>
                          <span>
                            {delta !== null && delta !== undefined
                              ? `较上期 ${delta >= 0 ? "+" : ""}${delta} 篇`
                              : "首次基线统计"}
                          </span>
                          {pubPct !== null && (
                            <>
                              <span>·</span>
                              <span>正式发表/录用 {pubPct}%</span>
                            </>
                          )}
                        </div>
                      </div>
                      <Badge variant={c.event_type === "growing" ? "success" : "accent"}>
                        {clusterLabels[c.event_type] || c.event_type}
                      </Badge>
                    </div>

                    {/* Intuitive Heat Meter */}
                    <div className="flex items-center gap-2 text-[11px]">
                      <span className="text-[var(--muted-foreground)] shrink-0">热度指数</span>
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--muted)]">
                        <div
                          className="h-full rounded-full bg-[var(--accent)]"
                          style={{ width: `${Math.min(100, Math.max(8, score ?? 20))}%` }}
                        />
                      </div>
                      <span className="tabular-nums font-medium">{score != null ? `${score}/100` : (m.note || "待积累")}</span>
                    </div>

                    {Array.isArray(m.top_venues) && m.top_venues.length > 0 && (
                      <div className="flex flex-wrap items-center gap-1">
                        {m.top_venues.map((v: string) => (
                          <Badge key={v} variant="outline">{v}</Badge>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Representative Papers List */}
                  {reps.length > 0 && (
                    <div className="space-y-1.5 border-t border-[var(--border)] pt-2.5">
                      <div className="text-[11px] font-medium text-[var(--muted-foreground)]">代表性论文（点击直达精读）</div>
                      {visibleReps.map((rp) => (
                        <div key={rp.paper_id} className="rounded border border-[var(--border)] bg-[var(--background)] px-2.5 py-2 text-xs">
                          <div className="flex items-start justify-between gap-2">
                            <Link
                              to={`/workbench?paper=${rp.paper_id}`}
                              className="font-medium leading-snug text-[var(--foreground)] hover:text-[var(--accent)] hover:underline line-clamp-1"
                            >
                              {rp.title}
                            </Link>
                            <ArrowRight className="mt-0.5 h-3 w-3 shrink-0 text-[var(--muted-foreground)]" />
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] text-[var(--muted-foreground)]">
                            <span>{rp.publication_label}</span>
                            {rp.date && <span>· {rp.date}</span>}
                            {rp.citation_count != null && <span>· 引用 {rp.citation_count}</span>}
                            {rp.has_pdf && <Badge variant="success">可取 PDF</Badge>}
                          </div>
                          {(rp.summary_statement || rp.abstract_snippet) && (
                            <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-[var(--muted-foreground)]">
                              {rp.summary_statement || rp.abstract_snippet}
                            </p>
                          )}
                        </div>
                      ))}

                      <div className="flex items-center justify-between pt-1">
                        {reps.length > 3 ? (
                          <button
                            type="button"
                            onClick={() => setExpandedClusters((prev) => ({ ...prev, [c.cluster_id]: !isExpanded }))}
                            className="inline-flex items-center gap-1 text-[11px] text-[var(--accent)] hover:underline cursor-pointer"
                          >
                            {isExpanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                            {isExpanded ? "收起" : `展开全部 ${reps.length} 篇`}
                          </button>
                        ) : <span />}
                        <button
                          type="button"
                          onClick={() =>
                            askCopilot(`请结合库内最新论文，帮我深入梳理「${c.label}」这个热点方向当前主流做法和尚未解决的问题。`)
                          }
                          className="inline-flex items-center gap-1 text-[11px] text-[var(--accent)] hover:underline cursor-pointer"
                        >
                          <Sparkles className="h-3 w-3" />
                          问 Copilot 该方向
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ) : (
          <div className="rounded-md border border-dashed border-[var(--border)] bg-[var(--surface)] p-6 text-xs text-[var(--muted-foreground)]">
            {data?.dynamic_clusters?.reason || "当前窗口样本较少，点击右上角「刷新本地热点」或补录近 90 天论文后即可生成细分方向卡片。"}
          </div>
        )}
      </section>

      {/* Layer 3: On-Demand AI Literature Synthesis & Period Reports */}
      <section className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] pb-3">
          <div>
            <div className="flex items-center gap-2">
              <Layers className="h-4 w-4 text-[var(--accent)]" />
              <h2 className="text-sm font-semibold">AI 跨文献研究整理总结（按需生成）</h2>
              <Badge variant="outline">仅点击时调用 1 次大模型 · 已自动缓存</Badge>
            </div>
            <p className="mt-0.5 text-[11px] text-[var(--muted-foreground)]">
              综合近 {windowDays} 天内各热点代表论文，提炼研究聚焦的核心问题、主流技术流派对比与下一步选题切入点。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant={synthesis ? "outline" : "default"}
              disabled={synthesisMut.isPending}
              onClick={() => synthesisMut.mutate(Boolean(synthesis))}
            >
              <Sparkles className="h-3.5 w-3.5" />
              {synthesisMut.isPending ? "整理生成中…" : synthesis ? "重新整理总结" : "生成 AI 热点整理"}
            </Button>
          </div>
        </div>

        {synthesis ? (
          <div className="space-y-4 text-xs">
            <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-sm text-[var(--foreground)]">{synthesis.headline}</span>
                {synthesisWrap?.model && <Badge variant="outline">{synthesisWrap.model}</Badge>}
              </div>
              <p className="leading-relaxed text-[var(--foreground)]">{synthesis.overview}</p>
            </div>

            {Array.isArray(synthesis.directions) && synthesis.directions.length > 0 && (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {synthesis.directions.map((dir: any, idx: number) => (
                  <div key={idx} className="rounded-md border border-[var(--border)] bg-[var(--background)] p-3.5 space-y-2">
                    <div className="font-semibold text-[var(--foreground)]">{idx + 1}. {dir.name}</div>
                    <p className="leading-relaxed text-[var(--muted-foreground)]">{dir.summary}</p>
                    {Array.isArray(dir.key_methods) && dir.key_methods.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {dir.key_methods.map((m: string) => <Badge key={m} variant="accent">{m}</Badge>)}
                      </div>
                    )}
                    {dir.challenges && (
                      <div className="rounded border-l-2 border-[var(--warning)] pl-2.5 text-[11px] text-[var(--muted-foreground)]">
                        <span className="font-medium text-[var(--foreground)]">局限与难点：</span>{dir.challenges}
                      </div>
                    )}
                    {Array.isArray(dir.papers) && dir.papers.length > 0 && (
                      <div className="pt-1 flex flex-wrap gap-1.5">
                        {dir.papers.map((p: any) => (
                          <Link
                            key={p.paper_id}
                            to={`/workbench?paper=${p.paper_id}`}
                            className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--surface)] px-2 py-0.5 text-[10px] hover:border-[var(--accent)] hover:text-[var(--accent)]"
                            title={p.title}
                          >
                            <BookOpen className="h-2.5 w-2.5" />
                            <span className="max-w-[200px] truncate">{p.title}</span>
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {synthesis.datasets_and_eval && (
                <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-3">
                  <div className="mb-1 font-semibold">数据集与评测动向</div>
                  <p className="leading-relaxed text-[var(--muted-foreground)]">{synthesis.datasets_and_eval}</p>
                </div>
              )}
              {Array.isArray(synthesis.research_advice) && synthesis.research_advice.length > 0 && (
                <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-3">
                  <div className="mb-1 font-semibold">研究切入建议</div>
                  <ul className="list-disc pl-4 space-y-1 text-[var(--muted-foreground)]">
                    {synthesis.research_advice.map((tip: string, i: number) => <li key={i}>{tip}</li>)}
                  </ul>
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="rounded-md border border-dashed border-[var(--border)] p-5 text-center text-xs text-[var(--muted-foreground)]">
            尚未生成当前窗口的 AI 跨文献综述。确认需要深度梳理各方向方法脉络时，点击右上角「生成 AI 热点整理」即可按需生成并缓存。
          </div>
        )}
      </section>

      {/* Layer 4: Trends Chart + Hot Terms/Datasets + Period Reports + Events */}
      <section className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <div className="lg:col-span-7 rounded-md border border-[var(--border)] bg-[var(--surface)] p-4 space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-[var(--accent)]" />
              <h2 className="text-xs font-semibold">时间演进曲线与高频词条分布</h2>
            </div>
            <span className="text-[11px] text-[var(--muted-foreground)]">
              {currentTopic && Object.keys(currentTopic.subtopics || {}).length === 0 ? "自动提取热点词分流" : "按配置子主题分流"}
            </span>
          </div>

          {chartData.length > 0 && subLabels.length > 0 ? (
            <div className="h-60 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="as_of" tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
                  <YAxis tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
                  <RechartsTooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", fontSize: "12px" }} />
                  <Legend wrapperStyle={{ fontSize: "11px" }} />
                  {subLabels.slice(0, 6).map((label, idx) => (
                    <Line
                      key={label}
                      type="monotone"
                      dataKey={label}
                      stroke={SERIES_COLORS[idx % SERIES_COLORS.length]}
                      strokeDasharray={SERIES_DASHES[idx % SERIES_DASHES.length]}
                      strokeWidth={2}
                      dot={{ r: 2 }}
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="py-10 text-center text-xs text-[var(--muted-foreground)]">近 {windowDays} 天暂无带日期文献，可切换至 90/180 天</div>
          )}

          {liveBars.length > 0 && (
            <div className="h-44 w-full border-t border-[var(--border)] pt-3">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={liveBars} layout="vertical" margin={{ left: 12, right: 12 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis type="number" tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
                  <YAxis type="category" dataKey="label" width={110} tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
                  <RechartsTooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", fontSize: "12px" }} />
                  <Bar dataKey="count" name="篇数" fill="#2563EB" radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        <div className="lg:col-span-5 space-y-4">
          {/* Top Terms & Datasets */}
          <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-4 space-y-3">
            <h3 className="text-xs font-semibold">高频技术词与评测数据集（近 {windowDays} 天）</h3>
            {Array.isArray(insights?.top_terms) && insights.top_terms.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {insights.top_terms.map((t: any) => (
                  <Badge key={t.term} variant="outline">{t.term} · {t.count} 篇 ({t.share}%)</Badge>
                ))}
              </div>
            ) : (
              <div className="text-[11px] text-[var(--muted-foreground)]">暂无高频技术词统计</div>
            )}
            {Array.isArray(insights?.top_datasets) && insights.top_datasets.length > 0 && (
              <div className="border-t border-[var(--border)] pt-2.5 space-y-1.5">
                <div className="text-[11px] font-medium text-[var(--muted-foreground)]">常见基准数据集</div>
                <div className="flex flex-wrap gap-1.5">
                  {insights.top_datasets.map((ds: any) => (
                    <Badge key={ds.name} variant="accent">{ds.name} · {ds.count}</Badge>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Filterable Research Events */}
          <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-4 space-y-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-xs font-semibold">科研动态事件</h3>
              <div className="flex flex-wrap gap-1">
                {([
                  ["all", "全部"],
                  ["code_released", "开源"],
                  ["venue_accepted", "录用"],
                  ["version_updated", "更新"],
                ] as const).map(([val, lbl]) => (
                  <Button
                    key={val}
                    size="sm"
                    variant={eventFilter === val ? "default" : "ghost"}
                    onClick={() => setEventFilter(val)}
                  >
                    {lbl}
                  </Button>
                ))}
              </div>
            </div>
            {trendEvents.length > 0 ? (
              <div className="max-h-72 overflow-y-auto space-y-1.5 pr-1">
                {trendEvents.slice(0, 15).map((ev: any) => (
                  <div key={ev.id} className="rounded border border-[var(--border)] bg-[var(--background)] px-2.5 py-2 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <Badge variant="outline">{eventLabels[ev.event_type] || ev.event_type}</Badge>
                      <span className="text-[10px] tabular-nums text-[var(--muted-foreground)]">{ev.created_at?.slice(0, 10)}</span>
                    </div>
                    {ev.paper_id ? (
                      <Link
                        to={`/workbench?paper=${ev.paper_id}`}
                        className="mt-1 block line-clamp-1 font-medium hover:text-[var(--accent)] hover:underline"
                      >
                        {ev.payload?.title || ev.payload?.label || "查看详情"}
                      </Link>
                    ) : (
                      <div className="mt-1 line-clamp-1 font-medium">{ev.payload?.title || ev.payload?.label || "科研动态"}</div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-[11px] text-[var(--muted-foreground)]">暂无匹配科研事件</div>
            )}
          </div>
        </div>
      </section>

      {/* Weekly / Monthly Period Reports */}
      <section className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant={periodType === "weekly" ? "default" : "outline"} onClick={() => { setPeriodType("weekly"); setSelectedPeriodKey("") }}>
              科研周报
            </Button>
            <Button size="sm" variant={periodType === "monthly" ? "default" : "outline"} onClick={() => { setPeriodType("monthly"); setSelectedPeriodKey("") }}>
              科研月报
            </Button>
            {reports.length > 0 && (
              <Select value={activeReport?.period_key || ""} onValueChange={setSelectedPeriodKey}>
                <SelectTrigger className="w-36">
                  <SelectValue placeholder="选择期数" />
                </SelectTrigger>
                <SelectContent>
                  {reports.map((r) => (
                    <SelectItem key={r.id} value={r.period_key}>{r.period_key}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
              <Switch checked={includeAiInReport} onCheckedChange={setIncludeAiInReport} />
              生成时附带 AI 综述（1 次调用）
            </label>
            <Button size="sm" variant="outline" disabled={genPeriodMut.isPending} onClick={() => genPeriodMut.mutate(periodType)}>
              {genPeriodMut.isPending ? "生成中…" : `生成本期${periodType === "weekly" ? "周报" : "月报"}`}
            </Button>
            {activeReport?.markdown && (
              <Button size="sm" variant="outline" onClick={downloadReport}>
                <Download className="h-3.5 w-3.5" />
                导出
              </Button>
            )}
          </div>
        </div>

        {activeReport?.markdown ? (
          <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4">
            <MarkdownView content={activeReport.markdown} />
          </div>
        ) : (
          <p className="text-xs text-[var(--muted-foreground)]">尚无{periodType === "weekly" ? "周报" : "月报"}，点击右侧按钮生成。</p>
        )}
      </section>
    </div>
  )
}
