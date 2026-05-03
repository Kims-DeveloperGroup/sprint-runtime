# `teams_runtime` Docs

Package-local documentation for the standalone `teams_runtime` module lives here.

## User Guides

- `quickstart.md`
  - first-time setup, workspace init, config, and startup flow
- `configuration_guide.md`
  - explains `discord_agents_config.yaml`, `team_runtime.yaml`, bot IDs, sprint IDs, and actions
- `operations_guide.md`
  - day-to-day commands, request flow, sprint rollover, and troubleshooting

## Maintainer Reference

- `specification.md`
  - product and runtime contract for the standalone module
- `architecture.md`
  - topology, role workflow, communication flow, and component boundaries
- `architecture_policy.md`
  - current module structure, target migration structure, dependency rules, and architecture-preservation policy
- `design.md`
  - major design decisions, defaults, and tradeoffs
- `implementation.md`
  - current code layout, runtime behavior, storage layout, and migration status

## Source Of Truth

- Package code: `teams_runtime/`
- Canonical shared contracts: `teams_runtime/shared/models.py`
- Package-local tests: `teams_runtime/tests/`
- Test execution tool: Python standard-library `unittest`
- Workspace template scaffold: `teams_runtime/core/template.py`
- Python dependencies: `teams_runtime/requirements.txt`

## Notes

- `teams_runtime` is intentionally self-contained and does not depend on repo-local `libs.*` modules.
- The default generated workspace path is `./teams_generated` unless the current directory is already a workspace root.
- Role relay transport defaults to `internal`; use `--relay-transport discord` when debugging relay envelopes on Discord.
- The top-level `teams/` folder in this repository is not a runtime dependency of `teams_runtime`.
- These docs stay package-local and are not copied into generated workspaces.
- `architecture.md` and `implementation.md` describe the current implementation; `architecture_policy.md` also tracks the target refactor structure and the rules for getting there.
