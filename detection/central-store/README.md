# Central store: image registry + device secrets

Two stores on the central server, side by side but separate:

| Store | Holds | Who writes | Who reads |
|---|---|---|---|
| **Images** (ports 5000/5001) | Container images: fusion, physical bridge, RF, camera, tools | `publisher` login on 5001 | Each host's own login on 5000 (read-only) |
| **Secrets** (ports 5002/5003) | One encrypted bundle per device: IQ5 pairing keys and certs, broker passwords | `keeper` login on 5003 | Each device's own login on 5002 (read-only) |

Every port is bound only to the address given to `server.sh init` (no VPN or Tailscale needed).
Put the server on the sensor network or a management VLAN and firewall 5000-5003 to the hosts
that need them; the traffic is TLS and every request needs a login.

Both are the stock Docker registry (`registry:3.1.2`, pinned by digest), TLS only, non-root,
read-only filesystem, no capabilities. Each store runs as two containers over one volume: a
read-write one that only the publisher/keeper can log into, and a read-only one (storage mounted
`:ro`, registry in read-only mode) for everyone else. A host's pull login therefore cannot push,
overwrite or delete anything, and the logins for one store don't work on the other.

## Why keys don't go in images

An image is meant to be copied: every host with a pull login gets every layer, layers are cached on
disk, and anything in them is there for good (deleting it in a later layer doesn't remove it).
Pairing keys are per device and must be revocable, so they stay out of images and out of git, as the
physical subsystem already requires.

The secrets store keeps them on the server without trusting the server with them:

* A bundle is a tar of the paths you name (for example `data/pki` and `secrets`), encrypted with
  [age](https://age-encryption.org) to **the device's own key** and to an **admin recovery key**.
  Neither private key is on the server. The plaintext only exists as a pipe (`tar | age`) inside a
  throwaway container on the machine that seals it.
* The device's private key is made on the device (`secrets.sh setup`) and never leaves it.
* The admin recovery key is made once (`secrets.sh admin-keygen`) and kept offline. It lets you
  open any bundle if a device dies and you need its pairing on a replacement.
* Every seal is kept as a dated version (`:20261120T173312Z`) as well as `:latest`.

**Tradeoff vs. a vault (HashiCorp Vault / OpenBao).** A vault would give per-path access policies,
an audit log of every read and short-lived credentials, but it is a stateful service that must be
unsealed after every reboot, and the server could read every secret. Encrypted bundles need nothing
running beyond the registry and a stolen server disk gives up no keys. What you give up: any device
login can download any device's bundle (still ciphertext only it can't open), there is no per-read
audit log beyond the registry's access log, and rotating means re-pairing and sealing again. For a
handful of devices on a private network that is the better trade; if the fleet grows or you need audited
access, the bundles can move into OpenBao without changing the devices' layout.

## Server setup (once)

On the central server (172.22.41.10, Linux, Docker with Compose v2) this is part of the repo-root
project: `./setup-central.sh` (see `DEPLOY.md`) runs `init`, gets the Let's Encrypt certificate,
adds the renewal to cron and starts the stores with everything else from `/srv/sensor-fusion`. To
run the stores alone (a test machine), from this folder:

```sh
./server.sh init 192.168.10.5 central.lan    # the server's LAN address hosts use, plus any other names
docker compose up -d --wait
```

`init` makes a small CA and a TLS certificate for those names, the `publisher` and `keeper` logins
(printed once and kept in `state/credentials/`), and `.env`. Everything goes in `state/` (0700,
gitignored). Already have a certificate from your own CA? Run
`TLS_CERT=central.crt TLS_KEY=central.key ./server.sh init ...` and hosts need no extra CA.

### Publicly trusted certificate (recommended): Let's Encrypt

With a real certificate hosts trust the store out of the box: nothing to copy, nothing to trust,
just `docker login`. Let's Encrypt checks the name through DNS (DNS-01), so the server stays on the
LAN and never has to be reachable from the internet.

1. In Cloudflare, add `A reg.bulexa.com -> 172.22.41.10`, **DNS only** (grey cloud).
2. Create a Cloudflare API token: *My Profile > API Tokens > Create Token > Edit zone DNS*, then
   **add a second permission, Zone > Zone > Read** (without it: "failed to find zone"), and set
   Zone Resources to Include > Specific zone > `bulexa.com`. On the server, save it (`state/` is private):
   `printf '%s' 'TOKEN' > state/cloudflare.token && chmod 600 state/cloudflare.token`
3. Get the certificate (asks once to accept the Let's Encrypt Subscriber Agreement; the email is
   only for expiry notices):
   `./server.sh letsencrypt reg.bulexa.com you@yourmail`
4. Renew automatically: `setup-central.sh` adds this to the user's crontab; by hand, `crontab -e` and add
   `0 4 * * * cd /srv/sensor-fusion/detection/central-store && ./server.sh letsencrypt >> state/renew.log 2>&1`
   (renews when a third of its life is left and restarts the registries and the broker only when it changed).

Hosts then use `reg.bulexa.com:5000` (pull) and `reg.bulexa.com:5001` (push), and devices
run `./secrets.sh setup reg.bulexa.com NAME -` (the `-` means "no extra CA"). If a router on
the way blocks public DNS answers that point at private IPs (DNS rebinding protection), allow
`reg.bulexa.com` there or add it to your local DNS.

Then one login per host:

```sh
./server.sh add-client kit-01 --secrets    # a sensor device: images + its own secrets bundle
./server.sh add-client fusion-01           # a host that only pulls images
./server.sh users                          # who can log in where
./server.sh remove-client kit-01           # revoke
```

Hand each host its `state/credentials/client-NAME.txt` and `state/ca-public.crt`, then delete the
credentials file. Back up the two volumes (`black-relay-central-store_images-data`,
`..._secrets-data`) and `state/`.

## Each host: trust the store and log in

With the Let's Encrypt certificate above, skip to `docker login`. Otherwise:

Linux:

```sh
sudo mkdir -p /etc/docker/certs.d/SERVER:5000
sudo cp ca-public.crt /etc/docker/certs.d/SERVER:5000/ca.crt
docker login SERVER:5000 -u NAME
```

macOS (Docker Desktop or OrbStack): add `ca-public.crt` to the login keychain as trusted
(`security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db ca-public.crt`),
then restart Docker. Not needed if the certificate is already trusted by the host.

## Publishing images (build machine)

The images the compose files pull are built by CI and published to ghcr.io (see the repo README).
The store is for an offline site, or to push a local build:

```sh
docker login SERVER:5001 -u publisher                     # needs the CA trusted, as above, for :5001
./publish-images.sh SERVER [image ...]                    # default list: the simulator images and the store toolbox
./publish-images.sh SERVER --bridge                       # the physical bridge, amd64 + arm64
```

Hosts then set `IMAGE_PREFIX=SERVER:5000/` in the `.env` next to the compose file and run
`docker compose pull && docker compose up -d --wait`. `publish-images.sh` pushes images that exist
locally under their local names, so build them first (`docker compose build`, or pull them from
ghcr.io).

## Sealing and fetching a device's secrets

On the device, once (it needs this folder, e.g. the repo checkout, and Docker):

```sh
./secrets.sh setup SERVER kit-01 ca-public.crt     # asks for kit-01's password; prints its age1... key
```

On the admin's machine, once: `./secrets.sh setup SERVER admin ca-public.crt` (any image login
works for this), then `./secrets.sh admin-keygen /Volumes/OFFLINE/blackrelay-admin.key`.

