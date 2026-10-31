// Isolated draft/payload unit tests. No browser, API, registry writes or database.
import assert from "node:assert/strict"
import test from "node:test"

import {
  createModelEditDraft,
  modelEditHasChanges,
  modelEditPayload,
  modelEditRoleOptions,
  toggleModelSelection,
  validateModelEdit,
} from "../../../frontend/src/components/model-management/model-edit-form.ts"

const model = (overrides = {}) => ({
  id: "model-a",
  provider_id: "provider-a",
  runtime_id: "connection-a",
  model_key: "published-alias",
  upstream_model_id: "existing-deployment",
  family_key: "generic",
  assignment_required: true,
  display_name: "Existing model",
  capabilities: ["chat", "streaming"],
  context_window: 1048576,
  input_cost_per_million: 3,
  output_cost_per_million: 15,
  cached_cost_per_million: 0.3,
  cache_write_cost_per_million: 3,
  price_source: "manual",
  price_reference: null,
  price_discount_percent: null,
  allowed_roles: ["owner", "admin", "member"],
  enabled: true,
  is_default: false,
  ...overrides,
})

test("unchanged metadata round-trips while routing identity comes from the saved model", () => {
  const source = model()
  const result = modelEditPayload(source, createModelEditDraft(source))
  for (const key of Object.keys(result)) assert.deepEqual(result[key], source[key], key)
  assert.equal("id" in result, false)
  assert.equal("gateway_profile_id" in result, false)
})

test("editing display and price fields cannot override routing identity or capabilities", () => {
  const source = model()
  const draft = { ...createModelEditDraft(source), displayName: " Renamed ",
    capabilities: ["chat", "tools"], inputPrice: "4.123456789",
    provider_id: "different", runtime_id: "different", model_key: "different",
    upstream_model_id: "different", assignment_required: false, family_key: "openai" }
  const result = modelEditPayload(source, draft)
  for (const key of ["provider_id", "runtime_id", "model_key", "upstream_model_id",
    "family_key", "assignment_required"]) assert.equal(result[key], source[key])
  assert.equal(result.display_name, "Renamed")
  assert.equal(result.input_cost_per_million, 4.123456789)
  assert.deepEqual(result.capabilities, source.capabilities)
})

test("null upstream identity is preserved without substituting the public alias", () => {
  const source = model({ upstream_model_id: null })
  assert.equal(modelEditPayload(source, createModelEditDraft(source)).upstream_model_id, null)
})

test("blank nullable fields remain null and zero prices remain explicitly free", () => {
  const source = model({ context_window: null, input_cost_per_million: null,
    output_cost_per_million: 0, cached_cost_per_million: null, cache_write_cost_per_million: 0 })
  const draft = createModelEditDraft(source)
  assert.equal(draft.contextWindow, "")
  assert.equal(draft.inputPrice, "")
  assert.equal(draft.outputPrice, "0")
  const result = modelEditPayload(source, draft)
  assert.equal(result.context_window, null)
  assert.equal(result.input_cost_per_million, null)
  assert.equal(result.output_cost_per_million, 0)
  assert.equal(result.cached_cost_per_million, null)
  assert.equal(result.cache_write_cost_per_million, 0)
})

test("clearing cache rates requests backend fallback, not a client-side fabricated rate", () => {
  const source = model()
  const result = modelEditPayload(source, { ...createModelEditDraft(source),
    cacheReadPrice: "", cacheWritePrice: " " })
  assert.equal(result.cached_cost_per_million, null)
  assert.equal(result.cache_write_cost_per_million, null)
  assert.equal(result.input_cost_per_million, 3)
})

test("capabilities are excluded from the draft and preserved from the registered model", () => {
  for (const capabilities of [["chat", "streaming"], ["tools", "vision", "reasoning"], []]) {
    const source = model({ capabilities })
    const draft = createModelEditDraft(source)
    assert.equal("capabilities" in draft, false)
    const payload = modelEditPayload(source, { ...draft, capabilities: ["invented-capability"] })
    assert.deepEqual(payload.capabilities, capabilities)
    assert.notEqual(payload.capabilities, source.capabilities)
  }
})

