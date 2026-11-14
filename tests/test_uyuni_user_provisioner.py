"""Tests for uyuni_user_provisioner.py."""

import pytest
import xmlrpc.client
from unittest.mock import MagicMock, call, patch

from uyuni_user_provisioner import _parse_name, ensure_user


# ── _parse_name ───────────────────────────────────────────────────────────────

class TestParseName:
    def test_first_and_last(self):
        assert _parse_name("mario.rossi") == ("Mario", "Rossi")

    def test_single_part_duplicates(self):
        assert _parse_name("mario") == ("Mario", "Mario")

    def test_capitalizes(self):
        assert _parse_name("ALBERTO.ameglio") == ("Alberto", "Ameglio")

    def test_three_segments_uses_first_two_for_names(self):
        assert _parse_name("mario.rossi.pippo") == ("Mario", "Rossi")

    def test_four_segments_uses_first_two_for_names(self):
        assert _parse_name("mario.rossi.ext.extra") == ("Mario", "Rossi")

    def test_underscore_treated_as_separator(self):
        assert _parse_name("mario_rossi") == ("Mario", "Rossi")

    def test_underscore_with_dot_mixed(self):
        assert _parse_name("mario_rossi.bianchi") == ("Mario", "Rossi")

    def test_strips_special_chars(self):
        # Characters not in alphanumeric + .-_ are stripped
        result = _parse_name("user!@#name")
        assert result[0] != ""

    def test_empty_string_fallback(self):
        assert _parse_name("") == ("User", "User")

    def test_only_special_chars_fallback(self):
        assert _parse_name("!!!") == ("User", "User")


# ── ensure_user ───────────────────────────────────────────────────────────────

def _make_xmlrpc_mock(user_exists: bool = False, current_roles: list = None):
    """Build a mock xmlrpc ServerProxy."""
    mock_client = MagicMock()
    mock_client.auth.login.return_value = "session-key"

    if user_exists:
        mock_client.user.getDetails.return_value = {"login": "mario@leobasi.com"}
    else:
        fault = xmlrpc.client.Fault(-213, "No such user")
        mock_client.user.getDetails.side_effect = fault

    mock_client.user.listRoles.return_value = current_roles or []
    return mock_client


def _run_ensure_user(mock_client, **kwargs):
    defaults = dict(
        uyuni_url="https://uyuni-test.internal",
        org="asl06",
        login="mario@asl06.onmicrosoft.com",
        uyuni_role="org_admin",
        sync_pass="sync-pass",
    )
    defaults.update(kwargs)
    with patch("uyuni_user_provisioner._make_client", return_value=mock_client):
        return ensure_user(**defaults)


class TestEnsureUserNewUser:
    def test_creates_user_when_not_exists(self):
        mock = _make_xmlrpc_mock(user_exists=False)
        _run_ensure_user(mock)
        mock.user.create.assert_called_once()
        create_args = mock.user.create.call_args[0]
        assert create_args[1] == "mario@asl06.onmicrosoft.com"  # login

    def test_create_uses_login_as_email(self):
        mock = _make_xmlrpc_mock(user_exists=False)
        _run_ensure_user(mock, login="mario@asl06.onmicrosoft.com")
        create_args = mock.user.create.call_args[0]
        assert create_args[5] == "mario@asl06.onmicrosoft.com"  # email = login

    def test_returns_ephemeral_password(self):
        mock = _make_xmlrpc_mock(user_exists=False)
        pw = _run_ensure_user(mock)
        assert isinstance(pw, str) and len(pw) > 0

    def test_ephemeral_password_unique_each_call(self):
        mock1 = _make_xmlrpc_mock(user_exists=False)
        mock2 = _make_xmlrpc_mock(user_exists=False)
        pw1 = _run_ensure_user(mock1)
        pw2 = _run_ensure_user(mock2)
        assert pw1 != pw2

    def test_adds_role_for_new_user(self):
        mock = _make_xmlrpc_mock(user_exists=False, current_roles=[])
        _run_ensure_user(mock, uyuni_role="org_admin")
        mock.user.addRole.assert_called_once_with("session-key", "mario@asl06.onmicrosoft.com", "org_admin")

    def test_no_role_for_readonly(self):
        mock = _make_xmlrpc_mock(user_exists=False, current_roles=[])
        _run_ensure_user(mock, uyuni_role=None)
        mock.user.addRole.assert_not_called()


class TestEnsureUserExistingUser:
    def test_updates_password_not_creates(self):
        mock = _make_xmlrpc_mock(user_exists=True)
        _run_ensure_user(mock)
        mock.user.create.assert_not_called()
        mock.user.setDetails.assert_called_once()

    def test_set_details_contains_password(self):
        mock = _make_xmlrpc_mock(user_exists=True)
        pw = _run_ensure_user(mock)
        call_args = mock.user.setDetails.call_args[0]
        assert call_args[2] == {"password": pw}

    def test_removes_extra_role(self):
        mock = _make_xmlrpc_mock(user_exists=True, current_roles=["org_admin", "system_group_admin"])
        _run_ensure_user(mock, uyuni_role="org_admin")
        mock.user.removeRole.assert_called_once_with(
            "session-key", "mario@asl06.onmicrosoft.com", "system_group_admin"
        )

    def test_does_not_readd_existing_role(self):
        mock = _make_xmlrpc_mock(user_exists=True, current_roles=["org_admin"])
        _run_ensure_user(mock, uyuni_role="org_admin")
        mock.user.addRole.assert_not_called()


class TestEnsureUserErrors:
    def test_empty_sync_pass_raises(self):
        mock = _make_xmlrpc_mock()
        with pytest.raises(RuntimeError, match="sync_pass is empty"):
            _run_ensure_user(mock, sync_pass="")

    def test_login_failure_raises(self):
        mock = _make_xmlrpc_mock()
        mock.auth.login.side_effect = Exception("connection refused")
        with pytest.raises(RuntimeError, match="XML-RPC login failed"):
            _run_ensure_user(mock)

    def test_invalid_session_raises(self):
        mock = _make_xmlrpc_mock()
        mock.auth.login.return_value = "key"
        mock.user.getDetails.side_effect = xmlrpc.client.Fault(-2, "Invalid session")
        with pytest.raises(RuntimeError, match="session invalid"):
            _run_ensure_user(mock)

    def test_logout_called_even_on_error(self):
        mock = _make_xmlrpc_mock()
        mock.user.getDetails.side_effect = xmlrpc.client.Fault(-1, "Generic error")
        with pytest.raises(xmlrpc.client.Fault):
            _run_ensure_user(mock)
        mock.auth.logout.assert_called_once_with("session-key")


class TestSyncRole:
    def test_upgrades_from_operator_to_admin(self):
        mock = _make_xmlrpc_mock(user_exists=True, current_roles=["system_group_admin"])
        _run_ensure_user(mock, uyuni_role="org_admin")
        mock.user.removeRole.assert_called_once_with(
            "session-key", "mario@asl06.onmicrosoft.com", "system_group_admin"
        )
        mock.user.addRole.assert_called_once_with(
            "session-key", "mario@asl06.onmicrosoft.com", "org_admin"
        )

    def test_downgrades_from_admin_to_readonly(self):
        mock = _make_xmlrpc_mock(user_exists=True, current_roles=["org_admin"])
        _run_ensure_user(mock, uyuni_role=None)
        mock.user.removeRole.assert_called_once_with(
            "session-key", "mario@asl06.onmicrosoft.com", "org_admin"
        )
        mock.user.addRole.assert_not_called()
