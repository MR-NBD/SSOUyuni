# SSOUyuni — Operations Guide

## Service management

### Start / Stop / Restart
```bash
sudo systemctl start ssouuni
sudo systemctl stop ssouuni
sudo systemctl restart ssouuni
sudo systemctl status ssouuni
```

### Health check
```bash
curl -sk https://uyuni-proxy.internal/api/health
# Expected: {"status": "ok"}
```

## Logs

### Live stream
```bash
journalctl -u ssouuni -f
```

### Filter by event type
```bash
# Logins today
journalctl -u ssouuni --since "today" --no-pager -g "Login:"
# Access denied
journalctl -u ssouuni --no-pager -g "Access denied:"
# New account provisioning
journalctl -u ssouuni --no-pager -g "UYUNI user created"
# Boot/startup
journalctl -u ssouuni --no-pager -g "Key Vault"
```

### Log format
```
INFO app: Login: 'user@tenant.onmicrosoft.com' (org=orgA, role=org_admin)
INFO app: Logout: 'user@tenant.onmicrosoft.com'
INFO app: Access denied: 'unknown@external.com' (org=orgA)
INFO uyuni_user_provisioner: UYUNI user created: 'user@tenant.onmicrosoft.com' in org 'orgA'
INFO uyuni_user_provisioner: Role 'org_admin' added to 'user@tenant.onmicrosoft.com'
```

## Log rotation

Add to `/etc/logrotate.d/ssouuni`:
```
/var/log/ssouuni/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
}
```
Note: with systemd journald, logs are managed by journald automatically. No additional logrotate needed unless you redirect logs to files.

## Secret rotation

### Entra client secret (every 24 months)

The App Registration client secret expires. With the shared multi-tenant App Registration, **one rotation covers all organizations**.

1. Create a new secret in Azure Portal → App Registration → Certificates & secrets → New client secret
2. Copy the **Value** (not Secret ID) immediately
3. Update Key Vault:
   ```bash
   az keyvault secret set --vault-name "<kv-name>" --name "entra-client-secret" --value "<new-value>"
   ```
4. Restart the service (secrets are loaded at boot only):
   ```bash
   sudo systemctl restart ssouuni
   sudo systemctl status ssouuni
   ```
5. Verify login works, then delete the old secret from Azure Portal.

**Set a calendar reminder for 23 months after creation.**

### Uyuni sync account password

If you rotate an org's `{org}-account-sync` password in Uyuni:
```bash
az keyvault secret set --vault-name "<kv-name>" --name "uyuni-sync-{org}" --value "<new-password>"
sudo systemctl restart ssouuni
```

## Monitoring

### Health check endpoint
`/api/health` returns `{"status": "ok"}` when the service is running. Use this for uptime monitors (Zabbix, Azure Monitor, etc.).

### Session count (live)
No built-in metrics endpoint. Session count is in-memory only. Check process health via systemctl.

### Prometheus (optional)
No built-in exporter. For black-box monitoring, use a probe on `/api/health`.

## Adding a new organization

See [add-organization.md](add-organization.md).

## Restart after configuration changes

Any change to `.env` or Key Vault secrets requires a service restart:
```bash
sudo systemctl restart ssouuni
journalctl -u ssouuni -n 20
# Look for: "Key Vault: N secrets loaded for M org(s)"
```

## Session behavior after restart

Sessions are stored in-memory only. After a service restart, all active sessions are lost and users must re-authenticate. This is by design. For persistent sessions across restarts, a Redis-backed session store would be needed (not currently implemented).

## Backup and disaster recovery

SSOUyuni has **no persistent state to back up**:
- Secrets: in Azure Key Vault (backed up by Azure)
- Uyuni accounts: in Uyuni database (backed up with Uyuni)
- Sessions: in-memory, ephemeral

For disaster recovery: redeploy via `setup.sh`, restore `.env`, restore `ssl/` certificates. The service is stateless.

## Upgrade

```bash
# On the proxy VM
cd /opt/ssouuni
git pull                           # if deployed via git
# or copy new files manually

# Install new dependencies if requirements.txt changed
venv/bin/pip install -r requirements.txt

sudo systemctl restart ssouuni
sudo systemctl status ssouuni
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Service fails at start: "Missing required env vars" | `.env` incomplete | Check all required vars in `.env` |
| `Key Vault secrets missing or empty` | KV secret missing or Managed Identity not configured | Check FASE 3 of installation guide |
| `ForbiddenByRbac` | Managed Identity lacks Key Vault Secrets User role | Assign role in Azure Portal |
| Login loop (Microsoft → callback → login) | Redirect URI mismatch | App Registration → Authentication — check URI |
| `Need admin approval` at login | Org admin consent not granted | See installation guide FASE 1.3.6 |
| `502 Bad Gateway` | gunicorn not running | `systemctl restart ssouuni` |
| Uyuni UI: CSS/JS broken | `IDENTITY_SYNC_URL` wrong | Fix in `.env`, restart |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Missing or wrong CA cert | Re-do FASE 4.5 of installation |
| Sessions lost | Service was restarted | Expected behavior — users re-authenticate |
