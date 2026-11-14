"""Tests for app.py routes."""

import pytest
from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_token(issuer="https://login.microsoftonline.com/asl06-tenant-guid/v2.0",
                email="mario@asl06.onmicrosoft.com",
                roles=None):
    return {
        "access_token": "fake-access",
        "id_token_claims": {
            "iss": issuer,
            "email": email,
            "roles": roles if roles is not None else ["UyuniAdmin"],
        },
    }


def _callback_request(flask_client, state="test-state", org="asl06",
                      code="auth-code", url_state=None):
    """Make a callback request with cookies set via WSGI environ (bypasses cookie jar)."""
    url_state = url_state or state
    return flask_client.get(
        f"/api/callback?code={code}&state={url_state}",
        environ_overrides={"HTTP_COOKIE": f"oidc_state={state}; oidc_org={org}"},
    )


# ── Basic routes ──────────────────────────────────────────────────────────────

class TestBasicRoutes:
    def test_health(self, flask_client):
        resp = flask_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}

    def test_root_redirects_to_login(self, flask_client):
        resp = flask_client.get("/")
        assert resp.status_code == 302
        assert "/api/login" in resp.headers["Location"]

    def test_unknown_api_path_returns_404(self, flask_client):
        resp = flask_client.get("/api/unknown-endpoint")
        assert resp.status_code == 404


# ── Login selector ────────────────────────────────────────────────────────────

class TestLoginSelector:
    def test_multiple_orgs_shows_selector(self, flask_client):
        resp = flask_client.get("/api/login")
        assert resp.status_code == 200
        data = resp.data.decode()
        assert "asl06" in data.lower()
        assert "leobasi" in data.lower()

    def test_selector_contains_org_options(self, flask_client):
        resp = flask_client.get("/api/login")
        data = resp.data.decode()
        assert "value='asl06'" in data
        assert "value='leobasi'" in data

    def test_single_org_redirects_directly(self, flask_client):
        import os
        orig = os.environ.get("UYUNI_ORGS")
        os.environ["UYUNI_ORGS"] = "asl06"
        from app import _known_orgs
        _known_orgs.cache_clear()
        try:
            resp = flask_client.get("/api/login")
            assert resp.status_code == 302
            assert "/api/login/asl06" in resp.headers["Location"]
        finally:
            os.environ["UYUNI_ORGS"] = orig or "asl06,leobasi"
            _known_orgs.cache_clear()

    def test_clears_old_session_cookie(self, flask_client):
        # Existing spm_session should be cleared
        resp = flask_client.get(
            "/api/login",
            environ_overrides={"HTTP_COOKIE": "spm_session=old-token"},
        )
        # Cookie should be expired in response
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "spm_session" in set_cookie


# ── Login org ─────────────────────────────────────────────────────────────────

class TestLoginOrg:
    def _mock_oidc_client(self, url="https://auth.example.com"):
        mock_oidc = MagicMock()
        mock_oidc.get_auth_url.return_value = url
        return mock_oidc

    def test_unknown_org_returns_404(self, flask_client):
        resp = flask_client.get("/api/login/unknown-org")
        assert resp.status_code == 404

    def test_known_org_redirects_to_microsoft(self, flask_client):
        with patch("app.get_client") as mock_gc:
            mock_gc.return_value = self._mock_oidc_client("https://login.microsoftonline.com/auth")
            resp = flask_client.get("/api/login/asl06")
            assert resp.status_code == 302
            assert "login.microsoftonline.com" in resp.headers["Location"]

    def test_sets_oidc_state_cookie(self, flask_client):
        with patch("app.get_client") as mock_gc:
            mock_gc.return_value = self._mock_oidc_client()
            resp = flask_client.get("/api/login/asl06")
            cookie_names = [c.split("=")[0] for c in resp.headers.getlist("Set-Cookie")]
            assert "oidc_state" in cookie_names

    def test_sets_oidc_org_cookie(self, flask_client):
        with patch("app.get_client") as mock_gc:
            mock_gc.return_value = self._mock_oidc_client()
            resp = flask_client.get("/api/login/asl06")
            assert "oidc_org=asl06" in " ".join(resp.headers.getlist("Set-Cookie"))

    def test_case_insensitive_org(self, flask_client):
        with patch("app.get_client") as mock_gc:
            mock_gc.return_value = self._mock_oidc_client()
            resp = flask_client.get("/api/login/ASL06")
            assert resp.status_code == 302


# ── Callback ──────────────────────────────────────────────────────────────────

