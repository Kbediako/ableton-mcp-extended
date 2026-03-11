# AbletonMCP Extended

Extended fork of `ahujasid/ableton-mcp` with a wider Ableton Remote Script bridge, better telemetry, and a generic MCP passthrough so new bridge commands do not require server-side wrapper work first.

## What This Fork Adds

- Expanded Ableton bridge with `162` commands
- Broader song, scene, track, clip-slot, mixer, routing, and device telemetry
- More writable controls for scenes, mixer state, song state, routing, and view navigation
- `get_supported_commands` MCP tool for runtime discovery
- `execute_ableton_command` MCP tool for calling any exposed bridge command directly
- JSON-safe bridge responses for richer Live object telemetry

## Capability Matrix

This is the practical runtime status of the bridge, not just what exists in source.

| Area | Status | Notes |
| --- | --- | --- |
| Song/session telemetry | Validated | `get_song_overview`, `get_song_state`, `get_scenes`, `get_visible_tracks` |
| Track and mixer telemetry | Validated | `get_track_info`, `get_track_mixer`, `get_track_view`, `get_track_sends`, `get_track_routing` |
| Clip-slot telemetry | Validated | `get_clip_slot_info`, `select_clip_slot`, `fire_clip_slot` |
| Scene operations | Validated | create, delete, inspect, select, fire |
| Song setters | Validated | loop, signature, groove, swing, root note, scale name, scale mode |
| Device and parameter control | Validated | device parameter reads/writes and topology inspection |
| Generic passthrough | Validated | `get_supported_commands`, `execute_ableton_command` |
| Session clip automation | Validated | clip-bound automation via bridge commands |
| Arrangement automation recording | Partial | available, but depends on Live transport and record state |
| Arrangement breakpoint editing | Blocked by API | no clean public Live API for direct breakpoint CRUD |
| `Song.View.selected_parameter` writes | Read-only in practice | Live reports no setter |
| `exclusive_arm` / `exclusive_solo` writes | Read-only in practice | readable, setter blocked on tested build |

## Current Limits

- Direct Arrangement breakpoint CRUD is still not exposed cleanly by Ableton's public API
- Some Live properties are readable but not writable, for example:
  - `exclusive_arm`
  - `exclusive_solo`
  - `Song.View.selected_parameter`
- Arrangement automation still needs either:
  - recording parameter moves
  - clip envelopes
  - or a hybrid UI workflow

## Repo Layout

- `AbletonMCP_Remote_Script/__init__.py`
  The Ableton Remote Script that opens a local socket server inside Live.
- `MCP_Server/server.py`
  The MCP server that talks to the Remote Script.

## Installation

### 1. Install the Remote Script

Copy the `AbletonMCP_Remote_Script` folder into one of Ableton's Remote Script locations and rename the folder to `AbletonMCP` if needed.

Likely macOS locations:

- `~/Music/Ableton/User Library/Remote Scripts/`
- `~/Library/Preferences/Ableton/Live 12.x.x/User Remote Scripts/`
- `Ableton Live.app/Contents/App-Resources/MIDI Remote Scripts/`

Recommended approach:

1. Use a user-level Remote Scripts directory first
2. Open Live
3. Go to `Preferences -> Link, Tempo & MIDI`
4. Set `Control Surface` to `AbletonMCP`
5. Set `Input` and `Output` to `None`

The script starts a socket server on `localhost:9877`.

### 2. Run the MCP Server

From GitHub:

```bash
uvx --from git+https://github.com/Kbediako/ableton-mcp-extended.git ableton-mcp-extended
```

Or locally:

```bash
cd /path/to/ableton-mcp-extended
uv run python -m MCP_Server.server
```

### 3. Add It to Your MCP Client

Example config:

```json
{
  "mcpServers": {
    "AbletonMCP": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Kbediako/ableton-mcp-extended.git",
        "ableton-mcp-extended"
      ]
    }
  }
}
```

## Recommended MCP Tools

The server still includes the original convenience tools such as:

- `get_session_info`
- `get_track_info`
- `create_midi_track`
- `create_clip`
- `add_notes_to_clip`
- `set_tempo`
- `fire_clip`
- `stop_clip`
- `start_playback`
- `stop_playback`

The two important additions for this fork are:

### `get_supported_commands`

Returns the runtime bridge version and the full list of currently available bridge commands.

Use this first when you want to see what the loaded Ableton bridge actually exposes.

### `execute_ableton_command`

Generic passthrough for any bridge command.

Example payload:

```json
{
  "command_type": "get_song_overview",
  "params": {}
}
```

Example with params:

```json
{
  "command_type": "set_song_loop",
  "params": {
    "enabled": true,
    "start_time": 0.0,
    "length": 4.0
  }
}
```

## Examples

Get runtime bridge commands:

```json
{
  "command_type": "get_supported_commands",
  "params": {}
}
```

Inspect song state:

```json
{
  "command_type": "get_song_overview",
  "params": {}
}
```

Inspect a track mixer:

```json
{
  "command_type": "get_track_mixer",
  "params": {
    "track_index": 0,
    "track_scope": "track"
  }
}
```

Select a clip slot:

```json
{
  "command_type": "select_clip_slot",
  "params": {
    "track_index": 0,
    "clip_index": 0,
    "track_scope": "track"
  }
}
```

## Development

```bash
uv sync
uv run python -m MCP_Server.server
```

## Release Notes

- Current public release: `1.1.0`
- See [CHANGELOG.md](https://github.com/Kbediako/ableton-mcp-extended/blob/main/CHANGELOG.md) for release history

## Attribution

This is a fork of the original `ableton-mcp` project by Siddharth Ahuja:

- [Original repo](https://github.com/ahujasid/ableton-mcp)

The original project is MIT licensed. This fork keeps that license.
