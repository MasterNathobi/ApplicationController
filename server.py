"""
ApplicationController MCP Server
Exposes PyAutoGUI + Windows UIAutomation tools to MCP clients.
Coordinates are physical pixels (process is set DPI-aware at startup).
"""
import asyncio
import base64
import ctypes
import io
import json
import re
import subprocess
import pyautogui
from PIL import Image
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# -- Optional: OpenCV ---------------------------------------------------------
try:
	import cv2
	import numpy as np
	_CV2_AVAILABLE = True
except ImportError:
	_CV2_AVAILABLE = False

# -- Optional: pywinauto (UIAutomation) ---------------------------------------
try:
	from pywinauto import Desktop, Application
	_PYWINAUTO_AVAILABLE = True
except ImportError:
	_PYWINAUTO_AVAILABLE = False

# -- Optional: pywin32 (window management) ------------------------------------
try:
	import win32gui
	import win32process
	import win32con
	_WIN32_AVAILABLE = True
except ImportError:
	_WIN32_AVAILABLE = False

# -- DPI awareness: physical pixel coordinates --------------------------------
try:
	ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
except Exception:
	try:
		ctypes.windll.user32.SetProcessDPIAware()
	except Exception:
		pass

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1

_INSTRUCTIONS = """
This server controls the local machine's mouse, keyboard, and screen.
All coordinates are physical pixels (the server is DPI-aware).

## Displaying screenshots
take_screenshot and capture_region return base64 PNG - most clients cannot render it inline.
Always save to disk and report the path:
  import pyautogui; pyautogui.screenshot().save(r"C:\\Users\\<user>\\Desktop\\shot.png")

## Preferred automation workflow
1. list_windows           - find the target window (hwnd, title, pid)
2. focus_window           - bring it to the foreground
3. ua_dump_tree / ua_find - map controls by automationId (no pixel-guessing)
4. ua_invoke / ua_set_value - interact deterministically, works off-screen
5. wait_for_window / wait_for_element / wait_input_idle - wait for state changes
6. Fall back to pixel-based click/type only when UIAutomation controls are unavailable

## type_text
Supports ASCII only. For Unicode/emoji: put text on clipboard, then press_key(['ctrl','v']).

## press_key
List input = hotkey (all keys held simultaneously). String input = single keypress.

## Failsafe
Move the mouse to the top-left corner of the screen to abort automation immediately.
""".strip()

app = Server("application-controller", instructions=_INSTRUCTIONS)


# =============================================================================
# Helpers
# =============================================================================

def _require_pywinauto():
	if not _PYWINAUTO_AVAILABLE:
		raise RuntimeError("pywinauto is not installed. Run: pip install pywinauto")

def _require_win32():
	if not _WIN32_AVAILABLE:
		raise RuntimeError("pywin32 is not installed. Run: pip install pywin32")

def _uia_root(scope_window=None, pid=None):
	desktop = Desktop(backend="uia")
	if pid:
		return desktop.window(process=pid)
	if scope_window:
		return desktop.window(title=scope_window)
	return desktop

def _uia_first(automation_id=None, name=None, control_type=None, scope_window=None, pid=None):
	root = _uia_root(scope_window=scope_window, pid=pid)
	criteria = {}
	if automation_id:
		criteria["auto_id"] = automation_id
	if name:
		criteria["title"] = name
	if control_type:
		criteria["control_type"] = control_type
	if not criteria:
		raise ValueError("Provide at least one of: automation_id, name, control_type")
	return root.child_window(**criteria)

def _uia_find_all(automation_id=None, name=None, control_type=None, scope_window=None, pid=None):
	criteria = {}
	if automation_id:
		criteria["auto_id"] = automation_id
	if name:
		criteria["title"] = name
	if control_type:
		criteria["control_type"] = control_type
	if not criteria:
		raise ValueError("Provide at least one of: automation_id, name, control_type")
	results = []
	if scope_window or pid:
		root = _uia_root(scope_window=scope_window, pid=pid)
		try:
			results = list(root.descendants(**criteria))
		except Exception:
			pass
	else:
		for win in Desktop(backend="uia").windows():
			try:
				results.extend(win.descendants(**criteria))
			except Exception:
				pass
	return results

