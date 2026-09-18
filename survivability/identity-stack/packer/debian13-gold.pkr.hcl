variable "hcloud_token" {
  type        = string
  default     = env("HCLOUD_TOKEN")
  sensitive   = true
  description = "Hetzner Cloud token. Operator-only; never set in Pi or CI."
}

variable "base_image" {
  type        = string
  default     = "debian-13"
  description = "Hetzner base image for the generic gold image."
}

variable "location" {
  type        = string
  default     = "hel1"
  description = "Build and validation location."
}

variable "server_type" {
  type        = string
  default     = "cx23"
  description = "Disposable build server type selected by the operator."
}

variable "snapshot_prefix" {
  type        = string
  default     = "survivability-debian13-gold"
  description = "Prefix for promoted snapshot names."
}

variable "snapshot_epoch" {
  type        = string
  default     = "manual"
  description = "Operator-supplied build identifier, usually UTC YYYYMMDDhhmm."
}

variable "build_owner" {
  type        = string
  description = "Non-secret operator label for the disposable builder."
}

variable "build_expires_at" {
  type        = string
  description = "UTC expiry label for the disposable builder."
}

variable "docker_package_version" {
  type        = string
  description = "Exact docker-ce package version, for example 5:29.0.4-1~debian.13~trixie."
}

variable "docker_cli_package_version" {
  type        = string
  description = "Exact docker-ce-cli package version matching docker_package_version."
}

variable "containerd_package_version" {
  type        = string
  description = "Exact containerd.io package version."
}

variable "docker_buildx_package_version" {
  type        = string
  description = "Exact docker-buildx-plugin package version."
}

variable "docker_compose_package_version" {
  type        = string
  description = "Exact docker-compose-plugin package version."
}

locals {
  snapshot_name = "${var.snapshot_prefix}-${var.snapshot_epoch}"
}

source "hcloud" "debian13_gold" {
  token        = var.hcloud_token
  image        = var.base_image
  location     = var.location
  server_type  = var.server_type
  ssh_username = "root"

  server_labels = {
    "expires-at" = var.build_expires_at
    managed_by   = "packer"
    owner        = var.build_owner
    project      = "survivability"
    purpose      = "gold-image-builder"
  }

  snapshot_name = local.snapshot_name
  snapshot_labels = {
    environment = "gold-image"
    managed_by  = "packer"
    os          = "debian-13"
    project     = "survivability"
    role        = "identity-stack-base"
    status      = "candidate"
  }
}

build {
  name = "debian13-gold"
  sources = [
    "source.hcloud.debian13_gold",
  ]

  provisioner "shell" {
    scripts = [
      "packer/scripts/00-wait-cloud-init.sh",
      "packer/scripts/10-base-packages.sh",
    ]
  }

  provisioner "shell" {
    environment_vars = [
      "DOCKER_PACKAGE_VERSION=${var.docker_package_version}",
      "DOCKER_CLI_PACKAGE_VERSION=${var.docker_cli_package_version}",
      "CONTAINERD_PACKAGE_VERSION=${var.containerd_package_version}",
      "DOCKER_BUILDX_PACKAGE_VERSION=${var.docker_buildx_package_version}",
      "DOCKER_COMPOSE_PACKAGE_VERSION=${var.docker_compose_package_version}",
    ]
    script = "packer/scripts/20-docker.sh"
  }

  provisioner "shell" {
    scripts = [
      "packer/scripts/40-hardening.sh",
      "packer/scripts/90-cleanup.sh",
    ]
  }

  post-processor "manifest" {
    output     = "packer-output/manifest.json"
    strip_path = true
  }
}
