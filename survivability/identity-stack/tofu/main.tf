locals {
  common_labels = {
    environment = "production"
    managed_by  = "opentofu"
    project     = var.project_slug
  }

  public_hostnames = toset([
    var.pocket_id_hostname,
    var.headscale_hostname,
  ])
}

provider "hcloud" {}

provider "cloudflare" {}

resource "hcloud_primary_ip" "production" {
  name          = var.existing_primary_ipv4_name
  type          = "ipv4"
  assignee_id   = tonumber(hcloud_server.production.id)
  assignee_type = "server"
  auto_delete   = false

  delete_protection = true
  labels            = local.common_labels

  lifecycle {
    prevent_destroy = true

    postcondition {
      condition     = self.id == var.expected_primary_ipv4_id
      error_message = "The managed Primary IPv4 ID does not match expected_primary_ipv4_id."
    }

    postcondition {
      condition     = self.ip_address == var.primary_ipv4_address
      error_message = "The managed Primary IPv4 address does not match primary_ipv4_address."
    }

    postcondition {
      condition     = self.assignee_id == tonumber(hcloud_server.production.id)
      error_message = "The Primary IPv4 is not assigned to the expected production server."
    }
  }
}

resource "hcloud_server" "production" {
  name        = var.existing_server_name
  server_type = "cpx32"
  location    = "hel1"

  backups            = var.server_backups_enabled
  delete_protection  = true
  rebuild_protection = true
  labels             = local.common_labels

  # Do not add public_net here. The provider does not reliably import that
  # block and may attempt to delete an already attached protected Primary IP.
  # hcloud_primary_ip.production owns the IPv4 assignment instead.
  lifecycle {
    prevent_destroy = true

    postcondition {
      condition     = tonumber(self.id) == var.expected_server_id
      error_message = "The managed server ID does not match expected_server_id."
    }

    postcondition {
      condition     = self.server_type == "cpx32" && self.location == "hel1"
      error_message = "The production server must remain a Helsinki CPX32."
    }

    postcondition {
      condition = (
        contains(["", "<nil>"], self.ipv6_address) &&
        contains(["", "<nil>"], self.ipv6_network)
      )
      error_message = "The production server must not have public IPv6 enabled."
    }
  }
}

resource "hcloud_firewall" "production" {
  name   = "${var.project_slug}-production"
  labels = local.common_labels

  rule {
    description = "Public SSH break-glass access"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["0.0.0.0/0"]
  }

  rule {
    description = "Public HTTP for ACME and HTTPS redirect"
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0"]
  }

  rule {
    description = "Public HTTPS edge"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0"]
  }

  rule {
    description = "Required IPv4 ICMP"
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0"]
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "hcloud_firewall_attachment" "production" {
  firewall_id = hcloud_firewall.production.id
  server_ids  = [hcloud_server.production.id]

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_dns_record" "public" {
  for_each = local.public_hostnames

  zone_id = var.cloudflare_zone_id
  name    = each.value
  type    = "A"
  content = var.primary_ipv4_address
  ttl     = 300
  proxied = false
  comment = "Managed by OpenTofu for the Survivability identity stack"

  lifecycle {
    precondition {
      condition = (
        each.value != var.cloudflare_zone_name &&
        endswith(each.value, ".${var.cloudflare_zone_name}")
      )
      error_message = "Public service hostnames must be subdomains of cloudflare_zone_name."
    }
  }
}

resource "hcloud_storage_box" "backups" {
  name             = var.storage_box_name
  storage_box_type = "bx11"
  location         = var.storage_box_location
  password         = var.storage_box_password

  delete_protection = true
  labels            = local.common_labels

  lifecycle {
    prevent_destroy = true
  }
}
