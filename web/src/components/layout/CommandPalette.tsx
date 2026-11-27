import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { Command } from "cmdk"
import { useQuery } from "@tanstack/react-query"
import { BookOpen, Compass, FileText, MessageSquare, Search, Settings, Sparkles } from "lucide-react"
import { apiFetch, PaperListItem } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"

export function CommandPalette() {
  const { commandOpen, setCommandOpen, activeTopics, topicId, setTopicId, askCopilot } = useApp()
  const [search, setSearch] = useState("")
  const navigate = useNavigate()

  const papersQuery = useQuery({
    queryKey: ["cmd-papers", topicId, search],
    queryFn: () =>
      apiFetch<{ papers: PaperListItem[] }>(
        `/api/papers?topic_id=${encodeURIComponent(topicId)}&q=${encodeURIComponent(search)}&limit=8`
      ),
    enabled: commandOpen && Boolean(topicId),
  })

  const go = (path: string) => {
    setCommandOpen(false)
    navigate(path)
  }

  return (
    <Dialog open={commandOpen} onOpenChange={setCommandOpen}>
      <DialogContent className="overflow-hidden p-0 max-w-xl">
        <DialogTitle className="sr-only">全局命令面板</DialogTitle>
        <Command className="bg-[var(--surface)] text-[var(--foreground)]">
          <div className="flex items-center border-b border-[var(--border)] px-3">
            <Search className="mr-2 h-4 w-4 text-[var(--muted-foreground)]" aria-hidden="true" />
            <Command.Input
              value={search}
              onValueChange={setSearch}
              placeholder="搜索论文、切换主题或跳转页面…"
              className="flex h-10 w-full bg-transparent text-xs outline-none placeholder:text-[var(--muted-foreground)]"
            />
          </div>
          <Command.List className="max-h-80 overflow-y-auto p-1.5 text-xs">
            <Command.Empty className="py-6 text-center text-[var(--muted-foreground)]">没有匹配项</Command.Empty>

            <Command.Group heading="页面导航" className="px-1 py-1 text-[11px] font-medium text-[var(--muted-foreground)]">
              <Command.Item onSelect={() => go("/briefing")} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]">
                <FileText className="h-3.5 w-3.5" /> 今日简报
              </Command.Item>
              <Command.Item onSelect={() => go("/workbench")} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]">
                <BookOpen className="h-3.5 w-3.5" /> 文献工作台
              </Command.Item>
              <Command.Item onSelect={() => go("/research")} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]">
                <Compass className="h-3.5 w-3.5" /> 深度研究与项目
              </Command.Item>
              <Command.Item onSelect={() => go("/assistant")} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]">
                <MessageSquare className="h-3.5 w-3.5" /> 对话助手
              </Command.Item>
              <Command.Item onSelect={() => go("/settings")} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]">
                <Settings className="h-3.5 w-3.5" /> 设置与运行
              </Command.Item>
            </Command.Group>

            {activeTopics.length > 0 && (
              <Command.Group heading="切换研究主题" className="px-1 py-1 text-[11px] font-medium text-[var(--muted-foreground)]">
                {activeTopics.map((t) => (
                  <Command.Item
                    key={t.id}
                    onSelect={() => {
                      setTopicId(t.id)
                      setCommandOpen(false)
                    }}
                    className="flex cursor-pointer items-center justify-between rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]"
                  >
                    <span>{t.name}</span>
                    {t.id === topicId && <span className="text-[10px] text-[var(--accent)]">当前</span>}
                  </Command.Item>
                ))}
              </Command.Group>
            )}

            {(papersQuery.data?.papers || []).length > 0 && (
              <Command.Group heading="当前主题论文" className="px-1 py-1 text-[11px] font-medium text-[var(--muted-foreground)]">
                {(papersQuery.data?.papers || []).map((p) => (
                  <Command.Item
                    key={p.paper_id}
                    value={`${p.title} ${p.paper_id}`}
                    onSelect={() => {
                      setCommandOpen(false)
                      navigate(`/workbench?paper=${p.paper_id}`)
                    }}
                    className="flex cursor-pointer items-center justify-between gap-2 rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]"
                  >
                    <span className="truncate">{p.title}</span>
                    <span className="shrink-0 text-[10px] text-[var(--muted-foreground)]">{p.date || ""}</span>
                  </Command.Item>
                ))}
              </Command.Group>
            )}

            <Command.Group heading="快捷 AI" className="px-1 py-1 text-[11px] font-medium text-[var(--muted-foreground)]">
              <Command.Item
                onSelect={() => {
                  setCommandOpen(false)
                  askCopilot(search ? `围绕「${search}」检索并总结最新进展` : "总结当前主题今日重点论文的核心方法对比")
                }}
                className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 aria-selected:bg-[var(--muted)]"
              >
                <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />
                在右侧 Copilot 抽屉中提问
              </Command.Item>
            </Command.Group>
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  )
}
