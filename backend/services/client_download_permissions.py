"""Download actions do not grant application ownership or model access."""

from fastapi import HTTPException

from turnstile_core.domain.menu_permissions import DownloadPermission

from .auth_service import SessionIdentity


def require_plugin_download(identity: SessionIdentity, permission: DownloadPermission) -> None:
    if permission == "subscription-plugin" and identity.role != "owner" and (
        identity.data_scope is not None and identity.directory_person_active is not True
    ):
        raise HTTPException(403, "需要关联有效人员后才能下载插件。")
    if identity.role != "owner" and permission not in identity.download_permissions:
        raise HTTPException(403, "没有此版本插件的下载权限。")
