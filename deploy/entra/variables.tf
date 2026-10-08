# SPDX-License-Identifier: AGPL-3.0-only

variable "display_name" {
  type        = string
  default     = "memory-manager"
  description = "Display name of the Entra app registration, as shown in the tenant's app list and the admin-consent prompt."
}

variable "public_url" {
  type        = string
  description = "Canonical public URL of the memory-manager deployment (no trailing slash), e.g. the value the server's own PUBLIC_URL is set to. The module appends /oidc/callback (ADR-0006) to build the web redirect URI."

  validation {
    condition     = can(regex("^https://[^/]+$", var.public_url))
    error_message = "public_url must be an https URL with no path and no trailing slash, e.g. https://memory.example.com."
  }
}

variable "secret_rotation_days" {
  type        = number
  default     = 180
  description = "Validity period of the generated client secret in days. Re-running apply after this changes forces a new secret (azuread_application_password has no update-in-place); roll it over by re-applying before it expires."

  validation {
    condition     = var.secret_rotation_days > 0
    error_message = "secret_rotation_days must be a positive number of days."
  }
}

variable "owners" {
  type        = list(string)
  default     = []
  description = "Object IDs of additional owners (users or service principals) for the app registration and its service principal. The principal running `tofu apply` does not need to be listed here to manage the app, but needs Application.ReadWrite.OwnedBy/.All (see README)."
}
