import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { ExternalLink, Plus, Sparkles, X } from "lucide-react"
import { Link } from "react-router-dom"
import { apiFetch } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { Button } from "@/components/ui/button"
import { ChatPanel } from "@/components/domain/ChatPanel"

export function CopilotDrawer() {
  const { copilotOpen, setCopilotOpen, topicId, copilotPromptSeed, askCopilot } = useApp()
  const [chatId, setChatId] = useState("")
  const [isDraft, setIsDraft] = useState(false)
  const [seed, setSeed] = useState("")

  const convsQuery = useQuery({
    queryKey: ["conversations"],
    queryFn: () => apiFetch<{ conversations: Array<{ id: string; title: string }> }>("/api/conversations"),
    enabled: copilotOpen,
  })

  useEffect(() => {
    if (!copilotOpen || isDraft) return
    const list = convsQuery.data?.conversations || []
    if (!chatId && list.length > 0) {
      setChatId(list[0].id)
    } else if (!chatId && convsQuery.isSuccess && list.length === 0) {
      setIsDraft(true)
    }
  }, [copilotOpen, convsQuery.data, convsQuery.isSuccess, chatId, isDraft])

  useEffect(() => {
    if (copilotPromptSeed) {
      setSeed(copilotPromptSeed)
      askCopilot("")
    }
  }, [copilotPromptSeed])

  if (!copilotOpen) return null

  return (
    <aside
      aria-label="全局 AI Copilot"
      className="flex h-full w-[380px] shrink-0 flex-col border-l border-[var(--border)] bg-[var(--surface)]"
    >
      <div className="flex h-12 items-center justify-between border-b border-[var(--border)] px-3">
        <div className="flex items-center gap-1.5 text-xs font-semibold">
          <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" aria-hidden="true" />
          <span>AI Copilot</span>
        </div>
        <div className="flex items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setIsDraft(true)
              setChatId("")
            }}
            title="新建会话"
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
          <Button size="sm" variant="ghost" asChild title="全屏打开对话助手">
            <Link to="/assistant" onClick={() => setCopilotOpen(false)}>
              <ExternalLink className="h-3.5 w-3.5" />
            </Link>
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setCopilotOpen(false)} aria-label="关闭 Copilot">
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1">
        {chatId || isDraft ? (
          <ChatPanel
            conversationId={isDraft ? "" : chatId}
            topicId={topicId}
            initialPrompt={seed}
            compact
            onPromptSeedConsumed={() => setSeed("")}
            onConversationCreated={(id) => {
              setIsDraft(false)
              setChatId(id)
            }}
            onNewConversation={() => {
              setIsDraft(true)
              setChatId("")
            }}
          />
        ) : (
          <div className="p-4 text-xs text-[var(--muted-foreground)]">正在准备会话…</div>
        )}
      </div>
    </aside>
  )
}
