"""Application navigation entitlements, never gateway or data-scope authorization."""

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MenuPermissionGroup = Literal["user", "organization_admin", "department_admin", "team_admin"]

PERSONAL_MENUS = ("user-settings", "finops-invoke")
ANALYSIS_MENUS = (
    "finops-overview",
    "finops-analytics",
    "finops-trends",
    "finops-requests",
    "assistant",
    "pinned-report",
    "copilot-requests",
)
MANAGEMENT_MENUS = (
    "finops-governance",
    "budgets",
    "models",
    "organization-management",
    "copilot-cost-centers",
    "copilot-unassigned-users",
)
ALL_MENUS = (
    *PERSONAL_MENUS,
    *ANALYSIS_MENUS,
    *MANAGEMENT_MENUS,
    "settings",
    "apim-native-routes",
    "gateway-releases",
    "applications",
    "copilot-enterprise-teams",
    "permission-management",
)
OWNER_MENUS = (
    "permission-management",
    "settings",
    "apim-native-routes",
    "gateway-releases",
    "applications",
    "copilot-enterprise-teams",
    "copilot-cost-centers",
    "copilot-unassigned-users",
)
MENU_GROUPS: dict[MenuPermissionGroup, tuple[str, ...]] = {
    "user": PERSONAL_MENUS,
    "team_admin": (*PERSONAL_MENUS, *ANALYSIS_MENUS, "models", "organization-management"),
    "department_admin": (*PERSONAL_MENUS, *ANALYSIS_MENUS, *MANAGEMENT_MENUS),
    "organization_admin": tuple(menu for menu in ALL_MENUS if menu not in OWNER_MENUS),
}
MENU_GROUPS = {
    group: tuple(menu for menu in menus if menu not in OWNER_MENUS)
    for group, menus in MENU_GROUPS.items()
}

MENU_CATALOG = (
    ("AI FinOps", "assistant", "FinOps助手", "all"),
    ("AI FinOps", "pinned-report", "报表中心", "apim"),
    ("AI 用量治理", "finops-overview", "管理总览", "apim"),
    ("AI 用量治理", "budgets", "预算管理", "apim"),
    ("AI 用量治理", "finops-analytics", "用量分布", "apim"),
    ("AI 用量治理", "finops-trends", "使用趋势", "apim"),
    ("AI 用量治理", "finops-governance", "异常治理", "apim"),
    ("AI 用量治理", "finops-requests", "请求追踪", "apim"),
    ("模型平台", "models", "模型管理", "apim"),
    ("模型平台", "applications", "订阅管理", "apim"),
    ("模型平台", "apim-native-routes", "负载均衡", "apim"),
    ("模型平台", "gateway-releases", "网关发布", "apim"),
    ("平台管理", "organization-management", "组织管理", "apim"),
    ("平台管理", "permission-management", "权限管理", "all"),
    ("平台管理", "settings", "系统配置", "all"),
    ("个人", "user-settings", "用户设置", "all"),
    ("个人", "finops-invoke", "调用测试", "apim"),
    ("GitHub Copilot", "copilot-requests", "请求追踪", "github-copilot"),
    ("GitHub Copilot", "copilot-cost-centers", "成本中心", "github-copilot"),
    ("GitHub Copilot", "copilot-unassigned-users", "未分配用户", "github-copilot"),
    ("GitHub Copilot", "copilot-enterprise-teams", "企业团队", "github-copilot"),
)


class PermissionPolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    groups: dict[MenuPermissionGroup, list[str]]

    @model_validator(mode="after")
    def validate_groups(self) -> "PermissionPolicyWrite":
        if set(self.groups) != set(MENU_GROUPS):
            raise ValueError("Provide all four permission groups")
        for values in self.groups.values():
            if len(values) != len(set(values)) or not set(values).issubset(ALL_MENUS):
                raise ValueError("Unknown or duplicate menu")
            if set(values).intersection(OWNER_MENUS):
                raise ValueError("Owner-only menus cannot be delegated")
            if "user-settings" not in values:
                raise ValueError("Personal settings must remain accessible")
        self.groups = {
            group: [menu for menu in ALL_MENUS if menu in values]
            for group, values in self.groups.items()
        }
        return self


def menu_permissions(
    groups: Iterable[MenuPermissionGroup] = ("user",),
    *,
    owner: bool = False,
    policy: dict[str, list[str]] | None = None,
) -> tuple[str, ...]:
    if owner:
        return ALL_MENUS
    allowed = {
        menu
        for group in groups
        for menu in (policy.get(group, []) if policy is not None else MENU_GROUPS[group])
        if menu not in OWNER_MENUS
    }
    allowed.add("user-settings")
    return tuple(menu for menu in ALL_MENUS if menu in allowed)
