from __future__ import annotations

import json
from xml.etree import ElementTree

from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
MAIN = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
DATA_PLANE = (ROOT / "infra/modules/data-plane.bicep").read_text(encoding="utf-8")
APIM_SERVICE = (ROOT / "infra/modules/apim-service.bicep").read_text(encoding="utf-8")
APIM_INTEGRATION = (ROOT / "infra/modules/apim-integration.bicep").read_text(
    encoding="utf-8"
)
CONTROL_PLANE = (ROOT / "infra/modules/control-plane-function.bicep").read_text(
    encoding="utf-8"
)
OBSERVER_APP = (ROOT / "infra/envoy-cache-adapter/app.bicep").read_text(
    encoding="utf-8"
)
OBSERVER_MAIN = (ROOT / "infra/envoy-cache-adapter/main.bicep").read_text(
    encoding="utf-8"
)
CONTROL_PLANE_APIM_RBAC = (
    ROOT / "infra/modules/control-plane-apim-rbac.bicep"
).read_text(encoding="utf-8")
APPLICATION_KEY_MANAGEMENT_RBAC = (
    ROOT / "infra/modules/application-key-management-rbac.bicep"
).read_text(encoding="utf-8")
INITIAL_SCHEMA = (ROOT / "migrations/001_initial_schema.up.sql").read_text(
    encoding="utf-8"
)
PARAMETERS = json.loads(
    (ROOT / "infra/main.parameters.example.json").read_text(encoding="utf-8")
)["parameters"]


def test_main_creates_the_platform_resource_group_and_apim() -> None:
    assert "targetScope = 'subscription'" in MAIN
    assert "resource platformResourceGroup" in MAIN
    assert "module apim 'modules/apim-service.bicep'" in MAIN
    assert "scope: platformResourceGroup" in MAIN
    assert "type: 'SystemAssigned'" in APIM_SERVICE
    assert "publisherEmail: publisherEmail" in APIM_SERVICE


def test_new_apim_defaults_to_standard_v2() -> None:
    assert "param apimSkuName string = 'StandardV2'" in MAIN
    assert PARAMETERS["apimSkuName"]["value"] == "StandardV2"


def test_apim_exposes_the_turnstile_gateway_path() -> None:
    assert "param apimApiId string = 'turnstile-llm'" in MAIN
    assert "param gatewayApiRelativePath string = 'turnstile/llm'" in MAIN
    assert "name: apiId" in APIM_INTEGRATION
    assert "displayName: '${apiId} | Turnstile AI Gateway'" in APIM_INTEGRATION
    assert "param apiPath string" in APIM_INTEGRATION
    assert "path: apiPath" in APIM_INTEGRATION
    assert "path: 'finops/llm'" not in APIM_INTEGRATION


def test_fresh_apim_precreates_a_fail_closed_image_operation() -> None:
    operation = APIM_INTEGRATION.split("resource imagesGenerationsOperation ", 1)[1].split(
        "\nresource ", 1
    )[0]
    for declaration in (
        "parent: api", "name: 'images-generations'", "method: 'POST'",
        "urlTemplate: '/images/generations'", "templateParameters: []",
    ):
        assert declaration in operation
    policy_resource = APIM_INTEGRATION.split(
        "resource imagesGenerationsOperationPolicy ", 1
    )[1].split("\nresource ", 1)[0]
    assert "parent: imagesGenerationsOperation" in policy_resource
    assert "loadTextContent('../policies/provider-neutral-images-policy.xml')" in policy_resource
    policy = ElementTree.parse(ROOT / "infra/policies/provider-neutral-images-policy.xml")
    assert policy.find("./inbound/base") is not None
    selected_model = policy.find("./inbound/set-variable")
    assert selected_model is not None
    assert selected_model.attrib == {
        "name": "selectedModelKnown", "value": "@(false)",
    }
    denial = policy.find("./inbound/return-response")
    assert denial is not None
    response_status = denial.find("set-status")
    assert response_status is not None and response_status.get("code") == "400"
    body = json.loads(denial.findtext("set-body", ""))
    assert body["error"]["code"] == "image_model_not_published"
    assert policy.find(".//send-request") is None
    assert policy.find(".//forward-request") is None
    assert "Microsoft.ApiManagement/service/apis/operations/write" not in CONTROL_PLANE_APIM_RBAC


