import React from "react"
import { NavLink } from "react-router-dom"
import { useMutation } from "@tanstack/react-query"
import {
  BookOpen, Compass, FileText, MessageSquare, Moon, PanelLeftClose, PanelLeftOpen, Play, Radar,
  Search, Settings, Sparkles, Sun, TrendingUp,
} from "lucide-react"
import { apiFetch } from "@/lib/api"
import { useApp } from "@/lib/app-context"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/controls"
import { CommandPalette } from "./CommandPalette"
import { CopilotDrawer } from "./CopilotDrawer"

const NAV_ITEMS = [
  { to: "/briefing", label: "今日简报", icon: FileText },
  { to: "/workbench", label: "文献工作台", icon: BookOpen },
  { to: "/trends", label: "趋势与热点", icon: TrendingUp },
  { to: "/research", label: "深度研究与项目", icon: Compass },
  { to: "/assistant", label: "对话助手", icon: MessageSquare },
  { to: "/settings", label: "设置与运行", icon: Settings },
]

export function AppShell({ children }: { children: React.ReactNode }) {
  const {
    activeTopics, topicId, setTopicId, darkMode, setDarkMode, compactMode, setCompactMode,
    copilotOpen, setCopilotOpen, setCommandOpen, embeddingReady, activeRunCount,
  } = useApp()
  const [collapsed, setCollapsed] = React.useState(false)
  const [scanMsg, setScanMsg] = React.useState("")

  const scanMut = useMutation({
    mutationFn: () => apiFetch<{ run_id: string; status: string }>(`/api/topics/${topicId}/runs`, { method: "POST" }),
    onSuccess: (res) => setScanMsg(`已提交检索（${res.status}）`),
    onError: (e: any) => setScanMsg(e?.message || "提交失败"),
  })

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[var(--background)] text-[var(--foreground)]">
      {/* Left Navigation Sidebar */}
      <aside
        className={cn(
          "flex shrink-0 flex-col border-r border-[var(--border)] bg-[var(--surface)] transition-[width] duration-150",
          collapsed ? "w-14" : "w-56"
        )}
      >
        <div className="flex h-12 items-center justify-between border-b border-[var(--border)] px-3">
          {!collapsed && (
            <div className="flex items-center gap-2">
              <Radar className="h-4 w-4 text-[var(--primary)]" aria-hidden="true" />
              <span className="text-sm font-semibold tracking-tight">ResearchRadar</span>
            </div>
          )}
          <Button
            size="icon"
            variant="ghost"
            className="h-7 w-7"
            onClick={() => setCollapsed((v) => !v)}
            aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
          >
            {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </Button>
        </div>

        <nav className="flex-1 space-y-0.5 p-2" aria-label="主导航">
          {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-xs font-medium transition-colors",
                  isActive
                    ? "bg-[var(--muted)] text-[var(--foreground)] font-semibold"
                    : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]/60 hover:text-[var(--foreground)]",
                  collapsed && "justify-center px-0"
                )
              }
              title={collapsed ? label : undefined}
            >
              <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
              {!collapsed && <span>{label}</span>}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-[var(--border)] p-2.5 text-[11px] text-[var(--muted-foreground)] space-y-2">
          {!collapsed && (
            <div className="flex items-center justify-between px-1">
              <span className="flex items-center gap-1.5">
                <span
                  className={cn(
                    "inline-block h-1.5 w-1.5 rounded-full",
                    embeddingReady ? "bg-[var(--success)]" : "bg-[var(--warning)]"
                  )}
                />
                {embeddingReady ? "BGE 就绪" : "仅关键词"}
              </span>
              {activeRunCount > 0 && (
                <span className="tabular-nums text-[var(--accent)]">运行中 {activeRunCount}</span>
              )}
            </div>
          )}
          <div className={cn("flex items-center gap-1", collapsed ? "flex-col" : "justify-between")}>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 px-2"
              onClick={() => setDarkMode(!darkMode)}
              title="切换深/浅色"
            >
              {darkMode ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              {!collapsed && <span>{darkMode ? "浅色" : "深色"}</span>}
            </Button>
            {!collapsed && (
              <Button
                size="sm"
                variant="ghost"
                className="h-7 px-2"
                onClick={() => setCompactMode(!compactMode)}
              >
                {compactMode ? "标准" : "紧凑"}
              </Button>
            )}
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* Global Top Header */}
        <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--surface)] px-4">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="hidden sm:inline text-[11px] font-medium text-[var(--muted-foreground)]">当前主题</span>
            {activeTopics.length > 0 ? (
              <Select value={topicId} onValueChange={setTopicId}>
                <SelectTrigger className="w-56 h-8">
                  <SelectValue placeholder="选择研究主题" />
                </SelectTrigger>
                <SelectContent>
                  {activeTopics.map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      {t.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <span className="text-xs text-[var(--muted-foreground)]">暂无活跃主题</span>
            )}

            <button
              type="button"
              onClick={() => setCommandOpen(true)}
              className="hidden md:flex h-8 w-64 items-center justify-between rounded-md border border-[var(--border)] bg-[var(--background)] px-2.5 text-xs text-[var(--muted-foreground)] hover:border-[var(--primary)]/40 cursor-pointer"
            >
              <span className="flex items-center gap-1.5">
                <Search className="h-3.5 w-3.5" aria-hidden="true" />
                <span>快速检索论文、跳转…</span>
              </span>
              <kbd className="rounded border border-[var(--border)] bg-[var(--surface)] px-1 text-[10px]">Ctrl K</kbd>
            </button>
          </div>

          <div className="flex items-center gap-2">
            {scanMsg && <span className="text-[11px] text-[var(--muted-foreground)]">{scanMsg}</span>}
            {topicId && (
              <Button
                size="sm"
                variant="outline"
                disabled={scanMut.isPending}
                onClick={() => scanMut.mutate()}
              >
                <Play className="h-3 w-3" />
                检索一次
              </Button>
            )}
            <Button
              size="sm"
              variant={copilotOpen ? "default" : "outline"}
              onClick={() => setCopilotOpen(!copilotOpen)}
            >
              <Sparkles className="h-3.5 w-3.5" />
              Copilot
            </Button>
          </div>
        </header>

        <div className="flex min-h-0 flex-1 overflow-hidden">
          <main className="min-w-0 flex-1 overflow-y-auto">{children}</main>
          <CopilotDrawer />
        </div>
      </div>

      <CommandPalette />
    </div>
  )
}