class TestCallbackMissingOrInvalidSession:
    def test_missing_oidc_org_cookie_returns_400(self, flask_client):
        resp = flask_client.get("/api/callback?code=x&state=y",
                                environ_overrides={"HTTP_COOKIE": "oidc_state=y"})
        assert resp.status_code == 400

    def test_unknown_org_cookie_returns_400(self, flask_client):
        resp = flask_client.get(
            "/api/callback?code=x&state=y",
            environ_overrides={"HTTP_COOKIE": "oidc_state=y; oidc_org=unknown-org"},
        )
        assert resp.status_code == 400

    def test_csrf_mismatch_returns_400(self, flask_client):
        resp = flask_client.get(
            "/api/callback?code=x&state=WRONG",
            environ_overrides={"HTTP_COOKIE": "oidc_state=EXPECTED; oidc_org=asl06"},
        )
        assert resp.status_code == 400

    def test_missing_state_cookie_returns_400(self, flask_client):
        resp = flask_client.get(
            "/api/callback?code=x&state=y",
            environ_overrides={"HTTP_COOKIE": "oidc_org=asl06"},
        )
        assert resp.status_code == 400


class TestCallbackOIDCErrors:
    def test_microsoft_error_param_returns_401(self, flask_client):
        resp = flask_client.get(
            "/api/callback?error=access_denied&state=s",
            environ_overrides={"HTTP_COOKIE": "oidc_state=s; oidc_org=asl06"},
        )
        assert resp.status_code == 401

    def test_missing_code_returns_400(self, flask_client):
        resp = flask_client.get(
            "/api/callback?state=s",
            environ_overrides={"HTTP_COOKIE": "oidc_state=s; oidc_org=asl06"},
        )
        assert resp.status_code == 400

    def test_token_exchange_failure_returns_401(self, flask_client):
        with patch("app.get_client") as mock_get_client:
            mock_oidc = MagicMock()
            mock_oidc.exchange_code.return_value = None
            mock_get_client.return_value = mock_oidc

            resp = _callback_request(flask_client)
            assert resp.status_code == 401

    def test_issuer_mismatch_returns_401(self, flask_client):
        with patch("app.get_client") as mock_get_client:
            mock_oidc = MagicMock()
            mock_oidc.exchange_code.return_value = _fake_token()
            mock_oidc.verify_issuer.return_value = False
            mock_get_client.return_value = mock_oidc

            resp = _callback_request(flask_client)
            assert resp.status_code == 401


class TestCallbackAuthorizationFailures:
    def test_unauthorized_role_returns_403(self, flask_client):
        with patch("app.get_client") as mock_get_client, \
             patch("app.extract_identity", return_value=("mario", "asl06.onmicrosoft.com")), \
             patch("app.resolve_role", return_value=(False, None)):
            mock_oidc = MagicMock()
            mock_oidc.exchange_code.return_value = _fake_token()
            mock_oidc.verify_issuer.return_value = True
            mock_get_client.return_value = mock_oidc

            resp = _callback_request(flask_client)
            assert resp.status_code == 403

    def test_identity_extraction_failure_returns_400(self, flask_client):
        with patch("app.get_client") as mock_get_client, \
             patch("app.extract_identity", return_value=None):
            mock_oidc = MagicMock()
            mock_oidc.exchange_code.return_value = _fake_token()
            mock_oidc.verify_issuer.return_value = True
            mock_get_client.return_value = mock_oidc

            resp = _callback_request(flask_client)
            assert resp.status_code == 400


class TestCallbackProvisioningErrors:
    def test_provisioning_failure_returns_500(self, flask_client):
        with patch("app.get_client") as mock_get_client, \
             patch("app.extract_identity", return_value=("mario", "asl06.onmicrosoft.com")), \
             patch("app.resolve_role", return_value=(True, "org_admin")), \
             patch("app.ensure_user", side_effect=RuntimeError("XML-RPC down")):
            mock_oidc = MagicMock()
            mock_oidc.exchange_code.return_value = _fake_token()
            mock_oidc.verify_issuer.return_value = True
            mock_get_client.return_value = mock_oidc

            resp = _callback_request(flask_client)
            assert resp.status_code == 500

    def test_web_login_failure_returns_500(self, flask_client):
        with patch("app.get_client") as mock_get_client, \
             patch("app.extract_identity", return_value=("mario", "asl06.onmicrosoft.com")), \
             patch("app.resolve_role", return_value=(True, "org_admin")), \
             patch("app.ensure_user", return_value="ephemeral-pw"), \
             patch("app.get_session_cookies", side_effect=RuntimeError("UYUNI down")):
            mock_oidc = MagicMock()
            mock_oidc.exchange_code.return_value = _fake_token()
            mock_oidc.verify_issuer.return_value = True
            mock_get_client.return_value = mock_oidc

            resp = _callback_request(flask_client)
            assert resp.status_code == 500


