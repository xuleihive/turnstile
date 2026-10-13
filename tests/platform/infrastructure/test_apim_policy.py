from datetime import UTC, datetime
from xml.etree import ElementTree

from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.domain.image_profiles import create_image_profile
from turnstile_core.integrations.apim_policy_components import (
    IMAGE_CONDITION,
    TEXT_CONDITION,
    parse_policy,
    validate_parent_policy,
)
from turnstile_core.integrations.ledger import ledger_stamp

ROOT = REPOSITORY_ROOT
POLICY_PATH = ROOT / "infra" / "policies" / "foundry-finops-policy.xml"
APIM_MODULE_PATH = ROOT / "infra" / "modules" / "apim-integration.bicep"
MAIN_TEMPLATE_PATH = ROOT / "infra" / "main.bicep"


def test_public_parent_supports_images_without_using_text_token_policies() -> None:
    policy = POLICY_PATH.read_text()
    root = parse_policy(policy)
    profile = create_image_profile(
        Settings.model_construct().image_generation_defaults, "unit-image"
    )
    validate_parent_policy(policy, policy, [profile])
    parents = {child: parent for parent in root.iter() for child in parent}
    for tag in ("llm-token-limit", "llm-emit-token-metric"):
        for node in root.iter(tag):
            assert parents[node].get("condition") == TEXT_CONDITION
    limits = list(root.iter("rate-limit-by-key"))
    assert len(limits) == 2
    assert all(
        int(node.get("increment-count", "0")) >= profile.burst_reservation_tokens for node in limits
    )
    for name in ("maxOutputBound", "applicationMaxOutputBound"):
        values = root.findall(f".//set-variable[@name='{name}']")
        image = next(node for node in values if parents[node].get("condition") == IMAGE_CONDITION)
        assert image.get("value") == '@((long)context.Variables["imageOutputBound"])'
    image_usage = next(
        node
        for node in root.findall(".//set-variable[@name='usagePayload']")
        if parents[node].get("condition") == IMAGE_CONDITION
    )
    assert "JTokenType.Integer" in image_usage.get("value", "")
    assert "input - cached" in image_usage.get("value", "")


def test_apim_model_path_never_calls_back_into_this_application() -> None:
    policy = POLICY_PATH.read_text()

    ElementTree.fromstring(policy)
    # The gateway may call a platform service, and it does: the budget ledger lives in
    # Table Storage precisely so admission never waits on the FinOps API. What it must
    # never do is call back into an application we operate, which is why the old
    # policy-snapshot endpoint was deleted.
    assert "policy-snapshot" not in policy
    assert "X-APIM-Policy-Key" not in policy
    assert "azurewebsites.net" not in policy
    # Person admission uses one read and one reservation write. Application admission
    # adds a Subscription mapping read, one Application partition read, and one write.
    # Every request remains inside Table Storage; none calls the Turnstile API.
    assert policy.count("<send-request") == 5
    assert policy.count('resource="https://storage.azure.com/"') == 5


def test_internal_attribution_headers_never_reach_a_provider() -> None:
    policy = POLICY_PATH.read_text()
    header_names = (
        "x-request-id",
        "x-org-id",
        "x-org-name",
        "x-department-id",
        "x-department-name",
        "x-project-id",
        "x-project-name",
        "x-agent-id",
        "x-agent-name",
        "x-model-id",
        "x-runtime-id",
        "x-user-id",
        "x-user-name",
        "x-request-source",
        "x-hive-organization",
        "x-hive-department",
        "x-hive-project",
        "x-hive-agent",
        "x-hive-user",
        "x-hive-workflow",
        "x-hive-run-id",
        "x-hive-turn-index",
        "x-hive-model",
        "x-hive-runtime",
        "x-turnstile-pool-member",
        "x-turnstile-pool-runtime",
    )

    for name in header_names:
        assert f'<set-header name="{name}" exists-action="delete" />' in policy

    cleanup = policy.index('<set-header name="x-request-id" exists-action="delete" />')
    inbound_end = policy.index("</inbound>")
    assert cleanup < inbound_end
    assert policy.index('name="telemetryWorkflow"') < cleanup
    assert policy.index('name="telemetryOrganization"') < cleanup
    assert policy.index('name="telemetryModel"') < cleanup
    outbound = policy.split("<outbound>", 1)[1].split("</outbound>", 1)[0]
    assert 'context.Variables[&quot;telemetryOrganization&quot;]' in outbound
    assert 'context.Variables[&quot;telemetryUserId&quot;]' in outbound
    assert 'context.Variables[&quot;telemetryWorkflow&quot;]' in outbound
    assert 'context.Variables[&quot;telemetryModel&quot;]' in outbound
    for name in header_names:
        assert f'GetValueOrDefault(&quot;{name}&quot;' not in outbound


