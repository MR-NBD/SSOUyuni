"""
uyuni_user_provisioner.py — Provisioning individual UYUNI accounts via XML-RPC.

On first login: creates account and assigns role from JWT.
On subsequent logins: resets ephemeral password and syncs role.

The UYUNI password is single-use: generated randomly each login,
set via user.setDetails(), used once to open the web session, then discarded.
The Microsoft JWT is the sole source of truth for authentication.

Service account: {org}-account-sync (Organization Administrator).
Password is passed by the caller (from secrets_client) — not read from env.
"""

import functools
import http.client
import logging
import os
import secrets
import ssl
import xmlrpc.client

logger = logging.getLogger(__name__)

_XMLRPC_TIMEOUT = 15

_MANAGEABLE_ROLES      = frozenset(["org_admin", "system_group_admin"])
_FAULT_USER_NOT_FOUND  = -213
_FAULT_SESSION_INVALID = -2


class _TimeoutTransport(xmlrpc.client.SafeTransport):
    """SafeTransport with configurable timeout — xmlrpc.client has no native timeout."""

    def __init__(self, timeout: int, context: ssl.SSLContext):
        super().__init__(context=context)
        self._timeout = timeout

    def make_connection(self, host: str) -> http.client.HTTPSConnection:
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


@functools.lru_cache(maxsize=1)
def _make_ssl_context() -> ssl.SSLContext:
    ca_cert = os.environ.get("UYUNI_CA_CERT", "").strip()
    if not ca_cert:
        raise RuntimeError(
            "UYUNI_CA_CERT must be set for secure XML-RPC. "
            "SSL verification bypass is not allowed in production."
        )
    return ssl.create_default_context(cafile=ca_cert)


def _make_client(uyuni_url: str) -> xmlrpc.client.ServerProxy:
    transport = _TimeoutTransport(timeout=_XMLRPC_TIMEOUT, context=_make_ssl_context())
    return xmlrpc.client.ServerProxy(
        uyuni_url.rstrip("/") + "/rpc/api",
        transport=transport,
    )


def _parse_name(local_part: str) -> tuple[str, str]:
    """
    "alberto.ameglio"    → ("Alberto", "Ameglio")
    "mario.rossi.leo"    → ("Mario", "Rossi")  — only first 2 segments used as name
    "mario_rossi"        → ("Mario", "Rossi")  — underscore treated as separator
    "mario"              → ("Mario", "Mario")  — last_name cannot be empty in UYUNI
    Strips non-alphanumeric characters (except .-_) to prevent XML-RPC injection.
    """
    sanitized = "".join(c for c in local_part if c.isalnum() or c in ".-_")
    if not sanitized:
        return "User", "User"
    parts = sanitized.replace("_", ".").split(".")
    first = parts[0].capitalize() if parts[0] else "User"
    last  = parts[1].capitalize() if len(parts) > 1 and parts[1] else first
    return first, last


def ensure_user(
    uyuni_url: str,
    org: str,
    login: str,
    uyuni_role: str | None,
    sync_pass: str,
) -> str:
    """
    Ensure user exists in UYUNI with correct role.
    Returns an ephemeral password (single-use: valid until next login, never stored).
    """
    if not sync_pass:
        raise RuntimeError(f"sync_pass is empty for org {org!r}")

    sync_user    = f"{org}-account-sync"
    ephemeral_pw = secrets.token_urlsafe(24)
    local_part   = login.split("@")[0]
    first_name, last_name = _parse_name(local_part)

    client = _make_client(uyuni_url)

    try:
        key = client.auth.login(sync_user, sync_pass)
    except Exception as e:
        raise RuntimeError(f"XML-RPC login failed for {sync_user!r}: {e}") from e

    try:
        user_exists = False
        try:
            client.user.getDetails(key, login)
            user_exists = True
        except xmlrpc.client.Fault as f:
            if f.faultCode == _FAULT_USER_NOT_FOUND:
                pass
            elif f.faultCode == _FAULT_SESSION_INVALID:
                raise RuntimeError(f"XML-RPC session invalid for {sync_user!r}") from f
            else:
                logger.error("XML-RPC Fault %d: %s", f.faultCode, f.faultString)
                raise

        if not user_exists:
            client.user.create(key, login, ephemeral_pw, first_name, last_name, login)
            logger.info("UYUNI user created: %r in org %r", login, org)
        else:
            client.user.setDetails(key, login, {"password": ephemeral_pw})

        _sync_role(client, key, login, uyuni_role)

    finally:
        try:
            client.auth.logout(key)
        except Exception:
            pass

    return ephemeral_pw


def _sync_role(
    client: xmlrpc.client.ServerProxy,
    key: str,
    login: str,
    desired_role: str | None,
) -> None:
    """
    Align UYUNI roles to desired role from JWT.
    Adds/removes only roles in _MANAGEABLE_ROLES — never touches satellite_admin.
    """
    try:
        current_roles = frozenset(client.user.listRoles(key, login))
    except Exception as e:
        logger.warning("Could not fetch roles for %r: %s — skipping sync", login, e)
        return

    desired_roles = frozenset([desired_role]) if desired_role else frozenset()

    for role in (_MANAGEABLE_ROLES - desired_roles) & current_roles:
        try:
            client.user.removeRole(key, login, role)
            logger.info("Role %r removed from %r", role, login)
        except Exception as e:
            logger.warning("removeRole %r from %r failed: %s", role, login, e)

    for role in desired_roles - current_roles:
        try:
            client.user.addRole(key, login, role)
            logger.info("Role %r added to %r", role, login)
        except Exception as e:
            logger.warning("addRole %r to %r failed: %s", role, login, e)
