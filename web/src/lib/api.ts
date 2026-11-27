export interface TopicItem {
  id: string
  name: string
  description: string
  keywords: string[]
  excluded: string[]
  proposed_expansions: string[]
  expansions: string[]
  categories: string[]
  subtopics: Record<string, string[]>
  timezone: string
  schedule: string
  enabled: boolean
  notify: boolean
  archived: boolean
  revision: number
}

export interface PaperRecord {
  source: string
  external_id: string
  title: string
  url: string
  authors: string[]
  abstract?: string | null
  doi?: string | null
  arxiv_id?: string | null
  open_pdf_urls?: string[]
  publication?: { status: string; venue?: string | null } | null
  citation_count?: number | null
}

export interface PaperListItem {
  paper_id: number
  title: string
  date?: string | null
  favorite: boolean
  is_read: boolean
  publication: { status: string; venue?: string | null }
  publication_label: string
  has_deep_analysis: boolean
  has_pdf: boolean
  pdf_status?: "available" | "unavailable" | "unchecked"
  pdf_strategy?: string | null
  pdf_reason?: string | null
  pdf_checked_at?: string | null
  authors: string[]
  records: PaperRecord[]
}

export interface AbstractNote {
  translation: string
  summary: string
  model?: string | null
  created_at: string
}

export interface PaperDetail extends PaperListItem {
  pdf_urls: string[]
  card?: any
  abstract_note?: AbstractNote | null
  deep_analysis?: {
    level: string
    analysis: any
    evidence: any[]
  } | null
  rag_chunks: Array<{
    id: string
    section: string
    page_start?: number | null
    page_end?: number | null
    text: string
  }>
}

export interface TurnItem {
  id: string
  conversation_id: string
  prompt: string
  answer?: string | null
  reasoning?: string | null
  partial_answer?: string | null
  status: string
  events: Array<{ name: string; arguments?: string; result: { status: string; data: any } }>
  usage?: { calls?: number; tokens?: number; cost_usd?: number }
  actions: Array<{ id: string; kind: string; status: string; preview: any; receipt: any }>
  research_jobs: ResearchJobItem[]
  timeline: TimelineEvent[]
  created_at: string
}

export interface StreamTurnMeta {
  activeTools: Record<string, { call_id: string; name: string; status: string; elapsed_ms?: number }>
  subagents: Record<string, { subagent_id: string; role: string; stage: string; detail?: string; elapsed_ms?: number }>
  liveUsage?: { calls?: number; max_calls?: number; tokens?: number; cost_usd?: number }
  followupTurnId?: string
}

export interface ArtifactDetail {
  id: string
  conversation_id: string
  turn_id: string
  tool_name: string
  title: string
  mime_type: string
  char_count: number
  content: string
  created_at: string
}

export interface SteerQueueItem {
  id: string
  turn_id?: string | null
  mode: "steer" | "follow_up"
  prompt: string
  status: string
  created_at: string
}

export interface TimelineEvent {
  stage: string
  status: string
  detail: string
  elapsed_ms?: number | null
  created_at: string
}

export interface ResearchJobItem {
  id: string
  status: string
  stage: string
  cancel_requested: number | boolean
  question: string
  conversation_id?: string
  turn_id?: string
  topic?: string
  after?: string
  before?: string
  scope?: { venues?: string[]; main_only?: boolean }
  project_memory?: { revision?: number }
  candidate_count: number
  pdf_count: number
  markdown?: string | null
  adjustments: Array<{ id: string; status: string; body: Record<string, any> }>
  timeline: TimelineEvent[]
}

const TOKEN_STORAGE_KEY = "rr_api_token"

export function getStoredToken(): string {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || ""
}

export function setStoredToken(token: string) {
  if (token.trim()) {
    localStorage.setItem(TOKEN_STORAGE_KEY, token.trim())
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY)
  }
}