def _dump_element(elem, max_depth, depth=0):
	try:
		rect = elem.rectangle()
		node = {
			"automationId": elem.automation_id() or "",
			"name": elem.window_text() or "",
			"controlType": elem.friendly_class_name() or "",
			"rect": {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()},
			"isEnabled": elem.is_enabled(),
			"isVisible": elem.is_visible(),
			"isOffscreen": not (elem.is_visible() and rect.width() > 0 and rect.height() > 0),
		}
		try:
			v = elem.get_value()
			if v is not None:
				node["value"] = v
		except Exception:
			pass
		if depth < max_depth:
			children = []
			try:
				for child in elem.children():
					child_node = _dump_element(child, max_depth, depth + 1)
					if child_node:
						children.append(child_node)
			except Exception:
				pass
			node["children"] = children
		return node
	except Exception:
		return None

def _elem_dict(elem):
	try:
		rect = elem.rectangle()
		d = {
			"automationId": elem.automation_id() or "",
			"name": elem.window_text() or "",
			"controlType": elem.friendly_class_name() or "",
			"rect": {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()},
			"isEnabled": elem.is_enabled(),
			"isVisible": elem.is_visible(),
		}
		try:
			v = elem.get_value()
			if v is not None:
				d["value"] = v
		except Exception:
			pass
		return d
	except Exception as e:
		return {"error": str(e)}

def _foreground_info():
	if not _WIN32_AVAILABLE:
		return {}
	try:
		hwnd = win32gui.GetForegroundWindow()
		return {"foreground_window_title": win32gui.GetWindowText(hwnd)}
	except Exception:
		return {}

def _list_windows_impl(pid=None):
	_require_win32()
	result = []
	fg = win32gui.GetForegroundWindow()
	def cb(hwnd, _):
		if not win32gui.IsWindowVisible(hwnd):
			return
		title = win32gui.GetWindowText(hwnd)
		if not title:
			return
		r = win32gui.GetWindowRect(hwnd)
		_, wpid = win32process.GetWindowThreadProcessId(hwnd)
		if pid is None or wpid == pid:
			result.append({
				"hwnd": hwnd,
				"title": title,
				"pid": wpid,
				"rect": {"x": r[0], "y": r[1], "w": r[2] - r[0], "h": r[3] - r[1]},
				"isVisible": True,
				"isModal": False,
				"isForeground": hwnd == fg,
			})
	win32gui.EnumWindows(cb, None)
	return result

def _png_response(img: Image.Image, scale: float = 1.0) -> types.ImageContent:
	if scale != 1.0:
		w = max(1, int(img.width * scale))
		h = max(1, int(img.height * scale))
		img = img.resize((w, h), Image.LANCZOS)
	buf = io.BytesIO()
	img.save(buf, format="PNG")
	encoded = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
	return types.ImageContent(type="image", mimeType="image/png", data=encoded)


# =============================================================================
# Tool definitions
# =============================================================================

