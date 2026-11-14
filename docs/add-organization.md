# SSOUyuni — Add Organization

This guide covers adding a new organization to an existing SSOUyuni installation.
With the shared multi-tenant App Registration, adding a new org requires fewer steps
than the initial installation: the App Registration and global secrets already exist.

**Prerequisites**:
- Access to Uyuni with Organization Administrator role for the new org
- Access to Key Vault from the proxy VM via Bastion
- The new org's tenant administrator available for the admin consent step

## STEP 1 — Admin consent in the new org's tenant

The new org's tenant administrator must open in a browser (with a **Global Administrator**
or **Application Administrator** account from their tenant):

```
https://login.microsoftonline.com/{tenant-id-nuova-org}/adminconsent?client_id={entra-client-id}&redirect_uri=https://uyuni-proxy.internal/api/callback
```

Replace:
- `{tenant-id-nuova-org}` with the Directory (tenant) ID of the new org's tenant
- `{entra-client-id}` with the value of the KV secret `entra-client-id`

After clicking **Accept**, the browser will show an error on `uyuni-proxy.internal` — this is normal.

> If the tenant requires additional administrative approval (`Need admin approval`),
> the admin must go to **Entra ID → Enterprise applications → uyuni-entra-sync →
> Permissions → Grant admin consent for [tenant]**.

## STEP 2 — Configure Enterprise Application in the new org's tenant

After consent, the Enterprise Application `uyuni-entra-sync` is present in the new org's tenant.

**Where**: `portal.azure.com` (in the new org's tenant) → **Entra ID** → **Enterprise applications** → `uyuni-entra-sync` (the one with client_id `{entra-client-id}`)

### 2.1 Enable "Assignment required"

**Properties** → **Assignment required**: **Yes** → **Save**

> Without this, any user in the tenant can start the OIDC flow.

### 2.2 Assign roles to users

**Users and groups** → **Add user/group**

For each authorized user:
1. Click **Add user/group**
2. Select the user
3. Select the appropriate role (`UyuniAdmin`, `UyuniOperator`, or `UyuniReadOnly`)
4. Click **Assign**

> If the "Add user/group" button is greyed out, the admin does not have sufficient permissions.
> Solution: assign them as **Owner** of the Enterprise Application, or assign them the
> Azure AD `Application Administrator` role in the tenant.

## STEP 3 — Create service account in Uyuni

Create the `{org}-account-sync` account in the correct Uyuni organization.
This account is used by SSOUyuni to create/update individual accounts via XML-RPC.

**Prerequisite**: access to Uyuni with **Uyuni Administrator** or **Organization Administrator**
role for the new org.

**Where**: Uyuni Web UI → **Admin** → **Users** → **Create User**

| Field | Value |
|-------|-------|
| Login | `{org}-account-sync` |
| Password | generate a secure password (≥ 20 characters) |
| Role | **Organization Administrator** |
| Organization | select the new organization |

> **Record the password** — it must be inserted in KV as `uyuni-sync-{org}`.

## STEP 4 — Add the 2 per-org KV secrets

From the proxy VM via Bastion:

```bash
az login --use-device-code
KV="uyuni-identity-kv"
ORG="neworg"   # replace with the org name in lowercase

az keyvault secret set --vault-name $KV --name "entra-${ORG}-tenant-id" --value "<new-org-tenant-id>"
az keyvault secret set --vault-name $KV --name "uyuni-sync-${ORG}"      --value "<account-sync-password>"
```

**Verify**: the 2 secrets must be present and non-empty.

```bash
for SECRET in entra-${ORG}-tenant-id uyuni-sync-${ORG}; do
    VAL=$(az keyvault secret show --vault-name $KV --name "$SECRET" --query value -o tsv 2>/dev/null)
    if [ -n "$VAL" ]; then
        echo "OK $SECRET"
    else
        echo "MISSING $SECRET — missing or empty"
    fi
done
```

## STEP 5 — Update `UYUNI_ORGS` in `.env` and restart

On the proxy VM, edit the `.env` file:

```bash
sudo nano /opt/ssouuni/.env
```

Add the new org name to the `UYUNI_ORGS` variable:

```bash
# Before (example):
UYUNI_ORGS={org1},{org2}

# After:
UYUNI_ORGS={org1},{org2},neworg
```

> The org name in `UYUNI_ORGS` must match **exactly** the prefix used in the
> KV secrets (`uyuni-sync-{org}`, `entra-{org}-tenant-id`). Always lowercase.

```bash
sudo systemctl restart ssouuni
sudo systemctl status ssouuni
```

Status must be `active (running)`. Verify in the log that secrets are loaded:

```bash
journalctl -u ssouuni -n 10
# Expected: Key Vault: N secrets loaded for M org(s)
```

Common errors:

| Message | Cause | Solution |
|---------|-------|----------|
| `Key Vault secrets missing or empty: uyuni-sync-neworg` | Secret missing or name wrong | Check STEP 4 — verify case |
| `Key Vault secrets missing or empty: entra-neworg-tenant-id` | Secret missing | Check STEP 4 |
| Service starts but login fails with "Organization not configured" | Org name in form does not match `UYUNI_ORGS` | The value entered in the selector must match exactly |

## STEP 6 — Verify operation

### 6.1 Health check

```bash
curl -sk https://uyuni-proxy.internal/api/health
# Expected: {"status": "ok"}
```

### 6.2 Full login test

From the client browser: `https://uyuni-proxy.internal/api/login`

1. The new org must appear in the selector
2. Select the new org → Microsoft login with an account from the tenant with an App Role assigned
3. Verify: Uyuni opens in the correct organization
4. Verify: the Uyuni role matches the App Role assigned in Entra

### 6.3 Verify logs

```bash
journalctl -u ssouuni -f
```

On successful login:

```
INFO uyuni_user_provisioner: UYUNI user created: 'user@neworg.onmicrosoft.com' in org 'neworg'
INFO app: Login: 'user@neworg.onmicrosoft.com' (org=neworg, role=org_admin)
```

## Role reference

| Entra App Role | Uyuni role | Notes |
|---------------|-----------|-------|
| `UyuniAdmin` | `org_admin` | Full visibility on all org systems |
| `UyuniOperator` | `system_group_admin` | Sees only groups manually assigned in Uyuni |
| `UyuniReadOnly` | *(no extra role)* | Regular user without explicit subscriptions sees nothing |

> **`system_group_admin` without assignments**: sees zero systems. Assign manually
> in Uyuni: Systems → System Groups → [group] → Admins → add the user.

## Removing an organization

To remove an org from the service (without deleting it from Uyuni):

1. Remove the name from `UYUNI_ORGS` in `.env`
2. `sudo systemctl restart ssouuni`
3. Optional: delete the 2 KV secrets for the org:
   ```bash
   az keyvault secret delete --vault-name $KV --name "entra-${ORG}-tenant-id"
   az keyvault secret delete --vault-name $KV --name "uyuni-sync-${ORG}"
   ```
4. Optional: disable the Enterprise Application in the org's tenant
   (**Entra ID → Enterprise applications → uyuni-entra-sync → Properties → Enabled: No**)

> Individual user accounts and the `{org}-account-sync` account in Uyuni remain intact.
> Users from that org will no longer be able to log in via SSOUyuni.
