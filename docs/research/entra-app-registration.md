# OpenTofu/`azuread` provider resources and Graph permission IDs for `deploy/entra/`

Retrieved 2026-10-08. Scope: exactly the provider resources, arguments and Microsoft Graph
application-permission identifiers `deploy/entra/` (#256) uses — nothing broader.
`docs/research/enterprise.md` §1 and `docs/research/entra-contract.md` already cover the
Entra/Graph wire contract itself (discovery, token claims, `getMemberGroups`, `users/delta`); this
note only pins down the Terraform-side facts, so the module is built from current documentation,
not recall (CLAUDE.md "Verify, don't recall").

## 1. Provider version

`hashicorp/azuread` latest release at retrieval time: **v3.10.0**
([GitHub releases](https://github.com/hashicorp/terraform-provider-azuread/releases), tag
`v3.10.0`). License MPL-2.0. `deploy/entra/versions.tf` pins `~> 3.0`.

## 2. Resources used, verified against the provider's own docs (`v3.10.0` tag)

Source for every item below: `docs/resources/<name>.md` and `docs/data-sources/<name>.md` in
[`hashicorp/terraform-provider-azuread@v3.10.0`](https://github.com/hashicorp/terraform-provider-azuread/tree/v3.10.0/docs).

- **`azuread_application_registration`** — the "more lightweight alternative" to the older
  monolithic `azuread_application` resource; the provider's own docs recommend it for new
  modules that compose the single-purpose resources below instead of one `azuread_application`
  block. Arguments used: `display_name`, `sign_in_audience = "AzureADMyOrg"`,
  `group_membership_claims = ["ApplicationGroup"]`, `requested_access_token_version = 2`.
- **`azuread_application_redirect_uris`** (`type = "Web"`) — the module's single web redirect,
  `<public_url>/oidc/callback`. Explicitly documented as incompatible with `azuread_application`,
  compatible with `azuread_application_registration`.
- **`azuread_application_app_role`** — one resource per app role; `role_id` must be a UUID. The
  doc's own example and "Tip" recommend a `random_uuid` resource so the ID is generated once and
  then stays fixed in state across applies — the pattern `deploy/entra/main.tf` follows for the
  three `Memory.*` roles. `allowed_member_types = ["User"]` is confirmed by the Graph `appRole`
  resource reference ([`graph/api/resources/approle`](https://learn.microsoft.com/en-us/graph/api/resources/approle)):
  `["User"]` means "assigned to users **and groups**", `["Application"]` means other
  applications — so one role value suffices for both user and group assignment.
- **`azuread_application_api_access`** — the split-out replacement for the `required_resource_access`
  block; takes `api_client_id` plus `role_ids`/`scope_ids`. The doc's own example resolves Graph's
  app ID via `azuread_application_published_app_ids` and a role's ID via
  `azuread_service_principal.<msgraph>.app_role_ids["<PermissionName>"]` — i.e. by name, not a
  literal GUID in the caller's own config. `deploy/entra/main.tf` follows exactly that pattern, so
  the two permission GUIDs in §3 below never have to be typed into the module itself.
- **`azuread_application_password`** — the client secret; `end_date_relative` (e.g. `"4320h"` for
  180 days) sets its validity. No in-place update: a changed rotation period or a re-apply after
  expiry creates a new credential.
- **`azuread_service_principal`** — `client_id` (the application's own `client_id`, not the
  Terraform resource ID), `app_role_assignment_required = true` (ADR-0006 §3), optional `owners`.
- **`azuread_application_owner`** — one resource per extra owner; the lightweight registration
  resource has no inline `owners` argument (unlike `azuread_application`), so owners beyond the
  applying principal need this separate resource.
- **Data sources**: `azuread_client_config` (the authenticated principal's own `tenant_id`, used
  for the `tenant_id` output and the admin-consent URL — no tenant ID is ever hardcoded in the
  module); `azuread_application_published_app_ids` (`result["MicrosoftGraph"]`, Graph's own
  well-known application ID, sourced by the provider itself from
  [`hashicorp/go-azure-sdk`](https://github.com/hashicorp/go-azure-sdk/blob/main/sdk/environments/application_ids.go),
  "no available official indexed source" per the provider's own doc, hence resolved through this
  data source rather than copied by hand); `azuread_service_principal` (looked up by `client_id`,
  exports `app_role_ids`).

## 3. Microsoft Graph application-permission IDs

From the [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
(retrieved 2026-10-08, "Application" column — these are *application* permissions, distinct from
the *delegated* permission of the same name):

| Permission | Application permission ID |
|---|---|
| `User.Read.All` | `df021288-bdef-4463-88db-98f22de89214` |
| `GroupMember.Read.All` | `98830695-27a2-44f7-8c18-0c3ebc9698f6` |

These match ADR-0006's addendum 2026-10-08 choice (`docs/research/entra-contract.md` §5 already
cites the [`directoryobject-getmembergroups`](https://learn.microsoft.com/en-us/graph/api/directoryobject-getmembergroups?view=graph-rest-1.0)
and [`user-delta`](https://learn.microsoft.com/en-us/graph/api/user-delta?view=graph-rest-1.0) pages
for *why* these two and not `User.ReadBasic.All`). `deploy/entra/main.tf` does not use these GUIDs
directly — see §2 above — but they are recorded here so a reviewer can cross-check the module's
`azuread_application_api_access.msgraph.role_ids` output against a known-good value without
re-deriving it from scratch.

## Sources (all retrieved 2026-10-08)

- https://github.com/hashicorp/terraform-provider-azuread/releases
- https://github.com/hashicorp/terraform-provider-azuread/tree/v3.10.0/docs/resources
- https://github.com/hashicorp/terraform-provider-azuread/tree/v3.10.0/docs/data-sources
- https://github.com/hashicorp/go-azure-sdk/blob/main/sdk/environments/application_ids.go
- https://learn.microsoft.com/en-us/graph/api/resources/approle
- https://learn.microsoft.com/en-us/graph/permissions-reference
- https://learn.microsoft.com/en-us/graph/api/directoryobject-getmembergroups?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-delta?view=graph-rest-1.0
