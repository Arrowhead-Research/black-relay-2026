terraform {
  required_version = "= 1.12.6"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.24.0"
    }

    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "= 1.68.0"
    }
  }
}