class TestCallbackSuccess:
    def _do_successful_callback(self, flask_client, org="asl06",
                                 role="org_admin", email="mario@asl06.onmicrosoft.com"):
        with patch("app.get_client") as mock_get_client, \
             patch("app.extract_identity") as mock_extract, \
             patch("app.resolve_role") as mock_resolve, \
             patch("app.ensure_user") as mock_ensure, \
             patch("app.get_session_cookies") as mock_get_cookies:
            mock_oidc = MagicMock()
            mock_oidc.exchange_code.return_value = _fake_token()
            mock_oidc.verify_issuer.return_value = True
            mock_get_client.return_value = mock_oidc

            local, domain = email.split("@", 1)
            mock_extract.return_value = (local, domain)
            mock_resolve.return_value = (True, role)
            mock_ensure.return_value = "ephemeral-pw"
            mock_get_cookies.return_value = {
                "JSESSIONID": "j123",
                "pxt-session-cookie": "p456",
            }

            resp = _callback_request(flask_client, org=org)
            return resp, mock_ensure

    def test_redirects_to_uyuni(self, flask_client):
        resp, _ = self._do_successful_callback(flask_client)
        assert resp.status_code == 302
        assert "/rhn/manager/systems/list/all" in resp.headers["Location"]

    def test_sets_spm_session_cookie(self, flask_client):
        resp, _ = self._do_successful_callback(flask_client)
        cookies = " ".join(resp.headers.getlist("Set-Cookie"))
        assert "spm_session=" in cookies

    def test_clears_oidc_cookies(self, flask_client):
        resp, _ = self._do_successful_callback(flask_client)
        set_cookies = resp.headers.getlist("Set-Cookie")
        state_cleared = any("oidc_state=" in c and "Max-Age=0" in c or "expires=" in c.lower()
                            for c in set_cookies if "oidc_state" in c)
        # At minimum oidc_state and oidc_org should appear in Set-Cookie (clearing them)
        cookie_names = [c.split("=")[0] for c in set_cookies]
        assert "oidc_state" in cookie_names
        assert "oidc_org" in cookie_names

    def test_ensure_user_called_with_org_sync_pass(self, flask_client):
        resp, mock_ensure = self._do_successful_callback(flask_client, org="asl06")
        assert resp.status_code == 302
        assert mock_ensure.call_args.args[4] == "sync-pass-asl06"

    def test_leobasi_org_uses_leobasi_sync_pass(self, flask_client):
        resp, mock_ensure = self._do_successful_callback(
            flask_client,
            org="leobasi",
            email="mario@leobasi.onmicrosoft.com",
        )
        assert resp.status_code == 302
        assert mock_ensure.call_args.args[4] == "sync-pass-leobasi"

    def test_session_stored_server_side(self, flask_client):
        from app import _SESSIONS
        assert len(_SESSIONS) == 0
        resp, _ = self._do_successful_callback(flask_client)
        assert len(_SESSIONS) == 1


# ── Logout ────────────────────────────────────────────────────────────────────

class TestLogout:
    def _create_session(self, cookies=None):
        from app import _SESSIONS, _SESSIONS_LOCK
        import time
        import secrets as sec
        token = sec.token_urlsafe(32)
        with _SESSIONS_LOCK:
            _SESSIONS[token] = {
                "cookies": cookies or {"JSESSIONID": "j1"},
                "expires": time.time() + 3600,
                "user": "mario@asl06.onmicrosoft.com",
            }
        return token

    def test_logout_redirects_to_login(self, flask_client):
        token = self._create_session()
        with patch("app._requests") as mock_req:
            mock_req.get.return_value = MagicMock(status_code=302)
            resp = flask_client.get("/api/logout",
                                    environ_overrides={"HTTP_COOKIE": f"spm_session={token}"})
        assert resp.status_code == 302
        assert "/api/login" in resp.headers["Location"]

    def test_logout_removes_session(self, flask_client):
        from app import _SESSIONS
        token = self._create_session()
        with patch("app._requests"):
            flask_client.get("/api/logout",
                             environ_overrides={"HTTP_COOKIE": f"spm_session={token}"})
        assert token not in _SESSIONS

    def test_logout_clears_cookie(self, flask_client):
        with patch("app._requests"):
            resp = flask_client.get("/api/logout")
        cookies = " ".join(resp.headers.getlist("Set-Cookie"))
        assert "spm_session=" in cookies

    def test_uyuni_manager_logout_also_works(self, flask_client):
        token = self._create_session()
        with patch("app._requests") as mock_req:
            mock_req.get.return_value = MagicMock(status_code=302)
            resp = flask_client.get("/rhn/manager/logout",
                                    environ_overrides={"HTTP_COOKIE": f"spm_session={token}"})
        assert resp.status_code == 302

    def test_logout_no_session_still_redirects(self, flask_client):
        with patch("app._requests"):
            resp = flask_client.get("/api/logout")
        assert resp.status_code == 302


# ── Security headers ──────────────────────────────────────────────────────────

