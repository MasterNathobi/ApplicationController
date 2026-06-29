# ApplicationController MCP Server — Agent Instructions

This MCP server exposes PyAutoGUI tools that control the local machine's mouse, keyboard, and screen.

## Displaying screenshots

The `take_screenshot` tool returns a base64-encoded PNG. **Do not attempt to render this inline** — most clients will not display it. Instead, always save the image to a file and confirm the path to the user:

```python
import pyautogui
screenshot = pyautogui.screenshot()
screenshot.save(r"C:\Users\<username>\Desktop\screenshot.png")
```

Or use the `take_screenshot` tool and then immediately write the result to disk via a powershell/python command so the user can open it.

## General usage tips

- Always call `get_screen_size` or `take_screenshot` first to understand the current screen state before clicking or typing.
- When automating multi-step sequences, use `sleep` (0.3–1s) between actions to let the UI settle.
- If you need to locate a UI element, use `take_screenshot` + `find_image_on_screen` with a cropped template of that element.
- `type_text` only supports ASCII characters. For Unicode/emoji, use the clipboard instead: write the text to clipboard and press `['ctrl', 'v']`.
- `press_key` with a list triggers a hotkey (all keys held simultaneously). Use a string for a single keypress.
- PyAutoGUI failsafe is enabled — moving the mouse to the top-left corner aborts automation immediately.