def test_apim_budget_admission_is_conservative_and_fails_open() -> None:
    policy = POLICY_PATH.read_text()

    # The reservation must be bounded by the request body, not by the response usage.
    # Every prompt token is at least one byte, so this can never understate the request
    # and it covers the cached portion that llm-token-limit cannot see.
    assert 'name="inputBound"' in policy
    assert "context.Request.Body.As&lt;byte[]&gt;(preserveContent: true).Length" in policy

    # An unreachable or unconfigured ledger leaves `available` false, and the guard
    # requires `available` before it can deny. A cost control must not cause an outage.
    assert 'ignore-error="true"' in policy
    assert 'state[&quot;available&quot;] = false;' in policy
    assert "catch (Exception) { }" in policy
    assert 'name="ledgerEnforced"' in policy
    assert (
        '(bool)((JObject)context.Variables[&quot;ledgerState&quot;])[&quot;available&quot;]'
        in policy
    )

    # Every R that remains is one request not yet settled by correlation ID. A global
    # watermark lets one pending stream retain every later request's upper bound, so the
    # admission path must sum all R rows and must not carry that frontier at all.
    assert 'rowKey.StartsWith(&quot;R|&quot;)' in policy
    assert 'reserved += (long?)row[&quot;Reserved&quot;] ?? 0;' in policy
    assert "CompareOrdinal" not in policy
    assert "Watermark" not in policy
    # Table returns at most 1,000 entities. A truncated set must fail open visibly rather
    # than enforce from an incomplete reservation total.
    assert "x-ms-continuation-NextPartitionKey" in policy
    assert "x-ms-continuation-NextRowKey" in policy
    assert "return state;" in policy
    assert policy.index("x-ms-continuation-NextPartitionKey") < policy.index(
        'state[&quot;available&quot;] = true;'
    )


def test_apim_writes_the_reservation_that_makes_admission_real_time() -> None:
    policy = POLICY_PATH.read_text()

    # Without this write the check degenerates into post-hoc counting: ConfirmedUsed trails
    # the request by minutes, so a burst would sail straight through a spent budget.
    assert 'name="reservedTokens"' in policy
    # Insert Entity: the keys travel in the body because the entity-key URL form
    # `Table(PartitionKey='..')` cannot survive the policy parser. Insert also refuses to
    # overwrite, which is what a reservation needs.
    assert "<set-method>POST</set-method>" in policy
    assert "<value>return-no-content</value>" in policy
    assert 'entity[&quot;RowKey&quot;] = &quot;R|&quot; + stamp' in policy
    assert 'entity[&quot;Reserved&quot;]' in policy
    # The stamp format remains shared with the Function for deterministic ordering and
    # diagnostics, while settlement identity is the correlation ID suffix.
    assert "yyyy-MM-dd'T'HH:mm:ss.fff&quot;) + &quot;Z&quot;" in policy
    # Pin the two formats to each other rather than trusting the format strings to look alike.
    sample = datetime(2026, 7, 27, 14, 22, 3, 456789, tzinfo=UTC)
    assert ledger_stamp(sample) == sample.strftime("%Y-%m-%dT%H:%M:%S.") + "456Z"
    assert len(ledger_stamp(sample)) == len("yyyy-MM-ddTHH:mm:ss.fffZ")
    # Written in audit mode too: "who would have been blocked" has to be measured.
    assert 'name="ledgerConfigured"' in policy
    assert policy.index('name="ledgerConfigured"') < policy.index("<set-method>POST</set-method>")


def test_apim_clamps_the_output_ceiling_instead_of_only_rejecting() -> None:
    policy = POLICY_PATH.read_text()

    # Rewriting max_tokens down to the remaining allowance makes an overspend physically
    # impossible while still letting a request through, which a bare 403 does not.
    assert 'name="clampedMaxTokens"' in policy
    assert 'name="maxOutputBound"' in policy
    # OpenAI allows max_tokens to be omitted; reserving zero output would understate the call.
    assert "__DEFAULT_MAX_OUTPUT_TOKENS__" in policy
    assert "param defaultMaxOutputTokens int" in APIM_MODULE_PATH.read_text()
    # Exactly one rewrite of the *caller's* request body. Two would each re-read and
    # re-serialise it and the second would silently discard the first. The ledger entity
    # body is a different document inside send-request, so anchor on the rewrite's content.
    assert policy.count("var raw = context.Request.Body.As&lt;string&gt;") == 1
    assert "body[&quot;max_completion_tokens&quot;]" in policy
    # The clamp must be decided before the body is rewritten.
    assert policy.index('name="clampedMaxTokens"') < policy.index(
        "var raw = context.Request.Body.As&lt;string&gt;"
    )


