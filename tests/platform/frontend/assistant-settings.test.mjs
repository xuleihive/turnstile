import assert from "node:assert/strict"
import test from "node:test"

import {
  assistantSettingsChanged,
  assistantSettingsModel,
  assistantSettingsUpdate,
} from "../../../frontend/src/components/assistant/settings-update.ts"

const settings = {
  model_id: "gpt",
  model_available: true,
  auto_title: true,
  api_format: "openai_responses",
  api_available: true,
  available_api_formats: ["openai_chat", "openai_responses"],
  available_models: [
    { id: "gpt", api_formats: ["openai_chat", "openai_responses"] },
    { id: "claude", api_formats: ["anthropic_messages"] },
  ],
}

test("changing to Claude resets an incompatible override to automatic", () => {
  assert.deepEqual(assistantSettingsUpdate(settings, { model_id: "claude" }), {
    model_id: "claude", auto_title: true, api_format: null,
  })
})

test("a title toggle preserves the selected API protocol", () => {
  assert.deepEqual(assistantSettingsUpdate(settings, { auto_title: false }), {
    model_id: "gpt", auto_title: false, api_format: "openai_responses",
  })
})

test("automatic protocol selection is persisted as null", () => {
  assert.equal(assistantSettingsUpdate(settings, { api_format: null }).api_format, null)
})

test("automatic model selection retains a compatible explicit protocol", () => {
  assert.deepEqual(assistantSettingsUpdate(settings, { model_id: null }), {
    model_id: null, auto_title: true, api_format: "openai_responses",
  })
})

test("unavailable stored choices are never re-saved", () => {
  assert.deepEqual(assistantSettingsUpdate({
    ...settings, model_available: false, api_available: false,
  }, { auto_title: false }), {
    model_id: null, auto_title: false, api_format: null,
  })
})

test("unknown legacy protocol metadata cannot enable an override", () => {
  assert.equal(assistantSettingsUpdate({
    ...settings, available_models: [{ id: "gpt" }],
  }, { auto_title: false }).api_format, null)
})

test("an older backend receives no unsupported protocol field", () => {
  const { available_api_formats, ...legacy } = settings
  assert.deepEqual(assistantSettingsUpdate(legacy, { auto_title: false }), {
    model_id: "gpt", auto_title: false,
  })
})

test("draft edits compose before saving without mutating the saved settings", () => {
  const original = structuredClone(settings)
  let draft = assistantSettingsUpdate(settings, { auto_title: false })
  draft = assistantSettingsUpdate(settings, { ...draft, model_id: "claude" })
  draft = assistantSettingsUpdate(settings, { ...draft, api_format: "anthropic_messages" })
  assert.deepEqual(draft, {
    model_id: "claude", auto_title: false, api_format: "anthropic_messages",
  })
  assert.deepEqual(settings, original)
  assert(assistantSettingsChanged(settings, draft))
  assert.equal(assistantSettingsModel(settings, draft).id, "claude")
})

test("reverting a draft disables saving again", () => {
  const original = assistantSettingsUpdate(settings, {})
  assert.equal(assistantSettingsChanged(settings, original), false)
  assert(assistantSettingsChanged(settings, { ...original, auto_title: false }))
  assert.equal(assistantSettingsChanged(settings, { ...original, auto_title: true }), false)
})

test("automatic draft selection filters protocols by a compatible effective model", () => {
  const draft = { model_id: null, api_format: "anthropic_messages", auto_title: true }
  assert.equal(assistantSettingsModel(settings, draft).id, "claude")
  assert.equal(assistantSettingsModel(settings, { ...draft, api_format: null }).id, "gpt")
  assert.equal(assistantSettingsModel({ ...settings, available_models: [] }, draft), undefined)
})

test("legacy draft edits omit the protocol and dirty checks do not invent one", () => {
  const { available_api_formats, ...legacy } = settings
  const saved = assistantSettingsUpdate(legacy, {})
  assert.equal(assistantSettingsChanged(legacy, saved), false)
  const draft = assistantSettingsUpdate(legacy, { ...saved, auto_title: false })
  assert.equal("api_format" in draft, false)
  assert(assistantSettingsChanged(legacy, draft))
})