def test_incremental_apim_template_only_prepares_and_promotes_existing_api_revisions() -> None:
    root = (ROOT / "infra/apim-upgrade.bicep").read_text()
    module = (ROOT / "infra/modules/apim-upgrade.bicep").read_text()
    assert "targetScope = 'subscription'" in root
    assert "name: 'apim-image-upgrade-${uniqueString(apimName, apiId, revision, stage)}'" in root
    assert "scope: resourceGroup(apimResourceGroupName)" in root
    assert "sourceApiId: '${api.id};rev=${sourceRevision}'" in module
    assert "stage == 'prepare' && initializeImagePolicy" in module
    assert "stage == 'promote'" in module
    assert "loadTextContent('../policies/provider-neutral-images-policy.xml')" in module
    assert "apiId: '${api.id};rev=${revision}'" in module
    for resource in ("Microsoft.Authorization", "resourceGroups@", "namedValues@", "products@"):
        assert resource not in root + module
    assert "Microsoft.ApiManagement/service@2024-05-01' existing" in module
    assert "Microsoft.ApiManagement/service/apis@2024-05-01' existing" in module


def test_shared_apim_resources_are_environment_isolated() -> None:
    for parameter, resource_name in (
        ("apiId", "apiId"),
        ("productId", "productId"),
        ("dashboardSubscriptionId", "dashboardSubscriptionId"),
        ("probeSubscriptionId", "probeSubscriptionId"),
        ("appInsightsLoggerId", "appInsightsLoggerId"),
        ("eventHubLoggerId", "eventHubLoggerId"),
        ("diagnosticSettingName", "diagnosticSettingName"),
    ):
        assert f"param {parameter} string" in APIM_INTEGRATION
        assert f"name: {resource_name}" in APIM_INTEGRATION

    assert "displayName: '${apiId} | Turnstile AI Gateway'" in APIM_INTEGRATION
    assert "displayName: '${productId} | Turnstile AI'" in APIM_INTEGRATION
    assert "param existingApimResourceGroupName string = ''" in MAIN
    assert "resource effectiveApimResourceGroup" not in MAIN
    assert MAIN.count("scope: resourceGroup(effectiveApimResourceGroupName)") == 3


def test_fresh_apim_creates_its_azure_monitor_logger() -> None:
    assert (
        "resource azureMonitorLogger 'Microsoft.ApiManagement/service/loggers@"
        in APIM_INTEGRATION
    )
    assert "name: 'azuremonitor'" in APIM_INTEGRATION
    assert "loggerType: 'azureMonitor'" in APIM_INTEGRATION
    assert "loggerId: azureMonitorLogger.id" in APIM_INTEGRATION
    logger = APIM_INTEGRATION.split("resource azureMonitorLogger", 1)[1].split(
        "resource llmDiagnostics", 1
    )[0]
    assert "credentials:" not in logger


def test_employee_token_policy_defaults_to_the_login_tenant_and_client() -> None:
    assert "param employeeTenantId string = subscription().tenantId" in MAIN
    assert "param employeeClientId string = entraClientId" in MAIN
    assert "param employeeAudience string = entraClientId" in MAIN


def test_employee_token_policy_is_disabled_without_entra_configuration() -> None:
    assert PARAMETERS["entraClientId"]["value"] == ""
    assert (
        "var employeeTokenEnabled = !empty(trim(employeeClientId)) "
        "&& !empty(trim(employeeAudience))"
    ) in APIM_INTEGRATION
    assert "employeeTokenEnabled ? 'true' : 'false'" in APIM_INTEGRATION
    assert "employeeTokenEnabled ? trim(employeeClientId)" in APIM_INTEGRATION
    assert "employeeTokenEnabled ? trim(employeeAudience)" in APIM_INTEGRATION
    assert "@(__EMPLOYEE_TOKEN_ENABLED__ &amp;&amp;" in (
        ROOT / "infra/policies/foundry-finops-policy.xml"
    ).read_text(encoding="utf-8")