def test_responses_is_a_first_class_openai_inference_operation() -> None:
    policy = POLICY_PATH.read_text()
    module = APIM_MODULE_PATH.read_text()

    assert "name: 'responses'" in module
    assert "urlTemplate: '/responses'" in module
    assert "name: 'responses-compact'" in module
    assert "urlTemplate: '/responses/compact'" in module
    responses_operation = module.split("resource responsesOperation ", 1)[1].split(
        "resource responsesCompactOperation ", 1
    )[0]
    responses_compact_operation = module.split(
        "resource responsesCompactOperation ", 1
    )[1].split("resource anthropicMessagesOperation ", 1)[0]
    assert "apiPolicy" in responses_operation
    assert "apiPolicy" in responses_compact_operation
    assert "provider-neutral-responses-policy.xml" in module
    assert (
        "resource responsesOperationPolicy "
        "'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = "
        "if (!preserveLegacyProviderRouting) {"
        in module
    )
    assert (
        "resource responsesCompactOperationPolicy "
        "'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = "
        "if (!preserveLegacyProviderRouting) {"
        in module
    )
    assert 'context.Operation.Id == &quot;responses&quot;' in policy
    assert 'context.Operation.Id == &quot;responses-compact&quot;' in policy
    anthropic, inference = (
        policy.split(f'name="{name}" value="', 1)[1].split('" />', 1)[0]
        for name in ("isAnthropicOperation", "isInferenceOperation")
    )
    assert "responses" not in anthropic
    assert "isResponsesOperation" in inference
    assert policy.count(
        'GetValueOrDefault&lt;bool&gt;(&quot;selectedModelKnown&quot;, '
        '!(bool)context.Variables[&quot;isResponsesOperation&quot;])'
    ) == 2
    assert 'body[&quot;max_output_tokens&quot;]' in policy
    assert policy.index('name="maxOutputBound"') < policy.index(
        'body[&quot;max_output_tokens&quot;] = clamp'
    )


def test_responses_compact_has_strict_admission_without_output_field_injection() -> None:
    policy = POLICY_PATH.read_text()

    assert 'name="isResponsesCompactOperation"' in policy
    assert '&quot;selectedContextWindow&quot;' in policy
    assert 'name="compactOutputBoundKnown"' in policy
    assert '&quot;compact_budget_bound_unavailable&quot;' in policy
    assert (
        'if ((bool)context.Variables[&quot;isResponsesCompactOperation&quot;]) '
        '{ return -1L; }'
        in policy
    )
    assert 'body.Remove(&quot;client_metadata&quot;)' in policy
    assert (
        'new[] { &quot;model&quot;, &quot;input&quot;, &quot;instructions&quot;, '
        '&quot;previous_response_id&quot; }'
        in policy
    )
    assert policy.index('name="isResponsesOperation"') < policy.index(
        'name="x-codex-installation-id" exists-action="delete"'
    )


def test_responses_rehydrates_missing_output_text_annotations() -> None:
    policy = POLICY_PATH.read_text()

    assert 'var input = body[&quot;input&quot;] as JArray;' in policy
    assert (
        'string.Equals((string)contentItem[&quot;type&quot;], &quot;output_text&quot;, '
        'StringComparison.Ordinal)'
        in policy
    )
    assert 'contentItem[&quot;annotations&quot;] == null' in policy
    assert policy.count('contentItem[&quot;annotations&quot;] = new JArray();') == 1
    assert policy.index('contentItem[&quot;annotations&quot;] = new JArray();') < policy.index(
        'var compactBody = new JObject();'
    )


def test_apim_marks_every_fail_open_admission_for_the_dashboard() -> None:
    policy = POLICY_PATH.read_text()

    # Failing open is the deliberate choice: a budget overrun is recoverable, lost work is not.
    # A silent fail-open is not, because it makes enforcement look permanently healthy.
    assert "ledger_unavailable" in policy
    assert "ledger_write_failed" in policy
    # An unallocated person is not a failure; that is the existing "no budget, no block" rule.
    assert "budget_unconfigured" in policy
    assert 'new JProperty(&quot;budget_admission&quot;' in policy
    # One retry before giving up, with a short timeout so the fallback stays cheap.
    assert "<retry" in policy
    assert 'timeout="2"' in policy


