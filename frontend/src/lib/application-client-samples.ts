import type { ManagedModel, ModelRuntime } from "../data-sources/apim/types"

export type ClientSampleLanguage = "bash" | "python" | "javascript"
export type GatewayApiOperation = "models" | "chat_completions" | "responses" | "responses_compact" | "anthropic_messages" | "anthropic_count_tokens" | "image_generations" | "decision"
export type ClientSampleOperation = GatewayApiOperation

export type ClientSampleRequest = {
  payload?: Record<string, unknown>
  headers?: Record<string, string>
}

export type GatewayApiOperationOption = {
  id: GatewayApiOperation
  label: string
  method: "GET" | "POST"
  path: string
}

export function gatewayApiOperations(): GatewayApiOperationOption[] {
  return [
    { id: "models", label: "List Models", method: "GET", path: "/v1/models" },
    { id: "chat_completions", label: "Chat Completions", method: "POST", path: "/chat/completions" },
    { id: "responses", label: "Responses", method: "POST", path: "/responses" },
    { id: "responses_compact", label: "Responses Compact", method: "POST", path: "/responses/compact" },
    { id: "anthropic_messages", label: "Anthropic Messages", method: "POST", path: "/v1/messages" },
    { id: "anthropic_count_tokens", label: "Token Count", method: "POST", path: "/v1/messages/count_tokens" },
    { id: "image_generations", label: "Image Generations", method: "POST", path: "/images/generations" },
    { id: "decision", label: "Decision", method: "POST", path: "/providers/microsoft/v1/systemone" },
  ]
}

export function operationSampleModels<Model extends { capabilities: readonly string[] }>(
  models: readonly Model[],
  operation: GatewayApiOperation,
): Model[] {
  return models.filter(model => operation === "models"
    || model.capabilities.includes("decision") === (operation === "decision")
    && model.capabilities.includes("image_generation") === (operation === "image_generations"))
}

export function modelSampleOperations(model: ManagedModel, runtime: ModelRuntime | undefined) {
  const configuredFormat = String(runtime?.config.api_format ?? "")
  const isAnthropic = (runtime?.brand_key === "microsoft_foundry" && model.family_key === "claude")
    || configuredFormat === "anthropic_messages"
    || (!configuredFormat && (
      runtime?.brand_key === "amazon_bedrock"
      || runtime?.brand_key === "anthropic"
      || runtime?.brand_key === "azure_databricks"
    ))
  const allowedIds = new Set<ClientSampleOperation>(
    isAnthropic
      ? ["anthropic_messages", "anthropic_count_tokens", "models"]
      : ["chat_completions", "responses", "responses_compact", "models"],
  )
  const defaultOperation: ClientSampleOperation = isAnthropic
    ? "anthropic_messages"
    : "chat_completions"
  return {
    defaultOperation,
    operations: gatewayApiOperations().filter((operation) => allowedIds.has(operation.id)),
  }
}

function shellQuote(value: string) {
  return `'${value.replaceAll("'", "'\\''")}'`
}

function requestFor(operation: ClientSampleOperation, modelKey: string) {
  switch (operation) {
    case "models":
      return { path: "/v1/models", payload: undefined }
    case "chat_completions":
      return { path: "/chat/completions", payload: { model: modelKey, messages: [{ role: "user", content: "Hello" }] } }
    case "responses":
      return { path: "/responses", payload: { model: modelKey, input: "Hello" } }
    case "responses_compact":
      return { path: "/responses/compact", payload: { model: modelKey, input: [{ role: "user", content: "Hello" }, { role: "assistant", content: "Hello!" }] } }
    case "anthropic_messages":
      return { path: "/v1/messages", payload: { model: modelKey, max_tokens: 256, messages: [{ role: "user", content: "Hello" }] } }
    case "anthropic_count_tokens":
      return { path: "/v1/messages/count_tokens", payload: { model: modelKey, messages: [{ role: "user", content: "Hello" }] } }
    case "image_generations":
      return { path: "/images/generations", payload: { model: modelKey, prompt: "A red kite flying above a green meadow", n: 1 } }
    case "decision":
      return {
        path: "/providers/microsoft/v1/systemone",
        payload: {
          model: modelKey,
          state: "The API returns 500 on every call.",
          questions: {
            urgent: {
              type: "noul",
              instructions: "Does this ticket need urgent attention?",
            },
            team: {
              type: "choice",
              instructions: "Which team should handle this ticket?",
              criteria: {
                billing: "Charges, invoices, and refunds",
                engineering: "Bugs, errors, and outages",
                support: "How-to and account questions",
              },
            },
            severity: {
              type: "score",
              instructions: "How severe is this incident?",
              criteria: ["Cosmetic", "Minor", "Major", "Critical"],
            },
          },
        },
      }
  }
}

export function clientSampleFor(
  language: ClientSampleLanguage,
  endpoint: string,
  modelKey: string,
  operation: ClientSampleOperation,
  options?: ClientSampleRequest,
) {
  const request = { ...requestFor(operation, modelKey), ...(options?.payload ? { payload: options.payload } : {}) }
  const url = `${endpoint.replace(/\/+$/, "")}${request.path}`
  const payload = JSON.stringify(request.payload)
  const modelList = operation === "models"
  const keyHeader = "Ocp-Apim-Subscription-Key"
  const keyVariable = "TURNSTILE_APIM_KEY"
  const extraHeaders = Object.entries(options?.headers ?? {})
  const anthropic = operation === "anthropic_messages" || operation === "anthropic_count_tokens"
  const anthropicHeaders = anthropic
    ? ['-H "anthropic-version: 2023-06-01"']
    : []

  if (language === "bash") {
    return [
      `curl ${shellQuote(url)}`,
      `-H "${keyHeader}: \${${keyVariable}:?Set ${keyVariable} first}"`,
      ...anthropicHeaders,
      ...extraHeaders.map(([name, value]) => `-H ${shellQuote(`${name}: ${value}`)}`),
      ...(modelList ? ['--request GET'] : [
        '-H "Content-Type: application/json"',
        `--data-raw ${shellQuote(payload!)}`,
      ]),
    ].join(" \\\n  ")
  }

  if (language === "python") {
    return `${modelList ? "" : "import json\n"}import os
from urllib.request import Request, urlopen

url = ${JSON.stringify(url)}
${modelList ? "" : `payload = json.loads(${JSON.stringify(payload)})\n`}headers = {
  "${keyHeader}": os.environ["${keyVariable}"],
${modelList ? "" : '  "Content-Type": "application/json",\n'}${anthropic ? '  "anthropic-version": "2023-06-01",\n' : ""}${extraHeaders.map(([name, value]) => `  ${JSON.stringify(name)}: ${JSON.stringify(value)},\n`).join("")}}
request = Request(url, ${modelList ? 'method="GET", ' : "data=json.dumps(payload).encode(), "}headers=headers)
with urlopen(request) as response:
    print(response.read().decode())`
  }

  return `const response = await fetch(${JSON.stringify(url)}, {
  method: "${modelList ? "GET" : "POST"}",
  headers: {
    "${keyHeader}": process.env.${keyVariable},
  ${modelList ? "" : '    "Content-Type": "application/json",\n'}${anthropic ? '    "anthropic-version": "2023-06-01",\n' : ""}${extraHeaders.map(([name, value]) => `    ${JSON.stringify(name)}: ${JSON.stringify(value)},\n`).join("")}  }${modelList ? "" : `,\n  body: JSON.stringify(${JSON.stringify(request.payload, null, 2)!.replaceAll("\n", "\n  ")})`}
});

if (!response.ok) throw new Error(await response.text());
console.log(await response.json());`
}