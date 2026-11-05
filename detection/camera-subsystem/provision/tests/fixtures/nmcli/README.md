Recorded on a development laptop with nmcli 1.52.1 (read-only commands), 2026-11-05.
Addresses, identifiers and SSIDs are redacted; field names, order and layout are as recorded.
Not recorded on Bookworm: the checklist must repeat these on the target image.

- site_active.txt, site_inactive.txt: `nmcli --terse --mode multiline --fields
  GENERAL.STATE,802-11-wireless.mode,DHCP4 connection show <wifi profile>`, active and inactive.
- state_activated.txt, state_inactive.txt: the same with `--fields GENERAL.STATE`. Inactive is empty.
- scan.txt: `nmcli --terse --fields SSID device wifi list --rescan no ifname wlan0`.
- A profile that does not exist exits 10, with "no such connection profile" on stderr.