def test_apim_reports_a_budget_denial_before_returning_it() -> None:
    policy = POLICY_PATH.read_text()

    # return-response aborts the pipeline, so the outbound log-to-eventhub never runs. Without
    # an explicit emission here a blocked employee leaves no trace at all, which makes
    # enforcement look like it never fired. That is worse than a silent fail-open.
    denial = policy.index('&quot;monthly_token_budget_exhausted&quot;')
    # Person model/budget and Application denial emitters, plus outbound and on-error.
    assert policy.count("<log-to-eventhub") == 5
    budget_log = policy.rfind("<log-to-eventhub", 0, denial)
    budget_return = policy.index("<return-response>", denial)
    assert budget_log < denial < budget_return
    assert '&quot;budget_admission&quot;, &quot;denied&quot;' in policy
    # The zeros are exact, not missing: nothing reached the provider.
    assert 'new JProperty(&quot;estimated&quot;, false)' in policy
    assert denial > 0


def test_apim_enforces_model_access_from_the_same_ledger_read() -> None:
    policy = POLICY_PATH.read_text()

    # Model access was unenforced between the 2026-07-24 Product cutover and this policy:
    # the synchronous call back into the application was removed and nothing replaced it,
    # so a person configured to deny all models kept receiving HTTP 200.
    assert '&quot;M&quot;' in policy
    assert "modelsAllowed" in policy
    assert "modelAdmission" in policy
    assert 'new JProperty(&quot;model_admission&quot;' in policy


def test_every_event_hub_message_carries_the_admission_outcome() -> None:
    policy = POLICY_PATH.read_text()

    # The outbound message and the on-error message are separate field lists, so a field
    # added to one is silently absent from the other. That gap made a request which WAS
    # admitted and then failed mid-transport land with a null admission, and null means
    # "never reached admission" — the trace stated the opposite of what happened.
    # Five emitters: person model denial, person budget denial, Application denial,
    # outbound, and on-error.
    assert policy.count("<log-to-eventhub") == 5
    assert policy.count('new JProperty(&quot;budget_admission&quot;') == 5
    assert policy.count('new JProperty(&quot;model_admission&quot;') == 5

    # All five platform calls target Table Storage. No request calls the application.
    assert policy.count("<send-request") == 5

    # Membership is delimited on both ends so one identifier cannot match a prefix of a
    # longer one, and it is tested against the delimited set the Function projects.
    assert '&quot;|&quot; + requested + &quot;|&quot;' in policy

    # No policy row means the person was never restricted. Starting to block them would
    # take out everyone who has not been configured yet.
    assert "policy_unconfigured" in policy

    # The denial has to be reported before the pipeline aborts, same as the budget one.
    model_denial = policy.index('&quot;model_not_assigned&quot;')
    assert policy.index("<log-to-eventhub") < model_denial


def test_apim_checks_model_access_before_spending_budget_headroom() -> None:
    policy = POLICY_PATH.read_text()

    # A model the person may not use at all should be refused regardless of how much
    # allowance is left, and the refusal reason should be the specific one.
    assert policy.index("modelAdmission&quot;] == &quot;denied&quot;") < policy.index(
        '&quot;monthly_token_budget_exhausted&quot;'
    )


def test_apim_employee_quota_moved_off_the_token_counter() -> None:
    policy = POLICY_PATH.read_text()

    # llm-token-limit counts prompt and completion only, so it under-counts cached
    # Anthropic traffic by orders of magnitude and cannot carry the monthly allowance.
    assert "__EMPLOYEE_MONTHLY_TOKEN_QUOTA__" not in policy
    # Burst protection stays: under-counting means it fires late, not never.
    assert 'tokens-per-minute="__EMPLOYEE_TOKENS_PER_MINUTE__"' in policy
    # Deleting this variable would silently break streamed telemetry, which consumes it.
    assert 'tokens-consumed-variable-name="tokensConsumed"' in policy
    # Application monthly quota now uses exact Ledger C + per-request R. The native
    # counter remains burst-only because it cannot account for cached tokens exactly.
    assert 'token-quota="__MONTHLY_TOKEN_QUOTA__"' not in policy
    assert 'name="applicationLedgerPartition"' in policy
    assert 'name="applicationReservedTokens"' in policy


