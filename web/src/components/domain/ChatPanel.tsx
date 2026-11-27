import { useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Bot, ChevronDown, ChevronRight, Cpu, FileText, Loader2, Send, Sparkles, Square, Zap,
} from "lucide-react"
import { apiFetch, SteerQueueItem, StreamTurnMeta, streamTurnEvents, TurnItem } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/input"
import { ActionCard } from "./ActionCard"
import { ArtifactDrawer } from "./ArtifactDrawer"
import { MarkdownView } from "./MarkdownView"
import { ResearchCard } from "./ResearchCard"
import { TimelineAccordion } from "./TimelineDrawer"

function ToolEvents({
  events,
  onOpenArtifact,
}: {
  events: TurnItem["events"]
  onOpenArtifact: (artifactId: string) => void
}) {
  if (!events || events.length === 0) return null
  return (
    <div className="mt-2 space-y-2">
      {events.map((ev, idx) => {
        const { name, result } = ev
        const data = result?.data
        const artifactId = data?.artifact_id as string | undefined
        if (["propose_action", "start_research", "cancel_research"].includes(name) && result?.status !== "error") {
          return null
        }
        if (name === "_steer") {
          return (
            <div key={idx} className="flex items-center gap-1.5 rounded border border-[var(--accent)]/30 bg-[var(--accent)]/5 px-2.5 py-1 text-[11px]">
              <Zap className="h-3 w-3 text-[var(--accent)] shrink-0" />
              <span className="font-medium">运行中插队指令：</span>
              <span className="text-[var(--muted-foreground)]">{data?.prompt || ev.arguments}</span>
            </div>
          )
        }
        if (result?.status === "error") {
          return (
            <div key={idx} className="rounded border border-[var(--destructive)]/30 bg-[var(--destructive-soft)] px-2.5 py-1.5 text-[11px] text-[var(--destructive)]">
              {name} 未完成：{typeof data === "string" ? data : JSON.stringify(data)}
            </div>
          )
        }
        const artifactFooter = artifactId ? (
          <div className="mt-2 flex items-center justify-between border-t border-[var(--border)]/60 pt-1.5 text-[11px]">
            <span className="text-[var(--muted-foreground)]">完整内容已存盘（{data.artifact_chars || ""} 字符）</span>
            <Button size="sm" variant="outline" className="h-6 px-2 text-[11px]" onClick={() => onOpenArtifact(artifactId)}>
              <FileText className="h-3 w-3" />
              查看完整产物
            </Button>
          </div>
        ) : null

        if (name === "search_library" || name === "search_external_papers" || name === "get_report") {
          const papers = (data?.papers || []) as any[]
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs">
              <div className="mb-1.5 flex items-center justify-between text-[11px] text-[var(--muted-foreground)]">
                <span>
                  {name === "search_library"
                    ? `论文库命中 ${data.total} 篇`
                    : name === "search_external_papers"
                    ? `外部检索命中 ${data.total} 篇`
                    : `日报 ${data.date}`}
                </span>
                {name === "get_report" && (
                  <Link to={`/briefing`} className="text-[var(--accent)] hover:underline">
                    查看简报
                  </Link>
                )}
              </div>
              <ol className="space-y-1.5">
                {papers.map((p, pIdx) => (
                  <li key={pIdx} className="flex items-start justify-between gap-2 border-t border-[var(--border)]/60 pt-1.5 first:border-t-0 first:pt-0">
                    <div>
                      {p.url ? (
                        <a href={p.url} target="_blank" rel="noopener noreferrer" className="font-medium text-[var(--foreground)] hover:text-[var(--accent)] hover:underline">
                          {pIdx + 1}. {p.title}
                        </a>
                      ) : (
                        <span className="font-medium">{pIdx + 1}. {p.title}</span>
                      )}
                      <div className="text-[11px] text-[var(--muted-foreground)]">
                        {[
                          p.topic_name,
                          p.date,
                          p.publication,
                          p.citation_count != null ? `引用 ${p.citation_count}` : null,
                          p.pdf_status === "available" ? "可取 PDF" : p.pdf_status === "unavailable" ? "无公开 PDF" : null,
                          p.source,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
              {artifactFooter}
            </div>
          )
        }
        if (name === "run_subagent" && Array.isArray(data?.results)) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs space-y-2">
              <div className="flex items-center gap-1.5 text-[11px] text-[var(--muted-foreground)]">
                <Bot className="h-3.5 w-3.5 text-[var(--accent)]" />
                <span>并发子代理查证（{data.count} 路）</span>
              </div>
              <div className="space-y-1.5">
                {data.results.map((sub: any, si: number) => (
                  <div key={si} className="rounded border border-[var(--border)]/70 bg-[var(--surface)] p-2">
                    <div className="flex items-center justify-between gap-2 text-[11px]">
                      <span className="font-semibold">{sub.role}</span>
                      <Badge variant={sub.status === "ok" ? "success" : "outline"}>{sub.status}</Badge>
                    </div>
                    {sub.summary && <p className="mt-1 text-[11px] text-[var(--foreground)]">{sub.summary}</p>}
                  </div>
                ))}
              </div>
              {artifactFooter}
            </div>
          )
        }
        if (name === "read_artifact" && data) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2 text-[11px] flex items-center justify-between gap-2">
              <span className="text-[var(--muted-foreground)] truncate">
                已读取产物：{data.title || data.artifact_id}（{data.query ? `匹配 ${data.matched_count} 处` : `偏移 ${data.offset}`}）
              </span>
              {data.artifact_id && (
                <Button size="sm" variant="outline" className="h-6 px-2 text-[11px]" onClick={() => onOpenArtifact(data.artifact_id)}>
                  查看原文
                </Button>
              )}
            </div>
          )
        }
        if (name === "web_search" && data) {
          const results = (data.results || []) as any[]
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs">
              <div className="mb-1.5 flex items-center justify-between text-[11px] text-[var(--muted-foreground)]">
                <span>全网检索（{data.provider === "searxng" ? "SearXNG" : "DuckDuckGo / GitHub / HF"}）· 命中 {data.total || results.length} 条</span>
              </div>
              <ol className="space-y-1.5">
                {results.map((r, ri) => (
                  <li key={ri} className="border-t border-[var(--border)]/60 pt-1.5 first:border-t-0 first:pt-0">
                    <a href={r.url} target="_blank" rel="noopener noreferrer" className="font-medium text-[var(--foreground)] hover:text-[var(--accent)] hover:underline">
                      {ri + 1}. {r.title}
                    </a>
                    <div className="text-[10px] text-[var(--muted-foreground)]">{r.source}{r.updated_at ? ` · ${r.updated_at}` : ""}</div>
                    {r.snippet && <p className="mt-0.5 line-clamp-2 text-[11px] text-[var(--muted-foreground)]">{r.snippet}</p>}
                  </li>
                ))}
              </ol>
              {artifactFooter}
            </div>
          )
        }
        if (name === "read_webpage" && data) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs">
              <div className="flex items-center justify-between gap-2 text-[11px] text-[var(--muted-foreground)]">
                <span className="truncate font-medium text-[var(--foreground)]">{data.title || data.url}</span>
                <span className="shrink-0">{data.extractor} · {data.char_count} 字符</span>
              </div>
              {data.content && <p className="mt-1 line-clamp-4 text-[11px] text-[var(--muted-foreground)] whitespace-pre-wrap">{data.content}</p>}
              {artifactFooter}
            </div>
          )
        }
        if (name === "fetch_paper_summary" && data) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs">
              <div className="text-[11px] text-[var(--muted-foreground)]">
                网页/摘要总结 · 来源：{(data.sources_used || []).join("、") || "摘要"}（未下载 PDF）
              </div>
              {data.title && <div className="mt-1 font-medium">{data.title}</div>}
              {data.abstract && <p className="mt-1 line-clamp-4 text-[11px] text-[var(--muted-foreground)]">{data.abstract}</p>}
            </div>
          )
        }
        if (name === "fetch_paper_pdf" && data) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs space-y-1">
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="font-medium">
                  {data.found ? `探索获取原始 PDF 成功（${data.strategy} · ${data.page_count} 页，未保存原文件）` : data.reason || "未获取到可用 PDF"}
                </span>
                {data.pdf_url && (
                  <a href={data.pdf_url} target="_blank" rel="noopener noreferrer" className="text-[var(--accent)] hover:underline">
                    原链接
                  </a>
                )}
              </div>
              {(data.attempts || []).length > 0 && (
                <div className="text-[10px] text-[var(--muted-foreground)]">
                  已尝试 {data.attempts.length} 条路径：{data.attempts.map((a: any) => `${a.strategy}(${a.status})`).join(" → ")}
                </div>
              )}
              {artifactFooter}
            </div>
          )
        }
        if (name === "rag_search") {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs">
              <div className="mb-1 text-[11px] text-[var(--muted-foreground)]">全文片段命中 {data?.total || 0} 条</div>
              <div className="space-y-2">
                {(data?.chunks || []).map((c: any, ci: number) => (
                  <div key={ci} className="border-t border-[var(--border)]/60 pt-1.5 first:border-t-0 first:pt-0">
                    <div className="flex items-center justify-between gap-2 text-[11px] font-medium">
                      <span className="truncate">{c.title}</span>
                      <span className="shrink-0 text-[var(--muted-foreground)]">
                        {c.section} {c.page_start ? `· P.${c.page_start}` : ""}
                      </span>
                    </div>
                    <p className="mt-0.5 line-clamp-3 text-[11px] text-[var(--muted-foreground)]">{c.text}</p>
                  </div>
                ))}
              </div>
              {artifactFooter}
            </div>
          )
        }
        if (name === "deep_read_paper" && data?.analysis) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs">
              <div className="mb-1 flex items-center gap-2">
                <Badge variant="accent">精读 {data.level || "L3"}</Badge>
              </div>
              <p className="text-[var(--foreground)]">{data.analysis.summary}</p>
              {data.analysis.limitations && (
                <p className="mt-1 text-[11px] text-[var(--muted-foreground)]">局限性：{data.analysis.limitations}</p>
              )}
            </div>
          )
        }
        if (name === "get_paper" && data) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-xs">
              <div className="font-medium">{data.title}</div>
              {data.abstract && <p className="mt-1 line-clamp-4 text-[11px] text-[var(--muted-foreground)]">{data.abstract}</p>}
            </div>
          )
        }
        if (name === "set_paper_state" && data) {
          return (
            <div key={idx} className="text-[11px] text-[var(--success)]">
              已更新《{data.title}》：{data.favorite ? "已收藏" : "未收藏"} · {data.is_read ? "已读" : "未读"}
            </div>
          )
        }
        if (name === "enqueue_daily" && data) {
          return (
            <div key={idx} className="text-[11px] text-[var(--muted-foreground)]">
              {data.reused ? "已复用当天检索任务" : "已提交检索任务"}（`{data.id}`）
            </div>
          )
        }
        if (name === "run_gap_analysis" && data?.markdown) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5">
              <MarkdownView content={data.markdown} />
              {artifactFooter}
            </div>
          )
        }
        if (artifactFooter) {
          return (
            <div key={idx} className="rounded border border-[var(--border)] bg-[var(--background)] p-2">
              {artifactFooter}
            </div>
          )
        }
        return null
      })}
    </div>
  )
}

