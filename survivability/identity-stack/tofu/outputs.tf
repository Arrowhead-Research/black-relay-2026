output "production_server_id" {
  description = "Verified ID of the adopted production CPX32."
  value       = hcloud_server.production.id
}

output "production_primary_ipv4_id" {
  description = "Verified ID of the protected independent Primary IPv4."
  value       = hcloud_primary_ip.production.id
}

output "production_primary_ipv4_address" {
  description = "Address of the protected independent Primary IPv4."
  value       = hcloud_primary_ip.production.ip_address
}

output "production_firewall_id" {
  description = "ID of the production Hetzner firewall."
  value       = hcloud_firewall.production.id
}

output "storage_box_id" {
  description = "ID of the protected BX11 storage box."
  value       = hcloud_storage_box.backups.id
}

output "storage_box_server" {
  description = "Public endpoint of the protected BX11 storage box."
  value       = hcloud_storage_box.backups.server
}
