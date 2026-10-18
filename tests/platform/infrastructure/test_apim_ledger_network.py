from tests.support.paths import REPOSITORY_ROOT


def test_classic_apim_private_ledger_access_preserves_sku_and_identity() -> None:
    source = (REPOSITORY_ROOT / "infra/modules/apim-ledger-network-upgrade.bicep").read_text()
    assert "sku: apimSku" in source
    assert "identity: apimIdentity" in source
    assert "properties: union(currentApimProperties" in source
    assert "virtualNetworkType: 'External'" in source
    assert "delegations: []" in source
    assert "networkSecurityGroup: { id: nsg.id }" in source
    assert "sourceAddressPrefix: 'ApiManagement'" in source
    assert "destinationPortRange: '3443'" in source
    assert "Microsoft.Network/publicIPAddresses" not in source


def test_ledger_endpoint_is_normalized_before_policy_substitution() -> None:
    source = (REPOSITORY_ROOT / "infra/modules/apim-integration.bicep").read_text()
    assert "var normalizedLedgerTableEndpoint = endsWith(trim(ledgerTableEndpoint), '/')" in source
    assert "length(trim(ledgerTableEndpoint)) - 1" in source
    assert "normalizedLedgerTableEndpoint" in source[source.index("'__LEDGER_TABLE_ENDPOINT__'"):]