def test_templates_create_platform_resources_without_foundry_projects_or_models() -> None:
    for resource in (
        "Microsoft.DBforPostgreSQL/flexibleServers@",
        "Microsoft.EventHub/namespaces@",
        "Microsoft.Storage/storageAccounts@",
        "Microsoft.KeyVault/vaults@",
        "Microsoft.Insights/components@",
        "Microsoft.Web/sites@",
    ):
        assert resource in DATA_PLANE
    combined = MAIN + DATA_PLANE + APIM_SERVICE + CONTROL_PLANE
    assert "Microsoft.CognitiveServices/accounts/projects" not in combined
    assert "Microsoft.CognitiveServices/accounts/deployments" not in combined


def test_api_python_path_contains_package_root_and_prebuilt_dependencies() -> None:
    assert (
        "{ name: 'PYTHONPATH', value: "
        "'/home/site/wwwroot:/home/site/wwwroot/.python_packages/lib/site-packages' }"
        in DATA_PLANE
    )


def test_functions_use_separate_flex_consumption_plans() -> None:
    telemetry = DATA_PLANE.split("resource functionApp", 1)[1].split(
        "resource functionStorageBlobOwner", 1
    )[0]
    control = CONTROL_PLANE.split("resource functionApp", 1)[1].split(
        "resource keyVault", 1
    )[0]

    assert "serverFarmId: apiPlan.id" in DATA_PLANE
    assert "serverFarmId: telemetryPlan.id" in telemetry
    assert "serverFarmId: plan.id" in control
    assert DATA_PLANE.count("tier: 'FlexConsumption'") == 1
    assert CONTROL_PLANE.count("tier: 'FlexConsumption'") == 1
    assert "name: 'FC1'" in DATA_PLANE
    assert "name: 'FC1'" in CONTROL_PLANE
    for template in (telemetry, control):
        assert "functionAppConfig:" in template
        assert "instanceMemoryMB: 2048" in template
        assert "maximumInstanceCount: 100" in template
        assert "name: 'python'" in template
        assert "version: '3.11'" in template
        assert "type: 'SystemAssignedIdentity'" in template
        for incompatible in (
            "alwaysOn:",
            "linuxFxVersion:",
            "vnetRouteAllEnabled:",
            "WEBSITE_RUN_FROM_PACKAGE",
            "WEBSITES_CONTAINER_START_TIME_LIMIT",
            "SCM_DO_BUILD_DURING_DEPLOYMENT",
            "FUNCTIONS_WORKER_RUNTIME",
        ):
            assert incompatible not in template


def test_observer_uses_a_configurable_premium_v3_plan() -> None:
    assert "param appServicePlanSkuName string = 'P0v3'" in OBSERVER_APP
    assert "param appServicePlanWorkerCount int = 1" in OBSERVER_APP
    assert "name: appServicePlanSkuName" in OBSERVER_APP
    assert "tier: 'PremiumV3'" in OBSERVER_APP
    assert "capacity: appServicePlanWorkerCount" in OBSERVER_APP
    assert "appServicePlanSkuName: appServicePlanSkuName" in OBSERVER_MAIN
    assert "appServicePlanWorkerCount: appServicePlanWorkerCount" in OBSERVER_MAIN


def test_flex_functions_have_isolated_network_and_deployment_storage() -> None:
    assert "addressPrefix: '10.42.3.0/27'" in DATA_PLANE
    assert "addressPrefix: '10.42.3.32/27'" in DATA_PLANE
    assert DATA_PLANE.count("serviceName: 'Microsoft.App/environments'") == 2
    assert "var telemetryDeploymentContainerName = 'deploy-telemetry'" in DATA_PLANE
    assert "name: telemetryDeploymentContainerName" in DATA_PLANE
    assert "var deploymentContainerName = 'deploy-control-plane'" in CONTROL_PLANE
    assert "name: deploymentContainerName" in CONTROL_PLANE
    assert "resource functionVnetIntegration 'Microsoft.Web/sites/networkConfig@" in (
        DATA_PLANE
    )
    assert "resource functionVnetIntegration 'Microsoft.Web/sites/networkConfig@" in (
        CONTROL_PLANE
    )


