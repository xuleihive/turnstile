
import pytest

from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.integrations.apim_application_tpm import (
    TPM_EXPRESSION,
    compose_application_tpm_policy,
)
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_policy_components import parse_policy, validate_parent_policy


def test_application_tpm_is_used_by_text_and_image_branches() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    root = parse_policy(source)
    value = root.find(".//set-variable[@name='effectiveApplicationTpm']")
    assert value is not None
    expression = value.attrib["value"]
    assert 'state["tokensPerMinute"]' in expression
    assert 'mapping["status"] == "ok"' in expression
    assert 'applicationAdmission' not in expression
    assert 'modelStatus' not in expression
    assert 'state["available"]' in expression
    assert "int.MaxValue" in expression
    assert 'state["enforce"]' not in expression
    subscription = [node for node in root.iter("llm-token-limit")
                    if "finops:subscription:" in node.attrib["counter-key"]]
    assert subscription[0].attrib["tokens-per-minute"] == TPM_EXPRESSION
    images = [node for node in root.iter("rate-limit-by-key")
              if "finops:images:subscription:" in node.attrib["counter-key"]]
    assert images[0].attrib["calls"] == TPM_EXPRESSION
    assert source.index('name="applicationLedgerState"') < source.index(
        'name="effectiveApplicationTpm"'
    )


def test_legacy_policy_upgrade_preserves_other_policy_components() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    import re
    legacy = re.sub(r'\n        <set-variable name="effectiveApplicationTpm".*?/>', '', source,
                    flags=re.DOTALL)
    legacy = legacy.replace(
        '@((int)context.Variables[&quot;effectiveApplicationTpm&quot;])', '__TOKENS_PER_MINUTE__',
    )
    upgraded = compose_application_tpm_policy(legacy)
    assert upgraded == source
    assert compose_application_tpm_policy(upgraded) == upgraded
    assert validate_parent_policy(legacy, source)
    before, after = parse_policy(legacy), parse_policy(upgraded)
    for section in ["backend", "outbound", "on-error"]:
        import xml.etree.ElementTree as ET
        left, right = before.find(section), after.find(section)
        assert left is not None and right is not None
        assert ET.tostring(left) == ET.tostring(right)
    with pytest.raises(PolicyCompilationError):
        compose_application_tpm_policy(legacy.replace("finops:subscription:", "unexpected:"))


def test_upgrade_normalizes_only_ledger_origin_trailing_slash() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    source = source.replace("__LEDGER_TABLE_ENDPOINT__", "https://ledger.table.core.windows.net/")
    assert "https://ledger.table.core.windows.net//" in source
    upgraded = compose_application_tpm_policy(source)
    assert "https://ledger.table.core.windows.net//" not in upgraded
    assert "https://ledger.table.core.windows.net/TurnstileLedger" in upgraded.replace(
        "__LEDGER_TABLE_NAME__", "TurnstileLedger"
    )
    assert compose_application_tpm_policy(upgraded) == upgraded


def test_early_candidate_upgrade_separates_model_admission_from_tpm() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    previous = source.replace(
        'var mapping = context.Variables.GetValueOrDefault&lt;JObject&gt;('
        '&quot;applicationMapState&quot;, null);\n'
        '          var mapped = mapping != null &amp;&amp; '
        '(string)mapping[&quot;status&quot;] == &quot;ok&quot;;',
        'var admission = context.Variables.GetValueOrDefault&lt;string&gt;('
        '&quot;applicationAdmission&quot;, &quot;&quot;);',
    ).replace('mapped &amp;&amp; configured', 'admission == &quot;ok&quot; &amp;&amp; configured')
    assert previous != source
    assert compose_application_tpm_policy(previous) == source
    assert validate_parent_policy(previous, source)
    assert compose_application_tpm_policy(source) == source
    custom = previous.replace('admission == &quot;ok&quot; &amp;&amp; configured', 'configured')
    with pytest.raises(PolicyCompilationError):
        validate_parent_policy(custom, source)
