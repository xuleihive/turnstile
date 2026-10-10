"""Feature entitlements supplement existing authentication and row authorization."""

from fastapi import HTTPException, Request

from ..services.auth_service import SessionIdentity

# These scoped reads are shared by multiple pages, not owned by their endpoint names.
USAGE_SUMMARY_MENUS = (
    "finops-overview",
    "finops-analytics",
    "finops-trends",
    "finops-governance",
    "finops-requests",
    "models",
)
OBSERVABILITY_READ_MENUS = {
    "overview": ("finops-overview",),
    "executive-overview": USAGE_SUMMARY_MENUS,
    "distribution": ("finops-overview", "finops-analytics", "finops-governance", "models"),
    "trends": (
        "finops-overview",
        "finops-trends",
        "finops-governance",
        "finops-requests",
        "models",
    ),
    "requests": USAGE_SUMMARY_MENUS,
    "anomalies": ("finops-overview", "finops-governance"),
    "audit-findings": ("finops-governance",),
    "runs": ("finops-requests",),
    "optimization-events": ("finops-governance",),
}


def require_menu_access(request: Request, identity: SessionIdentity) -> None:
    if identity.role == "owner":
        return
    path = request.url.path
    choices: tuple[str, ...] = ()
    if path.startswith("/api/v1/permission-management"):
        choices = ("permission-management",)
    elif path.startswith(("/api/v1/directory", "/api/v1/organization-management")):
        choices = ("organization-management",)
    elif path.startswith("/api/v1/budgets"):
        choices = ("budgets",)
    elif path.startswith("/api/v1/anomaly-rules"):
        choices = ("finops-governance",)
    elif path.startswith(("/api/v1/model-gateway/invoke", "/api/v1/model-gateway/images")):
        choices = ("finops-invoke",)
    elif path.startswith("/api/v1/assistant"):
        choices = ("pinned-report",) if "/pinned-charts" in path else ("assistant",)
    elif path.startswith("/api/v1/observability/"):
        feature = path.split("/")[4]
        choices = OBSERVABILITY_READ_MENUS.get(feature, ())
        if feature == "requests" and len(path.rstrip("/").split("/")) > 5:
            choices = ("finops-requests", "finops-governance")
    if choices and not set(choices).intersection(identity.menu_permissions):
        raise HTTPException(status_code=403, detail="Menu access is not granted")
