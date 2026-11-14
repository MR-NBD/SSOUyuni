"""Tests for secrets_client.py."""

import pytest
from unittest.mock import MagicMock, patch

import secrets_client


class TestInitForTesting:
    def test_sets_initialized(self):
        secrets_client._init_for_testing({"k": "v"})
        assert secrets_client._initialized

    def test_stores_secrets(self):
        secrets_client._init_for_testing({"uyuni-sync-asl06": "pass"})
        assert secrets_client._secrets["uyuni-sync-asl06"] == "pass"

    def test_reset_clears_all(self):
        secrets_client._init_for_testing({"k": "v"})
        secrets_client._reset()
        assert not secrets_client._initialized
        assert secrets_client._secrets == {}


class TestNotInitialized:
    def test_get_sync_password_raises(self):
        secrets_client._reset()
        with pytest.raises(RuntimeError, match="not initialized"):
            secrets_client.get_sync_password("asl06")

    def test_get_entra_config_raises(self):
        secrets_client._reset()
        with pytest.raises(RuntimeError, match="not initialized"):
            secrets_client.get_entra_config("asl06")


class TestGetSyncPassword:
    def test_known_org(self):
        secrets_client._init_for_testing({"uyuni-sync-asl06": "mypass"})
        assert secrets_client.get_sync_password("asl06") == "mypass"

    def test_multiple_orgs_independent(self):
        secrets_client._init_for_testing({
            "uyuni-sync-asl06": "pass-a",
            "uyuni-sync-leobasi": "pass-l",
        })
        assert secrets_client.get_sync_password("asl06") == "pass-a"
        assert secrets_client.get_sync_password("leobasi") == "pass-l"

    def test_unknown_org_raises_key_error(self):
        secrets_client._init_for_testing({"uyuni-sync-asl06": "pass"})
        with pytest.raises(KeyError, match="unknown"):
            secrets_client.get_sync_password("unknown")


class TestGetEntraConfig:
    def test_returns_all_fields(self):
        secrets_client._init_for_testing({
            "entra-asl06-tenant-id": "tid",
            "entra-client-id":       "cid",
            "entra-client-secret":   "csec",
        })
        cfg = secrets_client.get_entra_config("asl06")
        assert cfg == {"tenant_id": "tid", "client_id": "cid", "client_secret": "csec"}

    def test_shared_client_returned_for_all_orgs(self):
        secrets_client._init_for_testing({
            "entra-asl06-tenant-id":   "tid-a",
            "entra-leobasi-tenant-id": "tid-l",
            "entra-client-id":         "shared-cid",
            "entra-client-secret":     "shared-csec",
        })
        assert secrets_client.get_entra_config("asl06")["client_id"] == "shared-cid"
        assert secrets_client.get_entra_config("leobasi")["client_id"] == "shared-cid"
        assert secrets_client.get_entra_config("asl06")["tenant_id"] == "tid-a"
        assert secrets_client.get_entra_config("leobasi")["tenant_id"] == "tid-l"

    def test_missing_tenant_id_raises(self):
        secrets_client._init_for_testing({
            "entra-client-id":     "cid",
            "entra-client-secret": "csec",
        })
        with pytest.raises(KeyError):
            secrets_client.get_entra_config("asl06")

    def test_missing_client_id_raises(self):
        secrets_client._init_for_testing({
            "entra-asl06-tenant-id": "tid",
            "entra-client-secret":   "csec",
        })
        with pytest.raises(KeyError):
            secrets_client.get_entra_config("asl06")

    def test_unknown_org_raises(self):
        secrets_client._init_for_testing({
            "entra-asl06-tenant-id": "tid",
            "entra-client-id":       "cid",
            "entra-client-secret":   "csec",
        })
        with pytest.raises(KeyError):
            secrets_client.get_entra_config("unknown")


