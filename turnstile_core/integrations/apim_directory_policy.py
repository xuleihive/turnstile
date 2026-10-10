"""Versioned, fail-closed employee identity lookup inside the existing parent policy."""

from __future__ import annotations

import re
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from .apim_control_plane_contract import PolicyCompilationError
from .apim_policy_components import component_digest, parse_policy, serialize_policy

DIRECTORY_IDENTITY_POLICY_VERSION = 1
_MARKER = "directoryIdentityPolicyVersion"


def _variable(parent: ET.Element, name: str, value: str) -> None:
    ET.SubElement(parent, "set-variable", {"name": name, "value": value})


def _header(parent: ET.Element, name: str, value: str) -> None:
    header = ET.SubElement(parent, "set-header", {"name": name, "exists-action": "override"})
    ET.SubElement(header, "value").text = value


def _identity_block(cloud: str, endpoint: str, table: str) -> ET.Element:
    choose = ET.Element("choose")
    branch = ET.SubElement(choose, "when", {"condition": "@(true)"})
    _variable(branch, "directoryIdentityCloud", f'@("{cloud}")')
    _variable(branch, "directoryIdentityEndpoint", endpoint)
    _variable(branch, "directoryIdentityTable", table)
    _variable(
        branch,
        "directoryIdentityUrl",
        """@{
      Guid tenant;
      Guid oid;
      if (!Guid.TryParse((string)context.Variables["employeeTenant"], out tenant)
        || !Guid.TryParse((string)context.Variables["employeeObjectId"], out oid)) { return ""; }
      var pk = "I|1|" + (string)context.Variables["directoryIdentityCloud"]
        + "|" + tenant.ToString();
      var row = oid.ToString();
      var filter = Uri.EscapeDataString("PartitionKey eq '" + pk + "' and RowKey eq '" + row + "'");
      return (string)context.Variables["directoryIdentityEndpoint"] + "/"
        + (string)context.Variables["directoryIdentityTable"] + "()?$filter=" + filter;
    }""",
    )
    request = ET.SubElement(
        branch,
        "send-request",
        {
            "mode": "new",
            "response-variable-name": "directoryIdentityResponse",
            "timeout": "5",
            "ignore-error": "true",
        },
    )
    ET.SubElement(request, "set-url").text = '@((string)context.Variables["directoryIdentityUrl"])'
    ET.SubElement(request, "set-method").text = "GET"
    _header(request, "x-ms-version", "2020-12-06")
    _header(request, "Accept", "application/json;odata=nometadata")
    ET.SubElement(
        request,
        "authentication-managed-identity",
        {
            "resource": "https://storage.azure.com/",
        },
    )
    _variable(
        branch,
        "directoryIdentityJson",
        """@{
      var response = context.Variables.GetValueOrDefault<IResponse>(
        "directoryIdentityResponse", null);
      if (response == null || response.StatusCode != 200) { return ""; }
      try {
        if (response.Headers.ContainsKey("x-ms-continuation-NextPartitionKey")
          || response.Headers.ContainsKey("x-ms-continuation-NextRowKey")) { return ""; }
        var payload = response.Body.As<JObject>(preserveContent: true);
        var rows = payload["value"] as JArray;
        if (rows == null || rows.Count > 1 || payload["odata.nextLink"] != null
          || payload["@odata.nextLink"] != null) { return ""; }
        if (rows.Count == 0) { return "{}"; }
        if (!(rows[0] is JObject) || !rows[0].HasValues) { return ""; }
        return rows[0].ToString(Newtonsoft.Json.Formatting.None);
      } catch { return ""; }
    }""",
    )
    _variable(
        branch,
        "directoryIdentityStatus",
        """@{
      var response = context.Variables.GetValueOrDefault<IResponse>(
        "directoryIdentityResponse", null);
      if (response == null) { return "unavailable"; }
      if (response.StatusCode != 200) { return "unavailable"; }
      try {
        var row = JObject.Parse((string)context.Variables["directoryIdentityJson"]);
        if (row.Count == 0) { return "missing"; }
        if ((int?)row["ProtocolVersion"] != 1
          || (string)row["PartitionKey"] != "I|1|"
            + (string)context.Variables["directoryIdentityCloud"] + "|"
            + ((string)context.Variables["employeeTenant"]).ToLowerInvariant()
          || (string)row["RowKey"] !=
            ((string)context.Variables["employeeObjectId"]).ToLowerInvariant()
          || (string)row["Cloud"] != (string)context.Variables["directoryIdentityCloud"]
          || (string)row["TenantId"] !=
            ((string)context.Variables["employeeTenant"]).ToLowerInvariant()
          || (string)row["ObjectId"] !=
            ((string)context.Variables["employeeObjectId"]).ToLowerInvariant()
          || ((long?)row["DirectoryVersion"] ?? 0) < 1
          || ((long?)row["ProjectionSequence"] ?? 0) < 1) { return "invalid"; }
        if ((bool?)row["Disabled"] != false) { return "disabled"; }
        if (((long?)row["ExpiresAt"] ?? 0) <= DateTimeOffset.UtcNow.ToUnixTimeSeconds()) {
          return "expired";
        }
        foreach (var field in new [] { "UserId", "UserName", "OrganizationId",
          "OrganizationName", "DepartmentId", "DepartmentName" }) {
          if (string.IsNullOrWhiteSpace((string)row[field])
            || (string)row[field] == "unattributed") { return "invalid"; }
        }
        return "ok";
      } catch { return "invalid"; }
    }""",
    )
    reject = ET.SubElement(branch, "choose")
    missing = ET.SubElement(
        reject,
        "when",
        {
            "condition": '@((string)context.Variables["directoryIdentityStatus"] == "missing"'
            ' || (string)context.Variables["directoryIdentityStatus"] == "disabled")',
        },
    )
    denied = ET.SubElement(missing, "return-response")
    ET.SubElement(denied, "set-status", {"code": "403", "reason": "Forbidden"})
    _header(denied, "Content-Type", "application/json")
    ET.SubElement(denied, "set-body").text = (
        '{"type":"error","error":{"type":"directory_identity_denied",'
        '"message":"Employee identity is unmapped or disabled"}}'
    )
    unavailable = ET.SubElement(
        reject,
        "when",
        {
            "condition": '@((string)context.Variables["directoryIdentityStatus"] != "ok")',
        },
    )
    failed = ET.SubElement(unavailable, "return-response")
    ET.SubElement(failed, "set-status", {"code": "503", "reason": "Service Unavailable"})
    _header(failed, "Content-Type", "application/json")
    ET.SubElement(failed, "set-body").text = (
        '{"type":"error","error":{"type":"directory_identity_unavailable",'
        '"message":"Employee identity mapping is unavailable or expired"}}'
    )
    for name, field in (
        ("employeeName", "UserId"),
        ("employeeDepartmentId", "DepartmentId"),
        ("employeeDepartmentName", "DepartmentName"),
    ):
        _variable(
            branch,
            name,
            f'@((string)JObject.Parse((string)context.Variables["directoryIdentityJson"])["{field}"])',
        )
    for name, field in (
        ("x-user-id", "UserId"),
        ("x-user-name", "UserName"),
        ("x-org-id", "OrganizationId"),
        ("x-org-name", "OrganizationName"),
        ("x-department-id", "DepartmentId"),
        ("x-department-name", "DepartmentName"),
    ):
        _header(
            branch,
            name,
            f'@((string)JObject.Parse((string)context.Variables["directoryIdentityJson"])["{field}"])',
        )
    return choose


