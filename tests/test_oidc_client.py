"""Tests for oidc_client.py."""

import pytest
from unittest.mock import MagicMock, patch

import oidc_client
from oidc_client import (
    OIDCClient,
    extract_identity,
    get_client,
    resolve_role,
)


def _make_client(tenant_id="t-guid", client_id="c-guid", client_secret="s"):
    with patch("oidc_client.msal.ConfidentialClientApplication"):
        return OIDCClient(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)


class TestExtractIdentity:
    def test_email_claim_priority(self):
        token = {"id_token_claims": {
            "email": "mario@leobasi.onmicrosoft.com",
            "preferred_username": "mario_leobasi.onmicrosoft.com#EXT#@asl06.onmicrosoft.com",
        }}
        assert extract_identity(token) == ("mario", "leobasi.onmicrosoft.com")

    def test_preferred_username_fallback(self):
        token = {"id_token_claims": {
            "preferred_username": "alberto@asl06.onmicrosoft.com",
        }}
        assert extract_identity(token) == ("alberto", "asl06.onmicrosoft.com")

    def test_upn_fallback(self):
        token = {"id_token_claims": {"upn": "user@example.com"}}
        assert extract_identity(token) == ("user", "example.com")

    def test_lowercases_upn(self):
        token = {"id_token_claims": {"email": "User@Domain.COM"}}
        assert extract_identity(token) == ("user", "domain.com")

    def test_missing_claims_returns_none(self):
        assert extract_identity({"id_token_claims": {}}) is None

    def test_no_at_sign_returns_none(self):
        token = {"id_token_claims": {"email": "nodomain"}}
        assert extract_identity(token) is None

    def test_no_dot_in_domain_returns_none(self):
        token = {"id_token_claims": {"email": "user@nodot"}}
        assert extract_identity(token) is None

    def test_empty_string_returns_none(self):
        token = {"id_token_claims": {"email": ""}}
        assert extract_identity(token) is None

    def test_multi_segment_local_preserved(self):
        token = {"id_token_claims": {"email": "mario.rossi.leo@asl06.onmicrosoft.com"}}
        assert extract_identity(token) == ("mario.rossi.leo", "asl06.onmicrosoft.com")

    def test_preferred_username_wins_over_truncated_email(self):
        token = {"id_token_claims": {
            "email": "mario.rossi@asl06.onmicrosoft.com",
            "preferred_username": "mario.rossi.leo@asl06.onmicrosoft.com",
        }}
        assert extract_identity(token) == ("mario.rossi.leo", "asl06.onmicrosoft.com")

    def test_not_dict_returns_none(self):
        assert extract_identity("not a dict") is None

    def test_missing_id_token_claims_returns_none(self):
        assert extract_identity({}) is None


class TestResolveRole:
    def test_uyuni_admin(self):
        token = {"id_token_claims": {"roles": ["UyuniAdmin"]}}
        authorized, role = resolve_role(token)
        assert authorized is True
        assert role == "org_admin"

    def test_uyuni_operator(self):
        token = {"id_token_claims": {"roles": ["UyuniOperator"]}}
        authorized, role = resolve_role(token)
        assert authorized is True
        assert role == "system_group_admin"

    def test_uyuni_readonly(self):
        token = {"id_token_claims": {"roles": ["UyuniReadOnly"]}}
        authorized, role = resolve_role(token)
        assert authorized is True
        assert role is None

    def test_no_role_denied(self):
        token = {"id_token_claims": {"roles": []}}
        authorized, role = resolve_role(token)
        assert authorized is False
        assert role is None

    def test_missing_roles_claim_denied(self):
        token = {"id_token_claims": {}}
        authorized, _ = resolve_role(token)
        assert authorized is False

    def test_unknown_role_denied(self):
        token = {"id_token_claims": {"roles": ["SomeOtherRole"]}}
        authorized, _ = resolve_role(token)
        assert authorized is False

    def test_string_role_normalized_to_list(self):
        token = {"id_token_claims": {"roles": "UyuniAdmin"}}
        authorized, role = resolve_role(token)
        assert authorized is True
        assert role == "org_admin"

    def test_admin_takes_priority(self):
        token = {"id_token_claims": {"roles": ["UyuniAdmin", "UyuniOperator"]}}
        authorized, role = resolve_role(token)
        assert authorized is True
        assert role == "org_admin"

    def test_not_dict_returns_false(self):
        authorized, _ = resolve_role("not a dict")
        assert authorized is False