class TestInitSecretsFromKV:
    """Tests for init_secrets() — uses sys.modules patching for lazy imports."""

    def _make_mock_kv_client(self, data: dict):
        def get_secret(key):
            val = data.get(key)
            if val is None:
                raise Exception(f"Secret {key!r} not found in mock KV")
            s = MagicMock()
            s.value = val
            return s

        mock_client = MagicMock()
        mock_client.get_secret.side_effect = get_secret
        return mock_client

    def _mock_azure_modules(self, kv_client):
        """Return a sys.modules patch dict with mocked Azure SDK."""
        mock_identity = MagicMock()
        mock_kv_secrets = MagicMock()
        mock_kv_secrets.SecretClient = MagicMock(return_value=kv_client)
        return {
            "azure": MagicMock(),
            "azure.identity": mock_identity,
            "azure.keyvault": MagicMock(),
            "azure.keyvault.secrets": mock_kv_secrets,
        }

    def test_loads_all_secrets(self):
        secrets_client._reset()
        data = {
            "entra-client-id":       "cid",
            "entra-client-secret":   "csec",
            "entra-asl06-tenant-id": "tid",
            "uyuni-sync-asl06":      "syncpass",
        }
        kv_client = self._make_mock_kv_client(data)

        import sys
        with patch.dict("sys.modules", self._mock_azure_modules(kv_client)):
            secrets_client.init_secrets("https://fake.vault.azure.net/", ["asl06"])

        assert secrets_client.get_sync_password("asl06") == "syncpass"
        assert secrets_client.get_entra_config("asl06") == {
            "tenant_id": "tid", "client_id": "cid", "client_secret": "csec"
        }

    def test_multi_org_shared_client(self):
        secrets_client._reset()
        data = {
            "entra-client-id":           "shared-cid",
            "entra-client-secret":       "shared-csec",
            "entra-asl06-tenant-id":     "tid-asl06",
            "uyuni-sync-asl06":          "pass-asl06",
            "entra-leobasi-tenant-id":   "tid-leobasi",
            "uyuni-sync-leobasi":        "pass-leobasi",
        }
        kv_client = self._make_mock_kv_client(data)

        import sys
        with patch.dict("sys.modules", self._mock_azure_modules(kv_client)):
            secrets_client.init_secrets("https://fake.vault.azure.net/", ["asl06", "leobasi"])

        assert secrets_client.get_entra_config("asl06")["client_id"] == "shared-cid"
        assert secrets_client.get_entra_config("leobasi")["client_id"] == "shared-cid"
        assert secrets_client.get_entra_config("asl06")["tenant_id"] == "tid-asl06"
        assert secrets_client.get_entra_config("leobasi")["tenant_id"] == "tid-leobasi"

    def test_missing_secret_raises_runtime_error(self):
        secrets_client._reset()
        kv_client = self._make_mock_kv_client({})  # empty — all secrets missing

        import sys
        with patch.dict("sys.modules", self._mock_azure_modules(kv_client)):
            with pytest.raises(RuntimeError, match="missing or empty"):
                secrets_client.init_secrets("https://fake.vault.azure.net/", ["asl06"])

    def test_missing_secret_includes_reason(self):
        secrets_client._reset()
        kv_client = self._make_mock_kv_client({})

        import sys
        with patch.dict("sys.modules", self._mock_azure_modules(kv_client)):
            with pytest.raises(RuntimeError) as exc_info:
                secrets_client.init_secrets("https://fake.vault.azure.net/", ["asl06"])
        assert "uyuni-sync-asl06" in str(exc_info.value)

    def test_empty_secret_value_raises(self):
        secrets_client._reset()
        data = {
            "entra-client-id":       "",   # empty value
            "entra-client-secret":   "csec",
            "entra-asl06-tenant-id": "tid",
            "uyuni-sync-asl06":      "syncpass",
        }
        kv_client = self._make_mock_kv_client(data)

        import sys
        with patch.dict("sys.modules", self._mock_azure_modules(kv_client)):
            with pytest.raises(RuntimeError, match="missing or empty"):
                secrets_client.init_secrets("https://fake.vault.azure.net/", ["asl06"])
