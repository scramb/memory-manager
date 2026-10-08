# SPDX-License-Identifier: AGPL-3.0-only

# The three app roles ADR-0006 §3 and auth.tokens.MEMORY_ROLES (src/memory_manager/
# auth/tokens.py) fix by name - a user with none of them assigned is denied by the
# facade. allowed_member_types = ["User"] covers assignment to both users and
# security groups (Microsoft Graph's appRole resource: "assigned to users and
# groups" for ["User"], docs/research/entra-app-registration.md).
locals {
  app_roles = {
    "Memory.User" = {
      display_name = "Memory User"
      description  = "Read and write memory notes in the user's own namespace through the memory-manager MCP server."
    }
    "Memory.Curator" = {
      display_name = "Memory Curator"
      description  = "Memory.User plus write access to shared group/project namespaces (ADR-0008)."
    }
    "Memory.Admin" = {
      display_name = "Memory Admin"
      description  = "Memory.Curator plus the org namespace and the admin area (user/token management)."
    }
  }
}

data "azuread_client_config" "current" {}

data "azuread_application_published_app_ids" "well_known" {}

data "azuread_service_principal" "msgraph" {
  client_id = data.azuread_application_published_app_ids.well_known.result["MicrosoftGraph"]
}

# Lightweight registration resource (not azuread_application): this module only
# ever manages one application, split into single-purpose resources below -
# the provider's own recommended shape for new modules (provider docs,
# azuread_application_registration.html.markdown, "more lightweight
# alternative").
resource "azuread_application_registration" "this" {
  display_name = var.display_name

  sign_in_audience = "AzureADMyOrg"

  # ADR-0006 §4 "Recommended: groupMembershipClaims = ApplicationGroup" - only
  # groups assigned to this app appear in the groups claim, so overage becomes
  # practically irrelevant.
  group_membership_claims = ["ApplicationGroup"]

  # v2 tokens throughout (ADR-0006 §1/§3: oid/tid/roles/groups come from the v2
  # ID token).
  requested_access_token_version = 2
}

resource "azuread_application_owner" "this" {
  for_each = toset(var.owners)

  application_id  = azuread_application_registration.this.id
  owner_object_id = each.value
}

# Web redirect <public_url>/oidc/callback (ADR-0006 §9); CALLBACK_PATH in
# src/memory_manager/auth/login_entra.py.
resource "azuread_application_redirect_uris" "web" {
  application_id = azuread_application_registration.this.id
  type           = "Web"
  redirect_uris  = ["${var.public_url}/oidc/callback"]
}

# Generated once, then kept stable in state across applies - the role_id a
# user/group gets assigned to must never shift under them (provider docs'
# own "Tip" on azuread_application_app_role recommends exactly this pattern).
resource "random_uuid" "role" {
  for_each = local.app_roles
}

resource "azuread_application_app_role" "role" {
  for_each = local.app_roles

  application_id = azuread_application_registration.this.id
  role_id        = random_uuid.role[each.key].result

  allowed_member_types = ["User"]
  display_name         = each.value.display_name
  description          = each.value.description
  value                = each.key
}

# Graph application permissions, exactly User.Read.All + GroupMember.Read.All
# (ADR-0006 addendum 2026-10-08) - resolved by name through the provider's
# own data sources rather than hardcoded GUIDs, so a wrong recalled ID can
# never silently apply (CLAUDE.md "Verify, don't recall"; the two IDs are
# still recorded for reference in docs/research/entra-app-registration.md).
resource "azuread_application_api_access" "msgraph" {
  application_id = azuread_application_registration.this.id
  api_client_id  = data.azuread_application_published_app_ids.well_known.result["MicrosoftGraph"]

  role_ids = [
    data.azuread_service_principal.msgraph.app_role_ids["User.Read.All"],
    data.azuread_service_principal.msgraph.app_role_ids["GroupMember.Read.All"],
  ]
}

resource "azuread_application_password" "this" {
  application_id    = azuread_application_registration.this.id
  display_name      = "memory-manager facade"
  end_date_relative = "${var.secret_rotation_days * 24}h"
}

# "assignment required" (ADR-0006 §3): Entra issues a token for this app only
# to a user/group explicitly assigned to the service principal (on top of the
# per-role assignment above).
resource "azuread_service_principal" "this" {
  client_id                    = azuread_application_registration.this.client_id
  app_role_assignment_required = true
  owners                       = var.owners
}
