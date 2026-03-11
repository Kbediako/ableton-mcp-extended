# Changelog

## 1.1.0 - 2026-03-11

Initial public release of `ableton-mcp-extended`.

Added:

- expanded Ableton Remote Script bridge, validated at `162` commands
- generic MCP passthrough tool: `execute_ableton_command`
- bridge discovery tool: `get_supported_commands`
- wider telemetry for song, scenes, tracks, mixer, routing, clip slots, and view state
- broader song and scene control surface
- clip-slot selection and firing support
- JSON-safe bridge responses for richer Live object telemetry
- release documentation and GitHub Actions smoke test

Known limitations:

- direct Arrangement breakpoint CRUD is still not exposed cleanly by Ableton's public API
- some Live properties remain readable but not writable, including:
  - `exclusive_arm`
  - `exclusive_solo`
  - `Song.View.selected_parameter`