def test_application_output_clamp_uses_monthly_budget_not_tpm() -> None:
    root = ElementTree.fromstring(POLICY_PATH.read_text())
    clamp = root.find(".//set-variable[@name='applicationClampedMaxTokens']")
    assert clamp is not None
    expression = " ".join(clamp.attrib["value"].split())
    assert "tokensPerMinute" not in expression
    assert "reservationHeadroom" not in expression
    assert (
        'if (!(bool)context.Variables["applicationLedgerEnforced"]) { return -1L; }'
        in expression
    )
    assert (
        'long headroom = (long)context.Variables["applicationLedgerRemaining"] '
        '- (long)context.Variables["applicationInputBound"];'
        in expression
    )
    assert "if (headroom <= 0) { return -1L; }" in expression
    assert (
        'return (long)context.Variables["applicationMaxOutputBound"] '
        '> headroom ? headroom : -1L;'
        in expression
    )

    limits = root.findall(".//llm-token-limit")
    assert len(limits) == 2
    assert {limit.attrib["tokens-per-minute"] for limit in limits} == {
        "__TOKENS_PER_MINUTE__", "__EMPLOYEE_TOKENS_PER_MINUTE__",
    }
    assert all(limit.attrib["estimate-prompt-tokens"] == "true" for limit in limits)
    assert all(limit.attrib["retry-after-header-name"] == "Retry-After" for limit in limits)
    assert all("token-quota" not in limit.attrib for limit in limits)
    assert any("finops:subscription:" in limit.attrib["counter-key"] for limit in limits)
    assert any("finops:employee:" in limit.attrib["counter-key"] for limit in limits)


def test_apim_token_metrics_have_only_the_fixed_api_dimension() -> None:
    root = ElementTree.fromstring(POLICY_PATH.read_text())
    metrics = root.findall(".//llm-emit-token-metric")
    assert len(metrics) == 1
    assert metrics[0].attrib == {"namespace": "FinOps"}
    assert [dimension.attrib for dimension in metrics[0].findall("dimension")] == [
        {"name": "API ID"}
    ]
    policy_before_event_hub = POLICY_PATH.read_text().split("<log-to-eventhub")[0]
    assert "userMetricShard" not in policy_before_event_hub
    assert "CorrelationId" not in policy_before_event_hub
    assert not any(
        dimension.get("value") for dimension in metrics[0].findall("dimension")
    )


def test_event_hub_retains_request_and_business_dimensions_for_clustering() -> None:
    policy = POLICY_PATH.read_text()

    # Five emitters cover person model/budget denial, Application denial, completion and error.
    for field in (
        "correlation_id",
        "department",
        "user_id",
        "model_id",
        "request_source",
        "gateway_profile_id",
        "apim_subscription_id",
        "application_actor_type",
        "application_actor_id",
        "application_admission",
    ):
        assert policy.count(f'new JProperty(&quot;{field}&quot;') == 5


def test_application_identity_comes_from_apim_and_validated_token_context() -> None:
    policy = POLICY_PATH.read_text()
    module = APIM_MODULE_PATH.read_text()

    assert 'name="apimSubscriptionId"' in policy
    assert "context.Subscription?.Id" in policy
    assert "__GATEWAY_PROFILE_ID__" in policy
    assert "param gatewayProfileId string" in module
    assert "'__GATEWAY_PROFILE_ID__'" in module
    assert 'new JValue(&quot;person&quot;)' in policy
    assert '&quot;service:&quot; + subscriptionId' in policy


def test_key_calls_require_a_live_subscription_owner_and_override_client_identity() -> None:
    policy = POLICY_PATH.read_text()

    assert '&quot;OwnerUserId&quot;' in policy
    assert '&quot;OwnerEnabled&quot;' in policy
    assert '&quot;TenantId&quot;' in policy
    assert '&quot;TenantEnabled&quot;' in policy
    assert '&quot;OrganizationUnitId&quot;' in policy
    assert '&quot;OrganizationUnitEnabled&quot;' in policy
    assert '&quot;application_unassigned&quot;' in policy
    assert (
        '@((string)context.Variables[&quot;applicationAdmission&quot;] == &quot;ok&quot;'
        in policy
    )
    assert (
        '(bool)context.Variables[&quot;applicationAdmission&quot;] == &quot;ok&quot;'
        not in policy
    )
    assert 'name="x-user-id" exists-action="override"' in policy
    assert 'name="x-user-name" exists-action="override"' in policy
    assert 'name="x-org-id" exists-action="override"' in policy
    assert 'name="x-department-id" exists-action="override"' in policy
    assert '&quot;telemetryApplicationActorType&quot;' in policy
    assert '&quot;telemetryApplicationActorId&quot;' in policy


def test_application_denials_keep_the_mapped_owner_attribution() -> None:
    policy = POLICY_PATH.read_text()
    denial = policy.index("name=\"applicationDenialReason\"")
    log_start = policy.index("<log-to-eventhub", denial)
    log_end = policy.index("</log-to-eventhub>", log_start)
    denial_log = policy[log_start:log_end]

    assert '&quot;ownerUserId&quot;' in denial_log
    assert '&quot;application_actor_type&quot;' in denial_log
    assert '&quot;application_actor_id&quot;' in denial_log


