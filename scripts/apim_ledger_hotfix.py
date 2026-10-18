"""No-delete APIM private-ledger network repair; preview by default, sparse PATCH on apply."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

ARM = "https://management.azure.com"
ROOT = Path(__file__).resolve().parents[1]
RESOURCE = re.compile(
    r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
    r"(Microsoft.ApiManagement/service|Microsoft.Network/virtualNetworks)/([^/]+)$",
    re.IGNORECASE,
)


def resource_parts(value: str, kind: str) -> tuple[str, str, str]:
    match = RESOURCE.fullmatch(value)
    if match is None or match[3].lower() != kind.lower():
        raise ValueError(f"Expected a full {kind} resource ID")
    return match[1], match[2], match[4]


def plan_network(
    apim: dict[str, Any],
    network: dict[str, Any],
    subnet_name: str,
    prefix: str,
) -> dict[str, Any]:
    resource_parts(apim["id"], "Microsoft.ApiManagement/service")
    resource_parts(network["id"], "Microsoft.Network/virtualNetworks")
    if apim["id"].split("/")[2].lower() != network["id"].split("/")[2].lower():
        raise ValueError("Cross-subscription network repair is not supported")
    sku = apim["sku"]["name"]
    if sku not in {"Developer", "StandardV2"}:
        raise ValueError("Private ledger requires Developer or StandardV2; never auto-upgrade SKU")
    if apim["location"].lower().replace(" ", "") != network["location"].lower().replace(" ", ""):
        raise ValueError("APIM and VNet must be in the same region")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", subnet_name):
        raise ValueError("Invalid subnet name")
    desired = ipaddress.IPv4Network(prefix)
    if desired.prefixlen > 27:
        raise ValueError("APIM needs an IPv4 subnet of /27 or larger")
    ranges = [
        ipaddress.ip_network(x) for x in network["properties"]["addressSpace"]["addressPrefixes"]
    ]
    if not any(
        desired.subnet_of(parent) for parent in ranges if isinstance(parent, ipaddress.IPv4Network)
    ):
        raise ValueError("Subnet is outside the existing VNet")
    existing = None
    for subnet in network["properties"].get("subnets", []):
        props = subnet["properties"]
        if subnet["name"] == subnet_name:
            existing = subnet
            if props.get("addressPrefix") != prefix or not props.get("networkSecurityGroup"):
                raise ValueError("Existing subnet differs or has no NSG; refuse replacement")
            delegations = [x["properties"]["serviceName"] for x in props.get("delegations", [])]
            required = ["Microsoft.Web/serverFarms"] if sku == "StandardV2" else []
            if delegations != required:
                raise ValueError("Existing subnet delegation is incompatible with APIM SKU")
        else:
            for other in props.get("addressPrefixes", [props.get("addressPrefix")]):
                if other and desired.overlaps(ipaddress.ip_network(other)):
                    raise ValueError("Subnet overlaps an existing subnet")
    target = network["id"] + "/subnets/" + subnet_name
    properties = apim["properties"]
    if properties.get("provisioningState", "Succeeded") != "Succeeded":
        raise ValueError("APIM has a pending operation; wait before planning another change")
    current = properties.get("virtualNetworkConfiguration") or {}
    mode = properties.get("virtualNetworkType", "None")
    attached = mode == "External" and current.get("subnetResourceId", "").lower() == target.lower()
    if not attached and (mode != "None" or current.get("subnetResourceId")):
        raise ValueError("APIM already has a different network; refuse to move it")
    if (
        existing is not None
        and not attached
        and any(
            existing["properties"].get(key)
            for key in ("ipConfigurations", "serviceAssociationLinks", "resourceNavigationLinks")
        )
    ):
        raise ValueError("Existing subnet is occupied; refuse to attach another service")
    return {
        "version": 1,
        "apim_id": apim["id"],
        "vnet_id": network["id"],
        "sku": apim["sku"],
        "principal_id": apim["identity"]["principalId"],
        "subnet_id": target,
        "subnet_name": subnet_name,
        "prefix": prefix,
        "create_subnet": existing is None,
        "attach": not attached,
        "location": apim["location"],
        "previous_network": {"virtualNetworkType": mode, "virtualNetworkConfiguration": current},
    }


def validate_preview(result: dict[str, Any], allowed: set[str]) -> None:
    changes = result.get("changes", result.get("properties", {}).get("changes", []))
    for change in changes:
        if change["changeType"] not in {"NoChange", "Ignore"} and (
            change["changeType"] not in {"Create", "Modify"}
            or change["resourceId"].lower() not in {x.lower() for x in allowed}
        ):
            raise ValueError("Network preview changes an unapproved resource or deletes resources")


def private_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse symlinks and set private permissions before writing management-operation URLs.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(document, stream, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apim-resource-id", required=True)
    parser.add_argument("--vnet-resource-id", required=True)
    parser.add_argument("--subnet-name", default="snet-apim-ledger")
    parser.add_argument("--subnet-prefix", default="10.42.4.0/24")
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--probe-url")
    parser.add_argument("--probe-model")
    parser.add_argument("--probe-key-env", default="AIGATEWAY_PROBE_SUBSCRIPTION_KEY")
    parser.add_argument("--minimum-remaining-tokens", type=int, default=0)
    args = parser.parse_args()
    subscription, _, name = resource_parts(args.apim_resource_id, "Microsoft.ApiManagement/service")
    _, group, vnet_name = resource_parts(args.vnet_resource_id, "Microsoft.Network/virtualNetworks")

    def auth() -> dict[str, str]:
        output = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--subscription",
                subscription,
                "--resource",
                ARM + "/",
                "-o",
                "json",
            ],
            capture_output=True,
            check=True,
        )
        return {"Authorization": "Bearer " + json.loads(output.stdout)["accessToken"]}

    with httpx.Client(headers=auth(), timeout=90) as client:

        def read(rid: str, version: str) -> dict[str, Any]:
            response = client.get(ARM + rid, params={"api-version": version})
            response.raise_for_status()
            return dict(response.json())

        apim = read(args.apim_resource_id, "2024-05-01")
        network = read(args.vnet_resource_id, "2024-05-01")
        plan = plan_network(apim, network, args.subnet_name, args.subnet_prefix)
        private_json(args.plan_file, plan)
        if plan["create_subnet"]:
            nsg_name = "nsg-" + name + "-ledger"
            values = {
                "resourceGroupName": group,
                "location": network["location"],
                "virtualNetworkName": vnet_name,
                "apimSubnetName": args.subnet_name,
                "apimSubnetAddressPrefix": args.subnet_prefix,
                "networkSecurityGroupName": nsg_name,
                "apimSkuName": plan["sku"]["name"],
            }
            parameter_file = args.plan_file.with_suffix(".parameters.json")
            private_json(
                parameter_file, {"parameters": {k: {"value": v} for k, v in values.items()}}
            )
            command = [
                "az",
                "deployment",
                "sub",
                "--subscription",
                subscription,
                "--location",
                network["location"],
                "--name",
                "turnstile-apim-ledger-hotfix",
                "--template-file",
                str(ROOT / "infra/apim-ledger-subnet.bicep"),
                "--parameters",
                "@" + str(parameter_file),
            ]
            preview = subprocess.run(
                command[:3] + ["what-if", "--no-pretty-print", "-o", "json"] + command[3:],
                capture_output=True,
                check=True,
            )
            result = json.loads(preview.stdout)
            nsg_id = args.vnet_resource_id.split("/providers/")[0] + (
                "/providers/Microsoft.Network/networkSecurityGroups/" + nsg_name
            )
            # Never overwrite an unrelated NSG with the deterministic hotfix name.
            nsg = client.get(ARM + nsg_id, params={"api-version": "2024-05-01"})
            if nsg.status_code != 404:
                raise ValueError("Hotfix NSG already exists without its subnet; inspect manually")
            validate_preview(result, {plan["subnet_id"], nsg_id})
            private_json(args.plan_file.with_suffix(".whatif.json"), result)
            if args.apply:
                # Check the entire VNet again; do not create a subnet after concurrent changes.
                if read(args.vnet_resource_id, "2024-05-01") != network:
                    raise ValueError("VNet changed after preview; regenerate the plan")
                subprocess.run(command[:3] + ["create", "-o", "none"] + command[3:], check=True)
        if args.apply and plan["attach"]:
            if read(args.apim_resource_id, "2024-05-01") != apim:
                raise ValueError("APIM changed after preview; regenerate the plan")
            response = client.patch(
                ARM + args.apim_resource_id,
                params={"api-version": "2024-05-01"},
                json={
                    "properties": {
                        "virtualNetworkType": "External",
                        "virtualNetworkConfiguration": {"subnetResourceId": plan["subnet_id"]},
                    }
                },
            )
            response.raise_for_status()
            operation = response.headers.get("Azure-AsyncOperation") or response.headers.get(
                "Location"
            )
            if operation:
                if urlparse(operation).netloc != urlparse(ARM).netloc:
                    raise ValueError("Unexpected management-operation host; do not transmit token")
                private_json(args.plan_file.with_suffix(".operation.json"), {"url": operation})
                deadline = time.monotonic() + args.timeout_seconds
                while time.monotonic() < deadline:
                    client.headers.update(auth())
                    poll = client.get(operation)
                    if not poll.is_success:
                        raise ValueError(
                            f"Private operation polling returned HTTP {poll.status_code}"
                        )
                    status = poll.json().get("status")
                    if status == "Succeeded":
                        break
                    if status in {"Failed", "Canceled"}:
                        raise ValueError(
                            "APIM network operation failed; inspect the private journal"
                        )
                    time.sleep(30)
                else:
                    raise TimeoutError(
                        "Operation still pending; retain private journal and inspect Azure"
                    )
            after = read(args.apim_resource_id, "2024-05-01")
            if after["properties"].get("provisioningState") != "Succeeded":
                raise ValueError(
                    "APIM network is still pending; inspect the private operation journal"
                )
            verified = plan_network(
                after,
                read(args.vnet_resource_id, "2024-05-01"),
                args.subnet_name,
                args.subnet_prefix,
            )
            if (
                verified["attach"]
                or after["sku"] != apim["sku"]
                or (after["identity"]["principalId"] != plan["principal_id"])
            ):
                raise ValueError("APIM network readback or SKU/identity preservation failed")
            for key in ("hostnameConfigurations", "customProperties", "additionalLocations"):
                if after["properties"].get(key) != apim["properties"].get(key):
                    raise ValueError("Unrelated APIM configuration changed during the operation")
        if args.probe_url:
            if not args.apply or not args.probe_model or not os.environ.get(args.probe_key_env):
                raise ValueError(
                    "Probe requires --apply, --probe-model and the key environment variable"
                )
            if urlparse(args.probe_url).scheme != "https" or (
                urlparse(args.probe_url).netloc != urlparse(apim["properties"]["gatewayUrl"]).netloc
            ):
                raise ValueError("Probe URL must belong to the selected HTTPS APIM gateway")
            probe = httpx.post(
                args.probe_url,
                headers={
                    "Ocp-Apim-Subscription-Key": os.environ[args.probe_key_env],
                },
                json={
                    "model": args.probe_model,
                    "input": "Reply exactly OK.",
                    "max_output_tokens": 32,
                    "stream": False,
                },
                timeout=120,
            )
            probe.raise_for_status()
            remaining = int(probe.headers.get("x-ratelimit-remaining-tokens", "0"))
            if remaining < args.minimum_remaining_tokens:
                raise ValueError("Probe did not demonstrate the requested rate-limit floor")
            print(f"Business probe HTTP {probe.status_code}; remaining tokens {remaining}")
        print(
            json.dumps(
                {
                    "apply": args.apply,
                    "attach_needed": plan["attach"],
                    "subnet_creation_needed": plan["create_subnet"],
                    "sku_preserved": plan["sku"],
                    "storage_and_api_policies_untouched": True,
                }
            )
        )


if __name__ == "__main__":
    main()
