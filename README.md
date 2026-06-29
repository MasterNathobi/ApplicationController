# ApplicationController

An MCP server that exposes GUI automation tools to any MCP-compatible client (e.g. GitHub Copilot desktop app). It uses [PyAutoGUI](https://pyautogui.readthedocs.io/) to control the mouse, keyboard, and screen of the local machine.

## Tools

| Tool | Description |
|---|---|
| `take_screenshot` | Captures a screenshot and returns it as an image |
| `get_screen_size` | Returns the screen resolution |
| `click` | Left/right/middle click at (x, y) |
| `double_click` | Double-click at (x, y) |
| `move_mouse` | Move cursor without clicking |
| `drag` | Click and drag between two points |
| `scroll` | Scroll up or down at (x, y) |
| `type_text` | Type a string of text |
| `press_key` | Press a key or hotkey combination (e.g. `['ctrl', 'c']`) |
| `launch_app` | Launch an `.exe` by path |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

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