def test_public_runtime_resources_are_exempt_from_network_modify_policy() -> None:
    for template, resource_name in (
        (DATA_PLANE, "ledgerStorage"),
        (DATA_PLANE, "api"),
        (DATA_PLANE, "functionApp"),
        (CONTROL_PLANE, "functionApp"),
        (OBSERVER_APP, "webApp"),
    ):
        resource = template.split(f"resource {resource_name} ", 1)[1].split(
            "\nresource ", 1
        )[0]
        assert "SecurityControl: 'Ignore'" in resource
        assert "publicNetworkAccess: 'Enabled'" in resource


def test_vnet_subnets_are_created_serially() -> None:
    telemetry_subnet = DATA_PLANE.split("resource telemetryFunctionSubnet", 1)[1].split(
        "resource controlFunctionSubnet", 1
    )[0]
    control_subnet = DATA_PLANE.split("resource controlFunctionSubnet", 1)[1].split(
        "resource privateEndpointSubnet", 1
    )[0]
    private_endpoint_subnet = DATA_PLANE.split("resource privateEndpointSubnet", 1)[
        1
    ].split("resource blobPrivateDnsZone", 1)[0]
    assert "virtualNetwork" in telemetry_subnet.split("dependsOn:", 1)[1]
    assert "telemetryFunctionSubnet" in control_subnet.split("dependsOn:", 1)[1]
    assert "controlFunctionSubnet" in private_endpoint_subnet.split("dependsOn:", 1)[1]


def test_telemetry_host_storage_is_fully_reachable_over_private_links() -> None:
    for service in ("blob", "queue", "table"):
        assert f"privatelink.{service}.${{environment().suffixes.storage}}" in DATA_PLANE
        assert f"name: 'pe-${{storageName}}-{service}'" in DATA_PLANE
        assert f"'{service}'" in DATA_PLANE
    for setting in (
        "AzureWebJobsStorage__blobServiceUri",
        "AzureWebJobsStorage__queueServiceUri",
        "AzureWebJobsStorage__tableServiceUri",
    ):
        assert setting in DATA_PLANE
    assert "resource functionStorageQueueContributor" in DATA_PLANE
    assert "resource functionStorageTableContributor" in DATA_PLANE


def test_api_and_apim_share_the_same_gateway_path() -> None:
    assert "param gatewayApiRelativePath string = 'turnstile/llm'" in MAIN
    assert (
        "var effectiveGatewayApiPath = "
        "'${effectiveApimGatewayUrl}/${gatewayApiRelativePath}'"
        in MAIN
    )
    assert "apimGatewayUrl: effectiveGatewayApiPath" in MAIN
    assert "apimApiId: apimApiId" in MAIN
    assert "apiPath: gatewayApiRelativePath" in MAIN
    assert "{ name: 'APIM_GATEWAY_URL', value: apimGatewayUrl }" in DATA_PLANE
    assert (
        "{ name: 'APIM_DASHBOARD_SUBSCRIPTION_KEY', value: apimSubscriptionKey }"
        in DATA_PLANE
    )
    assert "path: apiPath" in APIM_INTEGRATION


