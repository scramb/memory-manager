# `deploy/entra/` — Entra app registration for the auth facade

OpenTofu module that creates the Microsoft Entra ID app registration ADR-0006's `LOGIN_MODE=entra`
facade needs: a confidential web app, its three `Memory.*` app roles, the group-claim setting,
the client secret and the Microsoft Graph application permissions the facade's own `oid`/`tid`/
`roles`/`groups` login and deprovisioning worker call for (`docs/adr/0006-enterprise-auth-entra.md`
§3, §4, §6, §9).

**Out of scope here** (see the issue this module closes): applying it against a real tenant is an
operator step, not part of this repository's CI; resource-server mode and SCIM are explicitly not
in v1 (ADR-0006 §8); the Flux example wiring these outputs into a cluster is a separate module.

## What it creates

| Resource | Why |
|---|---|
| `azuread_application_registration` | the app itself: `AzureADMyOrg`, v2 tokens, `group_membership_claims = ["ApplicationGroup"]` (ADR-0006 §4) |
| `azuread_application_redirect_uris` (`Web`) | the single web redirect `<public_url>/oidc/callback` (`CALLBACK_PATH` in `src/memory_manager/auth/login_entra.py`) |
| `random_uuid` + `azuread_application_app_role` (one pair per role) | the three roles `Memory.User`, `Memory.Curator`, `Memory.Admin` (`MEMORY_ROLES` in `src/memory_manager/auth/tokens.py`), assignable to users and groups, with a UUID generated once and then kept stable in Terraform state across applies |
| `azuread_application_api_access` | exactly two Graph **application** permissions: `User.Read.All` (users delta query, ADR-0006 §6) and `GroupMember.Read.All` (`getMemberGroups` on groups overage, ADR-0006 §4) — resolved by name through an `azuread_service_principal` data source's own role-ID map, never a hardcoded GUID |
| `azuread_application_password` | the client secret, valid for `secret_rotation_days` |
| `azuread_service_principal` | `app_role_assignment_required = true` (ADR-0006 §3: a user with no `Memory.*` role assigned never gets a token at all) |
| `azuread_application_owner` (optional, one per entry) | extra owners from the `owners` variable, beyond the principal running `tofu apply` |

Nothing here grants **admin consent** for the two Graph application permissions — Entra always
requires a tenant admin to do that interactively or via `az ad app permission admin-consent`, it
cannot be scripted from an app-only Terraform run (ADR-0006 §9 and Decision: "Admin consent stays
a manual operator step"). Use the `admin_consent_url` output.

## Inputs

| Variable | Default | Meaning |
|---|---|---|
| `display_name` | `"memory-manager"` | shown in the tenant's app list and the consent prompt |
| `public_url` | *(required)* | `https://<host>`, no path, no trailing slash — the server's own `PUBLIC_URL` |
| `secret_rotation_days` | `180` | client secret validity; re-apply before it expires to roll it over (no in-place update, a new secret value is generated) |
| `owners` | `[]` | object IDs of additional owners (users or service principals) for the app registration and its service principal |

## Outputs

| Output | Maps to |
|---|---|
| `client_id` | `ENTRA_CLIENT_ID` |
| `tenant_id` | `ENTRA_TENANT_ID` |
| `client_secret` (sensitive) | `ENTRA_CLIENT_SECRET` — put it straight into the operator's own secret store (`tofu output -raw client_secret`), never into a file committed to Git |
| `admin_consent_url` | open once as a tenant admin to consent to `User.Read.All` + `GroupMember.Read.All` |
| `service_principal_object_id` | used to assign `Memory.User`/`Memory.Curator`/`Memory.Admin` to users or groups (Entra admin center → Enterprise applications → this app → "Users and groups", or an operator's own Graph/PIM automation) |

The facade's other `ENTRA_*` variables (`ENTRA_ALLOWED_TENANTS`, `ENTRA_AUTHORITY`,
`ENTRA_GRAPH_URL`, `ENTRA_GROUPS_TTL_SECONDS`, `ENTRA_ACCESS_TOKEN_MINUTES`, `ENTRA_MAX_SESSION`)
are operator policy choices, not outputs of this module — see
`src/memory_manager/auth/login_entra.py`.

## Running it

The principal executing `tofu apply` (a signed-in user or a service principal with a
client-credentials login) needs, in the target tenant:

- `Application.ReadWrite.OwnedBy` (if it will own the app it creates) or `Application.ReadWrite.All`
- `Application.Read.All` or `Directory.Read.All` (to look up the Microsoft Graph service
  principal's own `app_role_ids` by name)

```sh
tofu -chdir=deploy/entra init
tofu -chdir=deploy/entra plan -var public_url=https://memory.example.com
tofu -chdir=deploy/entra apply -var public_url=https://memory.example.com
```

Then, as a tenant admin, open the `admin_consent_url` output once.

`tofu -chdir=deploy/entra init -backend=false && tofu -chdir=deploy/entra validate` (no
credentials, no state) is what CI runs on every change; it does not apply anything.