def compose_directory_identity_policy(
    source: str,
    *,
    cloud: str,
    endpoint: str,
    table: str,
) -> str:
    if cloud not in {"public", "usgov", "china"}:
        raise PolicyCompilationError("Unknown identity cloud")
    if endpoint != "__LEDGER_TABLE_ENDPOINT__":
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise PolicyCompilationError("Use the exact existing HTTPS ledger endpoint")
    if table != "__LEDGER_TABLE_NAME__" and (
        not table.isascii()
        or not table.isalnum()
        or not table[0].isalpha()
        or not 3 <= len(table) <= 63
    ):
        raise PolicyCompilationError("Invalid existing ledger table")
    root = parse_policy(source)
    if root.findall(f".//set-variable[@name='{_MARKER}']"):
        raise PolicyCompilationError("Directory identity policy is already present")
    inbound = root.find("inbound")
    employee = root.find("./inbound/choose/when/validate-azure-ad-token/..")
    if inbound is None or employee is None:
        raise PolicyCompilationError("The employee validation branch is missing")
    headers = employee.findall("./set-header[@name='x-request-source']")
    if len(headers) != 1 or employee.find(".//set-variable[@name='ledgerUrl']") is None:
        raise PolicyCompilationError("The reviewed employee attribution anchors are missing")
    ledger_url = employee.find(".//set-variable[@name='ledgerUrl']")
    assert ledger_url is not None
    ledger_expression = ledger_url.get("value", "")
    exact_target = f"{endpoint.rstrip('/')}/{table}()"
    configured = urlparse(endpoint)
    equivalent_targets = [
        urlparse(value) for value in re.findall(r'"(https://[^"]+)"', ledger_expression)
    ]
    same_table = any(
        target.scheme == configured.scheme
        and target.netloc == configured.netloc
        and target.path.lstrip("/") == f"{table}()"
        and target.query == "$filter="
        and not target.fragment
        for target in equivalent_targets
    )
    if exact_target not in ledger_expression and not same_table:
        raise PolicyCompilationError("Identity lookup must reuse the existing employee ledger")
    partition = employee.find(".//set-variable[@name='ledgerPartition']")
    reservations = [
        node for node in employee.iter("set-body") if 'entity["RowKey"] = "R|"' in (node.text or "")
    ]
    if partition is None or len(reservations) != 1:
        raise PolicyCompilationError("Employee reservation anchors are missing")
    original_period = 'DateTime.UtcNow.ToString("yyyy-MM")'
    original_stamp = """DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fff")"""
    if original_period not in partition.get("value", "") or original_stamp not in (
        reservations[0].text or ""
    ):
        raise PolicyCompilationError("Employee reservation period is not a reviewed version")
    partition.set(
        "value",
        partition.get("value", "").replace(
            original_period,
            '((DateTime)context.Variables["requestStarted"]).ToString("yyyy-MM")',
        ),
    )
    reservations[0].text = (reservations[0].text or "").replace(
        original_stamp,
        """((DateTime)context.Variables["requestStarted"]).ToString("yyyy-MM-dd'T'HH:mm:ss.fff")""",
    )
    _variable(inbound, _MARKER, "@(1)")
    employee.insert(
        list(employee).index(headers[0]) + 1, _identity_block(cloud, endpoint.rstrip("/"), table)
    )
    return serialize_policy(root)


