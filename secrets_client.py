"""
secrets_client.py — Azure Key Vault secret cache.

All secrets loaded once at boot via init_secrets().
No live re-fetch: service restart required after secret rotation.

Key Vault naming convention (multi-tenant App Registration):
  entra-client-id           Shared App Registration client ID (one for all tenants)
  entra-client-secret       Shared App Registration client secret (one for all tenants)
  entra-{org}-tenant-id     Azure AD tenant GUID for org (one per org)
  uyuni-sync-{org}          password for {org}-account-sync (one per org)
"""

import logging

logger = logging.getLogger(__name__)

_secrets: dict[str, str] = {}
_initialized: bool = False


def init_secrets(kv_url: str, orgs: list[str]) -> None:
    """
    Load all org secrets from Key Vault at boot.
    Raises RuntimeError if KV unreachable or any required secret missing or empty.
    """
    global _secrets, _initialized

    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    from concurrent.futures import ThreadPoolExecutor

    client = SecretClient(vault_url=kv_url, credential=DefaultAzureCredential())

    required: list[str] = ["entra-client-id", "entra-client-secret"]
    for org in orgs:
        required.extend([
            f"entra-{org}-tenant-id",
            f"uyuni-sync-{org}",
        ])

    def _fetch(key: str) -> tuple[str, str | None, str]:
        try:
            val = client.get_secret(key).value or ""
            if val:
                return key, val, ""
            logger.error("KV: secret %s is empty", key)
            return key, None, "empty value"
        except Exception as exc:
            logger.error("KV: failed to load %s — %s", key, exc)
            return key, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(len(required), 8)) as pool:
        results = list(pool.map(_fetch, required))

    loaded: dict[str, str] = {}
    failed: dict[str, str] = {}
    for key, val, err in results:
        if val:
            loaded[key] = val
            logger.debug("KV: loaded %s", key)
        else:
            failed[key] = err

    if failed:
        details = "; ".join(f"{k}: {v}" for k, v in sorted(failed.items()))
        raise RuntimeError(f"Key Vault secrets missing or empty: {details}")

    _secrets = loaded
    _initialized = True
    logger.info("Key Vault: %d secrets loaded for %d org(s)", len(loaded), len(orgs))


def get_sync_password(org: str) -> str:
    _require_init()
    key = f"uyuni-sync-{org}"
    try:
        return _secrets[key]
    except KeyError:
        raise KeyError(f"No sync password in KV for org {org!r} (key: {key!r})")


def get_entra_config(org: str) -> dict[str, str]:
    _require_init()
    try:
        return {
            "tenant_id":     _secrets[f"entra-{org}-tenant-id"],
            "client_id":     _secrets["entra-client-id"],
            "client_secret": _secrets["entra-client-secret"],
        }
    except KeyError as exc:
        raise KeyError(f"Entra config incomplete for org {org!r}: missing {exc}")


def _require_init() -> None:
    if not _initialized:
        raise RuntimeError("secrets_client not initialized — call init_secrets() at boot")


def _init_for_testing(data: dict[str, str]) -> None:
    """Inject secrets without KV. Test use only."""
    global _secrets, _initialized
    _secrets = dict(data)
    _initialized = True


def _reset() -> None:
    """Reset to uninitialized state. Test use only."""
    global _secrets, _initialized
    _secrets = {}
    _initialized = False