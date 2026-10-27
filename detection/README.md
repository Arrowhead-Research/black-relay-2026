# Detection Team

This repository contains the software, hardware, and documentation for the detection system. Each subsystem is maintained in its own directory, with its setup and operating instructions documented alongside it.

## Repository Structure

```text
detection/
├── README.md
├── camera-subsystem/
├── docs/
│   └── BOM.md
├── physical-subsystem/
├── rf-subsystem/
├── fusion-service/
└── camera-relay-server/
```

## Team and Ownership

| Team member | Role | Primary ownership |
|---|---|---|
| Jon Dencker | Team Leader | Team coordination and cross-subsystem decisions |
| Jayden Bulexa | Physical Subsystem Lead | `physical-subsystem` |
| Dmitri Warner | Camera Subsystem Lead | `camera-subsystem` |
| Bryan Jones | RF Subsystem Lead | `rf-subsystem` |
| George Sithole | Fusion Service Co-Lead | `fusion-service` |
| Chris Dew | Fusion Service Co-Lead | `fusion-service` |

Subsystem leads are the first point of contact for changes, documentation, and reviews affecting their subsystem. Changes that affect multiple subsystems should involve the relevant leads.

The owner for `camera-relay-server` is **to be assigned**. Until then, coordinate changes with the Camera Subsystem Lead and any other affected subsystem leads.

## CI Pipeline

The repository will have general-purpose CI jobs for common checks and builds. The team can add and maintain subsystem-specific CI jobs in this repository as our needs become clearer.

CI should provide useful feedback without making routine contributions unnecessarily difficult. Required checks for merging should be documented and should reflect the current needs of the project.

## Git Flow

`detection/develop` is the team's main integration branch.

- Make small, targeted changes and commits.
- Do not push commits directly to `detection/develop`.
- Create a branch for your work and submit a pull request targeting `detection/develop`.
- Keep pull requests focused and explain what changed, why, and how it was verified.

## Commit Conventions

We will not enforce a commit format technically, but commits should be logical, focused, and easy to understand. Use a short type prefix when it helps communicate intent:

| Type | Use |
|---|---|
| `feat` | Add or change a capability. |
| `fix` | Correct a bug or unintended behavior. |
| `docs` | Add or update documentation. |
| `refactor` | Restructure code without intentionally changing behavior. |
| `test` | Add or update tests. |
| `chore` | Make maintenance changes that do not fit the other categories. |

Examples:

```text
feat: add camera health event
fix: reject malformed relay messages
docs: clarify RF subsystem startup
refactor: isolate event parsing
test: cover fusion event ordering
chore: update development dependencies
```

These are guidelines, not merge requirements. Prefer commits that each represent one coherent step.

## Subsystem Documentation

Every subsystem directory must have a README that lets someone unfamiliar with the project understand, assemble, start, and verify it. Write the instructions as though the system has arrived in separate pieces and the reader has never seen it before.

Each subsystem README should include:

1. **Purpose and scope:** Quick sentence or two about what the subsystem does, the capability it provides, and how it integrates with the overall system.
2. **File tree:** The important directories and files, with a brief explanation of their roles.
3. **Physical assembly:** Required parts, connections, ports, cables, power requirements, and assembly order. Include diagrams or photos when useful. If there is no physical assembly, say so and document the relevant deployment or wiring setup.
4. **Configuration:** Required settings, environment variables, and sample configuration. Never include secrets or credentials.
5. **Startup instructions:** Prerequisites and exact steps to build, configure, and run the subsystem.
6. **Testing and verification:** How to run relevant checks and confirm the subsystem is working.
7. **Troubleshooting:** Common failures, symptoms, and recovery steps.
8. **Interfaces and dependencies:** Events, APIs, hardware interfaces, and other subsystems it relies on or serves.

Update the relevant subsystem README whenever setup, assembly, configuration, or operation changes.

## Testing

At this phase, testing should not slow down delivery with broad or exhaustive test requirements. Testing will focus on:

- **Business rules**, especially event definitions and expected event behavior.
- **Contracts**, including APIs, message formats, and interfaces between subsystems.

Add tests where they provide meaningful confidence in these rules and contracts. Document manual verification steps when automated tests are not practical. We can expand test coverage as the system and its risks mature.

## Pull Request Expectations

Pull requests should:

- Stay focused on a small, reviewable change.
- Explain the purpose and expected behavior.
- Identify affected subsystems and interfaces.
- State the checks or manual verification performed.
- Call out changes to event definitions, APIs, configuration, or assembly instructions.
- Update the relevant README when setup or operation changes.
- Request review from the relevant subsystem lead or leads.