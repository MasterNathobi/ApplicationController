# ApplicationController

An MCP server that exposes GUI automation tools to any MCP-compatible client (e.g. GitHub Copilot desktop app). It uses [PyAutoGUI](https://pyautogui.readthedocs.io/) to control the mouse, keyboard, and screen of the local machine.

## Tools

### Screenshot & observation
| Tool | Description |
|---|---|
| `take_screenshot` | Captures a full screenshot (optional `scale` to reduce size) |
| `capture_region` | Captures a region by coordinates or `automation_id` bounding box |
| `get_screen_size` | Returns the screen resolution |
| `get_mouse_position` | Returns current cursor position |
| `list_windows` | Lists visible top-level windows (hwnd, title, pid, rect, isForeground) |

### Mouse & keyboard
| Tool | Description |
|---|---|
| `click` | Left/right/middle click at (x, y) or by `automation_id` |
| `double_click` | Double-click at (x, y) |
| `move_mouse` | Move cursor without clicking |
| `drag` | Click and drag between two points |
| `scroll` | Scroll up or down at (x, y) |
| `type_text` | Type ASCII text (use clipboard + `press_key` for Unicode) |
| `press_key` | Press a key or hotkey combination (e.g. `['ctrl', 'c']`) |

### UIAutomation (Tier 1 — preferred over pixel clicks)
| Tool | Description |
|---|---|
| `ua_dump_tree` | Dump the full control tree of a window as JSON |
| `ua_find` | Find controls by `automationId`, name, or type |
| `ua_invoke` | Invoke (click) a control by `automationId` — works off-screen |
| `ua_set_value` | Set TextBox/ComboBox value via ValuePattern |
| `ua_get_value` | Read a control's current value |

### Eventful waiting (Tier 2 — replace fixed sleeps)
| Tool | Description |
|---|---|
| `wait_for_window` | Block until a window appears or disappears |
| `wait_for_element` | Block until a control becomes visible/enabled/exists |
| `wait_input_idle` | Block until a process finishes rendering |

### Window management (Tier 3)
| Tool | Description |
|---|---|
| `focus_window` | Bring a window to the foreground / restore if minimised |

### Quality-of-life (Tier 4)
| Tool | Description |
|---|---|
| `launch_app` | Launch an `.exe` by path |
| `sleep` | Pause for N seconds |
| `find_image_on_screen` | Locate a template image on screen (requires `opencv-python`) |
| `auto_dismiss_dialog` | Find a dialog by title and click a button to dismiss it |
| `batch` | Execute multiple tool calls in one round-trip |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

To enable the `find_image_on_screen` tool, also install OpenCV:

```bash
pip install opencv-python
```

Or install everything at once via the package extras:

```bash
pip install ".[image-search]"
```

> **Note:** `pywinauto` and `pywin32` are included in `requirements.txt`. They enable all UIAutomation tools (`ua_*`), window management (`list_windows`, `focus_window`, `wait_for_window`), and `wait_input_idle`. The server starts and the basic mouse/keyboard tools work without them — those tools will return a clear error message if called when the packages are absent.

### 2. Add to GitHub Copilot desktop app

In your Copilot settings, add an MCP server entry pointing to this server:

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

Replace `C:\\path\\to\\ApplicationController` with the actual path where you cloned this repo.

### 3. Safety note

PyAutoGUI's **failsafe** is enabled — move your mouse to the top-left corner of the screen at any time to abort an automation sequence.
