# Secrets

**Every command here runs on a trusted operator workstation only.** Never run
this procedure in Pi or GitHub Actions; Pi may run only `make lint` and
`make validate`, and neither Pi nor CI ever holds an age identity.

SOPS with age is the authoritative secrets system for this project. Git stores
only SOPS ciphertext and public age recipients. Every person has an individual
age identity, and no secret is maintained in two places.

Two encrypted files, split by blast radius:

| File | Holds | Consumed by |
| --- | --- | --- |
| `secrets/tooling.sops.env` | Hetzner, Cloudflare, B2, and `TF_VAR_*` credentials | OpenTofu, Packer, operator scripts |
| `ansible/inventory/production/group_vars/all/secrets.sops.yml` | Restic, LLDAP, Pocket ID, Headscale, SMTP secrets | Ansible, on the controller |

OpenTofu receives provider credentials and never application secrets; those are
decrypted by Ansible and rendered root-only on the host. The VPS never receives
an age private key.

1Password is optional and protects exactly one thing: your personal age private
key. Project credentials are never duplicated into 1Password.

## 1. Create your age identity

Every teammate does this once, on their own workstation:

```bash
mkdir -p -m 700 "${XDG_CONFIG_HOME:-$HOME/.config}/sops/age"
age-keygen -o "${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt"
chmod 600 "${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt"
```

Print the public recipient and send **only** that to an existing operator:

```bash
age-keygen -y "${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt"
```

The private key never leaves the workstation, never enters the repository, and
never enters Pi or CI.

## 2. Optional: hold your age key in 1Password

Store the private key in your personal vault, then create `.env.op.local` in the
repository root containing exactly one line:

```
SOPS_AGE_KEY=op://Private/identity-stack-age/private-key
```

`.env.op.local` is already ignored by the `.env.*` rule in `.gitignore`.
`scripts/operator/with-secrets.sh` resolves the reference with `op read` when
`op` is on `PATH`. The key material then transits an environment variable and is
readable through `/proc/<pid>/environ` by your own user; if that is not
acceptable, use the on-disk key from step 1 instead.

## 3. The offline recovery identity

Generate one recovery identity on an offline machine, store the private key
physically (printed, in a safe), and add only its recipient to `.sops.yaml` as
`&recovery_offline`. Without it, simultaneous loss of every teammate's key makes
every secret unrecoverable and forces rotation at every provider.

Never place the recovery private key on a workstation, in 1Password, or in any
backup that this project itself protects.

## 4. Edit secrets

```bash
sops secrets/tooling.sops.env
sops ansible/inventory/production/group_vars/all/secrets.sops.yml
```

Use the variable names in `secrets/tooling.env.example` and
`ansible/inventory/production/group_vars/all/secrets.example.yml`. Both example
files carry names and placeholders only and are never populated.

Verify and commit the ciphertext:

```bash
sops -d secrets/tooling.sops.env >/dev/null
sops -d ansible/inventory/production/group_vars/all/secrets.sops.yml >/dev/null
git add secrets/tooling.sops.env
```

## 5. Run tools with secrets

`scripts/operator/with-secrets.sh` is the single entry point. It resolves your
age identity, injects credentials into the child process only, and writes
nothing to disk. There is no environment file to source and nothing to `unset`
afterwards.

```bash
# OpenTofu and Packer: provider credentials, age identity scrubbed.
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu init -reconfigure -backend-config=backend.hcl
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu plan -var-file=production.tfvars -out=plan.tfplan
./scripts/operator/with-secrets.sh --tooling -- \
  ./scripts/operator/build-gold-image.sh --var-file packer/package-versions.auto.pkrvars.hcl

# Ansible: age identity only, no provider credentials.
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook ansible/playbooks/site.yml

# Both, for the one script that needs to create infrastructure and decrypt.
./scripts/operator/with-secrets.sh --tooling --age -- \
  ./scripts/operator/test-backup-restore.sh --snapshot-id ID --ssh-key NAME --identity-file PATH
```

The identity is resolved in this order, first match wins: an already-exported
`SOPS_AGE_KEY_FILE`; a 1Password reference in `.env.op.local`; then
`${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt`.

Do not enable shell tracing (`set -x`), and confirm variables by name rather
than by printing their values.

## 6. Add a teammate

```bash
# 1. Add their recipient to .sops.yaml under `keys:`, then reference the anchor
#    in every creation rule they should be able to read.
# 2. Re-encrypt each file to the new recipient set.
sops updatekeys ansible/inventory/production/group_vars/all/secrets.sops.yml
sops updatekeys secrets/tooling.sops.env
# 3. Commit .sops.yaml and both re-encrypted files together.
make validate
```

`make validate` fails if `.sops.yaml` names a recipient that a committed file
was not re-encrypted to, so a forgotten `sops updatekeys` cannot ship.

Everyone listed on `secrets/tooling.sops.env` holds full Hetzner, Cloudflare,
and Backblaze authority. Add a recipient there only for someone who is meant to
have it.

## 7. Remove a teammate

```bash
# 1. Remove their recipient and anchor from .sops.yaml.
sops updatekeys ansible/inventory/production/group_vars/all/secrets.sops.yml
sops updatekeys secrets/tooling.sops.env
# 2. Commit.
```

**Then rotate every secret they could read.** Removing a recipient only stops
them decrypting future commits; the ciphertext they could already read remains
in git history permanently. Treat offboarding as a full rotation, not a config
change.

## 8. Rotate a credential

A provider credential:

1. Rotate it with the provider (Hetzner, Cloudflare, Backblaze, Storage Box).
2. `sops secrets/tooling.sops.env` and replace the value.
3. Commit, then confirm with a `tofu plan` that reports no changes.

An application secret:

1. `sops ansible/inventory/production/group_vars/all/secrets.sops.yml`.
2. Commit.
3. Re-run the Ansible deployment and restart the affected services.

The restic repository password is the exception: losing it makes the repository
unreadable, so it is rotated only by creating a new repository. Keep its offline
recovery copy current.

## Failure modes

**`no age identity found`** — you have not completed step 1, or
`SOPS_AGE_KEY_FILE` points at a file that does not exist.

**`Error getting data key: 0 successful groups required, got 0`** — your
recipient is not on that file. Ask an operator to add you and run
`sops updatekeys`.

**`refusing to run inside a container`** — `with-secrets.sh` was invoked in Pi.
Run it from the trusted workstation.

**`missing secrets/tooling.sops.env`** — the file has not been created yet; see
step 4.
