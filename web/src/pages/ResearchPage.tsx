import { useState } from "react"
import { Link, Navigate, useSearchParams } from "react-router-dom"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Download, FolderKanban, GitCompare, Sparkles, TrendingUp, Upload } from "lucide-react"
import { apiFetch, ResearchJobItem } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input, Textarea } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/controls"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { MarkdownView } from "@/components/domain/MarkdownView"
import { ResearchCard } from "@/components/domain/ResearchCard"

export function ResearchPage() {
  const { topicId } = useApp()
  const [searchParams] = useSearchParams()
  if (searchParams.get("tab") === "trends") {
    return <Navigate to="/trends" replace />
  }
  const [tab, setTab] = useState("gap")
  const [notice, setNotice] = useState("")

  // Project creation
  const [projOpen, setProjOpen] = useState(false)
  const [pName, setPName] = useState("")
  const [pQ, setPQ] = useState("")
  const [pMethod, setPMethod] = useState("")
  const [pDs, setPDs] = useState("")
  const [pMs, setPMs] = useState("")
  const [selectedProjId, setSelectedProjId] = useState("")

  // Gap & Zotero
  const [gapProjId, setGapProjId] = useState("__none__")
  const [zotCol, setZotCol] = useState("")

  const jobsQuery = useQuery({
    queryKey: ["research-jobs", topicId],
    queryFn: () => apiFetch<{ jobs: ResearchJobItem[] }>(`/api/research/jobs?topic_id=${encodeURIComponent(topicId)}`),
    enabled: Boolean(topicId),
    refetchInterval: 4000,
  })

  const projectsQuery = useQuery({
    queryKey: ["projects", topicId],
    queryFn: () => apiFetch<{ projects: any[] }>(`/api/projects?topic_id=${encodeURIComponent(topicId)}`),
    enabled: Boolean(topicId),
  })

  const projects = projectsQuery.data?.projects || []
  const activeProjId = selectedProjId || projects[0]?.id || ""

  const projLinksQuery = useQuery({
    queryKey: ["project-links", activeProjId],
    queryFn: () => apiFetch<{ links: any[]; comparison_matrix: any[] }>(`/api/projects/${activeProjId}/links`),
    enabled: Boolean(activeProjId),
  })

  const gapQuery = useQuery({
    queryKey: ["gap-history", topicId],
    queryFn: () => apiFetch<{ analyses: any[] }>(`/api/gap?topic_id=${encodeURIComponent(topicId)}`),
    enabled: Boolean(topicId),
  })

  const createProjMut = useMutation({
    mutationFn: () =>
      apiFetch(`/api/projects?topic_id=${encodeURIComponent(topicId)}`, {
        method: "POST",
        body: JSON.stringify({
          name: pName,
          question: pQ,
          current_method: pMethod,
          datasets: pDs.split(",").map((s) => s.trim()).filter(Boolean),
          metrics: pMs.split(",").map((s) => s.trim()).filter(Boolean),
        }),
      }),
    onSuccess: (res: any) => {
      setProjOpen(false)
      setPName(""); setPQ(""); setPMethod(""); setPDs(""); setPMs("")
      setSelectedProjId(res.id)
      projectsQuery.refetch()
      setNotice("科研项目档案已保存")
    },
    onError: (e: any) => setNotice(e?.message || "保存失败"),
  })

  const compareMut = useMutation({
    mutationFn: (pid: string) => apiFetch(`/api/projects/${pid}/compare?relink=true`, { method: "POST" }),
    onSuccess: () => {
      projLinksQuery.refetch()
      setNotice("已完成关联匹配与方法矩阵刷新")
    },
  })

  const gapMut = useMutation({
    mutationFn: () =>
      apiFetch("/api/gap", {
        method: "POST",
        body: JSON.stringify({ topic_id: topicId, project_id: gapProjId === "__none__" ? null : gapProjId }),
      }),
    onSuccess: () => {
      gapQuery.refetch()
      setNotice("Research Gap 反向查证已完成")
    },
  })

  const zotPullMut = useMutation({
    mutationFn: () => apiFetch<any>("/api/zotero/pull", { method: "POST", body: JSON.stringify({ topic_id: topicId, collection_key: zotCol }) }),
    onSuccess: (r) => setNotice(`从 Zotero 导入 ${r.imported} 篇`),
    onError: (e: any) => setNotice(e?.message || "Zotero 导入失败"),
  })

  const zotPushMut = useMutation({
    mutationFn: () => apiFetch<any>("/api/zotero/push", { method: "POST", body: JSON.stringify({ topic_id: topicId, collection_key: zotCol }) }),
    onSuccess: (r) => setNotice(`已推送 ${r.exported} 篇收藏至 Zotero`),
    onError: (e: any) => setNotice(e?.message || "Zotero 推送失败"),
  })

  if (!topicId) {
    return <div className="p-8 text-xs text-[var(--muted-foreground)]">请先选择或创建研究主题。</div>
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] pb-4">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wider text-[var(--muted-foreground)]">
            深度研究与项目
          </div>
          <h1 className="font-serif-editorial mt-0.5 text-2xl font-semibold">专题研究、Gap 查证与方法对比矩阵</h1>
        </div>
        <div className="flex items-center gap-2">
          {notice && <Badge variant="accent">{notice}</Badge>}
          <Link to="/trends">
            <Button size="sm" variant="outline">
              <TrendingUp className="h-3.5 w-3.5" />
              前往趋势与热点总览
            </Button>
          </Link>
        </div>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="gap">专题研究与 Gap</TabsTrigger>
          <TabsTrigger value="projects">项目档案与方法矩阵</TabsTrigger>
        </TabsList>

        {/* Tab 1: Deep Research Jobs & Gap */}
        <TabsContent value="gap" className="space-y-6 pt-2">
          <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-xs font-semibold">Research Gap 反向查证与实验设计</h2>
                <p className="mt-0.5 text-[11px] text-[var(--muted-foreground)]">
                  自动汇总当前主题文献证据，在外部数据库反向查证研究空白是否已有先例。
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Select value={gapProjId} onValueChange={setGapProjId}>
                  <SelectTrigger className="w-44">
                    <SelectValue placeholder="关联项目" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">仅基于主题分析</SelectItem>
                    {projects.map((p) => (
                      <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button size="sm" disabled={gapMut.isPending} onClick={() => gapMut.mutate()}>
                  <Sparkles className="h-3.5 w-3.5" />
                  {gapMut.isPending ? "查证中…" : "运行 Gap 查证"}
                </Button>
              </div>
            </div>
          </div>

          {(jobsQuery.data?.jobs || []).length > 0 && (
            <div className="space-y-2">
              <h3 className="text-xs font-semibold text-[var(--muted-foreground)]">后台专题研究任务</h3>
              {jobsQuery.data!.jobs.map((job) => (
                <ResearchCard key={job.id} job={job} onChanged={() => jobsQuery.refetch()} />
              ))}
            </div>
          )}

          {(gapQuery.data?.analyses || []).length > 0 && (
            <div className="space-y-3">
              <h3 className="text-xs font-semibold text-[var(--muted-foreground)]">历史 Gap 分析报告</h3>
              {gapQuery.data!.analyses.map((g) => (
                <div key={g.id} className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-4">
                  <div className="mb-2 flex items-center justify-between text-xs">
                    <span className="font-medium tabular-nums">{g.created_at?.slice(0, 19)}</span>
                    <Badge variant={g.status === "succeeded" ? "success" : "outline"}>{g.status}</Badge>
                  </div>
                  {g.markdown && <MarkdownView content={g.markdown} />}
                </div>
              ))}
            </div>
          )}
        </TabsContent>

        {/* Tab 2: Projects, Method Matrix & Zotero */}
        <TabsContent value="projects" className="space-y-5 pt-2">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Dialog open={projOpen} onOpenChange={setProjOpen}>
                <DialogTrigger asChild>
                  <Button size="sm">
                    <FolderKanban className="h-3.5 w-3.5" />
                    新建科研项目档案
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>新建科研项目档案</DialogTitle>
                  </DialogHeader>
                  <div className="mt-3 space-y-2.5 text-xs">
                    <Input placeholder="项目名称" value={pName} onChange={(e) => setPName(e.target.value)} />
                    <Textarea placeholder="研究问题" value={pQ} onChange={(e) => setPQ(e.target.value)} />
                    <Textarea placeholder="当前方法与思路" value={pMethod} onChange={(e) => setPMethod(e.target.value)} />
                    <Input placeholder="数据集（逗号分隔）" value={pDs} onChange={(e) => setPDs(e.target.value)} />
                    <Input placeholder="评价指标（逗号分隔）" value={pMs} onChange={(e) => setPMs(e.target.value)} />
                    <div className="flex justify-end gap-2">
                      <Button variant="outline" onClick={() => setProjOpen(false)}>取消</Button>
                      <Button disabled={createProjMut.isPending} onClick={() => createProjMut.mutate()}>保存项目</Button>
                    </div>
                  </div>
                </DialogContent>
              </Dialog>
            </div>

            {/* Compact Zotero Sync Strip */}
            <div className="flex items-center gap-2">
              <Input
                className="w-48 h-8"
                placeholder="Zotero Collection Key（可选）"
                value={zotCol}
                onChange={(e) => setZotCol(e.target.value)}
              />
              <Button size="sm" variant="outline" disabled={zotPullMut.isPending} onClick={() => zotPullMut.mutate()}>
                <Download className="h-3.5 w-3.5" /> 导入
              </Button>
              <Button size="sm" variant="outline" disabled={zotPushMut.isPending} onClick={() => zotPushMut.mutate()}>
                <Upload className="h-3.5 w-3.5" /> 推送收藏
              </Button>
            </div>
          </div>

          {projects.length === 0 ? (
            <div className="rounded-md border border-dashed border-[var(--border)] p-8 text-center text-xs text-[var(--muted-foreground)]">
              当前主题下暂无科研项目档案。
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
              <div className="lg:col-span-4 space-y-2">
                {projects.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => setSelectedProjId(p.id)}
                    className={`w-full rounded-md border p-3 text-left text-xs cursor-pointer transition-colors ${
                      p.id === activeProjId
                        ? "border-[var(--primary)] bg-[var(--surface)]"
                        : "border-[var(--border)] bg-[var(--surface)]/50 hover:bg-[var(--surface)]"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold">{p.name}</span>
                      <Badge variant="outline">v{p.revision}</Badge>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[11px] text-[var(--muted-foreground)]">{p.question}</p>
                  </button>
                ))}
              </div>

              <div className="lg:col-span-8 space-y-4">
                {activeProjId && (
                  <div className="rounded-md border border-[var(--border)] bg-[var(--surface)] p-4 space-y-4">
                    <div className="flex items-center justify-between">
                      <h3 className="text-xs font-semibold">同协议方法对比矩阵与关联论文</h3>
                      <Button size="sm" variant="outline" disabled={compareMut.isPending} onClick={() => compareMut.mutate(activeProjId)}>
                        <GitCompare className="h-3.5 w-3.5" />
                        自动关联并刷新矩阵
                      </Button>
                    </div>

                    {(projLinksQuery.data?.comparison_matrix || []).map((grp: any, idx: number) => (
                      <div key={idx} className="space-y-1.5">
                        <div className="text-[11px] font-medium text-[var(--muted-foreground)]">
                          {grp.dataset} · {grp.split} · {grp.metric} · {grp.protocol}
                        </div>
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>论文</TableHead>
                              <TableHead>指标值</TableHead>
                              <TableHead>证据</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {(grp.entries || []).map((e: any, ei: number) => (
                              <TableRow key={ei}>
                                <TableCell className="font-medium">{e.title || e.paper_id}</TableCell>
                                <TableCell className="tabular-nums font-semibold">{e.value}</TableCell>
                                <TableCell className="text-[var(--muted-foreground)]">{e.evidence_id}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    ))}

                    {(projLinksQuery.data?.links || []).length > 0 && (
                      <div className="space-y-1.5">
                        <div className="text-[11px] font-medium text-[var(--muted-foreground)]">关联文献</div>
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>论文</TableHead>
                              <TableHead>关系</TableHead>
                              <TableHead>说明</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {projLinksQuery.data!.links.slice(0, 12).map((l: any, li: number) => (
                              <TableRow key={li}>
                                <TableCell className="font-medium">{l.title}</TableCell>
                                <TableCell><Badge variant="outline">{l.relation_type}</Badge></TableCell>
                                <TableCell className="text-[var(--muted-foreground)]">{l.notes}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  )
}
