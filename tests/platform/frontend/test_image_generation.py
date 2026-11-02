import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_image_frontend_unit_cases() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--test",
            str(Path(__file__).with_name("image-generation.test.mjs")),
            str(Path(__file__).with_name("model-publication-connections.test.mjs")),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_image_invocation_keeps_results_in_memory_and_excludes_text_models() -> None:
    page = (FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-invocation.tsx").read_text()
    result = (FRONTEND_SOURCE / "data-sources/apim/pages/image-generation-result.tsx").read_text()
    assert 'capabilities.includes("image_generation") === imageMode' in page
    assert "n: 1, stream: false" in page
    assert "image_configuration_schema_version === 4" in page
    assert "setImageResult(null)" in page
    assert 'setPrompt(next === "decision" ?' in page
    assert 'DECISION_STATE_SAMPLE) : next === "chat" ? CHAT_PROMPT_SAMPLE : "")' in page
    assert "URL.createObjectURL" in result and "URL.revokeObjectURL" in result
    assert "naturalWidth" in result and "naturalHeight" in result
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "setQueryData",
        "useMutation",
        "console.log",
    ):
        assert forbidden not in page + result


def test_image_options_offer_foundry_values_with_localized_accessible_names() -> None:
    result = (FRONTEND_SOURCE / "data-sources/apim/pages/image-generation-result.tsx").read_text()
    options = result.split("function ImageOptions(", 1)[1].split("\n}\n", 1)[0]
    assert 'suggestions: ["png", "jpeg"]' in result and "webp" not in result
    assert "aria-label" not in options
    assert "<Input list={`${id}-${key}`}" in options
    assert "<datalist id={`${id}-${key}`} data-no-localize>" in options


def test_invocation_results_and_denials_are_readable() -> None:
    page = (FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-invocation.tsx").read_text()
    client = (FRONTEND_SOURCE / "api/client.ts").read_text()
    assert ": `$${result.estimated_cost.toFixed(6)}`}" in page
    assert "currency" not in page
    for code in ("model_not_assigned", "budget_exceeded", "image_budget_insufficient"):
        assert f'["{code}", "' in page
    assert (
        "error instanceof ApiError && error.detail ? INVOCATION_DENIALS.get(error.detail)" in page
    )
    assert (
        "throw new ApiError(response.status, `API ${response.status}: ${detail}`, detail)"
        in client
    )


def test_image_editor_requires_prices_and_preserves_operation() -> None:
    editor = (FRONTEND_SOURCE / "components/model-management/model-edit-dialog.tsx").read_text()
    publication = (
        FRONTEND_SOURCE / "components/model-management/model-publication-dialog.tsx"
    ).read_text()
    assert "[draft.inputPrice, draft.cacheReadPrice, draft.outputPrice]" in editor
    assert "if (busy || !dirty || validation || imagePriceMissing) return" in editor
    assert "disabled={busy || imageGeneration || decision}" in editor
    assert "showAnthropic={!decision} decision={decision} />}" in editor
    assert "{!draft.contextPricingPreview && !decision && <div className=\"form-grid\">" in editor
    assert "这四个值" not in editor
    assert (
        'operation: decision ? "decision" : imageGeneration ? "image_generation" : undefined'
        in publication
    )
    assert "[inputPrice, outputPrice, cacheReadPrice]" in publication
    assert "context_window: imageGeneration ? null" in publication
    assert (
        "cache_write_cost_per_million: decision || imageGeneration || "
        "pricing.contextPricingPreview ? null"
        in publication
    )
