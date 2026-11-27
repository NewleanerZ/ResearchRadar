import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Activity, ArchiveRestore, Cpu, FolderCog, Plus, RefreshCw, Server, Sparkles, Trash2 } from "lucide-react"
import { apiFetch, RUN_LABELS, TopicItem } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input, Textarea } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch } from "@/components/ui/controls"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { TimelineAccordion } from "@/components/domain/TimelineDrawer"

export function SettingsPage() {
  const { topics, refetchTopics } = useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const [section, setSection] = useState(searchParams.get("tab") || "topics")
  const [notice, setNotice] = useState("")

  // Topic Editor / Creator
  const [createOpen, setCreateOpen] = useState(false)
  const [draftDesc, setDraftDesc] = useState("")
  const [draftTopic, setDraftTopic] = useState<TopicItem | null>(null)
  const [manualName, setManualName] = useState("")
  const [manualKeywords, setManualKeywords] = useState("")
  const [selectedTopicId, setSelectedTopicId] = useState("")
  const [topicFilter, setTopicFilter] = useState<"active" | "archived">("active")

  // Topic edit form states
  const activeList = topics.filter((t) => (topicFilter === "archived" ? t.archived : !t.archived))
  const editingTopic = topics.find((t) => t.id === selectedTopicId) || activeList[0] || null
  const [tName, setTName] = useState("")
  const [tDesc, setTDesc] = useState("")
  const [tKeywords, setTKeywords] = useState("")
  const [tExcluded, setTExcluded] = useState("")
  const [tExpansions, setTExpansions] = useState("")
  const [tNotify, setTNotify] = useState(true)

  // Project Memory
  const memoryQuery = useQuery({
    queryKey: ["topic-memory", editingTopic?.id],
    queryFn: () => apiFetch<{ revision: number; memory: any }>(`/api/topics/${editingTopic!.id}/memory`),
    enabled: Boolean(editingTopic?.id) && section === "topics",
  })
  const [mVenues, setMVenues] = useState("")
  const [mExcluded, setMExcluded] = useState("")
  const [mDatasets, setMDatasets] = useState("")
  const [mProtocol, setMProtocol] = useState("")
  const [mMainOnly, setMMainOnly] = useState(false)

  useEffect(() => {
    if (editingTopic) {
      setTName(editingTopic.name)
      setTDesc(editingTopic.description)
      setTKeywords(editingTopic.keywords.join("\n"))
      setTExcluded(editingTopic.excluded.join("\n"))
      setTExpansions(editingTopic.expansions.join("\n"))
      setTNotify(editingTopic.notify)
    }
  }, [editingTopic?.id, editingTopic?.revision])

  useEffect(() => {
    const mem = memoryQuery.data?.memory
    if (mem) {
      setMVenues((mem.venues || []).join(", "))
      setMExcluded((mem.excluded || []).join("\n"))
      setMDatasets((mem.datasets || []).join("\n"))
      setMProtocol(mem.research_protocol || "")
      setMMainOnly(Boolean(mem.main_only))
    }
  }, [memoryQuery.data])

  // Runs
  const runsQuery = useQuery({
    queryKey: ["runs-full"],
    queryFn: () => apiFetch<{ runs: any[] }>("/api/runs"),
    enabled: section === "runs",
    refetchInterval: section === "runs" ? 5000 : false,
  })
  const [selectedRunId, setSelectedRunId] = useState("")
  const pickedRun = (runsQuery.data?.runs || []).find((r) => r.id === selectedRunId) || runsQuery.data?.runs?.[0] || null

  // System Diagnostics & Config
  const diagQuery = useQuery({
    queryKey: ["settings-diagnostics"],
    queryFn: () => apiFetch<any>("/api/settings/diagnostics"),
    enabled: section === "system",
  })
  const mcpQuery = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: () => apiFetch<{ servers: any[] }>("/api/mcp-servers"),
    enabled: section === "system",
  })

  const [llmBaseUrl, setLlmBaseUrl] = useState("")
  const [llmModel, setLlmModel] = useState("")
  const [thinking, setThinking] = useState("off")
  const [streamChat, setStreamChat] = useState(true)
  const [keywordOnly, setKeywordOnly] = useState(false)
  const [extraSources, setExtraSources] = useState<string[]>([])
  const [searxngUrl, setSearxngUrl] = useState("")
  const [jinaReader, setJinaReader] = useState(false)
  const [hfMirror, setHfMirror] = useState("")
  const [secretInputs, setSecretInputs] = useState<Record<string, string>>({})
  const [secretClears, setSecretClears] = useState<Record<string, boolean>>({})
  const [mcpName, setMcpName] = useState("")
  const [mcpCmd, setMcpCmd] = useState("")
  const [mcpArgs, setMcpArgs] = useState("")
  const [mcpEnv, setMcpEnv] = useState("")
  const [mcpProbeOut, setMcpProbeOut] = useState("")

  useEffect(() => {
    const cfg = diagQuery.data?.config
    if (cfg) {
      setLlmBaseUrl(cfg.llm_base_url || "")
      setLlmModel(cfg.llm_model || "")
      setThinking(cfg.thinking_level || "off")
      setStreamChat(Boolean(cfg.stream_chat))
      setKeywordOnly(Boolean(cfg.keyword_only))
      setExtraSources(cfg.extra_sources || [])
      setSearxngUrl(cfg.searxng_url || "")
      setJinaReader(Boolean(cfg.jina_reader_enabled))
      setHfMirror(cfg.hf_endpoint || "")
    }
  }, [diagQuery.data])

  // Mutations
  const draftMut = useMutation({
    mutationFn: () => apiFetch<{ topic: TopicItem }>("/api/topics/draft", { method: "POST", body: JSON.stringify({ description: draftDesc }) }),
    onSuccess: (res) => setDraftTopic(res.topic),
    onError: (e: any) => setNotice(e?.message || "生成草案失败"),
  })

  const createTopicMut = useMutation({
    mutationFn: (payload: any) => apiFetch("/api/topics", { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: () => {
      setCreateOpen(false); setDraftTopic(null); setDraftDesc(""); setManualName(""); setManualKeywords("")
      refetchTopics()
      setNotice("主题已创建")
    },
    onError: (e: any) => setNotice(e?.message || "创建失败"),
  })

  const patchTopicMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: any }) =>
      apiFetch(`/api/topics/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: () => {
      refetchTopics()
      setNotice("主题设置已更新")
    },
    onError: (e: any) => setNotice(e?.message || "保存失败"),
  })

  const saveMemMut = useMutation({
    mutationFn: () =>
      apiFetch(`/api/topics/${editingTopic!.id}/memory`, {
        method: "PUT",
        body: JSON.stringify({
          revision: memoryQuery.data?.revision || 0,
          venues: mVenues.replace(/，/g, ",").split(",").map((s) => s.trim()).filter(Boolean),
          excluded: mExcluded.split("\n").map((s) => s.trim()).filter(Boolean),
          datasets: mDatasets.split("\n").map((s) => s.trim()).filter(Boolean),
          research_protocol: mProtocol,
          main_only: mMainOnly,
        }),
      }),
    onSuccess: () => {
      memoryQuery.refetch()
      setNotice("项目记忆已保存")
    },
    onError: (e: any) => setNotice(e?.message || "保存项目记忆失败"),
  })

  const retryRunMut = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/runs/${id}/retry`, { method: "POST" }),
    onSuccess: () => {
      runsQuery.refetch()
      setNotice("已重新加入运行队列")
    },
  })

  const retryNotifyMut = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/runs/${id}/retry-notification`, { method: "POST" }),
    onSuccess: () => {
      runsQuery.refetch()
      setNotice("已重新加入飞书投递队列")
    },
  })

  const saveEnvMut = useMutation({
    mutationFn: (payload: { updates: Record<string, string>; clear_keys?: string[] }) =>
      apiFetch("/api/settings/env", { method: "PUT", body: JSON.stringify(payload) }),
    onSuccess: () => {
      diagQuery.refetch()
      setSecretInputs({})
      setSecretClears({})
      setNotice("配置已安全写入 .env")
    },
    onError: (e: any) => setNotice(e?.message || "保存失败"),
  })

  const warmMut = useMutation({
    mutationFn: () => apiFetch("/api/settings/warm-embedding", { method: "POST", body: JSON.stringify({ hf_endpoint: hfMirror }) }),
    onSuccess: () => {
      diagQuery.refetch()
      setNotice("本地 BGE 向量模型下载完成")
    },
    onError: (e: any) => setNotice(e?.message || "模型下载失败"),
  })

  const addMcpMut = useMutation({
    mutationFn: () =>
      apiFetch("/api/mcp-servers", {
        method: "POST",
        body: JSON.stringify({
          name: mcpName,
          command: mcpCmd,
          args: mcpArgs.split(/\s+/).filter(Boolean),
          env_keys: mcpEnv.split(",").map((s) => s.trim()).filter(Boolean),
        }),
      }),
    onSuccess: () => {
      setMcpName(""); setMcpCmd(""); setMcpArgs(""); setMcpEnv("")
      mcpQuery.refetch()
      setNotice("已添加外部 MCP 服务")
    },
  })

  const menu = [
    { id: "topics", label: "主题与项目记忆", icon: FolderCog },
    { id: "runs", label: "运行中心", icon: Activity },
    { id: "system", label: "模型、数据源与密钥", icon: Cpu },
  ]

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      <aside className="w-56 shrink-0 border-r border-[var(--border)] bg-[var(--background)] p-3 space-y-1">
        <div className="px-2 pb-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">
          设置与运行
        </div>
        {menu.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => {
              setSection(id)
              setSearchParams({ tab: id })
            }}
            className={cn(
              "flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-xs font-medium cursor-pointer",
              section === id
                ? "bg-[var(--surface)] text-[var(--foreground)] border border-[var(--border)]"
                : "text-[var(--muted-foreground)] hover:bg-[var(--surface)]/60"
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            <span>{label}</span>
          </button>
        ))}
        {notice && <div className="mt-4 rounded border border-[var(--accent)]/30 bg-[var(--accent-soft)] p-2 text-[11px] text-[var(--accent)]">{notice}</div>}
      </aside>

      <div className="min-w-0 flex-1 overflow-y-auto bg-[var(--surface)] p-6">
        {/* Section 1: Topics & Memory */}
        {section === "topics" && (
          <div className="mx-auto max-w-5xl space-y-5">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] pb-3">
              <div className="flex items-center gap-2">
                <h1 className="font-serif-editorial text-xl font-semibold">研究主题与项目记忆</h1>
                <Button size="sm" variant={topicFilter === "active" ? "default" : "outline"} onClick={() => setTopicFilter("active")}>
                  活跃主题
                </Button>
                <Button size="sm" variant={topicFilter === "archived" ? "default" : "outline"} onClick={() => setTopicFilter("archived")}>
                  回收站
                </Button>
              </div>
              <Dialog open={createOpen} onOpenChange={setCreateOpen}>
                <DialogTrigger asChild>
                  <Button size="sm"><Plus className="h-3.5 w-3.5" /> 新建主题</Button>
                </DialogTrigger>
                <DialogContent className="max-w-xl">
                  <DialogHeader><DialogTitle>添加研究主题</DialogTitle></DialogHeader>
                  <div className="mt-3 space-y-3 text-xs">
                    <Textarea
                      placeholder="用一句话描述你想追踪的研究方向，例如：跟踪工业工件的三维点云异常检测，关注缺陷定位，排除医疗影像。"
                      value={draftDesc}
                      onChange={(e) => setDraftDesc(e.target.value)}
                    />
                    <Button size="sm" disabled={!draftDesc.trim() || draftMut.isPending} onClick={() => draftMut.mutate()}>
                      <Sparkles className="h-3.5 w-3.5" /> AI 生成检索词草案
                    </Button>
                    {draftTopic && (
                      <div className="space-y-2 rounded border border-[var(--border)] bg-[var(--background)] p-3">
                        <div className="font-semibold">{draftTopic.name}</div>
                        <div className="text-[var(--muted-foreground)]">核心词：{draftTopic.keywords.join("、")}</div>
                        <Button
                          size="sm"
                          onClick={() =>
                            createTopicMut.mutate({
                              ...draftTopic,
                              expansions: draftTopic.proposed_expansions || [],
                              proposed_expansions: [],
                            })
                          }
                        >
                          确认添加
                        </Button>
                      </div>
                    )}
                    <div className="border-t border-[var(--border)] pt-3 space-y-2">
                      <div className="text-[11px] font-medium text-[var(--muted-foreground)]">或手动填写</div>
                      <Input placeholder="主题名称" value={manualName} onChange={(e) => setManualName(e.target.value)} />
                      <Textarea placeholder="核心检索词（每行一个）" value={manualKeywords} onChange={(e) => setManualKeywords(e.target.value)} />
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!manualName.trim() || !manualKeywords.trim()}
                        onClick={() =>
                          createTopicMut.mutate({
                            id: `topic_${Math.random().toString(16).slice(2, 14)}`,
                            name: manualName.trim(),
                            description: draftDesc.trim(),
                            keywords: manualKeywords.split("\n").map((s) => s.trim()).filter(Boolean),
                            enabled: true,
                          })
                        }
                      >
                        手动保存主题
                      </Button>
                    </div>
                  </div>
                </DialogContent>
              </Dialog>
            </div>

            <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
              <div className="lg:col-span-4 space-y-2">
                {activeList.map((t) => (
                  <div
                    key={t.id}
                    onClick={() => setSelectedTopicId(t.id)}
                    className={cn(
                      "rounded-md border p-3 text-xs cursor-pointer transition-colors",
                      editingTopic?.id === t.id
                        ? "border-[var(--primary)] bg-[var(--background)]"
                        : "border-[var(--border)] hover:bg-[var(--background)]"
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold">{t.name}</span>
                      <Badge variant={t.archived ? "outline" : t.enabled ? "success" : "warning"}>
                        {t.archived ? "已删除" : t.enabled ? "自动追踪" : "手动"}
                      </Badge>
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--muted-foreground)]">每天 {t.schedule?.slice(0, 5)}</div>
                  </div>
                ))}
              </div>

              {editingTopic && (
                <div className="lg:col-span-8 space-y-5">
                  <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-3 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold">主题配置 · v{editingTopic.revision}</span>
                      <div className="flex items-center gap-2">
                        {editingTopic.archived ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              patchTopicMut.mutate({
                                id: editingTopic.id,
                                body: { archived: false, enabled: false, revision: editingTopic.revision },
                              })
                            }
                          >
                            <ArchiveRestore className="h-3.5 w-3.5" /> 恢复主题
                          </Button>
                        ) : (
                          <>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() =>
                                patchTopicMut.mutate({
                                  id: editingTopic.id,
                                  body: { enabled: !editingTopic.enabled, revision: editingTopic.revision },
                                })
                              }
                            >
                              {editingTopic.enabled ? "暂停自动追踪" : "开启自动追踪"}
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() =>
                                patchTopicMut.mutate({
                                  id: editingTopic.id,
                                  body: { archived: true, enabled: false, revision: editingTopic.revision },
                                })
                              }
                            >
                              <Trash2 className="h-3.5 w-3.5 text-[var(--destructive)]" />
                            </Button>
                          </>
                        )}
                      </div>
                    </div>

                    <Input value={tName} onChange={(e) => setTName(e.target.value)} placeholder="主题名称" />
                    <Textarea value={tDesc} onChange={(e) => setTDesc(e.target.value)} placeholder="研究方向" />
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                      <div>
                        <label className="mb-1 block text-[11px] text-[var(--muted-foreground)]">核心检索词（每行一个）</label>
                        <Textarea value={tKeywords} onChange={(e) => setTKeywords(e.target.value)} />
                      </div>
                      <div>
                        <label className="mb-1 block text-[11px] text-[var(--muted-foreground)]">排除词（每行一个）</label>
                        <Textarea value={tExcluded} onChange={(e) => setTExcluded(e.target.value)} />
                      </div>
                    </div>
                    <div>
                      <label className="mb-1 block text-[11px] text-[var(--muted-foreground)]">补充检索词（每行一个）</label>
                      <Textarea value={tExpansions} onChange={(e) => setTExpansions(e.target.value)} />
                    </div>
                    <div className="flex items-center justify-between pt-1">
                      <label className="inline-flex items-center gap-2 cursor-pointer">
                        <Switch checked={tNotify} onCheckedChange={setTNotify} />
                        <span>允许飞书通知推送</span>
                      </label>
                      <Button
                        size="sm"
                        onClick={() =>
                          patchTopicMut.mutate({
                            id: editingTopic.id,
                            body: {
                              name: tName,
                              description: tDesc,
                              keywords: tKeywords.split("\n").map((s) => s.trim()).filter(Boolean),
                              excluded: tExcluded.split("\n").map((s) => s.trim()).filter(Boolean),
                              expansions: tExpansions.split("\n").map((s) => s.trim()).filter(Boolean),
                              notify: tNotify,
                              revision: editingTopic.revision,
                            },
                          })
                        }
                      >
                        保存主题修改
                      </Button>
                    </div>
                  </div>

                  {/* Project Memory */}
                  <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-3 text-xs">
                    <div className="font-semibold">项目记忆（第 {memoryQuery.data?.revision || 0} 版 · 对话与专题共用）</div>
                    <Input placeholder="常用会议（逗号分隔，如 CVPR, ICML）" value={mVenues} onChange={(e) => setMVenues(e.target.value)} />
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                      <Textarea placeholder="排除条件（每行一项）" value={mExcluded} onChange={(e) => setMExcluded(e.target.value)} />
                      <Textarea placeholder="常用数据集（每行一项）" value={mDatasets} onChange={(e) => setMDatasets(e.target.value)} />
                    </div>
                    <Textarea placeholder="研究口径与偏好说明" value={mProtocol} onChange={(e) => setMProtocol(e.target.value)} />
                    <div className="flex items-center justify-between">
                      <label className="inline-flex items-center gap-2 cursor-pointer">
                        <Switch checked={mMainOnly} onCheckedChange={setMMainOnly} />
                        <span>专题默认仅看主会</span>
                      </label>
                      <Button size="sm" variant="outline" onClick={() => saveMemMut.mutate()}>保存项目记忆</Button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Section 2: Runs Center */}
        {section === "runs" && (
          <div className="mx-auto max-w-5xl space-y-4">
            <div className="flex items-center justify-between border-b border-[var(--border)] pb-3">
              <h1 className="font-serif-editorial text-xl font-semibold">运行中心</h1>
              <Button size="sm" variant="outline" onClick={() => runsQuery.refetch()}>
                <RefreshCw className="h-3.5 w-3.5" /> 刷新
              </Button>
            </div>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
              <div className="lg:col-span-5 space-y-1.5 max-h-[75vh] overflow-y-auto">
                {(runsQuery.data?.runs || []).map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    onClick={() => setSelectedRunId(r.id)}
                    className={cn(
                      "w-full rounded-md border p-3 text-left text-xs cursor-pointer",
                      pickedRun?.id === r.id ? "border-[var(--primary)] bg-[var(--background)]" : "border-[var(--border)] hover:bg-[var(--background)]"
                    )}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium tabular-nums">{r.local_day} · {r.topic_id}</span>
                      <Badge variant={r.status === "succeeded" ? "success" : r.status === "failed" ? "destructive" : "accent"}>
                        {RUN_LABELS[r.status] || r.status}
                      </Badge>
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--muted-foreground)] truncate">{r.kind}</div>
                  </button>
                ))}
              </div>
              <div className="lg:col-span-7">
                {pickedRun ? (
                  <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-3 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">{pickedRun.id}</span>
                      <div className="flex gap-2">
                        {(pickedRun.status === "failed" || pickedRun.status === "partial") && (
                          <Button size="sm" onClick={() => retryRunMut.mutate(pickedRun.id)}>重试任务</Button>
                        )}
                        {(pickedRun.notification === "failed" || pickedRun.notification === "uncertain") && (
                          <Button size="sm" variant="outline" onClick={() => retryNotifyMut.mutate(pickedRun.id)}>重发飞书</Button>
                        )}
                      </div>
                    </div>
                    {pickedRun.error && <div className="rounded bg-[var(--destructive-soft)] p-2 text-[var(--destructive)]">{pickedRun.error}</div>}
                    {pickedRun.scans?.length > 0 && (
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>来源</TableHead>
                            <TableHead>状态</TableHead>
                            <TableHead>页数</TableHead>
                            <TableHead>接收</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {pickedRun.scans.map((s: any, i: number) => (
                            <TableRow key={i}>
                              <TableCell>{s.source}</TableCell>
                              <TableCell>{s.status}</TableCell>
                              <TableCell className="tabular-nums">{s.pages}</TableCell>
                              <TableCell className="tabular-nums">{s.received}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    )}
                    <TimelineAccordion events={pickedRun.timeline || []} defaultOpen />
                  </div>
                ) : (
                  <div className="text-xs text-[var(--muted-foreground)]">暂无运行记录</div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Section 3: System Config, BGE, MCP & Secrets */}
        {section === "system" && diagQuery.data && (
          <div className="mx-auto max-w-4xl space-y-6 text-xs">
            <h1 className="font-serif-editorial text-xl font-semibold border-b border-[var(--border)] pb-3">
              模型、数据源与系统密钥
            </h1>

            {/* Diagnostics Strip */}
            <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Badge variant={diagQuery.data.embedding.ready ? "success" : "warning"}>
                    {diagQuery.data.embedding.ready ? "BGE 语义模型就绪" : "BGE 未就绪"}
                  </Badge>
                  <Badge variant={diagQuery.data.config.llm_model ? "accent" : "warning"}>
                    LLM · {diagQuery.data.config.llm_model || "未配置"}
                  </Badge>
                </div>
                <div className="flex items-center gap-2">
                  <Input
                    className="w-56 h-7"
                    placeholder="HF 镜像（如 https://hf-mirror.com）"
                    value={hfMirror}
                    onChange={(e) => setHfMirror(e.target.value)}
                  />
                  <Button size="sm" variant="outline" disabled={warmMut.isPending} onClick={() => warmMut.mutate()}>
                    {warmMut.isPending ? "下载中…" : "下载/修复 BGE 模型"}
                  </Button>
                </div>
              </div>
              {diagQuery.data.embedding.reasons?.length > 0 && (
                <div className="text-[11px] text-[var(--warning)]">{diagQuery.data.embedding.reasons.join("；")}</div>
              )}
            </div>

            {/* LLM & Sources */}
            <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-3">
              <div className="font-semibold">模型与数据源参数</div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div>
                  <label className="mb-1 block text-[11px] text-[var(--muted-foreground)]">LLM Base URL</label>
                  <Input value={llmBaseUrl} onChange={(e) => setLlmBaseUrl(e.target.value)} />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-[var(--muted-foreground)]">LLM 模型名</label>
                  <Input value={llmModel} onChange={(e) => setLlmModel(e.target.value)} />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-[var(--muted-foreground)]">深度思考等级</label>
                  <Select value={thinking} onValueChange={setThinking}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="off">off（关闭）</SelectItem>
                      <SelectItem value="low">low（轻度）</SelectItem>
                      <SelectItem value="medium">medium（平衡）</SelectItem>
                      <SelectItem value="high">high（深度）</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex items-center gap-5 pt-5">
                  <label className="inline-flex items-center gap-2 cursor-pointer">
                    <Switch checked={streamChat} onCheckedChange={setStreamChat} />
                    <span>流式输出</span>
                  </label>
                  <label className="inline-flex items-center gap-2 cursor-pointer">
                    <Switch checked={keywordOnly} onCheckedChange={setKeywordOnly} />
                    <span>仅关键词模式</span>
                  </label>
                </div>
              </div>

              <div>
                <div className="mb-1.5 text-[11px] text-[var(--muted-foreground)]">启用的扩展数据源</div>
                <div className="flex flex-wrap gap-1.5">
                  {(diagQuery.data.config.available_extra_sources || []).map((s: any) => {
                    const on = extraSources.includes(s.id)
                    return (
                      <button
                        key={s.id}
                        type="button"
                        onClick={() =>
                          setExtraSources((prev) => (prev.includes(s.id) ? prev.filter((x) => x !== s.id) : [...prev, s.id]))
                        }
                        className={cn(
                          "rounded border px-2 py-1 text-[11px] cursor-pointer",
                          on ? "border-[var(--primary)] bg-[var(--primary)] text-white" : "border-[var(--border)] bg-[var(--surface)]"
                        )}
                      >
                        {s.label}
                      </button>
                    )
                  })}
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div>
                  <label className="mb-1 block text-[11px] text-[var(--muted-foreground)]">
                    自托管 SearXNG 地址（可选，如 http://127.0.0.1:8888；留空则自动使用 DuckDuckGo / GitHub / HF）
                  </label>
                  <Input
                    placeholder="http://127.0.0.1:8888"
                    value={searxngUrl}
                    onChange={(e) => setSearxngUrl(e.target.value)}
                  />
                </div>
                <div className="flex items-center gap-3 pt-5">
                  <label className="inline-flex items-center gap-2 cursor-pointer">
                    <Switch checked={jinaReader} onCheckedChange={setJinaReader} />
                    <span>优先使用 Jina Reader 提取网页正文</span>
                  </label>
                </div>
              </div>

              <div className="flex justify-end">
                <Button
                  size="sm"
                  onClick={() =>
                    saveEnvMut.mutate({
                      updates: {
                        RADAR_LLM_BASE_URL: llmBaseUrl.trim(),
                        RADAR_LLM_MODEL: llmModel.trim(),
                        RADAR_THINKING_LEVEL: thinking,
                        RADAR_STREAM_CHAT: streamChat ? "true" : "false",
                        RADAR_KEYWORD_ONLY: keywordOnly ? "true" : "false",
                        RADAR_EXTRA_SOURCES: extraSources.join(","),
                        RADAR_SEARXNG_URL: searxngUrl.trim(),
                        RADAR_JINA_READER: jinaReader ? "true" : "false",
                      },
                    })
                  }
                >
                  保存参数到 .env
                </Button>
              </div>
            </div>

            {/* External MCP */}
            <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 font-semibold">
                  <Server className="h-3.5 w-3.5" /> 外部 stdio MCP 服务
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[11px] text-[var(--muted-foreground)]">开源预设：</span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-6 px-2 text-[11px]"
                    onClick={() => {
                      setMcpName("fetch")
                      setMcpCmd("uvx")
                      setMcpArgs("mcp-server-fetch")
                    }}
                  >
                    + mcp-server-fetch
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-6 px-2 text-[11px]"
                    onClick={() => {
                      setMcpName("searxng")
                      setMcpCmd("npx")
                      setMcpArgs("-y mcp-searxng")
                      setMcpEnv("SEARXNG_URL")
                    }}
                  >
                    + mcp-searxng
                  </Button>
                </div>
              </div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
                <Input placeholder="名称" value={mcpName} onChange={(e) => setMcpName(e.target.value)} />
                <Input placeholder="命令（python / npx）" value={mcpCmd} onChange={(e) => setMcpCmd(e.target.value)} />
                <Input placeholder="参数（空格分隔）" value={mcpArgs} onChange={(e) => setMcpArgs(e.target.value)} />
                <Button size="sm" disabled={!mcpName.trim() || !mcpCmd.trim()} onClick={() => addMcpMut.mutate()}>添加 MCP</Button>
              </div>
              {mcpProbeOut && <div className="text-[11px] text-[var(--accent)]">{mcpProbeOut}</div>}
              {(mcpQuery.data?.servers || []).map((srv) => (
                <div key={srv.id} className="flex items-center justify-between rounded border border-[var(--border)] bg-[var(--surface)] px-3 py-2">
                  <div>
                    <span className="font-semibold">{srv.name}</span>{" "}
                    <code className="text-[11px] text-[var(--muted-foreground)]">{srv.command} {(srv.args || []).join(" ")}</code>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={async () => {
                        try {
                          const r = await apiFetch<{ tools: any[] }>(`/api/mcp-servers/${srv.id}/probe`, { method: "POST" })
                          setMcpProbeOut(`${srv.name} 可用工具：${r.tools.map((t) => t.name).join("、") || "无"}`)
                        } catch (e: any) {
                          setMcpProbeOut(`探测失败：${e?.message}`)
                        }
                      }}
                    >
                      探测
                    </Button>
                    <Button
                      size="sm"
                      variant={srv.auto_approve ? "default" : "outline"}
                      onClick={() =>
                        apiFetch(`/api/mcp-servers/${srv.id}`, {
                          method: "PATCH",
                          body: JSON.stringify({ auto_approve: !srv.auto_approve }),
                        }).then(() => mcpQuery.refetch())
                      }
                      title="开启后，模型调用该 MCP 服务工具将跳过确认卡直接执行"
                    >
                      {srv.auto_approve ? "免确认" : "需确认"}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => apiFetch(`/api/mcp-servers/${srv.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !srv.enabled }) }).then(() => mcpQuery.refetch())}
                    >
                      {srv.enabled ? "禁用" : "启用"}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => apiFetch(`/api/mcp-servers/${srv.id}`, { method: "DELETE" }).then(() => mcpQuery.refetch())}
                    >
                      删除
                    </Button>
                  </div>
                </div>
              ))}
            </div>

            {/* Secrets */}
            <div className="rounded-md border border-[var(--border)] bg-[var(--background)] p-4 space-y-3">
              <div className="font-semibold">API 密钥与通知凭据（脱敏显示，不回显明文）</div>
              <div className="space-y-2">
                {(diagQuery.data.secrets || []).map((sec: any) => (
                  <div key={sec.key} className="grid grid-cols-12 items-center gap-2">
                    <div className="col-span-4">
                      <div className="font-medium">{sec.label}</div>
                      <div className="text-[10px] text-[var(--muted-foreground)]">{sec.masked}</div>
                    </div>
                    <div className="col-span-6">
                      <Input
                        type="password"
                        placeholder={sec.description}
                        value={secretInputs[sec.key] || ""}
                        onChange={(e) => setSecretInputs((p) => ({ ...p, [sec.key]: e.target.value }))}
                      />
                    </div>
                    <label className="col-span-2 flex items-center justify-end gap-1.5 text-[11px] cursor-pointer">
                      <input
                        type="checkbox"
                        checked={Boolean(secretClears[sec.key])}
                        onChange={(e) => setSecretClears((p) => ({ ...p, [sec.key]: e.target.checked }))}
                      />
                      清除
                    </label>
                  </div>
                ))}
              </div>
              <div className="flex justify-end">
                <Button
                  size="sm"
                  onClick={() => {
                    const updates = Object.fromEntries(
                      Object.entries(secretInputs).filter(([, v]) => v.trim().length > 0)
                    )
                    const clear_keys = Object.entries(secretClears)
                      .filter(([, v]) => v)
                      .map(([k]) => k)
                    if (Object.keys(updates).length > 0 || clear_keys.length > 0) {
                      saveEnvMut.mutate({ updates, clear_keys })
                    }
                  }}
                >
                  更新密钥配置
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