function ReasoningBlock({ text, defaultOpen = false }: { text: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="mb-2 rounded border border-[var(--border)] bg-[var(--background)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-2.5 py-1 text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] cursor-pointer"
      >
        <span>思考过程</span>
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
      </button>
      {open && (
        <pre className="whitespace-pre-wrap border-t border-[var(--border)] px-2.5 py-2 font-mono text-[11px] text-[var(--muted-foreground)]">
          {text}
        </pre>
      )}
    </div>
  )
}

function LiveProgressStrip({ meta, usage }: { meta: StreamTurnMeta | null; usage?: TurnItem["usage"] }) {
  const runningTools = Object.values(meta?.activeTools || {}).filter((t) => t.status === "running")
  const subagents = Object.values(meta?.subagents || {})
  const u = meta?.liveUsage || usage
  const hasUsage = u && (u.calls !== undefined || u.tokens !== undefined)
  if (runningTools.length === 0 && subagents.length === 0 && !hasUsage) return null

  return (
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2 rounded border border-[var(--border)] bg-[var(--background)] px-2.5 py-1.5 text-[11px]">
      <div className="flex flex-wrap items-center gap-1.5">
        {runningTools.map((t) => (
          <Badge key={t.call_id} variant="accent" className="gap-1">
            <Loader2 className="h-3 w-3 animate-spin" />
            {t.name}
          </Badge>
        ))}
        {subagents.map((s) => (
          <Badge key={s.subagent_id} variant={s.stage === "ok" ? "success" : "outline"} className="gap-1">
            <Bot className="h-3 w-3" />
            {s.role} · {s.stage}
          </Badge>
        ))}
      </div>
      {hasUsage && (
        <span className="flex items-center gap-1 text-[10px] text-[var(--muted-foreground)] tabular-nums">
          <Cpu className="h-3 w-3" />
          调用 {u.calls ?? 0}{meta?.liveUsage?.max_calls ? `/${meta.liveUsage.max_calls}` : ""}
          {u.tokens ? ` · ${u.tokens} tok` : ""}
          {typeof u.cost_usd === "number" && u.cost_usd > 0 ? ` · $${u.cost_usd.toFixed(4)}` : ""}
        </span>
      )}
    </div>
  )
}

