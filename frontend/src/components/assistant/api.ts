import { request } from "../../api/client"
import type {
  AssistantReply,
  AssistantSettings,
  AssistantSettingsWrite,
  AssistantSource,
  AssistantTurn,
  ChartSpec,
  Conversation,
  ConversationSummary,
  PinnedReport,
} from "./types"

const path = (source: AssistantSource, suffix: string) =>
  source === "github-copilot"
    ? `/api/v1/copilot/assistant${suffix}`
    : `/api/v1/assistant${suffix}`

export const assistantApi = {
  ask: (
    source: AssistantSource,
    body: {
      question: string
      history: AssistantTurn[]
      conversation_id?: string
      timezone: string
      locale: string
    },
  ) => request<AssistantReply>(path(source, "/ask"), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }),
  conversations: (source: AssistantSource) => request<{ items: ConversationSummary[] }>(
    path(source, "/conversations"),
  ).then((response) => response.items),
  conversation: (source: AssistantSource, id: string) => request<Conversation>(
    path(source, `/conversations/${encodeURIComponent(id)}`),
  ),
  renameConversation: (source: AssistantSource, id: string, title: string) =>
    request<Conversation>(path(source, `/conversations/${encodeURIComponent(id)}`), {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title }),
    }),
  titleConversation: (source: AssistantSource, id: string, locale: string) =>
    request<ConversationSummary>(
      path(source, `/conversations/${encodeURIComponent(id)}/title`),
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ locale }),
      },
    ),
  deleteConversation: (source: AssistantSource, id: string) => request<void>(
    path(source, `/conversations/${encodeURIComponent(id)}`),
    { method: "DELETE" },
  ),
  settings: () => request<AssistantSettings>("/api/v1/assistant/settings"),
  saveSettings: (body: AssistantSettingsWrite) =>
    request<AssistantSettings>("/api/v1/assistant/settings", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  pinnedReports: () => request<{ items: PinnedReport[] }>(
    "/api/v1/assistant/pinned-charts",
  ).then((response) => response.items),
  pin: (body: {
    report_id?: string
    title?: string
    description?: string
    original_question: string
    chart: ChartSpec
  }) => request<PinnedReport>("/api/v1/assistant/pinned-charts", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }),
  renamePinned: (id: string, title: string, description: string) =>
    request<PinnedReport>(`/api/v1/assistant/pinned-charts/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title, description }),
    }),
  setVisibility: (id: string, visibility: "private" | "public") =>
    request<PinnedReport>(`/api/v1/assistant/pinned-charts/${id}/visibility`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ visibility }),
    }),
  saveLayout: (id: string, layout: PinnedReport["layout"]) =>
    request<PinnedReport>(`/api/v1/assistant/pinned-charts/${id}/layout`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(layout),
    }),
  refreshPinned: (id: string, timezone: string, locale: string) =>
    request<PinnedReport>(
      `/api/v1/assistant/pinned-charts/${id}/refresh`
      + `?timezone=${encodeURIComponent(timezone)}&locale=${encodeURIComponent(locale)}`,
      { method: "POST" },
    ),
  unpin: (id: string) => request<void>(
    `/api/v1/assistant/pinned-charts/${id}`,
    { method: "DELETE" },
  ),
  removeChart: (reportId: string, chartId: string) => request<PinnedReport>(
    `/api/v1/assistant/pinned-charts/${reportId}/charts/${chartId}`,
    { method: "DELETE" },
  ),
  reorderCharts: (reportId: string, chartIds: string[]) => request<PinnedReport>(
    `/api/v1/assistant/pinned-charts/${reportId}/charts/order`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chart_ids: chartIds }),
    },
  ),
}
