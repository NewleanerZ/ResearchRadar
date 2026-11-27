import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Copy, Check, FileText, Search } from "lucide-react"
import { apiFetch, ArtifactDetail } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { MarkdownView } from "./MarkdownView"

export function ArtifactDrawer({
  conversationId,
  artifactId,
  onClose,
}: {
  conversationId: string
  artifactId: string | null
  onClose: () => void
}) {
  const [filter, setFilter] = useState("")
  const [copied, setCopied] = useState(false)

  const query = useQuery({
    queryKey: ["artifact-detail", conversationId, artifactId],
    queryFn: () =>
      apiFetch<ArtifactDetail>(`/api/conversations/${conversationId}/artifacts/${artifactId}`),
    enabled: Boolean(conversationId && artifactId),
  })

  const displayContent = useMemo(() => {
    const raw = query.data?.content || ""
    if (!filter.trim()) return raw
    const q = filter.trim().toLowerCase()
    const lines = raw.split("\n").filter((line) => line.toLowerCase().includes(q))
    return lines.length > 0 ? lines.join("\n") : "（未匹配到包含该关键词的内容行）"
  }, [query.data?.content, filter])

  const copyAll = async () => {
    if (!query.data?.content) return
    await navigator.clipboard.writeText(query.data.content)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <Dialog open={Boolean(artifactId)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh] flex flex-col gap-3">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-6">
            <FileText className="h-4 w-4 text-[var(--accent)]" />
            <span className="truncate">{query.data?.title || "产物详情"}</span>
            {query.data && (
              <Badge variant="outline" className="text-[10px]">
                {query.data.tool_name} · {query.data.char_count} 字符
              </Badge>
            )}
          </DialogTitle>
        </DialogHeader>

        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-[var(--muted-foreground)]" />
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="按关键词过滤产物内容…"
              className="pl-8 text-xs"
            />
          </div>
          <Button size="sm" variant="outline" onClick={copyAll} disabled={!query.data?.content}>
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            {copied ? "已复制" : "复制原文"}
          </Button>
        </div>

        <div className="min-h-[260px] flex-1 overflow-y-auto rounded border border-[var(--border)] bg-[var(--background)] p-3 text-xs">
          {query.isLoading ? (
            <div className="text-[var(--muted-foreground)]">正在加载完整产物…</div>
          ) : query.isError ? (
            <div className="text-[var(--destructive)]">加载产物失败</div>
          ) : query.data?.mime_type === "text/markdown" && !filter.trim() ? (
            <MarkdownView content={displayContent} />
          ) : (
            <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed">
              {displayContent}
            </pre>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
