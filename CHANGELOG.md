# Changelog

All notable changes to SSOUyuni are documented here.

## [1.0.0] — 2026-11-14

### Initial release

- Multi-tenant OIDC proxy for Uyuni via Microsoft Entra ID (Azure AD)
- Auto-provisioning of individual Uyuni accounts via XML-RPC
- Server-side session store (browser never sees Uyuni cookies)
- HTTP and WebSocket transparent proxy to Uyuni
- Azure Key Vault integration for secret management (Managed Identity)
- Per-IP auth failure rate limiting (in-memory, no external dependency)
- Optional audit log on Azure Table Storage
- systemd service with hardening (NoNewPrivileges, ProtectSystem, PrivateTmp)
- nginx TLS frontend with rate limiting on login endpoint
- Role mapping: UyuniAdmin → org_admin, UyuniOperator → system_group_admin, UyuniReadOnly → read-only
- 124 unit tests
