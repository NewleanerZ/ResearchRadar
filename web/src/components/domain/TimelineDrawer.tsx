import { useState } from "react"
import { ChevronDown, ChevronRight, Clock } from "lucide-react"
import { TimelineEvent, RUN_LABELS, SOURCE_LABELS, STAGE_LABELS } from "@/lib/api"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

const TOOL_LABELS: Record<string, string> = {
  list_topics: "读取主题",
  search_library: "查询论文库",
  search_external_papers: "外部学术检索",
  rag_search: "全文混合检索",
  deep_read_paper: "精读论文",
  get_paper: "读取论文",
  get_report: "读取日报",
  get_trend: "读取趋势",
  get_run: "查询任务",
  list_projects: "查询科研项目",
  compare_methods: "同协议方法对比",
  list_research_events: "查询科研事件",
  run_gap_analysis: "Research Gap 分析",
  list_external_mcp: "查看外部 MCP 工具",
  call_external_mcp: "调用外部 MCP 工具",
  read_artifact: "读取完整产物",
  run_subagent: "并发子代理查证",
  fetch_paper_summary: "获取论文网页摘要",
  fetch_paper_pdf: "探索获取原始 PDF",
  set_paper_state: "更新阅读状态",
  enqueue_daily: "提交检索",
  draft_topic: "生成主题草案",
  propose_action: "准备确认卡",
  start_research: "提交专题",
  cancel_research: "取消专题",
  steer_research: "调整专题",
  get_project_memory: "读取项目记忆",
}

const EVENT_LABELS: Record<string, string> = {
  turn: "对话",
  job: "任务",
  tool: "工具",
  model: "模型调用",
  source: "数据源",
  action: "操作",
  confirmation: "确认",
  project_memory: "项目记忆",
  adjustment: "专题调整",
  stage: "研究阶段",
  report: "专题报告",
  target_paper: "查指定论文",
}

const EVENT_STATUS: Record<string, string> = {
  ...RUN_LABELS,
  running: "进行中",
  ok: "完成",
  completed: "完成",
  complete: "完成",
  needs_confirmation: "待确认",
  queued: "已排队",
  loaded: "已采用",
  applied: "已应用",
  ready: "下一步",
  error: "失败",
  truncated: "截断",
  budget: "触及上限",
  done: "完成",
  stopping: "等待当前步骤结束",
  cancelled: "已取消",
  outside_scope: "超出范围",
}

function formatDetail(ev: TimelineEvent) {
  let data: any = {}
  try {
    data = JSON.parse(ev.detail || "{}")
  } catch {
    data = {}
  }
  const stage =
    TOOL_LABELS[data.tool] ||
    data.tool ||
    SOURCE_LABELS[data.source] ||
    data.source ||
    EVENT_LABELS[ev.stage] ||
    ev.stage
  const bits: string[] = []
  for (const [key, label] of [
    ["received", "记录"],
    ["total", "命中"],
    ["imported", "入库"],
    ["candidates", "候选"],
    ["evidence", "证据"],
    ["citations", "引用"],
    ["revision", "版本"],
  ] as const) {
    if (data[key] !== undefined && data[key] !== null) bits.push(`${label} ${data[key]}`)
  }
  for (const key of ["step", "next"] as const) {
    if (data[key]) bits.push(STAGE_LABELS[data[key]] || data[key])
  }
  if (data.paper) bits.push(String(data.paper))
  if (data.http_status) bits.push(`HTTP ${data.http_status}`)
  if (data.input_tokens !== undefined && data.output_tokens !== undefined) {
    bits.push(`Token ${data.input_tokens}/${data.output_tokens}`)
  }
  if (data.error || data.reason) bits.push(String(data.error || data.reason))
  return {
    time: new Date(ev.created_at).toLocaleTimeString("zh-CN", { hour12: false }),
    stage,
    status: EVENT_STATUS[ev.status] || ev.status,
    elapsed: ev.elapsed_ms ?? "—",
    result: bits.join(" · ") || "—",
  }
}

export function TimelineAccordion({ events, defaultOpen = false }: { events: TimelineEvent[]; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  if (!events || events.length === 0) return null
  const rows = events.map(formatDetail)
  const latest = rows[rows.length - 1]

  return (
    <div className="mt-2 rounded-md border border-[var(--border)] bg-[var(--background)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-2.5 py-1.5 text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] cursor-pointer"
      >
        <span className="flex items-center gap-1.5">
          <Clock className="h-3 w-3" aria-hidden="true" />
          <span>任务时间线（{rows.length} 步）</span>
          {!open && (
            <span className="text-[var(--foreground)]">
              · {latest.stage}（{latest.status}）
            </span>
          )}
        </span>
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
      </button>
      {open && (
        <div className="border-t border-[var(--border)] p-2">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="h-7 px-2">时间</TableHead>
                <TableHead className="h-7 px-2">阶段</TableHead>
                <TableHead className="h-7 px-2">状态</TableHead>
                <TableHead className="h-7 px-2 text-right">耗时(ms)</TableHead>
                <TableHead className="h-7 px-2">结果</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r, i) => (
                <TableRow key={i}>
                  <TableCell className="px-2 py-1 tabular-nums text-[var(--muted-foreground)]">{r.time}</TableCell>
                  <TableCell className="px-2 py-1 font-medium">{r.stage}</TableCell>
                  <TableCell className="px-2 py-1">{r.status}</TableCell>
                  <TableCell className="px-2 py-1 text-right tabular-nums">{r.elapsed}</TableCell>
                  <TableCell className="px-2 py-1 text-[var(--muted-foreground)]">{r.result}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