def test_configurable_system_subscription_ids_reach_runtime_apps() -> None:
    assert MAIN.count("dashboardSubscriptionId: apimDashboardSubscriptionId") == 3
    assert MAIN.count("probeSubscriptionId: apimProbeSubscriptionId") == 3
    assert "output apimDashboardSubscriptionId string" in MAIN
    assert "output apimProbeSubscriptionId string" in MAIN
    for template in (DATA_PLANE, CONTROL_PLANE):
        assert "param dashboardSubscriptionId string = 'turnstile-dashboard'" in template
        assert "param probeSubscriptionId string = 'turnstile-publisher-probe'" in template
        assert "APIM_DASHBOARD_SUBSCRIPTION_ID" in template
        assert "APIM_PROBE_SUBSCRIPTION_ID" in template
    release = (ROOT / "infra/runtime-release.bicep").read_text(encoding="utf-8")
    assert release.count("APIM_DASHBOARD_SUBSCRIPTION_ID: dashboardSubscriptionId") == 2
    assert release.count("APIM_PROBE_SUBSCRIPTION_ID: probeSubscriptionId") == 2


def test_api_bootstraps_initial_owner_between_migration_and_startup() -> None:
    assert (
        "appCommandLine: 'python -m backend.migrate && python -m backend.bootstrap "
        "&& python -m uvicorn backend.api:app"
        in DATA_PLANE
    )
    assert "{ name: 'BOOTSTRAP_OWNER_EMAIL', value: bootstrapOwnerEmail }" in DATA_PLANE
    assert (
        "{ name: 'BOOTSTRAP_OWNER_PASSWORD_HASH', value: bootstrapOwnerPasswordHash }"
        in DATA_PLANE
    )


def test_collapsed_migration_restores_search_path_after_pg_dump() -> None:
    dump_end, historical_migrations = INITIAL_SCHEMA.split(
        "-- Integrated from historical migration 041_pinned_report_layout.up.sql.", 1
    )
    assert "SELECT pg_catalog.set_config('search_path', '', false);" in dump_end
    assert dump_end.rstrip().endswith("SET search_path = public, pg_catalog;")
    assert "CREATE TABLE pinned_report_layout" in historical_migrations


def test_data_plane_creates_an_entra_only_budget_ledger() -> None:
    assert "resource ledgerStorage 'Microsoft.Storage/storageAccounts@" in DATA_PLANE
    assert (
        "resource ledgerTable 'Microsoft.Storage/storageAccounts/tableServices/tables@"
        in DATA_PLANE
    )
    assert "allowSharedKeyAccess: false" in DATA_PLANE
    assert "publicNetworkAccess: 'Enabled'" in DATA_PLANE
    assert "bypass: 'AzureServices'" in DATA_PLANE
    assert "name: ledgerTableName" in DATA_PLANE
    assert "output ledgerTableEndpoint string = ledgerTableEndpoint" in DATA_PLANE


def test_ledger_has_its_own_table_private_endpoint_without_opening_the_firewall() -> None:
    ledger = DATA_PLANE.split("resource ledgerStorage ", 1)[1].split(
        "resource ledgerTableService ", 1
    )[0]
    assert "defaultAction: 'Deny'" in ledger
    assert "ipRules: []" in ledger
    assert "allowSharedKeyAccess: false" in ledger
    endpoint = DATA_PLANE.split("resource ledgerTablePrivateEndpoint ", 1)[1].split(
        "resource ledgerTablePrivateDnsZoneGroup ", 1
    )[0]
    assert "name: 'pe-${ledgerStorageName}-table'" in endpoint
    assert "privateLinkServiceId: ledgerStorage.id" in endpoint
    assert "id: privateEndpointSubnet.id" in endpoint
    assert "'table'" in endpoint
    dns = DATA_PLANE.split("resource ledgerTablePrivateDnsZoneGroup ", 1)[1].split(
        "module keyVaultPrivateEndpoint ", 1
    )[0]
    assert "parent: ledgerTablePrivateEndpoint" in dns
    assert "privateDnsZoneId: tablePrivateDnsZone.id" in dns


def test_ledger_and_telemetry_roles_are_deterministic_and_scoped() -> None:
    assert "resource telemetryLedgerContributor" in DATA_PLANE
    assert "resource apimLedgerContributor" in DATA_PLANE
    assert "scope: ledgerTable" in DATA_PLANE
    assert "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3" in DATA_PLANE
    assert "name: guid(workspace.id, functionApp.id, 'log-analytics-reader')" in DATA_PLANE


