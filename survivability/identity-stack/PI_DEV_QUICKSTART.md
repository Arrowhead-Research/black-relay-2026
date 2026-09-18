The Pi development environment is running and healthy.

- Access: <http://127.0.0.1:8504>
- Workspace: `/home/robbie/pi-dev-coding/workspaces/black-relay-2026`
- Roadmap: `survivability/identity-stack/ARCHITECTURE_ROADMAP.md`
- Agent rules: `survivability/identity-stack/AGENTS.md`

Installed tooling includes Pi `0.85.1`, Pi Web `1.202609.0`, Pi Web Access
`0.28.0`, the image-wide `/skill:grill-me` skill, Ansible `2.21.3`, SOPS
`3.13.3`, age `1.3.2`, Docker CLI, Compose, and the pinned Ansible
collections.

Credential isolation was verified:

- No SSH agent or private keys.
- No age private key.
- No Docker socket.
- Only the workspace and Pi state volumes are mounted.
- Pi Web is bound only to `127.0.0.1:8504`.
- Container runs as UID 1000 with capabilities dropped.

Open Pi Web, use `/login` if AI provider authentication is needed, then start with:

```text
Read survivability/identity-stack/AGENTS.md and
survivability/identity-stack/ARCHITECTURE_ROADMAP.md. Summarize the current
phase and propose the smallest next change without accessing production or
requesting credentials.
```

Useful host commands:

```bash
make dev-status
make dev-doctor
make dev-logs
make dev-restart
make dev-down
```

The Black Relay workspace is a Git repository. All linting, Compose validation,
HTTP, health, and isolation checks pass.
