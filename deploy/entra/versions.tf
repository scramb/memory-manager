# SPDX-License-Identifier: AGPL-3.0-only

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    # MPL-2.0, OSS first (CLAUDE.md). ~> 3.0 pins the major version this
    # module's resources (azuread_application_registration and friends,
    # split out of azuread_application in the provider's 3.x line) were
    # verified against - docs/research/entra-app-registration.md.
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    # Only used to generate the three app roles' UUIDs once and keep them
    # stable in state across applies (the provider docs' own recommended
    # pattern for azuread_application_app_role.role_id) - no network calls,
    # no credentials, MPL-2.0.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
