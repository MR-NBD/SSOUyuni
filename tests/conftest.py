"""
conftest.py — Test fixtures for Identity Sync v3.

IMPORTANT: env vars must be set before any module from Identity-Sync-v3 is imported,
because app.py reads them at module level.
"""

import os
import sys

# ── Set env BEFORE any imports from the package ───────────────────────────────
os.environ["IDENTITY_SYNC_TESTING"] = "1"
os.environ.setdefault("UYUNI_URL", "https://uyuni-test.internal")
os.environ.setdefault("IDENTITY_SYNC_URL", "https://uyuni-ldo.internal")
os.environ.setdefault("UYUNI_ORGS", "asl06,leobasi")
os.environ.setdefault("UYUNI_CA_CERT", "/fake/ca.crt")
os.environ.setdefault("AZURE_KEYVAULT_URL", "https://fake-kv.vault.azure.net/")

# Add Identity-Sync-v3 root to path so modules are importable
_v3_root = os.path.dirname(os.path.dirname(__file__))
if _v3_root not in sys.path:
    sys.path.insert(0, _v3_root)

import pytest
from unittest.mock import MagicMock, patch
import secrets_client
import oidc_client

# ── Shared test data ──────────────────────────────────────────────────────────

TEST_SECRETS = {
    "uyuni-sync-asl06":           "sync-pass-asl06",
    "uyuni-sync-leobasi":         "sync-pass-leobasi",
    "entra-asl06-tenant-id":      "asl06-tenant-guid",
    "entra-asl06-client-id":      "asl06-client-guid",
    "entra-asl06-client-secret":  "asl06-client-secret",
    "entra-leobasi-tenant-id":    "leobasi-tenant-guid",
    "entra-leobasi-client-id":    "leobasi-client-guid",
    "entra-leobasi-client-secret":"leobasi-client-secret",
}

# ── Autouse fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_secrets():
    """Inject test secrets before each test, clean up after."""
    secrets_client._init_for_testing(TEST_SECRETS.copy())
    yield
    secrets_client._reset()


@pytest.fixture(autouse=True)
def mock_msal():
    """
    Prevent MSAL from making real network calls for tenant discovery.
    Tests that need specific MSAL behavior patch app.get_client directly.
    """
    mock_app = MagicMock()
    mock_app.get_authorization_request_url.return_value = "https://mock-auth.example.com/auth"
    mock_app.acquire_token_by_authorization_code.return_value = {}
    with patch("oidc_client.msal.ConfidentialClientApplication", return_value=mock_app):
        yield mock_app


@pytest.fixture(autouse=True)
def reset_oidc_cache():
    """Clear OIDCClient lru_cache between tests."""
    oidc_client.get_client.cache_clear()
    oidc_client._redirect_uri.cache_clear()
    yield
    oidc_client.get_client.cache_clear()
    oidc_client._redirect_uri.cache_clear()


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear server-side session store between tests."""
    from app import _SESSIONS, _SESSIONS_LOCK, _known_orgs
    with _SESSIONS_LOCK:
        _SESSIONS.clear()
    _known_orgs.cache_clear()
    yield
    with _SESSIONS_LOCK:
        _SESSIONS.clear()
    _known_orgs.cache_clear()


@pytest.fixture(autouse=True)
def clear_auth_failures():
    """Reset per-IP auth failure counters between tests."""
    from app import _AUTH_FAILURES, _AUTH_FAILURES_LOCK
    with _AUTH_FAILURES_LOCK:
        _AUTH_FAILURES.clear()
    yield
    with _AUTH_FAILURES_LOCK:
        _AUTH_FAILURES.clear()


# ── Flask test client ─────────────────────────────────────────────────────────

@pytest.fixture
def flask_client():
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    # use_cookies=False: prevents Werkzeug's cookie jar from deleting
    # HTTP_COOKIE set via environ_overrides when the jar is empty.
    with flask_app.test_client(use_cookies=False) as c:
        yield c
