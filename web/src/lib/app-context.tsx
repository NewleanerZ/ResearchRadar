import React, { createContext, useContext, useEffect, useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { apiFetch, TopicItem } from "./api"

interface AppContextValue {
  topics: TopicItem[]
  activeTopics: TopicItem[]
  topicId: string
  setTopicId: (id: string) => void
  currentTopic?: TopicItem
  darkMode: boolean
  setDarkMode: (v: boolean) => void
  compactMode: boolean
  setCompactMode: (v: boolean) => void
  copilotOpen: boolean
  setCopilotOpen: (v: boolean) => void
  copilotPromptSeed: string
  askCopilot: (prompt: string) => void
  commandOpen: boolean
  setCommandOpen: (v: boolean) => void
  refetchTopics: () => void
  embeddingReady: boolean
  activeRunCount: number
}

const AppContext = createContext<AppContextValue | null>(null)

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [darkMode, setDarkMode] = useState<boolean>(() => localStorage.getItem("rr_dark") === "1")
  const [compactMode, setCompactMode] = useState<boolean>(() => localStorage.getItem("rr_compact") === "1")
  const [topicId, setTopicIdState] = useState<string>(() => {
    const params = new URLSearchParams(window.location.search)
    return params.get("topic") || localStorage.getItem("rr_topic") || ""
  })
  const [copilotOpen, setCopilotOpen] = useState(false)
  const [copilotPromptSeed, setCopilotPromptSeed] = useState("")
  const [commandOpen, setCommandOpen] = useState(false)

  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode)
    localStorage.setItem("rr_dark", darkMode ? "1" : "0")
  }, [darkMode])

  useEffect(() => {
    document.documentElement.setAttribute("data-density", compactMode ? "compact" : "comfortable")
    localStorage.setItem("rr_compact", compactMode ? "1" : "0")
  }, [compactMode])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        setCommandOpen((prev) => !prev)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  const topicsQuery = useQuery({
    queryKey: ["topics"],
    queryFn: () => apiFetch<{ topics: TopicItem[] }>("/api/topics"),
    staleTime: 10_000,
  })

  const diagQuery = useQuery({
    queryKey: ["settings-diagnostics"],
    queryFn: () => apiFetch<{ embedding: { ready: boolean } }>("/api/settings/diagnostics"),
    staleTime: 30_000,
  })

  const runsQuery = useQuery({
    queryKey: ["runs-summary"],
    queryFn: () => apiFetch<{ runs: Array<{ status: string }> }>("/api/runs"),
    refetchInterval: 8000,
  })

  const topics = topicsQuery.data?.topics || []
  const activeTopics = useMemo(() => topics.filter((t) => !t.archived), [topics])

  useEffect(() => {
    if (activeTopics.length > 0 && !activeTopics.some((t) => t.id === topicId)) {
      const next = activeTopics[0].id
      setTopicIdState(next)
      localStorage.setItem("rr_topic", next)
    }
  }, [activeTopics, topicId])

  const setTopicId = (id: string) => {
    setTopicIdState(id)
    localStorage.setItem("rr_topic", id)
  }

  const askCopilot = (prompt: string) => {
    setCopilotPromptSeed(prompt)
    setCopilotOpen(true)
  }

  const currentTopic = activeTopics.find((t) => t.id === topicId)
  const activeRunCount = (runsQuery.data?.runs || []).filter(
    (r) => r.status === "queued" || r.status === "running"
  ).length

  return (
    <AppContext.Provider
      value={{
        topics,
        activeTopics,
        topicId,
        setTopicId,
        currentTopic,
        darkMode,
        setDarkMode,
        compactMode,
        setCompactMode,
        copilotOpen,
        setCopilotOpen,
        copilotPromptSeed,
        askCopilot,
        commandOpen,
        setCommandOpen,
        refetchTopics: () => topicsQuery.refetch(),
        embeddingReady: Boolean(diagQuery.data?.embedding?.ready),
        activeRunCount,
      }}
    >
      {children}
    </AppContext.Provider>
  )
}

export function useApp() {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error("useApp must be used inside AppProvider")
  return ctx
}