def strip_validated_directory_identity_policy(source: str) -> str:
    root = parse_policy(source)
    markers = root.findall(f".//set-variable[@name='{_MARKER}']")
    if not markers:
        return source
    inbound = root.find("inbound")
    employee = root.find("./inbound/choose/when/validate-azure-ad-token/..")
    if (
        len(markers) != 1
        or inbound is None
        or employee is None
        or markers[0] not in list(inbound)
        or markers[0].get("value") != "@(1)"
    ):
        raise PolicyCompilationError("Directory identity policy version is invalid")
    blocks = [
        node
        for node in employee.findall("choose")
        if node.find("./when/set-variable[@name='directoryIdentityCloud']") is not None
    ]
    if len(blocks) != 1:
        raise PolicyCompilationError("Directory identity lookup is missing or ambiguous")
    block = blocks[0]
    cloud_node = block.find("./when/set-variable[@name='directoryIdentityCloud']")
    endpoint_node = block.find("./when/set-variable[@name='directoryIdentityEndpoint']")
    table_node = block.find("./when/set-variable[@name='directoryIdentityTable']")
    assert cloud_node is not None
    if endpoint_node is None or table_node is None:
        raise PolicyCompilationError("Directory identity target is incomplete")
    cloud = cloud_node.get("value", "").removeprefix('@("').removesuffix('")')
    endpoint, table = endpoint_node.get("value", ""), table_node.get("value", "")
    if cloud not in {"public", "usgov", "china"}:
        raise PolicyCompilationError("Directory identity cloud is invalid")
    if component_digest(block) != component_digest(_identity_block(cloud, endpoint, table)):
        raise PolicyCompilationError("Directory identity lookup differs from the reviewed contract")
    employee.remove(block)
    inbound.remove(markers[0])
    partition = employee.find(".//set-variable[@name='ledgerPartition']")
    reservations = [
        node for node in employee.iter("set-body") if 'entity["RowKey"] = "R|"' in (node.text or "")
    ]
    if partition is None or len(reservations) != 1:
        raise PolicyCompilationError("Directory employee reservation anchors are invalid")
    period = '((DateTime)context.Variables["requestStarted"]).ToString("yyyy-MM")'
    stamp = (
        """((DateTime)context.Variables["requestStarted"]).ToString("yyyy-MM-dd'T'HH:mm:ss.fff")"""
    )
    if period not in partition.get("value", "") or stamp not in (reservations[0].text or ""):
        raise PolicyCompilationError("Directory employee reservation time is not frozen")
    partition.set(
        "value",
        partition.get("value", "").replace(
            period,
            'DateTime.UtcNow.ToString("yyyy-MM")',
        ),
    )
    reservations[0].text = (reservations[0].text or "").replace(
        stamp,
        """DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fff")""",
    )
    base = serialize_policy(root)
    # Validate target and insertion anchors against the base, including ledger reuse.
    composed = compose_directory_identity_policy(base, cloud=cloud, endpoint=endpoint, table=table)
    if component_digest(parse_policy(composed)) != component_digest(parse_policy(source)):
        raise PolicyCompilationError(
            "Directory identity lookup position differs from the reviewed contract"
        )
    return base