class TestOIDCClientVerifyIssuer:
    def test_matching_issuer(self):
        client = _make_client(tenant_id="my-tenant")
        token = {"id_token_claims": {
            "iss": "https://login.microsoftonline.com/my-tenant/v2.0"
        }}
        assert client.verify_issuer(token) is True

    def test_wrong_tenant(self):
        client = _make_client(tenant_id="tenant-a")
        token = {"id_token_claims": {
            "iss": "https://login.microsoftonline.com/tenant-b/v2.0"
        }}
        assert client.verify_issuer(token) is False

    def test_missing_iss_claim(self):
        client = _make_client(tenant_id="my-tenant")
        token = {"id_token_claims": {}}
        assert client.verify_issuer(token) is False

    def test_not_dict_returns_false(self):
        client = _make_client()
        assert client.verify_issuer("not a dict") is False

    def test_missing_id_token_claims(self):
        client = _make_client(tenant_id="my-tenant")
        assert client.verify_issuer({}) is False


class TestOIDCClientExchangeCode:
    def test_success(self):
        with patch("oidc_client.msal.ConfidentialClientApplication") as MockMSAL:
            mock_app = MagicMock()
            mock_app.acquire_token_by_authorization_code.return_value = {
                "access_token": "tok", "id_token_claims": {}
            }
            MockMSAL.return_value = mock_app
            client = OIDCClient("tid", "cid", "sec")
            result = client.exchange_code("code123")
            assert result is not None
            assert "access_token" in result

    def test_error_returns_none(self):
        with patch("oidc_client.msal.ConfidentialClientApplication") as MockMSAL:
            mock_app = MagicMock()
            mock_app.acquire_token_by_authorization_code.return_value = {
                "error": "invalid_grant",
                "error_description": "Code expired",
            }
            MockMSAL.return_value = mock_app
            client = OIDCClient("tid", "cid", "sec")
            result = client.exchange_code("bad-code")
            assert result is None


class TestOIDCClientGetAuthUrl:
    def test_calls_msal_get_authorization_request_url(self):
        with patch("oidc_client.msal.ConfidentialClientApplication") as MockMSAL:
            mock_app = MagicMock()
            mock_app.get_authorization_request_url.return_value = "https://login.microsoft.com/auth"
            MockMSAL.return_value = mock_app
            client = OIDCClient("tid", "cid", "sec")
            url = client.get_auth_url(state="my-state")
            assert url == "https://login.microsoft.com/auth"
            mock_app.get_authorization_request_url.assert_called_once()
            call_kwargs = mock_app.get_authorization_request_url.call_args[1]
            assert call_kwargs["state"] == "my-state"
            assert call_kwargs["prompt"] == "login"


class TestGetClient:
    def test_returns_oidc_client_instance(self):
        with patch("oidc_client.msal.ConfidentialClientApplication"):
            client = get_client("asl06")
            assert isinstance(client, OIDCClient)

    def test_cached_same_instance(self):
        with patch("oidc_client.msal.ConfidentialClientApplication"):
            c1 = get_client("asl06")
            c2 = get_client("asl06")
            assert c1 is c2

    def test_different_orgs_different_instances(self):
        with patch("oidc_client.msal.ConfidentialClientApplication"):
            c1 = get_client("asl06")
            oidc_client.get_client.cache_clear()
            c2 = get_client("leobasi")
            # Different orgs — different expected issuers
            assert c1._expected_issuer != c2._expected_issuer

    def test_unknown_org_raises(self):
        import secrets_client as sc
        sc._init_for_testing({})  # empty — no entra config for any org
        with pytest.raises(KeyError):
            get_client("unknown-org")