def test_application_admission_uses_dynamic_ledger_mapping_and_exact_reservation() -> None:
    policy = POLICY_PATH.read_text()

    assert '&quot;app-map|__GATEWAY_PROFILE_ID__&quot;' in policy
    assert policy.count(
        '.Replace(&quot;\\u0027&quot;, &quot;\\u0027\\u0027&quot;)'
    ) == 2
    assert '.Replace(&quot;\'&quot;' not in policy
    assert 'response-variable-name="applicationMapRead"' in policy
    assert 'state[&quot;applicationId&quot;]' in policy
    assert '&quot;app|&quot; + (string)((JObject)context.Variables' in policy
    assert 'response-variable-name="applicationLedgerRead"' in policy
    assert (
        'entity[&quot;Reserved&quot;] = (long)context.Variables'
        '[&quot;applicationReservedTokens&quot;]'
        in policy
    )
    assert '&quot;application_model_denied&quot;' in policy
    assert '&quot;application_monthly_token_budget_exhausted&quot;' in policy
    assert '&quot;application_ledger_write_failed&quot;' in policy
    assert policy.index('name="applicationModelAdmission"') < policy.index(
        'name="applicationBudgetCannotFit"'
    )


def test_apim_attributes_the_calling_client_application_to_an_agent() -> None:
    policy = POLICY_PATH.read_text()
    module = APIM_MODULE_PATH.read_text()

    # The Agent comes from the token's authorized-party claim, never from a caller header, so a
    # desktop client cannot claim to be a different Agent.
    assert "&quot;azp&quot;" in policy
    assert "__EMPLOYEE_CLIENT_MAP__" in policy
    assert '<value>@((string)context.Variables[&quot;clientAgentId&quot;])</value>' in policy
    assert '<value>@((string)context.Variables[&quot;clientAgentName&quot;])</value>' in policy
    assert "param employeeClientMap object" in module


def test_apim_derives_the_model_for_subscription_callers_that_omit_the_header() -> None:
    policy = POLICY_PATH.read_text()

    # Without this the request body names the model but telemetry records `unattributed`.
    assert 'name="subscriptionModel"' in policy
    subscription_model_branch = policy.split(
        '<set-variable name="subscriptionModel"', 1
    )[1].split("</when>", 1)[0]
    # The body is the source of truth for APIM subscriptions; caller-supplied model
    # attribution must never override model allow-listing or telemetry.
    assert '<set-header name="x-model-id" exists-action="override">' in subscription_model_branch
    assert '<set-header name="x-hive-model" exists-action="override">' in subscription_model_branch
    assert (
        '<set-header name="x-request-source" exists-action="override">'
        in subscription_model_branch
    )


def test_subscription_callers_cannot_forge_project_or_agent_billing_attribution() -> None:
    policy = POLICY_PATH.read_text()
    subscription_branch = policy.split(
        '<set-variable name="subscriptionAgent"', 1
    )[1].split('<set-variable name="subscriptionModel"', 1)[0]

    for header in ("x-project-id", "x-project-name", "x-agent-id", "x-agent-name"):
        assert f'<set-header name="{header}" exists-action="override">' in subscription_branch
    assert subscription_branch.count('>unattributed</value>') >= 4
    model_branch = policy.split(
        '<set-variable name="subscriptionModel"', 1
    )[1].split("</when>", 1)[0]
    assert '<set-header name="x-request-source" exists-action="override">' in model_branch


def test_apim_attributes_mapped_subscriptions_to_trusted_agents() -> None:
    policy = POLICY_PATH.read_text()
    module = APIM_MODULE_PATH.read_text()
    main = MAIN_TEMPLATE_PATH.read_text()

    subscription_branch = policy.split(
        '<set-variable name="subscriptionAgent"', 1
    )[1].split('<set-variable name="subscriptionModel"', 1)[0]
    assert "context.Subscription.Id" in subscription_branch
    assert "__SUBSCRIPTION_AGENT_MAP__" in subscription_branch
    assert "?? new JObject();" in subscription_branch
    assert (
        'context.Variables[&quot;subscriptionAgent&quot;])[&quot;id&quot;] != null'
        in subscription_branch
    )
    assert (
        'context.Variables[&quot;subscriptionAgent&quot;])[&quot;name&quot;] != null'
        in subscription_branch
    )
    assert '<set-header name="x-agent-id" exists-action="override">' in subscription_branch
    assert '<set-header name="x-agent-name" exists-action="override">' in subscription_branch
    assert "param subscriptionAgentMap object = {}" not in module
    assert "param subscriptionAgentMap object" in module
    assert "__SUBSCRIPTION_AGENT_MAP__" in module
    assert "param subscriptionAgentMap object = {}" in main
    assert (
        'state[&quot;applicationSlug&quot;] = (string)row[&quot;ApplicationSlug&quot;]'
        in policy
    )
    assert (
        'var applicationType = (string)row[&quot;ApplicationType&quot;]'
        in policy
    )
    assert (
        'state[&quot;applicationType&quot;] = applicationType;'
        in policy
    )
    assert (
        '&quot;agent-&quot; + (string)((JObject)context.Variables['
        '&quot;applicationMapState&quot;])[&quot;applicationSlug&quot;]'
        in policy
    )


