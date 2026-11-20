# Device secrets

Tools for moving a device's secrets (the IQ5 pairing keys and certificates, broker passwords)
between machines without anyone else being able to read them. The bundles are stored as private
packages on GitHub's registry (ghcr.io).

There is no local registry any more. Container images come from ghcr.io too, built by
`.github/workflows/publish-images.yml`; nothing in this folder runs on the central server.

| File | What it is |
|---|---|
| `secrets.sh` | Seals a device's files into an encrypted bundle, and fetches and opens one on the device |
| `publish-bundles.sh` | Pushes sealed bundles to the registry. Used by `.github/workflows/publish-secrets.yml` |
| `tools/Dockerfile` | The small toolbox image (age, oras, mosquitto_passwd) the scripts and the bridge's first start use |

## Why keys don't go in images

An image is meant to be copied: every host that can pull it gets every layer, layers are cached on
disk, and anything in them is there for good. Pairing keys are per device and must be revocable, so
they stay out of images and out of git.

A bundle is a tar of the paths you name (for example `data/pki` and `secrets`), encrypted with
[age](https://age-encryption.org) to the device's own key, and to an admin recovery key if you made
one. Neither private key is on GitHub. The device's private key is made on the device
(`secrets.sh setup`) and never leaves it. Every seal is kept as a dated version as well as `:latest`.

## Seal a device's secrets

On the device, after pairing. This writes a text file; nothing is uploaded yet:

```sh
detection/central-store/secrets.sh seal kit-01 detection/physical-subsystem/deploy data/pki secrets --out kit-01.age
```

Hand it to GitHub as a secret of the `secrets` environment, then run Actions > publish-secrets:

```sh
gh secret set BUNDLE_KIT_01 --env secrets < kit-01.age
```

To seal from another machine, give the device's `age1...` key with `--to` (the device prints it
with `secrets.sh recipient`).

## Fetch them on the device

Once per device, with a GitHub token that has `read:packages`:

```sh
detection/central-store/secrets.sh setup ghcr.io/arrowhead-research/blackrelay-secrets kit-01 - <github-user>
```

Then, whenever the device is set up or rebuilt:

```sh
detection/central-store/secrets.sh fetch detection/physical-subsystem/deploy
```

It replaces exactly the sealed paths and nothing else. File modes are kept; add
`--chown 10001:10000` if the bundle was sealed on another machine. The physical bridge's first
start (`deploy/scripts/bootstrap.sh`) does this fetch by itself.

## Recovery key

`secrets.sh admin-keygen <file>` makes an admin recovery key once. Keep the file offline. Bundles
sealed on a machine that has its public half in `~/.black-relay/admin-recipients` can also be opened
with it, which is how a dead device's pairing moves to a replacement.

## One-time GitHub setup

Create the `secrets` environment (Settings > Environments) with a required reviewer, and after the
first run check that the `blackrelay-secrets/*` packages are private (Organization > Packages).
