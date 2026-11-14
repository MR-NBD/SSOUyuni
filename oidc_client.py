"""
oidc_client.py — Multi-tenant OIDC with Microsoft Entra ID.

Each org authenticates against its own Entra tenant App Registration.
OIDCClient is instantiated per-org and cached via get_client().

Flow (per org):
  get_client(org)           → OIDCClient (cached, config from Key Vault)
  client.get_auth_url()     → browser redirect URL to org's tenant
  client.exchange_code()    → trade auth code for JWT (server-to-server)
  client.verify_issuer()    → MANDATORY: reject tokens from wrong tenant
  extract_identity()        → (username, domain) from JWT claims
  resolve_role()            → UYUNI role from App Role claim
"""

import functools
import logging
import os

import msal

import secrets_client

logger = logging.getLogger(__name__)

SCOPES = ["User.Read"]

_KNOWN_ROLES = frozenset({"UyuniAdmin", "UyuniOperator", "UyuniReadOnly"})


def _get_claims(token_result: object) -> dict | None:
    if not isinstance(token_result, dict):
        return None
    claims = token_result.get("id_token_claims")
    return claims if isinstance(claims, dict) else None


@functools.lru_cache(maxsize=1)
def _redirect_uri() -> str:
    base = os.environ.get("IDENTITY_SYNC_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("IDENTITY_SYNC_URL not set")
    return f"{base}/api/callback"


class OIDCClient:
    """OIDC client bound to a single org's Entra tenant. Thread-safe (MSAL is)."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._expected_issuer = (
            f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        )
        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )

    def get_auth_url(self, state: str) -> str:
        """
        Build Microsoft authorization URL for this org's tenant.
        prompt="login" forces credential entry on every access (shared VMs).
        """
        return self._app.get_authorization_request_url(
            scopes=SCOPES,
            redirect_uri=_redirect_uri(),
            state=state,
            prompt="login",
        )

    def exchange_code(self, code: str) -> dict | None:
        """Server-to-server token exchange only — the code must never be forwarded to the browser."""
        result = self._app.acquire_token_by_authorization_code(
            code=code,
            scopes=SCOPES,
            redirect_uri=_redirect_uri(),
        )
        if "error" in result:
            logger.error(
                "OIDC exchange failed: %s — %s (correlation_id=%s)",
                result.get("error"),
                result.get("error_description"),
                result.get("correlation_id"),
            )
            return None
        return result

    def verify_issuer(self, token_result: dict) -> bool:
        """Reject tokens whose iss claim doesn't match this org's tenant — prevents cross-tenant substitution."""
        claims = _get_claims(token_result)
        if claims is None:
            return False
        actual = claims.get("iss", "")
        if actual != self._expected_issuer:
            logger.warning(
                "Issuer mismatch: expected %r got %r",
                self._expected_issuer,
                actual,
            )
            return False
        return True


@functools.lru_cache(maxsize=None)
def get_client(org: str) -> OIDCClient:
    """Return cached OIDCClient for org. Config from secrets_client (boot-loaded)."""
    cfg = secrets_client.get_entra_config(org)
    return OIDCClient(**cfg)


def extract_identity(token_result: dict) -> tuple[str, str] | None:
    """
    Return (local_part, domain) from JWT claims.
    Priority: preferred_username (UPN) > email > upn.
    preferred_username is the full UPN for native users; skip if #EXT# (B2B guest format).
    """
    claims = _get_claims(token_result)
    if claims is None:
        return None

    pref = claims.get("preferred_username", "")
    if isinstance(pref, str) and pref and "#EXT#" not in pref.upper():
        upn = pref
    else:
        upn = claims.get("email") or claims.get("upn") or ""
    if not isinstance(upn, str):
        return None

    upn = upn.strip().lower()
    if "@" not in upn or len(upn) < 3:
        return None

    local, _, domain = upn.partition("@")
    if not local or not domain or "." not in domain:
        return None

    return local, domain


def resolve_role(token_result: dict) -> tuple[bool, str | None]:
    """
    Map Entra App Role to UYUNI role.
    Returns (authorized, uyuni_role): UyuniAdmin→org_admin, UyuniOperator→system_group_admin,
    UyuniReadOnly→None, no role→(False, None).
    """
    claims = _get_claims(token_result)
    if claims is None:
        return False, None

    roles_raw = claims.get("roles")
    if roles_raw is None:
        return False, None
    if isinstance(roles_raw, str):
        roles_raw = [roles_raw]
    if not isinstance(roles_raw, list):
        return False, None

    roles = frozenset(r for r in roles_raw if isinstance(r, str))

    if "UyuniAdmin" in roles:
        return True, "org_admin"
    if "UyuniOperator" in roles:
        return True, "system_group_admin"
    if "UyuniReadOnly" in roles:
        return True, None

    unknown = roles - _KNOWN_ROLES
    if unknown:
        logger.debug("Unrecognized Entra app roles (access denied): %s", sorted(unknown))
    return False, None
