import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE


def test_assistant_composer_grows_with_its_question_up_to_a_fixed_cap() -> None:
    source = (
        FRONTEND_SOURCE / "components/assistant/assistant-conversation.tsx"
    ).read_text(encoding="utf-8")

    assert "useLayoutEffect(() =>" in source
    assert 'input.style.height = "auto"' in source
    assert "Math.min(input.scrollHeight, 132)" in source
    assert "}, [inputRef, question])" in source


def test_member_invocation_picker_lists_self_and_current_department_testers() -> None:
    invocation = (
        FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-invocation.tsx"
    ).read_text(encoding="utf-8")
    shared = (
        FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-shared.tsx"
    ).read_text(encoding="utf-8")

    assert 'sessionUser.role === "owner"' in invocation
    assert "entities.invocation_testers ?? []" in invocation
    assert "person.parent_id === departmentId" in invocation
    assert "person.id !== sessionUser.email" in invocation
    assert "allowAll={false}" in invocation
    assert "TESTER_ID" not in invocation
    assert "@contoso.com" not in invocation
    assert "allowAll = true" in shared
    assert "allowAll && <SelectItem" in shared


def test_invocation_draft_survives_page_navigation_for_the_same_user() -> None:
    invocation = (
        FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-invocation.tsx"
    ).read_text(encoding="utf-8")

    assert "let invocationDraft: InvocationDraft | null = null" in invocation
    assert (
        "sessionUser && invocationDraft?.owner === sessionUser.email ? invocationDraft : null"
    ) in invocation
    assert "useState(draft?.prompt ?? CHAT_PROMPT_SAMPLE)" in invocation
    assert (
        "invocationDraft = { owner: sessionUser.email, mode, prompt, stateFormat, questions }"
    ) in invocation
    assert "Storage" not in invocation


def test_invocation_client_samples_follow_current_selection() -> None:
    invocation = (
        FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-invocation.tsx"
    ).read_text(encoding="utf-8")
    assert "selectedRouter?.name ?? invocationModel?.model_key" in invocation
    assert "selectedGateway?.endpoint" in invocation
    assert 'stateFormat === "json" ? JSON.parse(prompt) : prompt' in invocation
    assert "questions: JSON.parse(questions)" in invocation
    assert "sampleRuntime?.config.max_tokens_field" in invocation
    assert '"x-user-id": userId' in invocation
    assert "<ClientSampleCode endpoint={sampleEndpoint}" in invocation
    assert '<details className="invoke-code-sample">' in invocation
    assert invocation.index('<details className="invoke-code-sample">') < invocation.index(
        '<section className="finops-panel invoke-result">'
    )
    assert "Tabs." not in invocation
    assert "outputView" not in invocation
    assert "invoke-client-quickstart" not in invocation
    subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("invocation-client-samples.test.mjs"))],
        cwd=FRONTEND_SOURCE.parent.parent,
        check=True,
    )