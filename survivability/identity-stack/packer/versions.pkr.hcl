packer {
  required_version = "= 1.16.0"

  required_plugins {
    hcloud = {
      source  = "github.com/hetznercloud/hcloud"
      version = "= 1.8.0"
    }
  }
}
