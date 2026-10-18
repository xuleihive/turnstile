import type { AssistantSettings, AssistantSettingsWrite } from "./types"

export function assistantSettingsUpdate(
  settings: AssistantSettings,
  changes: Partial<AssistantSettingsWrite>,
): AssistantSettingsWrite {
  const next: AssistantSettingsWrite = {
    model_id: settings.model_available ? settings.model_id : null,
    auto_title: settings.auto_title,
    api_format: settings.api_available === false ? null : settings.api_format ?? null,
    ...changes,
  }
  if (!Array.isArray(settings.available_api_formats)) {
    delete next.api_format
    return next
  }
  const api = next.api_format
  if (api) {
    const candidates = next.model_id
      ? settings.available_models.filter((model) => model.id === next.model_id)
      : settings.available_models
    if (!candidates.some((model) => model.api_formats?.includes(api))) {
      next.api_format = null
    }
  }
  return next
}

export function assistantSettingsModel(
  settings: AssistantSettings,
  draft: AssistantSettingsWrite,
) {
  return settings.available_models.find((model) => model.id === draft.model_id)
    ?? settings.available_models.find((model) =>
      !draft.api_format || model.api_formats?.includes(draft.api_format))
    ?? settings.available_models[0]
}

export function assistantSettingsChanged(
  settings: AssistantSettings,
  draft: AssistantSettingsWrite,
) {
  const saved = assistantSettingsUpdate(settings, {})
  return draft.model_id !== saved.model_id
    || draft.auto_title !== saved.auto_title
    || (draft.api_format ?? null) !== (saved.api_format ?? null)
}