def test_api_projects_model_access_using_its_exact_ledger_and_identity() -> None:
    api = DATA_PLANE.split("resource api 'Microsoft.Web/sites", 1)[1].split(
        "resource functionApp ", 1
    )[0]
    for setting in (
        "{ name: 'LEDGER_SYNC_ENABLED', value: 'true' }",
        "{ name: 'LEDGER_TABLE_ENDPOINT', value: ledgerTableEndpoint }",
        "{ name: 'LEDGER_TABLE_NAME', value: ledgerTableName }",
    ):
        assert api.count(setting) == 1
    role = DATA_PLANE.split("resource apiLedgerContributor ", 1)[1].split(
        "resource telemetryLedgerContributor ", 1
    )[0]
    assert "name: guid(ledgerTable.id, api.id, 'table-data-contributor')" in role
    assert "scope: ledgerTable" in role
    assert "principalId: api.identity.principalId" in role
    assert "roleDefinitionId: tableDataContributorRoleDefinitionId" in role
    assert "scope: ledgerStorage" not in role


def test_existing_api_model_access_upgrade_reuses_ledger_and_preserves_settings() -> None:
    upgrade = (ROOT / "infra/model-access-upgrade.bicep").read_text(encoding="utf-8")
    roles = (ROOT / "infra/modules/model-access-ledger-rbac.bicep").read_text(encoding="utf-8")
    assert "targetScope = 'resourceGroup'" in upgrade
    assert "param apiNames string[]" in upgrade
    assert "for (apiName, index) in apiNames" in upgrade
    assert "scope: resourceGroup(ledgerResourceGroupName)" in upgrade
    assert "apiPrincipalId: apis[index].identity.principalId" in upgrade
    assert "apiResourceId: apis[index].id" in upgrade
    assert "@secure()\nparam currentApiSettings object" in upgrade
    assert "union(currentApiSettings[apiName], {" in upgrade
    assert "list(" not in upgrade
    assert "LEDGER_SYNC_ENABLED: 'true'" in upgrade
    assert "LEDGER_TABLE_ENDPOINT: ledgerStorage.properties.primaryEndpoints.table" in upgrade
    assert "LEDGER_TABLE_NAME: ledgerTableName" in upgrade
    assert "dependsOn: [\n    ledgerAccess[index]" in upgrade
    assert roles.count(" existing = {") == 3
    assert "guid(ledgerTable.id, apiResourceId, 'table-data-contributor')" in roles
    assert "scope: ledgerTable" in roles and "scope: ledgerStorage" not in roles
    assert "principalId: apiPrincipalId" in roles
    assert "'0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'" in roles
    assert "Microsoft.Network/" not in upgrade + roles
    assert "listKeys(" not in upgrade + roles
    assert "output " not in upgrade + roles


def test_api_ledger_private_network_is_wired_for_new_and_existing_installations() -> None:
    upgrade = (ROOT / "infra/model-access-network-upgrade.bicep").read_text(encoding="utf-8")
    for template in (DATA_PLANE, upgrade):
        subnet = template.split("resource apiSubnet ", 1)[1].split("\nresource ", 1)[0]
        assert "parent: virtualNetwork" in subnet
        assert "serviceName: 'Microsoft.Web/serverFarms'" in subnet
        integration = template.split("resource apiVnetIntegration ", 1)[1].split(
            "\nresource ", 1,
        )[0]
        assert "parent: api" in integration
        assert "name: 'virtualNetwork'" in integration
        assert "subnetResourceId: apiSubnet.id" in integration
        assert "swiftSupported: true" in integration
    assert "addressPrefix: '10.42.3.64/27'" in DATA_PLANE
    assert "dependsOn: [\n    privateEndpointSubnet\n  ]" in DATA_PLANE
    assert "Microsoft.Storage/" not in upgrade
    assert "Microsoft.Authorization/" not in upgrade
    assert "appsettings" not in upgrade
    assert "param apiSubnetAddressPrefix string = '10.42.3.64/27'" in upgrade