export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getStoredToken()
  const headers = new Headers(init.headers || {})
  if (!headers.has("Content-Type") && init.body && typeof init.body === "string") {
    headers.set("Content-Type", "application/json")
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`)
  }
  const resp = await fetch(path, { ...init, headers })
  if (!resp.ok) {
    let payload: any = null
    try {
      payload = await resp.json()
    } catch {
      payload = null
    }
    throw new ApiError(
      resp.status,
      payload?.code || `http_${resp.status}`,
      payload?.message || `请求失败 (${resp.status})`
    )
  }
  return resp.json() as Promise<T>
}

export function streamTurnEvents(
  turnId: string,
  onUpdate: (turn: TurnItem, complete: boolean, meta: StreamTurnMeta) => void,
  onError?: (err: string) => void
): () => void {
  const controller = new AbortController()
  const token = getStoredToken()
  const headers: Record<string, string> = {}
  if (token) headers["Authorization"] = `Bearer ${token}`

  ;(async () => {
    let currentTurn: TurnItem | null = null
    const meta: StreamTurnMeta = { activeTools: {}, subagents: {} }
    const emit = (complete: boolean) => {
      if (currentTurn) {
        onUpdate(
          { ...currentTurn },
          complete,
          {
            activeTools: { ...meta.activeTools },
            subagents: { ...meta.subagents },
            liveUsage: meta.liveUsage ? { ...meta.liveUsage } : undefined,
            followupTurnId: meta.followupTurnId,
          }
        )
      }
    }

    try {
      const resp = await fetch(`/api/chat/turns/${encodeURIComponent(turnId)}/stream`, {
        headers,
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) {
        onError?.(`流式连接失败 (${resp.status})`)
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const blocks = buffer.split("\n\n")
        buffer = blocks.pop() || ""
        for (const block of blocks) {
          let eventName = "update"
          const dataLines: string[] = []
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) eventName = line.slice(6).trim()
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim())
          }
          if (!dataLines.length) continue
          const dataStr = dataLines.join("\n")
          if (eventName === "error") {
            const parsed = JSON.parse(dataStr)
            onError?.(parsed.message || "流式返回异常")
            return
          }
          const payload = JSON.parse(dataStr)
          if (eventName === "snapshot" || eventName === "update") {
            currentTurn = payload as TurnItem
            emit(false)
          } else if (eventName === "complete") {
            currentTurn = payload as TurnItem
            emit(true)
          } else if (eventName === "reasoning_delta" && currentTurn) {
            currentTurn.reasoning = (currentTurn.reasoning || "") + (payload.delta || "")
            emit(false)
          } else if (eventName === "text_delta" && currentTurn) {
            currentTurn.partial_answer = (currentTurn.partial_answer || "") + (payload.delta || "")
            emit(false)
          } else if (eventName === "tool_start") {
            meta.activeTools[payload.call_id] = {
              call_id: payload.call_id,
              name: payload.name,
              status: "running",
            }
            emit(false)
          } else if (eventName === "tool_done") {
            meta.activeTools[payload.call_id] = {
              call_id: payload.call_id,
              name: payload.name,
              status: payload.status || "ok",
              elapsed_ms: payload.elapsed_ms,
            }
            emit(false)
          } else if (eventName === "subagent_progress") {
            meta.subagents[payload.subagent_id] = {
              subagent_id: payload.subagent_id,
              role: payload.role,
              stage: payload.stage,
              detail: payload.detail,
              elapsed_ms: payload.elapsed_ms,
            }
            emit(false)
          } else if (eventName === "usage_update") {
            meta.liveUsage = payload
            emit(false)
          } else if (eventName === "turn_state" && currentTurn) {
            currentTurn.status = payload.status || currentTurn.status
            emit(false)
          } else if (eventName === "followup_started") {
            meta.followupTurnId = payload.turn_id
            emit(false)
          }
        }
      }
    } catch (err: any) {
      if (err?.name !== "AbortError") {
        onError?.(err?.message || "连接中断")
      }
    }
  })()

  return () => controller.abort()
}

export const RUN_LABELS: Record<string, string> = {
  queued: "等待检索",
  running: "正在检索",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "运行失败",
  cancelled: "已取消",
}

export const SOURCE_LABELS: Record<string, string> = {
  arxiv: "arXiv",
  openalex: "OpenAlex",
  crossref: "Crossref",
  openreview: "OpenReview",
  dblp: "DBLP",
  acl: "ACL Anthology",
  pmlr: "PMLR",
  semantic_scholar: "Semantic Scholar",
  cvf: "CVF Open Access",
  github: "GitHub",
  huggingface: "Hugging Face",
}

export const STAGE_LABELS: Record<string, string> = {
  plan: "规划检索",
  collect: "检索候选论文",
  investigate: "阅读证据与补查",
  report: "核对引用并生成报告",
  finished: "已结束",
  cancelled: "已取消",
}
