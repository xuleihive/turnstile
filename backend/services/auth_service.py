"""Sign-in: passwords, sessions, and Microsoft tokens.

Three deliberate choices are worth stating, because each one is a place where the obvious
implementation is weaker than it looks.

**Passwords use scrypt from the standard library, not a fast hash.** `hashlib.scrypt` is
memory-hard, so an attacker holding the table cannot trade GPU parallelism for speed the
way they can against SHA-256 or PBKDF2. It needs no new dependency.

**Sessions are opaque and stored hashed.** The cookie carries 256 bits of `secrets`
randomness; the database keeps only its SHA-256. Whoever reads the table therefore cannot
replay a live session, and lookup is still a primary-key hit because the hash is
deterministic. A plain SHA-256 is right here and would be wrong for the password column --
the input is already full-entropy random, so there is nothing to brute-force.

**Microsoft tokens are verified against the published JWKS, not merely decoded.** The
reference implementation this was modelled on (`gbbai-dev-backend/core/authentication.py`)
calls `jwt.decode(..., options={"verify_signature": False})` and then trusts the email
claim. That accepts any token a caller cares to type: the payload is base64, not a secret.
Verification here is the whole point of the endpoint, so it is not optional and there is no
fallback path that skips it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
# OpenSSL refuses scrypt above 32 MiB by default, and these parameters need
# `128 * n * r` = 33.5 MiB -- so the call fails outright rather than running weakly. The
# ceiling is raised rather than the cost lowered: n is what makes the hash expensive to
# attack, and trimming it to fit a default is paying for security with the wrong currency.
SCRYPT_MAXMEM = 64 * 1024 * 1024
SCRYPT_SALT_BYTES = 16
SCRYPT_KEY_BYTES = 32
SESSION_TOKEN_BYTES = 32


class AuthError(Exception):
    """Sign-in was refused. The message is safe to show a caller."""


def _b64(raw: bytes) -> str:
    """Padless base64url, so the `$`-delimited hash string stays free of `=`."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    """`scrypt$<n>$<r>$<p>$<salt>$<key>`, all base64url.

    The parameters travel with the hash so raising them later does not invalidate every
    existing account: `verify_password` reads whatever the stored record used.
    """
    if not password:
        raise AuthError("密码不能为空。")
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM, dklen=SCRYPT_KEY_BYTES,
    )
    encode = _b64
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(key)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. A missing hash is a false, never an exception.

    `stored` is None for accounts that exist only in Microsoft. Returning False rather than
    raising keeps the caller from having to distinguish "wrong password" from "this person
    has no password", which is a distinction an attacker would happily enumerate.
    """
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        pad = _unb64
        salt = pad(salt_b64)
        expected = pad(key_b64)
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
            maxmem=SCRYPT_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> tuple[str, str]:
    """Returns `(cookie_value, sha256_hex)`. Only the second is ever persisted."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    return token, hash_session_token(token)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EntraIdentity:
    """What a verified Microsoft token is allowed to tell us."""

    email: str
    # None when the token carries no `name` claim. Deriving one from the address would put
    # a value on screen that Microsoft never asserted, and once rendered it is
    # indistinguishable from a real name.
    display_name: str | None
    tenant_id: str
    # The app roles assigned to the signer for this registration, from the token's `roles`
    # claim. Empty when none are assigned or the registration defines none.
    roles: tuple[str, ...] = ()


class EntraTokenVerifier:
    """Validates an ID token from any Microsoft work or school tenant.

    The frontend authenticates against `/organizations`, matching GBBAIP, because the app
    registration and the people signing in live in different tenants. Tokens therefore
    arrive issued by the *user's* tenant, not by the one that owns the registration -- so
    the issuer and `tid` cannot be pinned to a constant, and the earlier version of this
    class rejected every real sign-in for exactly that reason.

    What remains is still four checks, and together they are stronger than what they
    replace:

      signature  against Microsoft's published keys, so a token nobody signed is refused
      aud        must be this client, so a token minted for some other application is not
                 accepted merely because Microsoft signed it
      iss        must be the issuer for the token's own `tid`, so tenant A cannot present
                 a token claiming to be from tenant B
      domain     the mail domain must be on the allow-list

    The last one is the actual authorization gate now, not a secondary filter: without it
    any Microsoft work account anywhere would pass the first three. That is why it is an
    exact comparison of the whole domain rather than the substring test in the reference
    implementation, which admits `attacker@microsoft.com.example.net`.
    """

    def __init__(
        self,
        client_id: str,
        allowed_email_domains: tuple[str, ...],
        tenant_ids: tuple[str, ...] = (),
    ) -> None:
        self._client_id = client_id
        self._allowed_email_domains = tuple(d.lower().lstrip("@") for d in allowed_email_domains)
        # Empty means any tenant, the multi-tenant default. A single-tenant deployment pins
        # its own tenant, so a correctly signed token from anywhere else is still refused.
        self._tenant_ids = tuple(t.strip().lower() for t in tenant_ids)
        # The `common` key set covers every tenant, which is what a multi-tenant app needs;
        # PyJWKClient caches it and refetches on an unknown `kid`, so Microsoft's key
        # rotation is a non-event rather than an outage.
        self._jwks = PyJWKClient(
            "https://login.microsoftonline.com/common/discovery/v2.0/keys",
            cache_keys=True,
        )

    def verify(self, token: str) -> EntraIdentity:
        if not self._client_id:
            raise AuthError("Microsoft 登录尚未配置。")
        if not self._allowed_email_domains:
            raise AuthError("Microsoft 登录尚未配置允许的邮箱域名。")
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            # `iss` is checked below against the token's own tenant rather than here,
            # because there is no single expected value.
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._client_id,
                options={"require": ["exp", "iat", "aud", "iss", "tid"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError) as error:
            raise AuthError("Microsoft 登录未能完成，请重试。") from error

        tenant_id = str(claims.get("tid") or "")
        if not tenant_id or claims.get("iss") != f"https://login.microsoftonline.com/{tenant_id}/v2.0":
            # Without this a token from one tenant could carry another tenant's `tid`, and
            # anything downstream that trusted `tid` would be reading an attacker's value.
            raise AuthError("该账户不属于此组织。")
        if self._tenant_ids and tenant_id.lower() not in self._tenant_ids:
            raise AuthError("该账户不属于此组织。")

        email = _claim_email(claims)
        if not email:
            raise AuthError("该 Microsoft 账户没有可用的邮箱地址。")
        if not self._domain_allowed(email):
            raise AuthError("该账户不属于此组织。")

        raw_roles = claims.get("roles")
        roles: tuple[str, ...] = ()
        if isinstance(raw_roles, list):
            roles = tuple(r for r in raw_roles if isinstance(r, str))
        return EntraIdentity(
            email=email,
            display_name=str(claims["name"]).strip() if claims.get("name") else None,
            tenant_id=tenant_id,
            roles=roles,
        )

    def _domain_allowed(self, email: str) -> bool:
        """Exact domain match, never a substring.

        `"contoso.com" in email` is the bug in the implementation this replaces: it admits
        `attacker@contoso.com.example.net`. Splitting on the last `@` and comparing the
        whole domain is the only form that means what it reads like.
        """
        if not self._allowed_email_domains:
            return True
        return email.rsplit("@", 1)[-1] in self._allowed_email_domains


def _claim_email(claims: dict[str, Any]) -> str:
    """Microsoft spreads the address across three claims depending on account type.

    `email` is present when the optional claim is configured, `preferred_username` is the
    usual v2.0 carrier, and `upn` appears on work accounts from older token versions. They
    are read in that order and the result is lower-cased so it matches the stored key.
    """
    for name in ("email", "preferred_username", "upn"):
        value = claims.get(name)
        if isinstance(value, str) and "@" in value:
            return value.strip().lower()
    return ""


def session_expiry(hours: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=hours)


@dataclass(frozen=True)
class EntraCaller:
    """An API caller proved by a Microsoft Entra access token rather than a session."""

    subject: str
    # The person's address for a delegated token; `app:<client id>` for a workload, which
    # has no address and so never becomes a row in app_user.
    email: str
    display_name: str | None
    roles: tuple[str, ...]
    delegated: bool
    expires_at: datetime


class EntraAccessTokenVerifier:
    """Validates an access token issued for this API, for automation and scripts.

    A person's token (delegated, carrying `scp`) must include the API scope and pass the
    same mail-domain allow-list as sign-in. A workload's token (app-only, no `scp`) has no
    address, so its authorization rests entirely on the app role and the pinned tenant --
    which is why a tenant must be pinned before any token is accepted: in a multi-tenant
    registration another tenant's administrator can assign this app's roles to anyone in
    their own tenant.
    """

    def __init__(
        self,
        client_id: str,
        allowed_email_domains: tuple[str, ...],
        tenant_ids: tuple[str, ...],
        required_scope: str = "Turnstile.Manage",
    ) -> None:
        self._client_id = client_id
        self._allowed_email_domains = tuple(d.lower().lstrip("@") for d in allowed_email_domains)
        self._tenant_ids = tuple(t.strip().lower() for t in tenant_ids)
        self._required_scope = required_scope
        self._jwks = PyJWKClient(
            "https://login.microsoftonline.com/common/discovery/v2.0/keys",
            cache_keys=True,
        )

    def verify(self, token: str) -> EntraCaller:
        if not self._client_id or not self._tenant_ids:
            raise AuthError("Microsoft 登录尚未配置。")
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=[self._client_id, f"api://{self._client_id}"],
                options={"require": ["exp", "iat", "aud", "iss", "tid", "oid"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError) as error:
            raise AuthError("Microsoft 登录未能完成，请重试。") from error

        tenant_id = str(claims.get("tid") or "").lower()
        if claims.get("iss") != f"https://login.microsoftonline.com/{tenant_id}/v2.0":
            raise AuthError("该账户不属于此组织。")
        if tenant_id not in self._tenant_ids:
            raise AuthError("该账户不属于此组织。")

        raw_roles = claims.get("roles")
        roles: tuple[str, ...] = ()
        if isinstance(raw_roles, list):
            roles = tuple(r for r in raw_roles if isinstance(r, str))
        scopes = str(claims.get("scp") or "").split()
        delegated = bool(scopes)
        if delegated:
            if self._required_scope not in scopes:
                raise AuthError("该令牌未包含所需的权限范围。")
            email = _claim_email(claims)
            if not email or email.rsplit("@", 1)[-1] not in self._allowed_email_domains:
                raise AuthError("该账户不属于此组织。")
            name = str(claims["name"]).strip() if claims.get("name") else None
        else:
            client = str(claims.get("azp") or claims.get("appid") or "")
            if not client:
                raise AuthError("Microsoft 登录未能完成，请重试。")
            email = f"app:{client}"
            name = None
        return EntraCaller(
            subject=str(claims["oid"]),
            email=email,
            display_name=name,
            roles=roles,
            delegated=delegated,
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
        )