@app.list_tools()
async def list_tools() -> list[types.Tool]:
	return [
		# -- Screenshot / screen ----------------------------------------------
		types.Tool(
			name="take_screenshot",
			description="Capture a screenshot of the current screen and return it as a base64-encoded PNG image.",
			inputSchema={
				"type": "object",
				"properties": {
					"scale": {"type": "number", "description": "Resize factor 0-1 to reduce image size (default: 1.0)"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="capture_region",
			description=(
				"Capture a region of the screen as a base64-encoded PNG. "
				"Specify a rectangle (x, y, w, h) or an automation_id to capture that control's bounding box. "
				"Much cheaper than take_screenshot when only one part of the screen is relevant."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer", "description": "Left edge (physical pixels)"},
					"y": {"type": "integer", "description": "Top edge (physical pixels)"},
					"w": {"type": "integer", "description": "Width"},
					"h": {"type": "integer", "description": "Height"},
					"automation_id": {"type": "string", "description": "Capture the bounding box of this control instead of x/y/w/h"},
					"scope_window": {"type": "string", "description": "Window title to scope the automation_id search"},
					"scale": {"type": "number", "description": "Resize factor 0-1 (default: 1.0)"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="get_screen_size",
			description="Return the screen width and height in pixels.",
			inputSchema={"type": "object", "properties": {}, "required": []},
		),
		types.Tool(
			name="get_mouse_position",
			description="Return the current mouse cursor position as (x, y) screen coordinates.",
			inputSchema={"type": "object", "properties": {}, "required": []},
		),
		# -- Mouse ------------------------------------------------------------
		types.Tool(
			name="click",
			description=(
				"Click the mouse. Accepts either screen coordinates (x, y) or an automation_id "
				"(resolved via UIAutomation - more reliable than pixel coordinates on scaled displays). "
				"Requires pywinauto when automation_id is used."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer", "description": "X coordinate (physical pixels)"},
					"y": {"type": "integer", "description": "Y coordinate (physical pixels)"},
					"button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default: left)"},
					"automation_id": {"type": "string", "description": "Click this control by AutomationId instead of coordinates"},
					"scope_window": {"type": "string", "description": "Window title to scope the automation_id search"},
					"return_state": {"type": "boolean", "description": "Also return the foreground window title after the click"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="double_click",
			description="Double-click the mouse at the given screen coordinates.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer", "description": "X coordinate"},
					"y": {"type": "integer", "description": "Y coordinate"},
				},
				"required": ["x", "y"],
			},
		),
		types.Tool(
			name="move_mouse",
			description="Move the mouse cursor to the given screen coordinates without clicking.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer", "description": "X coordinate"},
					"y": {"type": "integer", "description": "Y coordinate"},
				},
				"required": ["x", "y"],
			},
		),
		types.Tool(
			name="drag",
			description="Click and drag from one screen position to another.",
			inputSchema={
				"type": "object",
				"properties": {
					"from_x": {"type": "integer", "description": "Start X coordinate"},
					"from_y": {"type": "integer", "description": "Start Y coordinate"},
					"to_x": {"type": "integer", "description": "End X coordinate"},
					"to_y": {"type": "integer", "description": "End Y coordinate"},
					"duration": {"type": "number", "description": "Duration of drag in seconds (default: 0.5)"},
				},
				"required": ["from_x", "from_y", "to_x", "to_y"],
			},
		),
		types.Tool(
			name="scroll",
			description="Scroll the mouse wheel at the given coordinates.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer", "description": "X coordinate"},
					"y": {"type": "integer", "description": "Y coordinate"},
					"clicks": {"type": "integer", "description": "Scroll clicks. Positive = up, negative = down."},
				},
				"required": ["x", "y", "clicks"],
			},
		),
		# -- Keyboard ---------------------------------------------------------
		types.Tool(
			name="type_text",
			description="Type a string of ASCII text. For Unicode/emoji use the clipboard + press_key(['ctrl','v']).",
			inputSchema={
				"type": "object",
				"properties": {
					"text": {"type": "string", "description": "Text to type (ASCII only)"},
					"interval": {"type": "number", "description": "Seconds between keypresses (default: 0.05)"},
					"return_state": {"type": "boolean", "description": "Also return the foreground window title after typing"},
				},
				"required": ["text"],
			},
		),
		types.Tool(
			name="press_key",
			description=(
				"Press one or more keys. Use a single key name (e.g. 'enter', 'tab', 'escape') "
				"or a list for a hotkey held simultaneously (e.g. ['ctrl', 'c'])."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"keys": {
						"oneOf": [
							{"type": "string", "description": "Single key name"},
							{"type": "array", "items": {"type": "string"}, "description": "Hotkey combination"},
						]
					},
					"presses": {"type": "integer", "description": "Number of times to press (default: 1)"},
					"return_state": {"type": "boolean", "description": "Also return the foreground window title after pressing"},
				},
				"required": ["keys"],
			},
		),
		# -- Apps / timing ----------------------------------------------------
		types.Tool(
			name="launch_app",
			description="Launch an application by its executable path.",
			inputSchema={
				"type": "object",
				"properties": {
					"path": {"type": "string", "description": "Full path to the executable, e.g. C:\\\\Windows\\\\notepad.exe"},
					"args": {"type": "array", "items": {"type": "string"}, "description": "Optional command-line arguments"},
				},
				"required": ["path"],
			},
		),
		types.Tool(
			name="sleep",
			description="Pause execution for a given number of seconds.",
			inputSchema={
				"type": "object",
				"properties": {
					"seconds": {"type": "number", "description": "Seconds to wait (can be fractional, e.g. 0.5)"},
				},
				"required": ["seconds"],
			},
		),
		# -- Image search -----------------------------------------------------
		types.Tool(
			name="find_image_on_screen",
			description="Locate a template image on screen and return its centre coordinates. Requires opencv-python.",
			inputSchema={
				"type": "object",
				"properties": {
					"image_base64": {"type": "string", "description": "Base64-encoded PNG of the image to search for"},
					"confidence": {"type": "number", "description": "Match confidence threshold 0-1 (default: 0.9)"},
				},
				"required": ["image_base64"],
			},
		),
		# -- UIAutomation - Tier 1 --------------------------------------------
		types.Tool(
			name="ua_dump_tree",
			description=(
				"Dump the UIAutomation control tree of a window as JSON. "
				"Each node: automationId, name, controlType, rect, isEnabled, isVisible, isOffscreen, value. "
				"Use this to map a window's controls before interacting - no screenshot needed."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"window_title": {"type": "string", "description": "Exact title of the window to dump"},
					"pid": {"type": "integer", "description": "Process ID (alternative to window_title)"},
					"max_depth": {"type": "integer", "description": "Maximum tree depth (default: 5)"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="ua_find",
			description=(
				"Find UIAutomation controls matching given criteria anywhere on the desktop (or scoped to a window). "
				"Returns list with rect, state, value for each match."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string", "description": "AutomationId to match (maps to x:Name in WPF)"},
					"name": {"type": "string", "description": "Control name/title to match"},
					"control_type": {"type": "string", "description": "Control type, e.g. Button, Edit, ComboBox, CheckBox"},
					"scope_window": {"type": "string", "description": "Limit search to this window title"},
					"pid": {"type": "integer", "description": "Limit search to this process ID"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="ua_invoke",
			description=(
				"Invoke (click/activate) a UIAutomation control by automationId or name. "
				"Uses the UIAutomation Invoke pattern - works even when the control is off-screen. "
				"Returns the foreground window title after invocation."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string", "description": "AutomationId of the control"},
					"name": {"type": "string", "description": "Name/title of the control"},
					"control_type": {"type": "string", "description": "Control type to narrow the search"},
					"scope_window": {"type": "string", "description": "Window title to scope the search"},
					"pid": {"type": "integer", "description": "Process ID to scope the search"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="ua_set_value",
			description=(
				"Set the value of a TextBox, ComboBox, or other ValuePattern control via UIAutomation. "
				"Faster and more reliable than typing character-by-character."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"value": {"type": "string", "description": "Value to set"},
					"automation_id": {"type": "string", "description": "AutomationId of the control"},
					"name": {"type": "string", "description": "Name/title of the control"},
					"scope_window": {"type": "string", "description": "Window title to scope the search"},
					"pid": {"type": "integer", "description": "Process ID to scope the search"},
				},
				"required": ["value"],
			},
		),
		types.Tool(
			name="ua_get_value",
			description="Read the current text/value of a UIAutomation control. Use for assertions and state checks.",
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string", "description": "AutomationId of the control"},
					"name": {"type": "string", "description": "Name/title of the control"},
					"scope_window": {"type": "string", "description": "Window title to scope the search"},
					"pid": {"type": "integer", "description": "Process ID to scope the search"},
				},
				"required": [],
			},
		),
		# -- Eventful waiting - Tier 2 ----------------------------------------
		types.Tool(
			name="wait_for_window",
			description=(
				"Block until a window with the given title appears or disappears. "
				"Replaces sleep + screenshot loops when waiting for dialogs or modals."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"title": {"type": "string", "description": "Window title substring to match (case-insensitive)"},
					"title_regex": {"type": "string", "description": "Regex pattern for the window title"},
					"timeout": {"type": "number", "description": "Maximum seconds to wait (default: 10)"},
					"mode": {"type": "string", "enum": ["appear", "disappear"], "description": "Wait for window to appear or disappear (default: appear)"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="wait_for_element",
			description=(
				"Block until a UIAutomation element reaches the desired state. "
				"Use instead of fixed sleeps when waiting for a control to become visible or enabled."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string", "description": "AutomationId of the element"},
					"name": {"type": "string", "description": "Name/title of the element"},
					"scope_window": {"type": "string", "description": "Window title to scope the search"},
					"state": {"type": "string", "enum": ["visible", "enabled", "exists"], "description": "State to wait for (default: exists)"},
					"timeout": {"type": "number", "description": "Maximum seconds to wait (default: 10)"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="wait_input_idle",
			description=(
				"Wait until the UI thread of a process is idle (finished rendering). "
				"Returns as soon as the app is ready to receive input, or after the timeout."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"pid": {"type": "integer", "description": "Process ID to wait on"},
					"timeout": {"type": "number", "description": "Maximum seconds to wait (default: 10)"},
				},
				"required": ["pid"],
			},
		),
		# -- Cheaper observation - Tier 3 -------------------------------------
		types.Tool(
			name="list_windows",
			description=(
				"List visible top-level windows. Returns hwnd, title, pid, rect, isVisible, isModal, isForeground. "
				"Use to verify a window is open/closed/foreground without taking a screenshot."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"pid": {"type": "integer", "description": "Filter to windows owned by this process ID"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="focus_window",
			description="Bring a window to the foreground and restore it if minimised.",
			inputSchema={
				"type": "object",
				"properties": {
					"hwnd": {"type": "integer", "description": "Window handle from list_windows"},
					"title": {"type": "string", "description": "Window title substring (case-insensitive partial match)"},
				},
				"required": [],
			},
		),
		# -- Quality-of-life - Tier 4 -----------------------------------------
		types.Tool(
			name="auto_dismiss_dialog",
			description=(
				"Find a dialog window by title and click a button to dismiss it in one call. "
				"Use for expected popups such as confirmation dialogs or dev-mode warnings."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"title": {"type": "string", "description": "Dialog window title (partial match, case-insensitive)"},
					"button": {"type": "string", "description": "Button text to click (e.g. 'OK', 'Yes', 'Cancel')"},
				},
				"required": ["title", "button"],
			},
		),
		types.Tool(
			name="batch",
			description=(
				"Execute a sequence of tool calls server-side and return all results in a single round-trip. "
				"Each step is { \"tool\": \"<name>\", \"arguments\": { ... } }. "
				"Steps run sequentially; a failing step records its error and execution continues."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"steps": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {
								"tool": {"type": "string", "description": "Tool name"},
								"arguments": {"type": "object", "description": "Tool arguments"},
							},
							"required": ["tool", "arguments"],
						},
						"description": "Ordered list of tool calls to execute",
					},
				},
				"required": ["steps"],
			},
		),
	]


# =============================================================================
# Tool handlers
# =============================================================================

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent]:

	# -- Screenshot / screen --------------------------------------------------
	if name == "take_screenshot":
		screenshot = pyautogui.screenshot()
		return [_png_response(screenshot, arguments.get("scale", 1.0))]

	elif name == "capture_region":
		scale = arguments.get("scale", 1.0)
		if "automation_id" in arguments:
			_require_pywinauto()
			elem = _uia_first(
				automation_id=arguments["automation_id"],
				scope_window=arguments.get("scope_window"),
			)
			r = elem.rectangle()
			x, y, w, h = r.left, r.top, r.width(), r.height()
		else:
			x = arguments["x"]
			y = arguments["y"]
			w = arguments["w"]
			h = arguments["h"]
		screenshot = pyautogui.screenshot()
		region = screenshot.crop((x, y, x + w, y + h))
		return [_png_response(region, scale)]

	elif name == "get_screen_size":
		w, h = pyautogui.size()
		return [types.TextContent(type="text", text=f"Screen size: {w}x{h} pixels")]

	elif name == "get_mouse_position":
		pos = pyautogui.position()
		return [types.TextContent(type="text", text=f"Mouse position: ({pos.x}, {pos.y})")]

	# -- Mouse ----------------------------------------------------------------
	elif name == "click":
		automation_id = arguments.get("automation_id")
		return_state = arguments.get("return_state", False)
		if automation_id:
			_require_pywinauto()
			elem = _uia_first(automation_id=automation_id, scope_window=arguments.get("scope_window"))
			elem.click_input()
			msg = f"Clicked element automationId='{automation_id}'"
		else:
			x = arguments["x"]
			y = arguments["y"]
			button = arguments.get("button", "left")
			pyautogui.click(x, y, button=button)
			msg = f"Clicked {button} at ({x}, {y})"
		if return_state:
			info = _foreground_info()
			msg += f" | foreground: '{info.get('foreground_window_title', 'unknown')}'"
		return [types.TextContent(type="text", text=msg)]

	elif name == "double_click":
		x = arguments["x"]
		y = arguments["y"]
		pyautogui.doubleClick(x, y)
		return [types.TextContent(type="text", text=f"Double-clicked at ({x}, {y})")]

	elif name == "move_mouse":
		x = arguments["x"]
		y = arguments["y"]
		pyautogui.moveTo(x, y)
		return [types.TextContent(type="text", text=f"Moved mouse to ({x}, {y})")]

	elif name == "drag":
		from_x = arguments["from_x"]
		from_y = arguments["from_y"]
		to_x = arguments["to_x"]
		to_y = arguments["to_y"]
		duration = arguments.get("duration", 0.5)
		pyautogui.moveTo(from_x, from_y)
		pyautogui.dragTo(to_x, to_y, duration=duration)
		return [types.TextContent(type="text", text=f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})")]

	elif name == "scroll":
		x = arguments["x"]
		y = arguments["y"]
		clicks = arguments["clicks"]
		pyautogui.scroll(clicks, x=x, y=y)
		direction = "up" if clicks > 0 else "down"
		return [types.TextContent(type="text", text=f"Scrolled {direction} {abs(clicks)} clicks at ({x}, {y})")]

	# -- Keyboard -------------------------------------------------------------
	elif name == "type_text":
		text = arguments["text"]
		interval = arguments.get("interval", 0.05)
		return_state = arguments.get("return_state", False)
		pyautogui.write(text, interval=interval)
		msg = f"Typed: {text!r}"
		if return_state:
			info = _foreground_info()
			msg += f" | foreground: '{info.get('foreground_window_title', 'unknown')}'"
		return [types.TextContent(type="text", text=msg)]

	elif name == "press_key":
		keys = arguments["keys"]
		presses = arguments.get("presses", 1)
		return_state = arguments.get("return_state", False)
		if isinstance(keys, list):
			pyautogui.hotkey(*keys)
			msg = f"Pressed hotkey: {'+'.join(keys)}"
		else:
			pyautogui.press(keys, presses=presses)
			msg = f"Pressed '{keys}' {presses} time(s)"
		if return_state:
			info = _foreground_info()
			msg += f" | foreground: '{info.get('foreground_window_title', 'unknown')}'"
		return [types.TextContent(type="text", text=msg)]

	# -- Apps / timing --------------------------------------------------------
	elif name == "launch_app":
		path = arguments["path"]
		args = arguments.get("args", [])
		subprocess.Popen([path] + args)
		return [types.TextContent(type="text", text=f"Launched: {path}")]

	elif name == "sleep":
		seconds = arguments["seconds"]
		await asyncio.sleep(seconds)
		return [types.TextContent(type="text", text=f"Slept for {seconds} second(s)")]

	# -- Image search ---------------------------------------------------------
	elif name == "find_image_on_screen":
		if not _CV2_AVAILABLE:
			return [types.TextContent(type="text", text="Error: opencv-python is not installed. Run: pip install opencv-python")]
		confidence = arguments.get("confidence", 0.9)
		image_bytes = base64.standard_b64decode(arguments["image_base64"])
		template_arr = np.frombuffer(image_bytes, dtype=np.uint8)
		template = cv2.imdecode(template_arr, cv2.IMREAD_COLOR)
		if template is None:
			return [types.TextContent(type="text", text="Error: could not decode the provided image")]
		screenshot = pyautogui.screenshot()
		screen = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
		result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
		_, max_val, _, max_loc = cv2.minMaxLoc(result)
		if max_val < confidence:
			return [types.TextContent(type="text", text=f"Image not found on screen (best match confidence: {max_val:.3f})")]
		th, tw = template.shape[:2]
		cx = max_loc[0] + tw // 2
		cy = max_loc[1] + th // 2
		return [types.TextContent(type="text", text=f"Found at centre ({cx}, {cy}) with confidence {max_val:.3f}")]

	# -- UIAutomation - Tier 1 ------------------------------------------------
	elif name == "ua_dump_tree":
		_require_pywinauto()
		window_title = arguments.get("window_title")
		pid = arguments.get("pid")
		if not window_title and not pid:
			return [types.TextContent(type="text", text="Error: provide window_title or pid")]
		max_depth = arguments.get("max_depth", 5)
		root = _uia_root(scope_window=window_title, pid=pid)
		tree = _dump_element(root, max_depth)
		return [types.TextContent(type="text", text=json.dumps(tree, indent=2))]

	elif name == "ua_find":
		_require_pywinauto()
		elements = _uia_find_all(
			automation_id=arguments.get("automation_id"),
			name=arguments.get("name"),
			control_type=arguments.get("control_type"),
			scope_window=arguments.get("scope_window"),
			pid=arguments.get("pid"),
		)
		return [types.TextContent(type="text", text=json.dumps([_elem_dict(e) for e in elements], indent=2))]

	elif name == "ua_invoke":
		_require_pywinauto()
		elem = _uia_first(
			automation_id=arguments.get("automation_id"),
			name=arguments.get("name"),
			control_type=arguments.get("control_type"),
			scope_window=arguments.get("scope_window"),
			pid=arguments.get("pid"),
		)
		try:
			elem.invoke()
		except Exception:
			elem.click_input()
		info = _foreground_info()
		return [types.TextContent(type="text", text=f"Invoked. Foreground: '{info.get('foreground_window_title', 'unknown')}'")]

	elif name == "ua_set_value":
		_require_pywinauto()
		value = arguments["value"]
		elem = _uia_first(
			automation_id=arguments.get("automation_id"),
			name=arguments.get("name"),
			scope_window=arguments.get("scope_window"),
			pid=arguments.get("pid"),
		)
		try:
			elem.set_edit_text(value)
		except Exception:
			try:
				elem.select(value)
			except Exception:
				elem.set_value(value)
		return [types.TextContent(type="text", text=f"Set value to: {value!r}")]

	elif name == "ua_get_value":
		_require_pywinauto()
		elem = _uia_first(
			automation_id=arguments.get("automation_id"),
			name=arguments.get("name"),
			scope_window=arguments.get("scope_window"),
			pid=arguments.get("pid"),
		)
		try:
			value = elem.get_value()
		except Exception:
			value = elem.window_text()
		return [types.TextContent(type="text", text=f"Value: {value!r}")]

	# -- Eventful waiting - Tier 2 --------------------------------------------
	elif name == "wait_for_window":
		_require_win32()
		title = arguments.get("title")
		title_regex = arguments.get("title_regex")
		timeout = arguments.get("timeout", 10)
		mode = arguments.get("mode", "appear")
		loop = asyncio.get_running_loop()
		deadline = loop.time() + timeout

		def matches(t):
			if title and title.lower() in t.lower():
				return True
			if title_regex and re.search(title_regex, t):
				return True
			return False

		while loop.time() < deadline:
			windows = _list_windows_impl()
			matched = [w for w in windows if matches(w["title"])]
			if mode == "appear" and matched:
				return [types.TextContent(type="text", text=f"Window appeared: '{matched[0]['title']}' (hwnd={matched[0]['hwnd']})")]
			elif mode == "disappear" and not matched:
				return [types.TextContent(type="text", text="Window disappeared")]
			await asyncio.sleep(0.2)

		return [types.TextContent(type="text", text=f"Timeout after {timeout}s waiting for window to {mode}")]

	elif name == "wait_for_element":
		_require_pywinauto()
		timeout = arguments.get("timeout", 10)
		state = arguments.get("state", "exists")
		loop = asyncio.get_running_loop()
		deadline = loop.time() + timeout

		while loop.time() < deadline:
			try:
				elem = _uia_first(
					automation_id=arguments.get("automation_id"),
					name=arguments.get("name"),
					scope_window=arguments.get("scope_window"),
				)
				if state == "exists":
					return [types.TextContent(type="text", text="Element exists")]
				elif state == "visible" and elem.is_visible():
					return [types.TextContent(type="text", text="Element is visible")]
				elif state == "enabled" and elem.is_enabled():
					return [types.TextContent(type="text", text="Element is enabled")]
			except Exception:
				pass
			await asyncio.sleep(0.2)

		return [types.TextContent(type="text", text=f"Timeout after {timeout}s waiting for element state '{state}'")]

	elif name == "wait_input_idle":
		pid = arguments["pid"]
		timeout_ms = int(arguments.get("timeout", 10) * 1000)
		PROCESS_QUERY_INFORMATION = 0x0400
		SYNCHRONIZE = 0x00100000
		handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | SYNCHRONIZE, False, pid)
		if not handle:
			return [types.TextContent(type="text", text=f"Could not open process {pid}")]
		try:
			result = await asyncio.get_running_loop().run_in_executor(
				None, lambda: ctypes.windll.user32.WaitForInputIdle(handle, timeout_ms)
			)
			if result == 0:
				return [types.TextContent(type="text", text="Process is idle and ready for input")]
			elif result == 0x102:  # WAIT_TIMEOUT
				return [types.TextContent(type="text", text=f"Timeout after {arguments.get('timeout', 10)}s - process may still be busy")]
			else:
				return [types.TextContent(type="text", text=f"WaitForInputIdle returned {result}")]
		finally:
			ctypes.windll.kernel32.CloseHandle(handle)

	# -- Cheaper observation - Tier 3 -----------------------------------------
	elif name == "list_windows":
		windows = _list_windows_impl(pid=arguments.get("pid"))
		return [types.TextContent(type="text", text=json.dumps(windows, indent=2))]

	elif name == "focus_window":
		_require_win32()
		hwnd = arguments.get("hwnd")
		title = arguments.get("title")
		if not hwnd and title:
			title_lower = title.lower()
			for w in _list_windows_impl():
				if title_lower in w["title"].lower():
					hwnd = w["hwnd"]
					break
		if not hwnd:
			return [types.TextContent(type="text", text="Window not found")]
		win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
		win32gui.SetForegroundWindow(hwnd)
		actual_title = win32gui.GetWindowText(hwnd)
		return [types.TextContent(type="text", text=f"Focused: '{actual_title}' (hwnd={hwnd})")]

	# -- Quality-of-life - Tier 4 ---------------------------------------------
	elif name == "auto_dismiss_dialog":
		_require_pywinauto()
		title = arguments["title"]
		button_text = arguments["button"]
		target = None
		for win in Desktop(backend="uia").windows():
			if title.lower() in win.window_text().lower():
				target = win
				break
		if not target:
			return [types.TextContent(type="text", text=f"Dialog not found: '{title}'")]
		btn = target.child_window(title=button_text, control_type="Button")
		try:
			btn.invoke()
		except Exception:
			btn.click_input()
		return [types.TextContent(type="text", text=f"Dismissed dialog '{title}' via button '{button_text}'")]

	elif name == "batch":
		steps = arguments["steps"]
		results = []
		for i, step in enumerate(steps):
			tool_name = step["tool"]
			tool_args = step.get("arguments", {})
			try:
				step_results = await call_tool(tool_name, tool_args)
				outputs = [r.text if hasattr(r, "text") else "[image]" for r in step_results]
				results.append({"step": i, "tool": tool_name, "success": True, "output": outputs})
			except Exception as e:
				results.append({"step": i, "tool": tool_name, "success": False, "error": str(e)})
		return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

	else:
		return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
	async with stdio_server() as (read_stream, write_stream):
		await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
	asyncio.run(main())
