export type ChartKind = "bar" | "line" | "table" | "kpi"
export type ChartSeries = { key: string; label: string }
export type ChartQuery = { tool: string; arguments: Record<string, unknown> }

export type ChartSpec = {
  id: string
  kind: ChartKind
  title: string
  time_range_label: string
  basis: string
  unit: string
  category_key: string
  category_label: string
  series: ChartSeries[]
  rows: Array<Record<string, string | number>>
  generated_at: string
  range_start?: string
  range_end?: string
  query: ChartQuery
}

export type AssistantStep = {
  sequence: number
  tool: string
  arguments: Record<string, unknown>
  status: "ok" | "error"
  summary: string
  row_count: number
}

export type AssistantTurn = { role: "user" | "assistant"; content: string }
export type AssistantSource = "apim" | "github-copilot"
export type AssistantApiFormat = "openai_chat" | "openai_responses" | "anthropic_messages"
export type AssistantSettingsWrite = {
  model_id: string | null
  auto_title: boolean
  api_format?: AssistantApiFormat | null
}

export type AssistantReply = {
  conversation_id: string
  message: string
  charts: ChartSpec[]
  steps: AssistantStep[]
  latency_ms: number
  model: string
  total_tokens: number
}

export type PinnedChart = {
  id: string
  report_id: string
  original_question: string
  chart: ChartSpec
  position: number
  created_at: string
  updated_at: string
}

export type PinnedReportLayout = {
  version: 1
  spans: Record<string, number>
  row_heights: Record<string, number>
}

export type PinnedReport = {
  id: string
  title: string
  description: string
  owner_id: string
  visibility: "private" | "public"
  can_manage: boolean
  position: number
  layout: PinnedReportLayout
  created_at: string
  updated_at: string
  charts: PinnedChart[]
}

export type ConversationSummary = {
  id: string
  title: string
  title_source: "question" | "model" | "user"
  owner_id: string
  turn_count: number
  created_at: string
  updated_at: string
}

export type ConversationExchange = {
  position: number
  question: string
  reply: AssistantReply
  created_at: string
}

export type Conversation = ConversationSummary & {
  exchanges: ConversationExchange[]
}

export type AssistantModelChoice = {
  id: string
  display_name: string
  model_key: string
  runtime_name: string
  input_cost_per_million: number | null
  output_cost_per_million: number | null
  api_formats?: AssistantApiFormat[]
}

export type AssistantSettings = {
  model_id: string | null
  auto_title: boolean
  api_format?: AssistantApiFormat | null
  api_available?: boolean
  effective_api_format?: AssistantApiFormat | null
  effective_api_path?: string | null
  available_api_formats?: AssistantApiFormat[]
  effective_model_id: string | null
  effective_model_name: string | null
  model_available: boolean
  available_models: AssistantModelChoice[]
  updated_at: string | null
  updated_by: string | null
}
