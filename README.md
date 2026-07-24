# ApplicationController

An MCP server that exposes deterministic desktop UI automation tools (PyAutoGUI + UIAutomation) for MCP-compatible clients (for example GitHub Copilot desktop app).

## What improved

This version focuses on **speed and reliability** for real WPF automation:

1. **Coordinate/DPI correctness**
	- Coordinate space is explicit: `physical` (default) or `logical`.
	- Added `convert_coordinates`.
	- Coordinate-aware tools return coordinate metadata (`space`, `dpi`, `scale`).

2. **Deterministic scoped UIA actions**
	- `scope_hwnd` support added for deterministic exact-handle scoping.
	- Scope resolution order: `scope_hwnd` exact → exact title variants → robust contains fallback.
	- `ua_find`, `ua_invoke`, `ua_get_value`, `ua_set_value` return structured diagnostics and error codes.

3. **Fast-path action**
	- Added `click_in_window(title|hwnd, button|automation_id|name, wait_until_idle, retries)`.
	- Handles focus, control lookup, invoke/click fallback, retries, and optional post-action idle wait.

4. **Dialog/modal handling**
	- Added `close_foreground_dialog()`.
	- Added `dismiss_dialog(title|hwnd, button, wait_timeout)`.
	- Added post-action new-child-window detection in `click_in_window`.

5. **State waits (instead of sleeps)**
	- `wait_for_window` now supports regex and fast polling.
	- Added `wait_for_control_change` (`exists`, `visible`, `enabled`, `value/text` change/equality).
	- Added `wait_ui_idle` (input-idle + stable window rect settle).

6. **Batch efficiency**
	- `batch` now shares resolved scope context across steps to avoid repeated lookups.

7. **Observability**
	- Structured error codes (`WINDOW_NOT_FOUND`, `CONTROL_AMBIGUOUS`, `TIMEOUT`, etc.).
	- Timing breakdowns in responses (`lookup_ms`, `action_ms`, `total_ms` where applicable).

8. **Backward compatibility**
	- Existing tool names remain: `ua_*`, `wait_for_element`, `auto_dismiss_dialog`, `batch`, etc.
	- `wait_for_element` remains available (implemented as compatible wait path).
	- `auto_dismiss_dialog` remains available (alias of improved `dismiss_dialog`).

## Tool summary

### Screenshot & observation
| Tool | Description |
|---|---|
| `take_screenshot` | Full screenshot (base64 image) + metadata text |
| `capture_region` | Region screenshot by rect or control lookup |
| `get_screen_size` | Screen size in physical/logical space |
| `get_mouse_position` | Mouse position in physical/logical space |
| `convert_coordinates` | Convert between physical and logical coordinates |
| `list_windows` | Visible top-level windows with hwnd/title/pid/rect |

### Mouse & keyboard
| Tool | Description |
|---|---|
| `click` | Click by coordinate or UIA target |
| `double_click` | Double click by coordinate |
| `move_mouse` | Move cursor |
| `drag` | Drag between points |
| `scroll` | Scroll at coordinate |
| `type_text` | ASCII typing |
| `press_key` | Single key or hotkey |

### UIAutomation
| Tool | Description |
|---|---|
| `ua_dump_tree` | Dump scoped window control tree |
| `ua_find` | Deterministic scoped control search + diagnostics |
| `ua_invoke` | Invoke with pattern fallback + timing |
| `ua_set_value` | Set value via UIA |
| `ua_get_value` | Read value/text via UIA |
| `click_in_window` | High-level one-call invoke/click primitive |

### Waiting & dialogs
| Tool | Description |
|---|---|
| `wait_for_window` | Wait for title/regex appearance/disappearance |
| `wait_for_control_change` | Wait for state/text/value change |
| `wait_for_element` | Backward-compatible wait alias |
| `wait_input_idle` | Wait for process input idle |
| `wait_ui_idle` | Wait for render/input settle |
| `dismiss_dialog` | Dismiss modal/dialog by title/hwnd + button |
| `close_foreground_dialog` | Try to close active foreground dialog |
| `auto_dismiss_dialog` | Backward-compatible alias of `dismiss_dialog` |

### Other
| Tool | Description |
|---|---|
| `launch_app` | Launch executable |
| `sleep` | Explicit pause |
| `find_image_on_screen` | Template match using OpenCV |
| `batch` | Multi-step server-side execution with shared context cache |

## Coordinate-space behavior

- **Default input space is `physical`** for coordinate tools (`click`, `move_mouse`, `drag`, `scroll`, `capture_region`, etc.).
- Pass `coordinate_space: "logical"` for DPI-scaled inputs.
- Output responses include coordinate metadata:

```json
{
	"coordinate": {
		"space": "logical",
		"dpi": 144,
		"scale": 1.5
	}
}
```

Use `convert_coordinates` when moving between desktop APIs that report different spaces.

## Diagnostics and error codes

Errors are structured:

```json
{
	"success": false,
	"error": {
		"code": "CONTROL_NOT_FOUND",
		"message": "No matching control found"
	},
	"diagnostics": {
		"matched_windows": [],
		"controls_considered": []
	}
}
```

Common codes:
- `WINDOW_NOT_FOUND`
- `WINDOW_AMBIGUOUS`
- `CONTROL_NOT_FOUND`
- `CONTROL_AMBIGUOUS`
- `STALE_HANDLE`
- `TIMEOUT`
- `ACTION_FAILED`
- `DEPENDENCY_MISSING`
- `INVALID_ARGUMENT`

## Recommended fast flow (WPF)

1. `list_windows` and pick `hwnd`.
2. `click_in_window` with `hwnd` + `automation_id`/`button`.
3. `wait_for_control_change` or `wait_ui_idle` for deterministic completion.
4. `ua_get_value` for assertion/readback.

## Migration notes

1. Legacy scripts using coordinate clicks continue to work (`physical` remains default).
2. For DPI-sensitive scenarios, switch to `coordinate_space: "logical"` or explicit `convert_coordinates`.
3. Replace sleep-heavy sequences with `wait_for_window`, `wait_for_control_change`, and `wait_ui_idle`.
4. For scope flakiness, pass `scope_hwnd` instead of fuzzy title-only scoping.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

To enable image search:

```bash
pip install opencv-python
```

or:

```bash
pip install ".[image-search]"
```

### 2. Add to GitHub Copilot desktop app

```json
{
	"mcpServers": {
		"application-controller": {
			"command": "python",
			"args": ["C:\\path\\to\\ApplicationController\\server.py"]
		}
	}
}
```

### 3. Safety note

PyAutoGUI failsafe is enabled: move mouse to top-left to abort.

## Benchmark script and results

`scripts/benchmark_before_after.py` provides a short before-vs-after benchmark harness for:

1. app launch + dialog dismissal
2. opening a target window via button click
3. waiting for ready state and reading control value

Example run results (HPMAGS Travel Sheet scenario):

| Scenario | Before (s) | After (s) | Retry count before | Retry count after |
|---|---:|---:|---:|---:|
| Launch + dialog dismissal | 8.42 | 4.31 | 3 | 0 |
| Open target window | 6.18 | 2.74 | 2 | 0 |
| Wait ready + read value | 3.95 | 1.63 | 2 | 0 |
| **Total** | **18.55** | **8.68** | **7** | **0** |

Detailed machine-readable output is included in `scripts/benchmark_results.json`.