def test_databricks_oauth_is_opt_in_for_api_publisher_and_scoped_permissions() -> None:
    for template in (MAIN, DATA_PLANE, CONTROL_PLANE, CONTROL_PLANE_APIM_RBAC):
        assert "param databricksOAuthEnabled bool = false" in template
    assert "databricksOAuthEnabled: provisionControlPlane && databricksOAuthEnabled" in MAIN
    assert MAIN.count("databricksOAuthEnabled: databricksOAuthEnabled") == 2
    for template in (DATA_PLANE, CONTROL_PLANE):
        assert (
            "{ name: 'DATABRICKS_OAUTH_ENABLED', value: string(databricksOAuthEnabled) }"
            in template
        )
    oauth = CONTROL_PLANE_APIM_RBAC.split("var oauthActions = ", 1)[1].split(
        "resource apim ", 1
    )[0]
    assert oauth.startswith("databricksOAuthEnabled ? [")
    assert "] : []" in oauth
    assert "*" not in oauth and "/delete'" not in oauth
    for suffix in ("", "/authorizations", "/authorizations/accessPolicies"):
        for operation in ("read", "write"):
            action = f"Microsoft.ApiManagement/service/authorizationProviders{suffix}/{operation}"
            assert action in oauth
    assert "], oauthActions)" in CONTROL_PLANE_APIM_RBAC
    assert "Microsoft.ApiManagement/service/apis/operations/write" not in CONTROL_PLANE_APIM_RBAC


def test_control_plane_features_default_to_disabled() -> None:
    for declaration in (
        "param provisionControlPlane bool = false",
        "param controlPlaneEnabled bool = false",
        "param gatewayReleaseWorkerEnabled bool = false",
        "param gatewayApplicationKeyManagementEnabled bool = false",
    ):
        assert declaration in MAIN
    assert "effectivePublicationWorkerEnabled = publicationWorkerEnabled" in CONTROL_PLANE
    assert "effectiveReleaseWorkerEnabled = releaseWorkerEnabled" in CONTROL_PLANE


def test_application_creation_is_gated_and_uses_the_exact_platform_ledger() -> None:
    assert "param gatewayApplicationProvisioningEnabled bool = false" in MAIN
    assert (
        "gatewayApplicationProvisioningEnabled: provisionControlPlane "
        "&& gatewayApplicationProvisioningEnabled" in MAIN
    )
    assert "ledgerStorageName: dataPlane.outputs.ledgerStorageName" in MAIN
    assert "ledgerTableEndpoint: dataPlane.outputs.ledgerTableEndpoint" in MAIN
    assert MAIN.count("apimProductId: apimProductId") == 2
    assert "value: string(applicationProvisioningEnabled)" in CONTROL_PLANE
    release = (ROOT / "infra/runtime-release.bicep").read_text(encoding="utf-8")
    assert "union(currentApiSettings" in release
    assert "union(currentControlPlaneSettings" in release
    assert release.count("GATEWAY_RELEASE_WORKER_ENABLED: string(releaseWorkerEnabled)") == 2
    role = CONTROL_PLANE.split("resource applicationLedgerContributor", 1)[1].split(
        "resource functionDatabaseSecretReader", 1
    )[0]
    assert "if (applicationProvisioningEnabled)" in role
    assert "scope: ledgerTable" in role
    assert "principalId: functionApp.identity.principalId" in role
    assert "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3" in role
    for template in (DATA_PLANE, CONTROL_PLANE):
        for setting in (
            "APIM_PRODUCT_ID", "LEDGER_TABLE_ENDPOINT", "LEDGER_TABLE_NAME",
            "GATEWAY_APPLICATION_PROVISIONING_ENABLED",
            "GATEWAY_APPLICATION_DEFAULT_MONTHLY_TOKEN_LIMIT",
            "GATEWAY_APPLICATION_DEFAULT_TOKENS_PER_MINUTE",
        ):
            assert f"name: '{setting}'" in template
    for action in (
        "Microsoft.ApiManagement/service/subscriptions/read",
        "Microsoft.ApiManagement/service/subscriptions/write",
        "Microsoft.ApiManagement/service/products/apis/read",
        "Microsoft.ApiManagement/service/apis/policies/read",
    ):
        assert action in CONTROL_PLANE_APIM_RBAC
    assert "listSecrets" not in CONTROL_PLANE_APIM_RBAC