test("tool calling is the one capability an owner can declare in the editor", () => {
  const source = model({ capabilities: ["chat", "streaming"] })
  const draft = createModelEditDraft(source)
  assert.equal(draft.toolCalling, false)
  const declared = { ...draft, toolCalling: true }
  assert.equal(modelEditHasChanges(draft, declared), true)
  assert.deepEqual(modelEditPayload(source, declared).capabilities, ["chat", "streaming", "tools"])
  const tools = model({ capabilities: ["chat", "tools", "streaming"] })
  assert.equal(createModelEditDraft(tools).toolCalling, true)
  assert.deepEqual(modelEditPayload(tools, { ...createModelEditDraft(tools), toolCalling: false })
    .capabilities, ["chat", "streaming"])
})

test("legacy and custom roles round-trip without silently adding or dropping permissions", () => {
  const source = model({ allowed_roles: ["owner", "admin", "service", "custom-role"] })
  const draft = createModelEditDraft(source)
  assert.deepEqual(modelEditRoleOptions(source), ["owner", "member", "admin", "service", "custom-role"])
  assert.deepEqual(modelEditPayload(source, draft).allowed_roles, source.allowed_roles)
  assert.deepEqual(modelEditRoleOptions(model({ allowed_roles: ["member"] })), ["owner", "member"])
  assert.deepEqual(modelEditPayload(source, { ...draft, allowedRoles: [] }).allowed_roles, [])
  const selected = toggleModelSelection(draft.allowedRoles, "member", true)
  assert.deepEqual(toggleModelSelection(selected, "member", true), selected)
  assert.deepEqual(toggleModelSelection(selected, "member", false), source.allowed_roles)
})

test("restoring the same checkbox choices does not leave a spurious pending change", () => {
  const initial = createModelEditDraft(model())
  assert.equal(modelEditHasChanges(initial, { ...initial,
    allowedRoles: [...initial.allowedRoles].reverse(),
  }), false)
  assert.equal(modelEditHasChanges(initial, { ...initial, allowedRoles: ["owner"] }), true)
})

test("invalid display names are rejected before a save payload can be constructed", () => {
  const source = model()
  for (const displayName of ["", "   ", "x".repeat(256)]) {
    const draft = { ...createModelEditDraft(source), displayName }
    assert.equal(validateModelEdit(draft), "display_name")
    assert.throws(() => modelEditPayload(source, draft), /display_name/)
  }
})

test("context limits require safe positive integers and rates finite non-negative values", () => {
  const draft = createModelEditDraft(model())
  for (const contextWindow of ["0", "-1", "1.5", "Infinity", "not-a-number", "9007199254740992"]) {
    assert.equal(validateModelEdit({ ...draft, contextWindow }), "context_window")
  }
  for (const value of ["-1", "Infinity", "NaN", "1e999"]) {
    for (const key of ["inputPrice", "outputPrice", "cacheReadPrice", "cacheWritePrice"]) {
      assert.equal(validateModelEdit({ ...draft, [key]: value }), "prices")
    }
  }
  assert.equal(validateModelEdit({ ...draft, contextWindow: "1", inputPrice: "0" }), null)
})

test("availability never enters a metadata draft or payload and defaults survive disablement", () => {
  const source = model({ enabled: false, is_default: true })
  const draft = createModelEditDraft(source)
  assert.equal("enabled" in draft, false)
  assert.equal(validateModelEdit(draft), null)
  const payload = modelEditPayload(source, { ...draft, enabled: true })
  assert.equal("enabled" in payload, false)
  assert.equal(payload.is_default, true)
  assert.equal(modelEditHasChanges(draft, createModelEditDraft({ ...source, enabled: true })), false)
})