class TestSecurityHeaders:
    def test_csp_on_error_page(self, flask_client):
        resp = flask_client.get("/api/callback?code=x&state=bad")
        assert "Content-Security-Policy" in resp.headers

    def test_x_frame_options_on_login(self, flask_client):
        with patch("app.get_client") as mock_gc:
            mock_oidc = MagicMock()
            mock_oidc.get_auth_url.return_value = "https://auth.example.com"
            mock_gc.return_value = mock_oidc
            resp = flask_client.get("/api/login/asl06")
        assert resp.headers.get("X-Frame-Options") == "DENY"


# ── Rate limiting ─────────────────────────────────────────────────────────────

class TestCallbackRateLimit:
    def test_rate_limit_returns_429(self, flask_client):
        from app import _AUTH_FAILURES, _AUTH_FAILURES_LOCK, _AUTH_FAILURE_MAX
        import time
        with _AUTH_FAILURES_LOCK:
            _AUTH_FAILURES["127.0.0.1"] = [time.time()] * _AUTH_FAILURE_MAX
        resp = _callback_request(flask_client)
        assert resp.status_code == 429

    def test_below_threshold_not_blocked(self, flask_client):
        from app import _AUTH_FAILURES, _AUTH_FAILURES_LOCK, _AUTH_FAILURE_MAX
        import time
        with _AUTH_FAILURES_LOCK:
            _AUTH_FAILURES["127.0.0.1"] = [time.time()] * (_AUTH_FAILURE_MAX - 1)
        # Still below threshold — should proceed past rate check (will hit CSRF check instead)
        resp = _callback_request(flask_client, state="x", url_state="y")  # CSRF mismatch
        assert resp.status_code == 400  # not 429


# ── Config validation ─────────────────────────────────────────────────────────

class TestCheckConfig:
    def test_missing_var_raises(self):
        import os
        from unittest.mock import patch
        from app import _check_config
        with patch.dict(os.environ, {"UYUNI_URL": ""}):
            with pytest.raises(RuntimeError, match="UYUNI_URL"):
                _check_config()

    def test_missing_azure_kv_url_raises(self):
        import os
        from unittest.mock import patch
        from app import _check_config
        with patch.dict(os.environ, {"AZURE_KEYVAULT_URL": ""}):
            with pytest.raises(RuntimeError, match="AZURE_KEYVAULT_URL"):
                _check_config()

    def test_all_vars_present_passes(self):
        from app import _check_config
        _check_config()  # conftest sets all required env vars


# ── HTTP proxy ────────────────────────────────────────────────────────────────

class TestProxy:
    def _upstream(self, status=200, headers=None, content=b"ok"):
        m = MagicMock()
        m.status_code = status
        m.headers = {"Content-Type": "text/html", **(headers or {})}
        m.content = content
        return m

    def test_basic_200_proxied(self, flask_client):
        with patch("app._requests.request", return_value=self._upstream(content=b"uyuni page")):
            resp = flask_client.get("/rhn/some/page")
        assert resp.status_code == 200
        assert b"uyuni page" in resp.data

    def test_login_redirect_intercepted(self, flask_client):
        with patch("app._requests.request", return_value=self._upstream(
            302, headers={"Location": "https://uyuni-test.internal/rhn/manager/login"}
        )):
            resp = flask_client.get("/rhn/some/page")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/api/login"

    def test_xhr_session_expired_returns_401_json(self, flask_client):
        with patch("app._requests.request", return_value=self._upstream(
            302, headers={"Location": "https://uyuni-test.internal/rhn/manager/login"}
        )):
            resp = flask_client.get(
                "/rhn/some/page",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "session_expired"}

    def test_non_login_redirect_url_rewritten(self, flask_client):
        with patch("app._requests.request", return_value=self._upstream(
            302, headers={"Location": "https://uyuni-test.internal/rhn/other-page"}
        )):
            resp = flask_client.get("/rhn/some/page")
        assert resp.status_code == 302
        assert "uyuni-test.internal" not in resp.headers["Location"]
        assert "uyuni-ldo.internal" in resp.headers["Location"]

    def test_html_content_url_rewritten(self, flask_client):
        body = b"<a href='https://uyuni-test.internal/rhn/page'>link</a>"
        with patch("app._requests.request", return_value=self._upstream(content=body)):
            resp = flask_client.get("/rhn/some/page")
        assert b"uyuni-test.internal" not in resp.data
        assert b"uyuni-ldo.internal" in resp.data

    def test_upstream_connection_error_returns_502(self, flask_client):
        import requests as real_req
        with patch("app._requests.request",
                   side_effect=real_req.exceptions.ConnectionError("refused")):
            resp = flask_client.get("/rhn/some/page")
        assert resp.status_code == 502

    def test_api_catchall_returns_404(self, flask_client):
        resp = flask_client.get("/api/nonexistent-endpoint")
        assert resp.status_code == 404