def test_apim_normalizes_openai_prompt_tokens_to_exclude_cache() -> None:
    policy = POLICY_PATH.read_text()

    # Chat's prompt_tokens and Responses' input_tokens include their cached subset, while
    # Anthropic's input_tokens excludes it. Storing either raw OpenAI value alongside
    # cached_tokens would count cache twice in both the token total and the cost.
    assert "var inclusiveInput = promptTokens" in policy
    assert "inclusiveInput.Value - cacheRead - cacheWrite" in policy
    assert "input_tokens_details&quot;]?[&quot;cached_tokens" in policy
    assert 'context.Variables[&quot;isResponsesOperation&quot;]' in policy
    assert "Math.Max(" in policy
    # The cache figures must be computed before the input that subtracts them.
    assert policy.index("var cacheRead") < policy.index("var promptTokens")
    assert policy.index("var cacheWrite") < policy.index("var promptTokens")
    # The Anthropic shape keeps its own already-exclusive value.
    assert "usage?[&quot;input_tokens&quot;]" in policy


def test_apim_cache_fallback_is_nullable_and_after_nested_measurements() -> None:
    for path in (POLICY_PATH, POLICY_PATH.with_name("llm-gateway-policy.xml")):
        root = ElementTree.fromstring(path.read_text())
        expression = next(
            node.attrib["value"] for node in root.iter("set-variable")
            if node.attrib.get("name") == "usagePayload"
            and 'usage?["prompt_tokens_details"]' in node.attrib.get("value", "")
        )
        nested = '(long?)usage?["prompt_tokens_details"]?["cached_tokens"]'
        fallback = '?? (long?)usage?["cached_tokens"]'
        assert nested in expression
        assert fallback in expression
        assert expression.index(nested) < expression.index(fallback)


def test_apim_strips_request_fields_the_databricks_backend_rejects() -> None:
    policy = POLICY_PATH.read_text()

    # Claude Desktop advertises `context_management` for every Sonnet 4+ model and sends it in
    # the request body. The Databricks Anthropic endpoint validates strictly and answers
    # `400 context_management: Extra inputs are not permitted`, so the gateway removes it.
    assert "&quot;context_management&quot;" in policy
    # Only the Anthropic route is rewritten, and an unparsable body passes through untouched.
    assert policy.index('name="isAnthropicOperation"') < policy.index(
        "var raw = context.Request.Body.As&lt;string&gt;"
    )
    assert "return raw;" in policy


def test_apim_token_limits_use_product_subscription_identity() -> None:
    policy = POLICY_PATH.read_text()
    module = APIM_MODULE_PATH.read_text()

    assert 'value="@(context.Subscription.Id)"' in policy
    assert "finops:subscription:" in policy
    assert 'token-quota="__MONTHLY_TOKEN_QUOTA__"' not in policy
    assert 'token-quota-period="Monthly"' not in policy
    assert 'tokens-per-minute="__TOKENS_PER_MINUTE__"' in policy
    assert "finops:user:" not in policy
    assert "Microsoft.ApiManagement/service/products@" in module
    assert "Microsoft.ApiManagement/service/products/apis@" in module
    assert "scope: product.id" in module


def test_apim_product_remains_the_subscription_key_authority() -> None:
    module = APIM_MODULE_PATH.read_text()
    policy = POLICY_PATH.read_text()
    api = module.split("resource api 'Microsoft.ApiManagement/service/apis@", 1)[1].split(
        "resource product '", 1
    )[0]
    product = module.split("resource product 'Microsoft.ApiManagement/service/products@", 1)[
        1
    ].split("resource productApi '", 1)[0]

    assert "header: 'Ocp-Apim-Subscription-Key'" in api
    assert "subscriptionRequired: true" in product
    assert "subscriptionsLimit: 1000" in product
    assert '<set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />' in policy
    assert '<set-query-parameter name="subscription-key" exists-action="delete" />' in policy


