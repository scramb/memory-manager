# SPDX-License-Identifier: AGPL-3.0-only

output "client_id" {
  value       = azuread_application_registration.this.client_id
  description = "ENTRA_CLIENT_ID."
}

output "tenant_id" {
  value       = data.azuread_client_config.current.tenant_id
  description = "ENTRA_TENANT_ID - the tenant the provider is authenticated against."
}

output "client_secret" {
  value       = azuread_application_password.this.value
  description = "ENTRA_CLIENT_SECRET. Sensitive - read it once (tofu output -raw client_secret) into the operator's own secret store, never into a file committed to Git."
  sensitive   = true
}

output "admin_consent_url" {
  value       = "https://login.microsoftonline.com/${data.azuread_client_config.current.tenant_id}/adminconsent?client_id=${azuread_application_registration.this.client_id}"
  description = "Manual operator step (ADR-0006 §9): a tenant admin must open this URL once to grant the Microsoft Graph application permissions (User.Read.All, GroupMember.Read.All) tenant-wide consent."
}

output "service_principal_object_id" {
  value       = azuread_service_principal.this.object_id
  description = "Object ID of the service principal, needed to assign the Memory.User/Curator/Admin app roles to users or groups (Entra admin center -> Enterprise applications -> this app -> Users and groups, or an operator's own Graph/PIM automation)."
}
