variable "state_passphrase" {
  description = "Passphrase used for client-side encryption of OpenTofu state and plans. Supply only from trusted secret storage."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.state_passphrase) >= 20
    error_message = "The OpenTofu state passphrase must contain at least 20 characters."
  }
}

variable "expected_server_id" {
  description = "Numeric ID of the existing production CPX32; used to reject importing the wrong server."
  type        = number

  validation {
    condition     = var.expected_server_id > 0 && floor(var.expected_server_id) == var.expected_server_id
    error_message = "expected_server_id must be a positive integer."
  }
}

variable "existing_server_name" {
  description = "Desired name of the imported production CPX32."
  type        = string

  validation {
    condition     = length(trimspace(var.existing_server_name)) > 0
    error_message = "existing_server_name must not be empty."
  }
}

variable "server_backups_enabled" {
  description = "Current Hetzner server-backup setting. Match the imported server during adoption; Phase 7 may change it."
  type        = bool
}

variable "expected_primary_ipv4_id" {
  description = "Numeric ID of the existing independent Primary IPv4; used to reject importing the wrong address."
  type        = number

  validation {
    condition     = var.expected_primary_ipv4_id > 0 && floor(var.expected_primary_ipv4_id) == var.expected_primary_ipv4_id
    error_message = "expected_primary_ipv4_id must be a positive integer."
  }
}

variable "existing_primary_ipv4_name" {
  description = "Desired name of the imported production Primary IPv4."
  type        = string

  validation {
    condition     = length(trimspace(var.existing_primary_ipv4_name)) > 0
    error_message = "existing_primary_ipv4_name must not be empty."
  }
}

variable "primary_ipv4_address" {
  description = "Expected address of the imported production Primary IPv4."
  type        = string

  validation {
    condition     = can(cidrnetmask("${var.primary_ipv4_address}/32"))
    error_message = "primary_ipv4_address must be a valid IPv4 address without a CIDR suffix."
  }
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID containing the public identity records."
  type        = string

  validation {
    condition     = length(trimspace(var.cloudflare_zone_id)) > 0
    error_message = "cloudflare_zone_id must not be empty."
  }
}

variable "cloudflare_zone_name" {
  description = "Public DNS zone name, without a trailing dot."
  type        = string

  validation {
    condition     = length(regexall("^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$", var.cloudflare_zone_name)) == 1
    error_message = "cloudflare_zone_name must be a lowercase DNS zone without a trailing dot."
  }
}

variable "pocket_id_hostname" {
  description = "Fully qualified public hostname for Pocket ID."
  type        = string
}

variable "headscale_hostname" {
  description = "Fully qualified public hostname for Headscale."
  type        = string
}

variable "storage_box_name" {
  description = "Name of the protected BX11 storage box."
  type        = string

  validation {
    condition     = length(trimspace(var.storage_box_name)) > 0
    error_message = "storage_box_name must not be empty."
  }
}

variable "storage_box_location" {
  description = "Hetzner storage-box location code selected by the operator."
  type        = string

  validation {
    condition     = contains(["fsn1", "nbg1", "hel1"], var.storage_box_location)
    error_message = "storage_box_location must be a Hetzner location code: fsn1, nbg1, or hel1."
  }
}

variable "storage_box_password" {
  description = "Initial BX11 password. Supply only from trusted secret storage; it is retained only in encrypted state."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.storage_box_password) >= 20
    error_message = "storage_box_password must contain at least 20 characters."
  }
}

variable "project_slug" {
  description = "Non-sensitive label applied to managed Hetzner resources."
  type        = string
  default     = "survivability"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.project_slug))
    error_message = "project_slug must contain lowercase letters, digits, and hyphens only."
  }
}
