from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.apim_ledger_hotfix import plan_network, private_json, validate_preview

BASE = "/subscriptions/sub/resourceGroups/group/providers/"


def resources(sku: str = "Developer") -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "id": BASE + "Microsoft.ApiManagement/service/gateway",
            "location": "southeastasia",
            "sku": {"name": sku, "capacity": 1},
            "identity": {"principalId": "same-identity"},
            "properties": {"virtualNetworkType": "None"},
        },
        {
            "id": BASE + "Microsoft.Network/virtualNetworks/platform",
            "location": "southeastasia",
            "properties": {"addressSpace": {"addressPrefixes": ["10.42.0.0/16"]}, "subnets": []},
        },
    )


@pytest.mark.parametrize("sku", ["Developer", "StandardV2"])
def test_plan_is_idempotent_and_never_changes_sku(sku: str) -> None:
    apim, network = resources(sku)
    before = deepcopy((apim, network))
    plan = plan_network(apim, network, "snet-apim-ledger", "10.42.4.0/24")
    assert (apim, network) == before
    assert plan["attach"] and plan["create_subnet"]
    assert plan["sku"] == apim["sku"]
    apim["properties"] = {
        "virtualNetworkType": "External",
        "virtualNetworkConfiguration": {"subnetResourceId": plan["subnet_id"]},
    }
    network["properties"]["subnets"] = [
        {
            "name": "snet-apim-ledger",
            "properties": {
                "addressPrefix": "10.42.4.0/24",
                "networkSecurityGroup": {"id": "keep-custom-nsg"},
                "delegations": [{"properties": {"serviceName": "Microsoft.Web/serverFarms"}}]
                if sku == "StandardV2"
                else [],
            },
        }
    ]
    rerun = plan_network(apim, network, "snet-apim-ledger", "10.42.4.0/24")
    assert not rerun["attach"] and not rerun["create_subnet"]


def test_plan_rejects_unsupported_sku_overlap_and_existing_network_move() -> None:
    apim, network = resources("BasicV2")
    with pytest.raises(ValueError, match="never auto-upgrade"):
        plan_network(apim, network, "ledger", "10.42.4.0/24")
    apim["sku"]["name"] = "Developer"
    network["properties"]["subnets"] = [
        {
            "name": "unrelated",
            "properties": {
                "addressPrefix": "10.42.4.0/25",
            },
        }
    ]
    with pytest.raises(ValueError, match="overlaps"):
        plan_network(apim, network, "ledger", "10.42.4.0/24")
    network["properties"]["subnets"] = []
    apim["properties"]["virtualNetworkType"] = "Internal"
    with pytest.raises(ValueError, match="refuse to move"):
        plan_network(apim, network, "ledger", "10.42.4.0/24")


def test_preview_rejects_deletes_and_unrelated_changes() -> None:
    for resource, change in [("approved", "Delete"), ("unrelated", "Modify")]:
        with pytest.raises(ValueError):
            validate_preview(
                {"changes": [{"resourceId": resource, "changeType": change}]}, {"approved"}
            )
    validate_preview(
        {"changes": [{"resourceId": "approved", "changeType": "Create"}]}, {"approved"}
    )


def test_journal_is_private_and_refuses_symlinks(tmp_path: Path) -> None:
    file = tmp_path / "private.json"
    file.touch(mode=0o644)
    private_json(file, {"safe": True})
    assert file.stat().st_mode & 0o777 == 0o600
    link = tmp_path / "link.json"
    link.symlink_to(file)
    with pytest.raises(OSError):
        private_json(link, {})