def test_count_tokens_is_an_anthropic_operation_but_never_billed() -> None:
    policy = POLICY_PATH.read_text()
    module = APIM_MODULE_PATH.read_text()

    # The operation has to exist. Without it APIM answers 404 and, because an unmatched
    # request leaves context.Operation.Id empty, the provider test falls through to
    # microsoft_foundry -- so Databricks-bound traffic was reported as Foundry errors.
    assert "name: 'anthropic-count-tokens'" in module
    assert "urlTemplate: '/v1/messages/count_tokens'" in module

    # Anthropic, so it reaches the Databricks backend and is attributed to the right
    # provider...
    assert "context.Operation.Id == &quot;anthropic-count-tokens&quot;" in policy
    anthropic, inference = (
        policy.split(f'name="{name}" value="', 1)[1].split('" />', 1)[0]
        for name in ("isAnthropicOperation", "isInferenceOperation")
    )
    assert "anthropic-count-tokens" in anthropic
    # ...but not inference, because it burns no model tokens. Counting it would charge
    # quota for a request that costs nothing and would put a zero-usage row in the trace.
    assert "anthropic-count-tokens" not in inference


def test_a_request_that_matched_no_operation_never_becomes_usage_telemetry() -> None:
    policy = POLICY_PATH.read_text()

    # The outbound emitter has always been guarded on isInferenceOperation; the on-error
    # one was not, so anything that failed before routing still produced a usage row. Every
    # attribution field fell back to "unattributed" and `runtime` fell back to the
    # hardcoded default, which is how an internet scanner sending HEAD /turnstile/llm/api/hello
    # was recorded as a model call on Microsoft Foundry. Measured over 90 days of
    # production: 400 such rows, 24.9% of all requests and 72.6% of all failures, reporting
    # the gateway at 65.6% success when it was really 87.5%.
    on_error = policy.split("<on-error>", 1)[1]
    guard = on_error.split("<log-to-eventhub", 1)[0]
    assert "isInferenceOperation" in guard

    # GetValueOrDefault, never a cast. on-error runs even when inbound did not, and the
    # variable is absent then -- those 400 rows all carry latency_ms = 0, which this
    # handler only emits when `requestStarted` is missing, and `requestStarted` is the
    # first statement of inbound. A cast would throw inside the error handler itself.
    assert "GetValueOrDefault&lt;bool&gt;(&quot;isInferenceOperation&quot;, false)" in guard
    assert "(bool)context.Variables[&quot;isInferenceOperation&quot;]" not in guard

    # The two denial emitters must keep firing: a refusal nobody can see stops someone
    # working and leaves the dashboard unable to explain why. They sit inside the
    # employee-token branch's own isInferenceOperation guard rather than carrying one.
    assert policy.count("<log-to-eventhub") == 5


def test_gateway_log_carries_the_only_available_client_fingerprint() -> None:
    module = APIM_MODULE_PATH.read_text()

    # Claude Desktop's Chat, Cowork and Code surfaces share one app registration and one
    # OAuth token cache, so the token's azp claim is identical for all three and the agent
    # map cannot tell them apart. The User-Agent can, and the gateway log is the only
    # channel that carries it -- our own Event Hub payload has no such field.
    assert "category: 'GatewayLogs'" in module
    # The category alone leaves RequestHeaders empty; the header must be whitelisted too.
    assert "'User-Agent'" in module
    # Headers only. A body allow-list here would put prompts in Log Analytics, which the
    # Event Hub payload is careful never to do.
    assert "body:" not in module.split("frontend: {", 1)[1].split("}\n  }\n}", 1)[0]


def test_claude_surfaces_are_refined_from_the_user_agent_and_degrade_coarsely() -> None:
    policy = POLICY_PATH.read_text()

    # The azp claim is identical for all three surfaces, so the agent name has to be refined
    # from something else. Every token of an entry must be present, because a surface is
    # identified by a combination -- "agent-sdk" alone appears in both Cowork and Code.
    assert "__EMPLOYEE_SURFACE_MAP__" in policy
    assert "GetValueOrDefault(&quot;User-Agent&quot;" in policy
    assert "StringComparison.OrdinalIgnoreCase" in policy
    # No match must fall back to the client-map name, not to unattributed. The User-Agent
    # carries version numbers that change with every Claude update; degrading to a coarser
    # but still correct "Claude Desktop" is recoverable, degrading to unknown is not.
    refinement = policy.split('name="clientAgentName"', 1)[1].split('}" />', 1)[0]
    assert refinement.rstrip().endswith("return name;")
    assert refinement.index("&quot;unattributed&quot;") < refinement.index("User-Agent")


def test_repository_does_not_preconfigure_employee_surface_attribution() -> None:
    main = MAIN_TEMPLATE_PATH.read_text()

    assert "param employeeSurfaceMap array = []" in main
    assert "param employeeClientMap object = {}" in main
