import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { MessageSquare, Plus } from "lucide-react"
import { apiFetch } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { ChatPanel } from "@/components/domain/ChatPanel"

export function AssistantPage() {
  const { topicId } = useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const [currentId, setCurrentId] = useState(searchParams.get("conversation") || "")
  const [isDraft, setIsDraft] = useState(false)

  const convsQuery = useQuery({
    queryKey: ["conversations"],
    queryFn: () => apiFetch<{ conversations: Array<{ id: string; title: string; context: any }> }>("/api/conversations"),
  })

  const convs = convsQuery.data?.conversations || []

  useEffect(() => {
    if (isDraft) return
    if (convs.length > 0 && (!currentId || !convs.some((c) => c.id === currentId))) {
      setCurrentId(convs[0].id)
    } else if (convsQuery.isSuccess && convs.length === 0) {
      setIsDraft(true)
      setCurrentId("")
    }
  }, [convs, convsQuery.isSuccess, currentId, isDraft])

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      {/* Compact Conversation History Sidebar */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--background)]">
        <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2.5">
          <span className="text-xs font-semibold">会话记录</span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setIsDraft(true)
              setCurrentId("")
              setSearchParams({})
            }}
          >
            <Plus className="h-3.5 w-3.5" />
            新对话
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-1.5 space-y-0.5">
          {convs.length === 0 && (
            <div className="px-2.5 py-3 text-[11px] text-[var(--muted-foreground)]">
              暂无历史对话，发送首条提问后将自动记录在此。
            </div>
          )}
          {convs.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => {
                setIsDraft(false)
                setCurrentId(c.id)
                setSearchParams({ conversation: c.id })
              }}
              className={cn(
                "flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-xs transition-colors cursor-pointer",
                !isDraft && c.id === currentId
                  ? "bg-[var(--surface)] font-medium text-[var(--foreground)] border border-[var(--border)]"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--surface)]/60 hover:text-[var(--foreground)]"
              )}
            >
              <MessageSquare className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{c.title}</span>
            </button>
          ))}
        </div>
      </aside>

      <div className="min-w-0 flex-1 bg-[var(--surface)]">
        {currentId || isDraft ? (
          <ChatPanel
            conversationId={isDraft ? "" : currentId}
            topicId={topicId}
            onConversationCreated={(id) => {
              setIsDraft(false)
              setCurrentId(id)
              setSearchParams({ conversation: id })
            }}
          />
        ) : (
          <div className="p-8 text-xs text-[var(--muted-foreground)]">正在加载对话…</div>
        )}
      </div>
    </div>
  )
}
