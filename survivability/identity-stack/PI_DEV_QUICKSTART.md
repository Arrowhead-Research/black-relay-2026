The Pi development environment is running and healthy.

- Access: <http://127.0.0.1:8504>
- Workspace: `/home/robbie/pi-dev-coding/workspaces/hetzner-identity-stack`
- Roadmap: `ARCHITECTURE_ROADMAP.md`
- Agent rules: `AGENTS.md`

Installed tooling includes Pi `0.85.1`, Pi Web `1.202609.0`, Ansible `2.21.3`, SOPS `3.13.3`, age `1.3.2`, Docker CLI, Compose, and the pinned Ansible collections.

Credential isolation was verified:

- No SSH agent or private keys.
- No age private key.
- No Docker socket.
- Only the workspace and Pi state volumes are mounted.
- Pi Web is bound only to `127.0.0.1:8504`.
- Container runs as UID 1000 with capabilities dropped.

Open Pi Web, use `/login` if AI provider authentication is needed, then start with:

```text
Read AGENTS.md and ARCHITECTURE_ROADMAP.md. Summarize the current phase and
propose the smallest next change without accessing production or requesting
credentials.
```

Useful host commands:

```bash
make dev-status
make dev-doctor
make dev-logs
make dev-restart
make dev-down
```

The workspace is initialized as a Git repository, but no commit was created. All linting, Compose validation, HTTP, health, and isolation checks pass.