export function ChatPanel({
  conversationId,
  topicId = "",
  initialPrompt = "",
  compact = false,
  onPromptSeedConsumed,
  onConversationCreated,
  onNewConversation,
}: {
  conversationId: string
  topicId?: string
  initialPrompt?: string
  compact?: boolean
  onPromptSeedConsumed?: () => void
  onConversationCreated?: (id: string) => void
  onNewConversation?: () => void
}) {
  const queryClient = useQueryClient()
  const [prompt, setPrompt] = useState("")
  const [liveTurn, setLiveTurn] = useState<TurnItem | null>(null)
  const [streamMeta, setStreamMeta] = useState<StreamTurnMeta | null>(null)
  const [streamError, setStreamError] = useState("")
  const [notice, setNotice] = useState("")
  const [openArtifactId, setOpenArtifactId] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const cleanupStreamRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    if (initialPrompt) {
      setPrompt(initialPrompt)
      onPromptSeedConsumed?.()
    }
  }, [initialPrompt, onPromptSeedConsumed])

  const turnsQuery = useQuery({
    queryKey: ["chat-turns", conversationId],
    queryFn: () => apiFetch<{ turns: TurnItem[] }>(`/api/conversations/${conversationId}/turns`),
    enabled: Boolean(conversationId),
  })

  const steerQueueQuery = useQuery({
    queryKey: ["steer-queue", conversationId],
    queryFn: () => apiFetch<{ items: SteerQueueItem[] }>(`/api/conversations/${conversationId}/steer-queue`),
    enabled: Boolean(conversationId),
  })

  const turns = conversationId ? (turnsQuery.data?.turns || []) : []
  const activeTurn = turns.find((t) => t.status === "queued" || t.status === "running")

  useEffect(() => {
    cleanupStreamRef.current?.()
    cleanupStreamRef.current = null
    setLiveTurn(null)
    setStreamMeta(null)
    if (!activeTurn) return

    const stop = streamTurnEvents(
      activeTurn.id,
      (updated, complete, meta) => {
        setLiveTurn(updated)
        setStreamMeta(meta)
        if (complete) {
          setLiveTurn(null)
          setStreamMeta(null)
          turnsQuery.refetch()
          steerQueueQuery.refetch()
          queryClient.invalidateQueries({ queryKey: ["conversations"] })
        }
      },
      (msg) => {
        setStreamError(msg)
        turnsQuery.refetch()
      }
    )
    cleanupStreamRef.current = stop
    return () => stop()
  }, [activeTurn?.id])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [turns.length, liveTurn?.partial_answer])

  const sendMut = useMutation({
    mutationFn: async (text: string) => {
      let targetId = conversationId
      const isNew = !targetId
      if (isNew) {
        const created = await apiFetch<{ id: string }>("/api/conversations", {
          method: "POST",
          body: JSON.stringify({ topic_id: topicId || null }),
        })
        targetId = created.id
      }
      const queued = await apiFetch<{ turn_id: string }>("/api/chat/turns", {
        method: "POST",
        body: JSON.stringify({ conversation_id: targetId, prompt: text }),
      })
      if (isNew) {
        await queryClient.refetchQueries({ queryKey: ["conversations"] })
        onConversationCreated?.(targetId)
      }
      return { ...queued, targetId }
    },
    onSuccess: (res) => {
      setPrompt("")
      setStreamError("")
      setNotice("")
      queryClient.invalidateQueries({ queryKey: ["chat-turns", res.targetId] })
      queryClient.invalidateQueries({ queryKey: ["conversations"] })
    },
    onError: (e: any) => setStreamError(e?.message || "发送失败"),
  })

  const steerMut = useMutation({
    mutationFn: ({ turnId, text, mode }: { turnId: string; text: string; mode: "steer" | "follow_up" }) =>
      apiFetch(`/api/chat/turns/${turnId}/steer`, {
        method: "POST",
        body: JSON.stringify({ prompt: text, mode }),
      }),
    onSuccess: (_, vars) => {
      setPrompt("")
      setStreamError("")
      setNotice(vars.mode === "steer" ? "已发送运行中插队指令，将在当前工具步骤后注入。" : "已排入下一轮执行队列。")
      steerQueueQuery.refetch()
      turnsQuery.refetch()
    },
    onError: (e: any) => setStreamError(e?.message || "发送引导失败"),
  })

  const compactMut = useMutation({
    mutationFn: () => apiFetch<{ compacted: boolean; compacted_count: number }>(`/api/conversations/${conversationId}/compact`, { method: "POST" }),
    onSuccess: (res) => {
      setPrompt("")
      setNotice(res.compacted ? `已完成上下文压缩（累计压缩 ${res.compacted_count} 轮）。` : "当前历史较短，无需压缩。")
    },
    onError: (e: any) => setStreamError(e?.message || "压缩失败"),
  })

  const cancelMut = useMutation({
    mutationFn: (turnId: string) =>
      apiFetch(`/api/chat/turns/${turnId}/cancel`, { method: "POST" }),
    onSuccess: () => turnsQuery.refetch(),
  })

  const submit = (mode: "auto" | "steer" | "follow_up" = "auto") => {
    const trimmed = prompt.trim()
    if (!trimmed || sendMut.isPending || steerMut.isPending) return
    if (trimmed === "/compact" && conversationId && !activeTurn) {
      compactMut.mutate()
      return
    }
    if (trimmed === "/clear" && !activeTurn) {
      setPrompt("")
      onNewConversation?.()
      return
    }
    if (activeTurn) {
      steerMut.mutate({ turnId: activeTurn.id, text: trimmed, mode: mode === "follow_up" ? "follow_up" : "steer" })
      return
    }
    sendMut.mutate(trimmed)
  }

  const queuedFollowups = steerQueueQuery.data?.items || []

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {turns.length === 0 && (
          <div className="rounded-md border border-dashed border-[var(--border)] p-4 text-xs text-[var(--muted-foreground)]">
            <div className="mb-1.5 flex items-center gap-1.5 font-medium text-[var(--foreground)]">
              <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />
              科研对话工作区
            </div>
            可以直接查询论文库、触发主题检索、并发子代理比对多篇论文，或发起带原文页码证据的专题研究。支持 `/compact` 压缩历史、`/clear` 新建对话。
          </div>
        )}

        {turns.map((turn) => {
          const current = liveTurn && liveTurn.id === turn.id ? liveTurn : turn
          const isRunning = current.status === "queued" || current.status === "running"
          const isAwaiting = current.status === "awaiting_confirmation"
          const isCallback = current.prompt === "[后台专题完成回传]"

          return (
            <div key={current.id} className="space-y-2.5">
              {!isCallback ? (
                <div className="flex justify-end">
                  <div className="max-w-[85%] rounded-md border border-[var(--border)] bg-[var(--muted)] px-3 py-2 text-xs text-[var(--foreground)]">
                    {current.prompt}
                  </div>
                </div>
              ) : (
                <Badge variant="success">后台专题研究完成回传</Badge>
              )}

              <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-3 text-xs">
                {isRunning && <LiveProgressStrip meta={streamMeta} usage={current.usage} />}
                {isAwaiting && (
                  <div className="mb-2 flex items-center gap-2">
                    <Badge variant="warning">等待确认后继续</Badge>
                    <span className="text-[11px] text-[var(--muted-foreground)]">确认或取消下方操作后，助手将继续完成后续步骤。</span>
                  </div>
                )}
                {current.reasoning && <ReasoningBlock text={current.reasoning} defaultOpen={isRunning} />}

                {current.answer ? (
                  <MarkdownView content={current.answer} />
                ) : isRunning ? (
                  <div className="space-y-2">
                    {current.partial_answer ? (
                      <MarkdownView content={current.partial_answer + " ▌"} />
                    ) : (
                      <span className="text-[var(--muted-foreground)]">正在分析与调用工具…</span>
                    )}
                    <div className="flex items-center gap-2">
                      <Button size="sm" variant="outline" onClick={() => cancelMut.mutate(current.id)}>
                        <Square className="h-3 w-3" />
                        停止生成
                      </Button>
                    </div>
                  </div>
                ) : null}

                {!isRunning && current.usage?.calls ? (
                  <div className="mt-1.5 text-[10px] text-[var(--muted-foreground)] tabular-nums">
                    本轮模型调用 {current.usage.calls} 次
                    {current.usage.tokens ? ` · ${current.usage.tokens} tokens` : ""}
                    {typeof current.usage.cost_usd === "number" && current.usage.cost_usd > 0
                      ? ` · $${current.usage.cost_usd.toFixed(4)}`
                      : ""}
                  </div>
                ) : null}

                <TimelineAccordion events={current.timeline} defaultOpen={isRunning} />
                <ToolEvents events={current.events} onOpenArtifact={(aid) => setOpenArtifactId(aid)} />
                {current.actions.map((act) => (
                  <ActionCard
                    key={act.id}
                    conversationId={conversationId}
                    action={act}
                    onConfirmed={() => {
                      turnsQuery.refetch()
                      queryClient.invalidateQueries({ queryKey: ["conversations"] })
                    }}
                  />
                ))}
                {current.research_jobs.map((job) => (
                  <ResearchCard key={job.id} job={job} onChanged={() => turnsQuery.refetch()} />
                ))}
              </div>
            </div>
          )
        })}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-[var(--border)] bg-[var(--surface)] p-3 space-y-2">
        {queuedFollowups.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
            <span className="text-[var(--muted-foreground)]">排队指令：</span>
            {queuedFollowups.map((item) => (
              <Badge key={item.id} variant="outline">
                {item.mode === "steer" ? "插队" : "下轮"} · {item.prompt.slice(0, 28)}
              </Badge>
            ))}
          </div>
        )}
        {notice && <div className="text-[11px] text-[var(--accent)]">{notice}</div>}
        {streamError && <div className="text-[11px] text-[var(--destructive)]">{streamError}</div>}
        <div className="flex items-end gap-2">
          <Textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault()
                submit(activeTurn ? "steer" : "auto")
              }
            }}
            placeholder={
              activeTurn
                ? "运行中可输入插队引导（Enter 立即注入当前循环，或点击排入下轮）…"
                : compact
                ? "围绕当前上下文提问（Enter 发送，支持 /compact）…"
                : "询问论文、并发子代理比对、或发起专题研究（Enter 发送，Shift+Enter 换行）"
            }
            className={compact ? "min-h-[44px] max-h-28 resize-none" : "min-h-[56px] max-h-36 resize-none"}
          />
          {activeTurn ? (
            <div className="flex shrink-0 items-center gap-1.5">
              <Button
                size="sm"
                onClick={() => submit("steer")}
                disabled={!prompt.trim() || steerMut.isPending}
                className="h-9 px-2.5"
                title="立即注入当前推理步骤"
              >
                <Zap className="h-3.5 w-3.5" />
                插队
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => submit("follow_up")}
                disabled={!prompt.trim() || steerMut.isPending}
                className="h-9 px-2.5"
                title="等待本轮完成后自动执行"
              >
                下轮
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => cancelMut.mutate(activeTurn.id)}
                className="h-9 px-2.5"
                title="停止当前生成"
              >
                <Square className="h-3.5 w-3.5" />
              </Button>
            </div>
          ) : (
            <Button
              onClick={() => submit("auto")}
              disabled={!prompt.trim() || sendMut.isPending || compactMut.isPending}
              className="h-9 shrink-0 px-3"
            >
              <Send className="h-3.5 w-3.5" />
              发送
            </Button>
          )}
        </div>
      </div>

      <ArtifactDrawer
        conversationId={conversationId}
        artifactId={openArtifactId}
        onClose={() => setOpenArtifactId(null)}
      />
    </div>
  )
}
