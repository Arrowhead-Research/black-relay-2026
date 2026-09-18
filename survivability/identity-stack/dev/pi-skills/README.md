# Pi image skills

`grill-me` and its `grilling` dependency are vendored from
https://github.com/mattpocock/skills at commit
`3cca18b368ae95cdbdebbff572ccafa662551015` (MIT; licenses included).
Upstream paths: `skills/productivity/{grill-me,grilling}/SKILL.md`.
Source article: https://www.aihero.dev/skills-grill-me

Local adaptations:

- Replace the unavailable Skill tool with Pi's relative-file read workflow.
- Allow local read-only research without requiring sub-agent support, and retain
  the project's operator-only production audit and credential boundaries.

The container installs these as image-wide skills under `~/.agents/skills`.
Start a fresh Pi session, then use `/skill:grill-me` followed by an idea or plan.
