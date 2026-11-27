import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useMutation, useQuery } from "@tanstack/react-query"
import {
  BookOpen, CalendarPlus, Check, ExternalLink, FileCheck2, FileSearch, Languages, RefreshCw, Sparkles, Star, ThumbsDown, ThumbsUp, UserCheck,
} from "lucide-react"
import { apiFetch, PaperDetail, PaperListItem, SOURCE_LABELS } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger, Switch } from "@/components/ui/controls"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

export function WorkbenchPage() {
  const { topicId, currentTopic, askCopilot } = useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const [query, setQuery] = useState("")
  const [ragMode, setRagMode] = useState(false)
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [pdfAvailableOnly, setPdfAvailableOnly] = useState(false)
  const [fromDate, setFromDate] = useState("")
  const [toDate, setToDate] = useState("")
  const [selectedSources, setSelectedSources] = useState<string[]>([])
  const [page, setPage] = useState(0)
  const [activeTab, setActiveTab] = useState("card")
  const [pdfPage, setPdfPage] = useState<number | null>(null)
  const [backfillOpen, setBackfillOpen] = useState(false)
  const [bfStart, setBfStart] = useState(() => {
    const d = new Date()
    d.setDate(d.getDate() - 29)
    return d.toISOString().slice(0, 10)
  })
  const [bfEnd, setBfEnd] = useState(() => new Date().toISOString().slice(0, 10))
  const [notice, setNotice] = useState("")
  const [pdfNonce, setPdfNonce] = useState(0)
  const [abstractNoteErr, setAbstractNoteErr] = useState("")

  const limit = 25
  const selectedPaperId = Number(searchParams.get("paper") || 0)

  const papersQuery = useQuery({
    queryKey: ["papers", topicId, query, fromDate, toDate, favoriteOnly, unreadOnly, pdfAvailableOnly, selectedSources.join(","), page],
    queryFn: () => {
      const sp = new URLSearchParams({
        topic_id: topicId,
        q: query,
        favorite: String(favoriteOnly),
        unread: String(unreadOnly),
        pdf_available: String(pdfAvailableOnly),
        limit: String(limit),
        offset: String(page * limit),
      })
      if (fromDate) sp.set("from", fromDate)
      if (toDate) sp.set("to", toDate)
      if (selectedSources.length > 0) sp.set("source", selectedSources.join(","))
      return apiFetch<{
        total: number
        pdf_counts?: { available: number; unavailable: number; unchecked: number }
        available_sources: Array<{ id: string; label: string }>
        papers: PaperListItem[]
      }>(`/api/papers?${sp.toString()}`)
    },
    enabled: Boolean(topicId) && !ragMode,
  })

  const ragQuery = useQuery({
    queryKey: ["rag-search", topicId, query],
    queryFn: () =>
      apiFetch<{ hits: any[] }>(`/api/rag/search?topic_id=${encodeURIComponent(topicId)}&q=${encodeURIComponent(query)}&limit=15`),
    enabled: Boolean(topicId) && ragMode && query.trim().length > 0,
  })

  const papers = papersQuery.data?.papers || []
  const currentId = selectedPaperId || papers[0]?.paper_id || 0

  const detailQuery = useQuery({
    queryKey: ["paper-detail", topicId, currentId],
    queryFn: () => apiFetch<PaperDetail>(`/api/papers/${currentId}?topic_id=${encodeURIComponent(topicId)}`),
    enabled: Boolean(topicId && currentId),
  })

  const prefQuery = useQuery({
    queryKey: ["preference", topicId],
    queryFn: () => apiFetch<any>(`/api/topics/${topicId}/preference`),
    enabled: Boolean(topicId),
  })

  // Keyboard J/K navigation across paper list
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) || ragMode || papers.length === 0) return
      const idx = papers.findIndex((p) => p.paper_id === currentId)
      if (e.key.toLowerCase() === "j") {
        e.preventDefault()
        const next = papers[Math.min(papers.length - 1, Math.max(0, idx + 1))]
        if (next) setSearchParams({ paper: String(next.paper_id) })
      } else if (e.key.toLowerCase() === "k") {
        e.preventDefault()
        const prev = papers[Math.max(0, idx - 1)]
        if (prev) setSearchParams({ paper: String(prev.paper_id) })
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [papers, currentId, ragMode, setSearchParams])

  const stateMut = useMutation({
    mutationFn: (payload: { paperId: number; favorite?: boolean; is_read?: boolean }) =>
      apiFetch(`/api/papers/${payload.paperId}/state`, {
        method: "PATCH",
        body: JSON.stringify({
          topic_id: topicId,
          favorite: payload.favorite,
          is_read: payload.is_read,
        }),
      }),
    onSuccess: () => {
      papersQuery.refetch()
      detailQuery.refetch()
    },
  })

  const feedbackMut = useMutation({
    mutationFn: (payload: { paperId: number; action: "like" | "ignore" }) =>
      apiFetch(`/api/papers/${payload.paperId}/feedback`, {
        method: "POST",
        body: JSON.stringify({ topic_id: topicId, action: payload.action }),
      }),
    onSuccess: (_, vars) => {
      setNotice(vars.action === "like" ? "已记录喜欢并更新兴趣画像" : "已记录忽略")
      prefQuery.refetch()
    },
  })

  const deepReadMut = useMutation({
    mutationFn: ({ paperId, fetchPdf }: { paperId: number; fetchPdf: boolean }) =>
      apiFetch(`/api/papers/${paperId}/deep-read`, {
        method: "POST",
        body: JSON.stringify({ topic_id: topicId, level: fetchPdf ? "L3" : "L2", fetch_pdf: fetchPdf }),
      }),
    onSuccess: (res: any, vars) => {
      setNotice(res?.analysis ? (vars.fetchPdf ? "已获取 PDF 并完成 L3 精读" : "基于摘要/网页的精读已完成") : res?.reason || "精读未完成")
      detailQuery.refetch()
      papersQuery.refetch()
    },
  })

  const backfillMut = useMutation({
    mutationFn: () =>
      apiFetch<{ run_id: string }>(`/api/topics/${topicId}/backfill`, {
        method: "POST",
        body: JSON.stringify({ start: bfStart, through: bfEnd }),
      }),
    onSuccess: (res) => {
      setNotice(`已加入补录队列（${res.run_id}）`)
      setBackfillOpen(false)
    },
    onError: (e: any) => setNotice(e?.message || "补录提交失败"),
  })

  const precheckMut = useMutation({
    mutationFn: () =>
      apiFetch<{ checked: number; available: number; unavailable: number; remaining: number }>("/api/papers/precheck", {
        method: "POST",
        body: JSON.stringify({ topic_id: topicId, force: false, limit: 120 }),
      }),
    onSuccess: (res) => {
      setNotice(`预检测完成：可取 PDF ${res.available} 篇，无公开 PDF ${res.unavailable} 篇${res.remaining > 0 ? `（还剩 ${res.remaining} 篇未检）` : ""}`)
      papersQuery.refetch()
      detailQuery.refetch()
    },
  })

  const freezePrefMut = useMutation({
    mutationFn: (frozen: boolean) =>
      apiFetch(`/api/topics/${topicId}/preference/freeze`, {
        method: "POST",
        body: JSON.stringify({ frozen }),
      }),
    onSuccess: () => prefQuery.refetch(),
  })

  const resetPrefMut = useMutation({
    mutationFn: () => apiFetch(`/api/topics/${topicId}/preference`, { method: "DELETE" }),
    onSuccess: () => {
      setNotice("已重置个性化画像")
      prefQuery.refetch()
    },
  })

  const abstractNoteMut = useMutation({
    mutationFn: ({ paperId, force = false }: { paperId: number; force?: boolean }) =>
      apiFetch<{ available: boolean; translation?: string; summary?: string; reason?: string }>(
        `/api/papers/${paperId}/abstract-note`,
        {
          method: "POST",
          body: JSON.stringify({ topic_id: topicId, force }),
        },
      ),
    onSuccess: (res) => {
      if (res?.available) {
        setAbstractNoteErr("")
        detailQuery.refetch()
      } else {
        setAbstractNoteErr(res?.reason || "暂未能生成摘要翻译与总结")
      }
    },
    onError: (e: any) => setAbstractNoteErr(e?.message || "摘要翻译与总结生成失败"),
  })

  useEffect(() => {
    setAbstractNoteErr("")
  }, [currentId])

  if (!topicId) {
    return (
      <div className="p-8 text-xs text-[var(--muted-foreground)]">
        请先在「设置与运行 → 主题管理」中创建研究主题。
      </div>
    )
  }

  const detail = detailQuery.data
  const card = detail?.card
  const deep = detail?.deep_analysis?.analysis
  const abstractNote = detail?.abstract_note
  const primaryRecord = detail?.records?.[0]
  const total = papersQuery.data?.total || 0
  const totalPages = Math.max(1, Math.ceil(total / limit))

  const abstractNoteBlock = detail ? (
    <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-3.5 text-xs space-y-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 font-semibold text-[var(--foreground)]">
          <Languages className="h-3.5 w-3.5 text-[var(--accent)]" />
          <span>摘要中文翻译与大模型总结</span>
          {abstractNote?.model && <Badge variant="outline">{abstractNote.model}</Badge>}
        </div>
        <Button
          size="sm"
          variant="ghost"
          disabled={abstractNoteMut.isPending || !primaryRecord?.abstract}
          onClick={() => abstractNoteMut.mutate({ paperId: detail.paper_id, force: Boolean(abstractNote) })}
        >
          <RefreshCw className={cn("h-3 w-3", abstractNoteMut.isPending && "animate-spin")} />
          {abstractNoteMut.isPending ? "生成中…" : abstractNote ? "重新生成" : "立即生成"}
        </Button>
      </div>
      {abstractNote ? (
        <div className="space-y-2.5">
          <div className="rounded border-l-2 border-[var(--accent)] bg-[var(--surface)] px-3 py-2">
            <div className="mb-0.5 text-[11px] font-semibold text-[var(--muted-foreground)]">AI 摘要总结</div>
            <p className="leading-relaxed text-[var(--foreground)]">{abstractNote.summary}</p>
          </div>
          <div>
            <div className="mb-0.5 text-[11px] font-semibold text-[var(--muted-foreground)]">摘要中文翻译</div>
            <p className="leading-relaxed whitespace-pre-wrap text-[var(--foreground)]">{abstractNote.translation}</p>
          </div>
        </div>
      ) : abstractNoteMut.isPending ? (
        <div className="text-[11px] text-[var(--muted-foreground)]">正在调用大模型翻译原文摘要并提炼要点总结…</div>
      ) : abstractNoteErr ? (
        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-[var(--warning)]">
          <span>{abstractNoteErr}</span>
          {primaryRecord?.abstract && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => abstractNoteMut.mutate({ paperId: detail.paper_id, force: true })}
            >
              重试
            </Button>
          )}
        </div>
      ) : !primaryRecord?.abstract ? (
        <div className="text-[11px] text-[var(--muted-foreground)]">该论文当前无原文摘要，暂无法生成翻译与总结。</div>
      ) : (
        <div className="text-[11px] text-[var(--muted-foreground)]">点击右上角「立即生成」获取基于原文摘要的中文翻译与 AI 总结。</div>
      )}
    </div>
  ) : null

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Workbench Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] bg-[var(--surface)] px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-64">
            <Input
              placeholder={ragMode ? "输入问题进行全文 RAG 检索…" : "搜索标题、摘要、作者…"}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value)
                setPage(0)
              }}
            />
          </div>
          <label className="inline-flex items-center gap-1.5 text-xs text-[var(--muted-foreground)] cursor-pointer">
            <Switch checked={ragMode} onCheckedChange={setRagMode} />
            <span>全文 RAG</span>
          </label>

          {!ragMode && (
            <>
              <Button
                size="sm"
                variant={favoriteOnly ? "default" : "outline"}
                onClick={() => {
                  setFavoriteOnly((v) => !v)
                  setPage(0)
                }}
              >
                <Star className="h-3 w-3" /> 收藏
              </Button>
              <Button
                size="sm"
                variant={unreadOnly ? "default" : "outline"}
                onClick={() => {
                  setUnreadOnly((v) => !v)
                  setPage(0)
                }}
              >
                未读
              </Button>
              <Button
                size="sm"
                variant={pdfAvailableOnly ? "default" : "outline"}
                onClick={() => {
                  setPdfAvailableOnly((v) => !v)
                  setPage(0)
                }}
                title="只看预检测确认可获取 PDF 的论文"
              >
                <FileCheck2 className="h-3 w-3" /> 可取 PDF
                {papersQuery.data?.pdf_counts ? ` (${papersQuery.data.pdf_counts.available})` : ""}
              </Button>
              <Input
                type="date"
                className="w-32 h-7"
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
                title="起始日期"
              />
              <Input
                type="date"
                className="w-32 h-7"
                value={toDate}
                onChange={(e) => setToDate(e.target.value)}
                title="结束日期"
              />
            </>
          )}
        </div>

        <div className="flex items-center gap-2">
          {notice && <span className="text-[11px] text-[var(--accent)]">{notice}</span>}
          <Button
            size="sm"
            variant="outline"
            disabled={precheckMut.isPending}
            onClick={() => precheckMut.mutate()}
            title="轻量预检测库内论文是否可获取原始 PDF，不下载保存大文件"
          >
            <FileCheck2 className="h-3.5 w-3.5" />
            {precheckMut.isPending
              ? "预检测中…"
              : `预检测 PDF${papersQuery.data?.pdf_counts?.unchecked ? ` (${papersQuery.data.pdf_counts.unchecked} 待检)` : ""}`}
          </Button>

          {/* Interest Profile Popover */}
          <Popover>
            <PopoverTrigger asChild>
              <Button size="sm" variant="outline">
                <UserCheck className="h-3.5 w-3.5" />
                兴趣画像
              </Button>
            </PopoverTrigger>
            <PopoverContent className="space-y-2.5">
              <div className="font-semibold">个性化兴趣画像 · {currentTopic?.name}</div>
              {prefQuery.data && (
                <>
                  <div className="text-[11px] text-[var(--muted-foreground)] tabular-nums">
                    正向 {prefQuery.data.positive_count || 0} 篇 · 负向 {prefQuery.data.negative_count || 0} 篇 ·{" "}
                    {prefQuery.data.frozen ? "已冻结" : "自动学习中"}
                  </div>
                  {(prefQuery.data.positive_terms || []).length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {prefQuery.data.positive_terms.slice(0, 8).map((term: string) => (
                        <Badge key={term} variant="outline">{term}</Badge>
                      ))}
                    </div>
                  )}
                  <div className="flex items-center gap-2 pt-1">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => freezePrefMut.mutate(!prefQuery.data.frozen)}
                    >
                      {prefQuery.data.frozen ? "解除冻结" : "冻结画像"}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => resetPrefMut.mutate()}>
                      重置
                    </Button>
                  </div>
                </>
              )}
            </PopoverContent>
          </Popover>

          {/* Backfill Dialog */}
          <Dialog open={backfillOpen} onOpenChange={setBackfillOpen}>
            <DialogTrigger asChild>
              <Button size="sm" variant="outline">
                <CalendarPlus className="h-3.5 w-3.5" />
                按日期补录
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>按日期补录论文 · {currentTopic?.name}</DialogTitle>
              </DialogHeader>
              <div className="mt-3 space-y-3 text-xs">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="mb-1 block text-[var(--muted-foreground)]">开始日期</label>
                    <Input type="date" value={bfStart} onChange={(e) => setBfStart(e.target.value)} />
                  </div>
                  <div>
                    <label className="mb-1 block text-[var(--muted-foreground)]">结束日期</label>
                    <Input type="date" value={bfEnd} onChange={(e) => setBfEnd(e.target.value)} />
                  </div>
                </div>
                <p className="text-[11px] text-[var(--muted-foreground)]">
                  包含起止两天，按当前主题关键词检索多源后去重入库。进度可在「设置与运行 → 运行中心」查看。
                </p>
                <div className="flex justify-end gap-2">
                  <Button variant="outline" onClick={() => setBackfillOpen(false)}>取消</Button>
                  <Button disabled={backfillMut.isPending} onClick={() => backfillMut.mutate()}>
                    开始补录
                  </Button>
                </div>
              </div>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {/* Master-Detail Split Body */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left Paper / RAG List */}
        <div className="flex w-[39%] min-w-[300px] flex-col border-r border-[var(--border)] bg-[var(--background)]">
          {!ragMode && (papersQuery.data?.available_sources || []).length > 0 && (
            <div className="flex flex-wrap gap-1 border-b border-[var(--border)] bg-[var(--surface)] px-3 py-1.5">
              {papersQuery.data!.available_sources.map((src) => {
                const active = selectedSources.includes(src.id)
                return (
                  <button
                    key={src.id}
                    type="button"
                    onClick={() => {
                      setSelectedSources((prev) =>
                        prev.includes(src.id) ? prev.filter((s) => s !== src.id) : [...prev, src.id]
                      )
                      setPage(0)
                    }}
                    className={cn(
                      "rounded px-1.5 py-0.5 text-[10px] border cursor-pointer transition-colors",
                      active
                        ? "border-[var(--primary)] bg-[var(--primary)] text-white"
                        : "border-[var(--border)] bg-[var(--background)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                    )}
                  >
                    {src.label}
                  </button>
                )
              })}
            </div>
          )}

          <div className="flex-1 overflow-y-auto divide-y divide-[var(--border)]">
            {ragMode ? (
              (ragQuery.data?.hits || []).length > 0 ? (
                ragQuery.data!.hits.map((hit, idx) => (
                  <button
                    key={idx}
                    type="button"
                    onClick={() => {
                      setSearchParams({ paper: String(hit.paper_id) })
                      if (hit.page_start) {
                        setPdfPage(hit.page_start)
                        setActiveTab("rag")
                      }
                    }}
                    className="w-full p-3 text-left hover:bg-[var(--surface)] cursor-pointer"
                  >
                    <div className="text-xs font-medium text-[var(--foreground)]">{hit.title}</div>
                    <div className="mt-1 text-[11px] text-[var(--accent)]">
                      {hit.section} · 第 {hit.page_start} 页
                    </div>
                    <p className="mt-1 line-clamp-3 text-[11px] text-[var(--muted-foreground)]">{hit.text}</p>
                  </button>
                ))
              ) : (
                <div className="p-6 text-center text-xs text-[var(--muted-foreground)]">
                  {query.trim() ? "未检索到匹配的全文片段" : "输入关键词检索已索引 PDF 的章节与页码片段"}
                </div>
              )
            ) : papers.length > 0 ? (
              papers.map((p) => {
                const selected = p.paper_id === currentId
                return (
                  <button
                    key={p.paper_id}
                    type="button"
                    onClick={() => {
                      setSearchParams({ paper: String(p.paper_id) })
                      setPdfPage(null)
                    }}
                    className={cn(
                      "w-full px-3.5 py-3 text-left transition-colors cursor-pointer",
                      selected ? "bg-[var(--surface)] border-l-2 border-l-[var(--primary)]" : "hover:bg-[var(--surface)]/60"
                    )}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className={cn("text-xs font-medium leading-snug", !p.is_read && "font-semibold")}>
                        {p.title}
                      </span>
                      {p.favorite && <Star className="h-3.5 w-3.5 shrink-0 fill-[var(--warning)] text-[var(--warning)]" />}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--muted-foreground)]">
                      <span className="tabular-nums">{p.date || "日期待确认"}</span>
                      <span>·</span>
                      <span className="truncate max-w-[180px]">{p.publication_label}</span>
                      {p.has_deep_analysis && <Badge variant="accent">已精读</Badge>}
                      {p.pdf_status === "available" ? (
                        <Badge variant="success">可取 PDF</Badge>
                      ) : p.pdf_status === "unavailable" ? (
                        <Badge variant="outline">无公开 PDF</Badge>
                      ) : (
                        <Badge variant="outline">待检测</Badge>
                      )}
                    </div>
                  </button>
                )
              })
            ) : (
              <div className="p-8 text-center text-xs text-[var(--muted-foreground)]">当前条件下暂无匹配论文</div>
            )}
          </div>

          {!ragMode && (
            <div className="flex items-center justify-between border-t border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-[11px] text-[var(--muted-foreground)]">
              <span className="tabular-nums">共 {total} 篇 · J/K 切篇</span>
              <div className="flex items-center gap-1.5">
                <Button size="sm" variant="ghost" disabled={page <= 0} onClick={() => setPage((p) => p - 1)}>
                  上一页
                </Button>
                <span className="tabular-nums">{page + 1} / {totalPages}</span>
                <Button size="sm" variant="ghost" disabled={page + 1 >= totalPages} onClick={() => setPage((p) => p + 1)}>
                  下一页
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* Right Three-Part Structured Reader */}
        <div className="flex min-w-0 flex-1 flex-col bg-[var(--surface)]">
          {detail ? (
            <>
              {/* Part 1: Compact Action Bar & Title Header */}
              <div className="border-b border-[var(--border)] px-5 py-3.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge variant={detail.publication?.status === "published" ? "success" : "outline"}>
                      {detail.publication_label}
                    </Badge>
                    {detail.pdf_status === "available" ? (
                      <Badge variant="success">可获取 PDF{detail.pdf_strategy ? ` · ${detail.pdf_strategy}` : ""}</Badge>
                    ) : detail.pdf_status === "unavailable" ? (
                      <Badge variant="outline">无公开 PDF</Badge>
                    ) : (
                      <Badge variant="outline">PDF 待检测</Badge>
                    )}
                    <span className="text-[11px] tabular-nums text-[var(--muted-foreground)]">{detail.date || "日期未确认"}</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Button
                      size="sm"
                      variant={detail.favorite ? "default" : "outline"}
                      onClick={() => stateMut.mutate({ paperId: detail.paper_id, favorite: !detail.favorite })}
                    >
                      <Star className="h-3.5 w-3.5" />
                      {detail.favorite ? "已收藏" : "收藏"}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => stateMut.mutate({ paperId: detail.paper_id, is_read: !detail.is_read })}
                    >
                      <Check className="h-3.5 w-3.5" />
                      {detail.is_read ? "标记未读" : "标记已读"}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => feedbackMut.mutate({ paperId: detail.paper_id, action: "like" })}
                    >
                      <ThumbsUp className="h-3.5 w-3.5" /> 喜欢
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => feedbackMut.mutate({ paperId: detail.paper_id, action: "ignore" })}
                    >
                      <ThumbsDown className="h-3.5 w-3.5" /> 忽略
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={deepReadMut.isPending}
                      onClick={() => deepReadMut.mutate({ paperId: detail.paper_id, fetchPdf: false })}
                    >
                      <FileSearch className="h-3.5 w-3.5" />
                      {deepReadMut.isPending ? "总结中…" : "摘要/网页总结"}
                    </Button>
                    <Button
                      size="sm"
                      disabled={deepReadMut.isPending}
                      onClick={() => deepReadMut.mutate({ paperId: detail.paper_id, fetchPdf: true })}
                    >
                      <FileSearch className="h-3.5 w-3.5" />
                      {deepReadMut.isPending ? "获取中…" : "获取 PDF 精读"}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => askCopilot(`请帮我分析论文《${detail.title}》（ID ${detail.paper_id}）的创新点与实验局限。`)}
                    >
                      <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />
                      问 Copilot
                    </Button>
                  </div>
                </div>
                <h1 className="font-serif-editorial mt-2.5 text-xl font-semibold leading-snug text-[var(--foreground)]">
                  {detail.title}
                </h1>
                <div className="mt-1 text-xs text-[var(--muted-foreground)]">
                  {(primaryRecord?.authors || []).slice(0, 10).join(" · ") || "作者未确认"}
                </div>
              </div>

              {/* Part 2: Tabbed Content Reader */}
              <Tabs value={activeTab} onValueChange={setActiveTab} className="flex min-h-0 flex-1 flex-col px-5 pt-3">
                <TabsList className="w-fit">
                  <TabsTrigger value="card">AI 精读卡片</TabsTrigger>
                  <TabsTrigger value="rag">原文摘要与 RAG 证据</TabsTrigger>
                  <TabsTrigger value="pdf" disabled={detail.pdf_urls.length === 0 && detail.pdf_status !== "available"}>
                    内置 PDF 预览{detail.pdf_urls.length === 0 && detail.pdf_status !== "available" ? "（暂无）" : ""}
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="card" className="min-h-0 flex-1 overflow-y-auto pb-6 space-y-4">
                  {abstractNoteBlock}
                  {deep ? (
                    <div className="space-y-3 text-xs">
                      <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-3.5">
                        <div className="mb-1 flex items-center gap-2 font-semibold">
                          <Badge variant="accent">精读 {detail.deep_analysis?.level}</Badge>
                          <span>结构化结论</span>
                        </div>
                        <p className="leading-relaxed">{deep.summary}</p>
                      </div>
                      {deep.problem && (
                        <div>
                          <div className="font-semibold text-[var(--muted-foreground)]">研究问题</div>
                          <p className="mt-1">{deep.problem}</p>
                        </div>
                      )}
                      {deep.method && (
                        <div>
                          <div className="font-semibold text-[var(--muted-foreground)]">核心方法</div>
                          <p className="mt-1">{deep.method}</p>
                        </div>
                      )}
                      {Array.isArray(deep.datasets) && deep.datasets.length > 0 && (
                        <div>
                          <div className="font-semibold text-[var(--muted-foreground)]">数据集</div>
                          <div className="mt-1 flex flex-wrap gap-1.5">
                            {deep.datasets.map((d: string) => <Badge key={d} variant="outline">{d}</Badge>)}
                          </div>
                        </div>
                      )}
                      {deep.limitations && (
                        <div>
                          <div className="font-semibold text-[var(--muted-foreground)]">局限性</div>
                          <p className="mt-1">{deep.limitations}</p>
                        </div>
                      )}
                    </div>
                  ) : card?.analysis ? (
                    <div className="space-y-3 text-xs">
                      <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-3.5">
                        <div className="mb-1 font-semibold">日报中文卡片总结</div>
                        <p className="leading-relaxed">{card.analysis.summary?.statement}</p>
                      </div>
                      {card.analysis.inference && (
                        <div className="border-l-2 border-[var(--primary)] pl-3 text-[var(--muted-foreground)]">
                          主题关联：{card.analysis.inference}
                        </div>
                      )}
                      {Array.isArray(card.evidence) && card.evidence.length > 0 && (
                        <div className="space-y-1.5">
                          <div className="font-semibold text-[var(--muted-foreground)]">摘要摘句证据</div>
                          {card.evidence.map((ev: any, i: number) => (
                            <blockquote key={i} className="rounded border border-[var(--border)] bg-[var(--background)] p-2.5 text-[11px]">
                              {ev.text}
                            </blockquote>
                          ))}
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="rounded-md border border-dashed border-[var(--border)] p-6 text-xs text-[var(--muted-foreground)]">
                      暂未生成 AI 卡片或精读报告。可点击右上角「L3 精读」基于公开 PDF 进行结构化提炼，或切换「原文摘要与 RAG 证据」阅读原文。
                    </div>
                  )}
                </TabsContent>

                <TabsContent value="rag" className="min-h-0 flex-1 overflow-y-auto pb-6 space-y-4 text-xs">
                  {abstractNoteBlock}
                  <div>
                    <div className="mb-1.5 font-semibold text-[var(--muted-foreground)]">原始摘要</div>
                    <p className="leading-relaxed whitespace-pre-wrap rounded-md border border-[var(--border)] bg-[var(--background)] p-3.5">
                      {primaryRecord?.abstract || "来源未提供摘要"}
                    </p>
                  </div>
                  {detail.rag_chunks.length > 0 && (
                    <div className="space-y-2">
                      <div className="font-semibold text-[var(--muted-foreground)]">已索引全文片段（{detail.rag_chunks.length}）</div>
                      {detail.rag_chunks.map((c) => (
                        <div key={c.id} className="rounded-md border border-[var(--border)] bg-[var(--background)] p-3">
                          <div className="flex items-center justify-between text-[11px] font-medium">
                            <span>{c.section}</span>
                            {c.page_start && detail.pdf_urls.length > 0 && (
                              <button
                                type="button"
                                onClick={() => {
                                  setPdfPage(c.page_start || 1)
                                  setActiveTab("pdf")
                                }}
                                className="text-[var(--accent)] hover:underline cursor-pointer"
                              >
                                定位 PDF 第 {c.page_start} 页 →
                              </button>
                            )}
                          </div>
                          <p className="mt-1 text-[11px] leading-relaxed text-[var(--muted-foreground)]">{c.text}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </TabsContent>

                <TabsContent value="pdf" className="min-h-0 flex-1 flex flex-col pb-3">
                  {detail.pdf_status === "unavailable" && (
                    <div className="mb-2 flex items-center justify-between rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-xs text-[var(--muted-foreground)]">
                      <span>预检测未发现可用公开 PDF（{detail.pdf_reason || "可能受机构订阅限制"}），建议用「摘要/网页总结」查看内容。</span>
                      <Button size="sm" variant="outline" onClick={() => precheckMut.mutate()}>
                        重新检测
                      </Button>
                    </div>
                  )}
                  {(detail.pdf_urls.length > 0 || detail.pdf_status === "available") && detail.pdf_status !== "unavailable" && (
                    <>
                      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-[var(--muted-foreground)]">
                        <span>优先使用预检测缓存直链解析 PDF；若遇远端限流可重试或新窗口直开。</span>
                        <div className="flex items-center gap-1.5">
                          <Button size="sm" variant="outline" onClick={() => setPdfNonce((n) => n + 1)}>
                            <RefreshCw className="h-3 w-3" />
                            刷新预览
                          </Button>
                          {detail.pdf_urls[0] && (
                            <a
                              href={detail.pdf_urls[0]}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--background)] px-2.5 py-1 text-xs font-medium text-[var(--foreground)] hover:border-[var(--accent)] hover:text-[var(--accent)]"
                            >
                              新窗口打开 PDF
                              <ExternalLink className="h-3 w-3" />
                            </a>
                          )}
                        </div>
                      </div>
                      <iframe
                        key={`${detail.paper_id}-${pdfNonce}`}
                        title={`PDF-${detail.paper_id}`}
                        src={`/api/papers/${detail.paper_id}/pdf?topic_id=${encodeURIComponent(topicId)}&v=${pdfNonce}${pdfPage ? `#page=${pdfPage}` : ""}`}
                        className="min-h-0 flex-1 w-full rounded-md border border-[var(--border)] bg-[var(--background)]"
                      />
                    </>
                  )}
                </TabsContent>
              </Tabs>

              {/* Part 3: Bottom Source Links Strip */}
              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border)] px-5 py-2.5 text-[11px]">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[var(--muted-foreground)]">数据源：</span>
                  {detail.records.map((r, i) => (
                    <a
                      key={i}
                      href={r.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-2 py-0.5 text-[var(--foreground)] hover:border-[var(--accent)] hover:text-[var(--accent)]"
                    >
                      {SOURCE_LABELS[r.source] || r.source}
                      <ExternalLink className="h-2.5 w-2.5" />
                    </a>
                  ))}
                </div>
                {primaryRecord?.citation_count !== null && primaryRecord?.citation_count !== undefined && (
                  <span className="tabular-nums text-[var(--muted-foreground)]">引用数：{primaryRecord.citation_count}</span>
                )}
              </div>
            </>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center text-xs text-[var(--muted-foreground)]">
              <BookOpen className="mb-2 h-6 w-6 opacity-40" />
              <span>从左侧选择一篇论文开始阅读（支持 J / K 快捷键切换）</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
