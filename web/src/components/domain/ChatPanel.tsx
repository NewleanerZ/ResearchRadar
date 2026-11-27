import { useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronDown, ChevronRight, ExternalLink, Send, Sparkles, Square } from "lucide-react"
import { apiFetch, SOURCE_LABELS, streamTurnEvents, TurnItem } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/input"
import { ActionCard } from "./ActionCard"
import { MarkdownView } from "./MarkdownView"
import { ResearchCard } from "./ResearchCard"
import { TimelineAccordion } from "./TimelineDrawer"

function ToolEvents({ events }: { events: TurnItem["events"] }) {
  if (!events || events.length === 0) return null
  return (
    <div className="mt-2 space-y-2">
      {events.map((ev, idx) => {
        const { name, result } = ev
        const data = result?.data
        if (["propose_action", "start_research", "cancel_research"].includes(name) && result?.status !== "error") {
          return null
        }
        if (result?.status === "error") {
          return (
            <div key={idx} className="rounded border border-[var(--destructive)]/30 bg-[var(--destructive-soft)] px-2.5 py-1.5 text-[11px] text-[var(--destructive)]">
              {name} 未完成：{typeof data === "string" ? data : JSON.stringify(data)}
            </div>
          )
        }
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

export function ChatPanel({
  conversationId,
  topicId = "",
  initialPrompt = "",
  compact = false,
  onPromptSeedConsumed,
  onConversationCreated,
}: {
  conversationId: string
  topicId?: string
  initialPrompt?: string
  compact?: boolean
  onPromptSeedConsumed?: () => void
  onConversationCreated?: (id: string) => void
}) {
  const queryClient = useQueryClient()
  const [prompt, setPrompt] = useState("")
  const [liveTurn, setLiveTurn] = useState<TurnItem | null>(null)
  const [streamError, setStreamError] = useState("")
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

  const turns = conversationId ? (turnsQuery.data?.turns || []) : []
  const activeTurn = turns.find((t) => t.status === "queued" || t.status === "running")

  useEffect(() => {
    cleanupStreamRef.current?.()
    cleanupStreamRef.current = null
    setLiveTurn(null)
    if (!activeTurn) return

    const stop = streamTurnEvents(
      activeTurn.id,
      (updated, complete) => {
        setLiveTurn(updated)
        if (complete) {
          setLiveTurn(null)
          turnsQuery.refetch()
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
      queryClient.invalidateQueries({ queryKey: ["chat-turns", res.targetId] })
      queryClient.invalidateQueries({ queryKey: ["conversations"] })
    },
    onError: (e: any) => setStreamError(e?.message || "发送失败"),
  })

  const cancelMut = useMutation({
    mutationFn: (turnId: string) =>
      apiFetch(`/api/chat/turns/${turnId}/cancel`, { method: "POST" }),
    onSuccess: () => turnsQuery.refetch(),
  })

  const submit = () => {
    const trimmed = prompt.trim()
    if (!trimmed || activeTurn || sendMut.isPending) return
    sendMut.mutate(trimmed)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {turns.length === 0 && (
          <div className="rounded-md border border-dashed border-[var(--border)] p-4 text-xs text-[var(--muted-foreground)]">
            <div className="mb-1.5 flex items-center gap-1.5 font-medium text-[var(--foreground)]">
              <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />
              科研对话工作区
            </div>
            可以直接查询论文库、触发主题检索、精读某篇论文，或发起带原文页码证据的专题研究。
          </div>
        )}

        {turns.map((turn) => {
          const current = liveTurn && liveTurn.id === turn.id ? liveTurn : turn
          const isRunning = current.status === "queued" || current.status === "running"
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

                <TimelineAccordion events={current.timeline} defaultOpen={isRunning} />
                <ToolEvents events={current.events} />
                {current.actions.map((act) => (
                  <ActionCard
                    key={act.id}
                    conversationId={conversationId}
                    action={act}
                    onConfirmed={() => turnsQuery.refetch()}
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

      <div className="border-t border-[var(--border)] bg-[var(--surface)] p-3">
        {streamError && <div className="mb-1.5 text-[11px] text-[var(--destructive)]">{streamError}</div>}
        <div className="flex items-end gap-2">
          <Textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            placeholder={compact ? "围绕当前上下文提问（Enter 发送）…" : "询问论文、管理主题、或发起专题研究（Enter 发送，Shift+Enter 换行）"}
            className={compact ? "min-h-[44px] max-h-28 resize-none" : "min-h-[56px] max-h-36 resize-none"}
            disabled={Boolean(activeTurn)}
          />
          <Button
            onClick={submit}
            disabled={!prompt.trim() || Boolean(activeTurn) || sendMut.isPending}
            className="h-9 shrink-0 px-3"
          >
            <Send className="h-3.5 w-3.5" />
            发送
          </Button>
        </div>
      </div>
    </div>
  )
}