Sealing is done where the files are, usually the device right after pairing. It asks for the keeper
password (or reads `BR_KEEPER_PASSWORD`) and never stores it:

```sh
cd detection/physical-subsystem/deploy
../../central-store/secrets.sh seal kit-01 . data/pki secrets --to age1...   # first time: the device's key
../../central-store/secrets.sh seal kit-01 . data/pki secrets                 # later: key is remembered
```

To seal from another machine, put the device's `age1...` key in `--to`; to include the admin
recovery key, copy `admin-recipients` into that machine's `~/.black-relay/` first.

On the device, to restore or redeploy:

```sh
cd detection/physical-subsystem/deploy
../../central-store/secrets.sh fetch .            # replaces exactly data/pki and secrets, nothing else
../../central-store/secrets.sh fetch . --chown 10001:10000   # set one owner if it was sealed elsewhere
../../central-store/secrets.sh list               # stored versions
docker compose up -d
```

File modes are kept, and so are numeric owners when sealed on the device itself (the bridge expects
its key 0600 owned by uid 10001).

## Maintenance

* `./server.sh gc images` removes layers no tag points to (stops that store for a few seconds).
  Secrets keep every dated version unless you delete tags.
* Certificates: the store's own certificate lasts 825 days, the CA 10 years. Delete
  `state/tls/server.crt` and re-run `init` to reissue. The CA key (`state/ca/ca.key`) can be moved
  offline once the certificate is issued.
* Lost device: `./server.sh remove-client NAME`, then pair the replacement (which makes new keys)
  and seal for it. To reuse the old pairing, open its bundle with the admin key.

Tested locally: push only via 5001/5003, pulls via 5000/5002 with push and delete refused (405),
logins rejected on the wrong store, real `docker login`/`pull`/`push`, seal → fetch round trip,
no plaintext anywhere in the secrets volume, the admin key opens a bundle and an unrelated key cannot,
revocation, and `gc`.