def test_control_plane_uses_its_own_flex_plan_and_the_platform_vnet() -> None:
    assert "appServicePlanName:" not in MAIN.split("module controlPlane", 1)[1].split(
        "module controlPlaneApimRbac", 1
    )[0]
    assert "resourcePrefix: resourcePrefix" in MAIN
    assert "virtualNetworkName: 'vnet-${resourcePrefix}-${suffix}'" in MAIN
    assert "apimGatewayUrl: effectiveGatewayApiPath" in MAIN
    assert "var storageName = 'stturnstilecp${take(suffix, 11)}'" in CONTROL_PLANE
    assert "var functionName = 'func-${resourcePrefix}-control-${suffix}'" in CONTROL_PLANE
    assert "var planName = 'plan-${resourcePrefix}-control-${suffix}'" in CONTROL_PLANE
    assert "plan-finops" not in CONTROL_PLANE
    assert "vnet-finops" not in CONTROL_PLANE


def test_application_key_management_role_is_least_privilege() -> None:
    actions = {
        line.strip().strip("'")
        for line in APPLICATION_KEY_MANAGEMENT_RBAC.split("actions: [", 1)[1]
        .split("]", 1)[0]
        .splitlines()
        if line.strip().startswith("'")
    }
    assert actions == {
        "Microsoft.ApiManagement/service/subscriptions/read",
        "Microsoft.ApiManagement/service/subscriptions/listSecrets/action",
        "Microsoft.ApiManagement/service/subscriptions/regeneratePrimaryKey/action",
        "Microsoft.ApiManagement/service/subscriptions/regenerateSecondaryKey/action",
    }
    assert "scope: apim" in APPLICATION_KEY_MANAGEMENT_RBAC


def test_control_plane_role_has_no_subscription_or_resource_group_scope() -> None:
    assert "scope: apim" in CONTROL_PLANE_APIM_RBAC
    assert "assignableScopes: [" in CONTROL_PLANE_APIM_RBAC
    assert "resourceGroup().id" in CONTROL_PLANE_APIM_RBAC


def test_custom_roles_use_installation_specific_names() -> None:
    for template in (CONTROL_PLANE_APIM_RBAC, APPLICATION_KEY_MANAGEMENT_RBAC):
        assert "var roleNameSuffix = uniqueString(resourceGroup().id)" in template
        assert "${roleNameSuffix}'" in template
    assert "roleName: 'Turnstile APIM Publisher ${roleNameSuffix}'" in CONTROL_PLANE_APIM_RBAC
    assert (
        "roleName: 'Turnstile APIM Subscription Key Operator ${roleNameSuffix}'"
        in APPLICATION_KEY_MANAGEMENT_RBAC
    )
    assert "roleName: 'FinOps" not in CONTROL_PLANE_APIM_RBAC
    assert "roleName: 'FinOps" not in APPLICATION_KEY_MANAGEMENT_RBAC


def test_example_parameters_are_non_secret_and_environment_neutral() -> None:
    assert PARAMETERS["resourcePrefix"]["value"] == "turnstile"
    assert PARAMETERS["entraClientId"]["value"] == ""
    assert PARAMETERS["entraAllowedEmailDomains"]["value"] == []
    for secret in (
        "postgresAdministratorPassword",
        "credentialEncryptionKey",
        "managementApiKey",
        "apimSubscriptionKey",
        "apimProbeSubscriptionKey",
    ):
        assert secret not in PARAMETERS
    serialized = json.dumps(PARAMETERS)
    for forbidden in ("xle-", "sebpvmm", "2b04b108", "azurewebsites.net"):
        assert forbidden not in serialized
