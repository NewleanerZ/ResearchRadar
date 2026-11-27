import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, MessageSquare, Pencil, Plus, Trash2, X } from "lucide-react"
import { apiFetch } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ChatPanel } from "@/components/domain/ChatPanel"

export function AssistantPage() {
  const { topicId } = useApp()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [currentId, setCurrentId] = useState(searchParams.get("conversation") || "")
  const [isDraft, setIsDraft] = useState(false)
  const [editingId, setEditingId] = useState("")
  const [editingTitle, setEditingTitle] = useState("")

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

  const renameMut = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      apiFetch(`/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
    onSuccess: () => {
      setEditingId("")
      queryClient.invalidateQueries({ queryKey: ["conversations"] })
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/conversations/${id}`, { method: "DELETE" }),
    onSuccess: (_, id) => {
      if (id === currentId) {
        setCurrentId("")
        setSearchParams({})
      }
      queryClient.invalidateQueries({ queryKey: ["conversations"] })
    },
  })

  const startNew = () => {
    setIsDraft(true)
    setCurrentId("")
    setSearchParams({})
  }

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      {/* Compact Conversation History Sidebar */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--background)]">
        <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2.5">
          <span className="text-xs font-semibold">会话记录</span>
          <Button size="sm" variant="outline" onClick={startNew}>
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
            <div
              key={c.id}
              className={cn(
                "group flex items-center gap-1 rounded-md px-2 py-1.5 text-xs transition-colors",
                !isDraft && c.id === currentId
                  ? "bg-[var(--surface)] font-medium text-[var(--foreground)] border border-[var(--border)]"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--surface)]/60 hover:text-[var(--foreground)]"
              )}
            >
              {editingId === c.id ? (
                <div className="flex flex-1 items-center gap-1">
                  <Input
                    value={editingTitle}
                    onChange={(e) => setEditingTitle(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && editingTitle.trim()) {
                        renameMut.mutate({ id: c.id, title: editingTitle.trim() })
                      } else if (e.key === "Escape") {
                        setEditingId("")
                      }
                    }}
                    className="h-6 px-1.5 text-xs"
                    autoFocus
                  />
                  <button
                    type="button"
                    onClick={() => editingTitle.trim() && renameMut.mutate({ id: c.id, title: editingTitle.trim() })}
                    className="p-0.5 hover:text-[var(--accent)] cursor-pointer"
                    aria-label="保存标题"
                  >
                    <Check className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditingId("")}
                    className="p-0.5 hover:text-[var(--destructive)] cursor-pointer"
                    aria-label="取消重命名"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ) : (
                <>
                  <button
                    type="button"
                    onClick={() => {
                      setIsDraft(false)
                      setCurrentId(c.id)
                      setSearchParams({ conversation: c.id })
                    }}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left cursor-pointer py-0.5"
                  >
                    <MessageSquare className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span className="truncate">{c.title}</span>
                  </button>
                  <div className="hidden shrink-0 items-center gap-0.5 group-hover:flex">
                    <button
                      type="button"
                      onClick={() => {
                        setEditingId(c.id)
                        setEditingTitle(c.title)
                      }}
                      className="rounded p-1 hover:bg-[var(--muted)] hover:text-[var(--foreground)] cursor-pointer"
                      title="重命名会话"
                      aria-label="重命名会话"
                    >
                      <Pencil className="h-3 w-3" />
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteMut.mutate(c.id)}
                      className="rounded p-1 hover:bg-[var(--muted)] hover:text-[var(--destructive)] cursor-pointer"
                      title="删除会话"
                      aria-label="删除会话"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </>
              )}
            </div>
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
            onNewConversation={startNew}
          />
        ) : (
          <div className="p-8 text-xs text-[var(--muted-foreground)]">正在加载对话…</div>
        )}
      </div>
    </div>
  )
}
