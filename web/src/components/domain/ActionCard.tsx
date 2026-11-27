import { useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Check, X } from "lucide-react"
import { apiFetch, RUN_LABELS } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { TimelineAccordion } from "./TimelineDrawer"

const FIELD_NAMES: Record<string, string> = {
  name: "名称",
  description: "研究方向",
  keywords: "核心检索词",
  excluded: "排除词",
  expansions: "已确认扩展词",
  proposed_expansions: "待确认扩展词",
  categories: "arXiv 分类",
  subtopics: "子主题",
  timezone: "时区",
  schedule: "检索时间",
  enabled: "关注",
  archived: "已删除",
  notify: "飞书通知",
}

function displayValue(val: any): string {
  if (val === null || val === undefined) return "—"
  if (typeof val === "boolean") return val ? "是" : "否"
  if (Array.isArray(val)) return val.join("、")
  if (typeof val === "object") return JSON.stringify(val)
  return String(val)
}

export function ActionCard({
  conversationId,
  action,
  onConfirmed,
}: {
  conversationId: string
  action: { id: string; kind: string; status: string; preview: any; receipt: any }
  onConfirmed: () => void
}) {
  const [error, setError] = useState("")
  const preview = action.preview || {}
  const receipt = action.receipt || {}

  const confirmMutation = useMutation({
    mutationFn: (approve: boolean) =>
      apiFetch(`/api/conversations/${conversationId}/actions/${action.id}/confirm`, {
        method: "POST",
        body: JSON.stringify({ approve }),
      }),
    onSuccess: () => {
      setError("")
      onConfirmed()
    },
    onError: (err: any) => setError(err?.message || "操作失败"),
  })

  const liveRunQuery = useQuery({
    queryKey: ["run-live", receipt.run_id],
    queryFn: () => apiFetch<any>(`/api/runs/${receipt.run_id}`),
    enabled: Boolean(action.status === "applied" && receipt.run_id),
    refetchInterval: (q) => {
      const s = q.state.data?.status
      return s === "queued" || s === "running" ? 2500 : false
    },
  })

  const changes = preview.after
    ? Object.entries(preview.after)
        .filter(([k]) => k in FIELD_NAMES)
        .map(([k, v]) => ({
          field: FIELD_NAMES[k],
          before: displayValue(preview.before?.[k]),
          after: displayValue(v),
        }))
    : []

  return (
    <div className="mt-2 rounded-md border border-[var(--border)] bg-[var(--surface)] p-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-[var(--foreground)]">{preview.label || "待确认操作"}</span>
        <Badge
          variant={
            action.status === "pending"
              ? "warning"
              : action.status === "dismissed"
              ? "outline"
              : "success"
          }
        >
          {action.status === "pending" ? "待确认" : action.status === "dismissed" ? "已取消" : "已执行"}
        </Badge>
      </div>
      {preview.topic && <div className="mt-1 text-[var(--muted-foreground)]">主题：{preview.topic}</div>}
      {preview.start && (
        <div className="mt-1 text-[var(--muted-foreground)]">
          补录范围：{preview.start} 至 {preview.end} · {(preview.sources || ["arXiv", "OpenAlex"]).join(" / ")}
        </div>
      )}
      {Array.isArray(preview.venues) && preview.venues.length > 0 && (
        <div className="mt-1 text-[var(--muted-foreground)]">会议范围：{preview.venues.join("、")}</div>
      )}
      {changes.length > 0 && (
        <div className="mt-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="h-7 px-2">设置项</TableHead>
                <TableHead className="h-7 px-2">修改前</TableHead>
                <TableHead className="h-7 px-2">修改后</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {changes.map((c) => (
                <TableRow key={c.field}>
                  <TableCell className="px-2 py-1 font-medium">{c.field}</TableCell>
                  <TableCell className="px-2 py-1 text-[var(--muted-foreground)]">{c.before}</TableCell>
                  <TableCell className="px-2 py-1 text-[var(--accent)]">{c.after}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {error && <div className="mt-2 text-[var(--destructive)]">{error}</div>}
      {action.status === "pending" && (
        <div className="mt-2.5 flex items-center gap-2">
          <Button
            size="sm"
            disabled={confirmMutation.isPending}
            onClick={() => confirmMutation.mutate(true)}
          >
            <Check className="h-3.5 w-3.5" />
            确认执行
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={confirmMutation.isPending}
            onClick={() => confirmMutation.mutate(false)}
          >
            <X className="h-3.5 w-3.5" />
            取消
          </Button>
        </div>
      )}
      {action.status === "applied" && liveRunQuery.data && (
        <div className="mt-2 space-y-1.5 border-t border-[var(--border)] pt-2">
          <div className="flex items-center justify-between text-[11px]">
            <span>任务 `{liveRunQuery.data.id}`</span>
            <Badge variant={liveRunQuery.data.status === "succeeded" ? "success" : "accent"}>
              {RUN_LABELS[liveRunQuery.data.status] || liveRunQuery.data.status}
            </Badge>
          </div>
          <TimelineAccordion events={liveRunQuery.data.timeline || []} />
        </div>
      )}
    </div>
  )
}
