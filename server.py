"""
ApplicationController MCP Server
Exposes PyAutoGUI + Windows UIAutomation tools to MCP clients.
"""
import asyncio
import base64
import ctypes
import io
import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import pyautogui
from PIL import Image
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

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
	import win32con
	import win32gui
	import win32process
	_WIN32_AVAILABLE = True
except ImportError:
	_WIN32_AVAILABLE = False


class ErrorCode:
	WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
	WINDOW_AMBIGUOUS = "WINDOW_AMBIGUOUS"
	CONTROL_NOT_FOUND = "CONTROL_NOT_FOUND"
	CONTROL_AMBIGUOUS = "CONTROL_AMBIGUOUS"
	INVALID_ARGUMENT = "INVALID_ARGUMENT"
	TIMEOUT = "TIMEOUT"
	STALE_HANDLE = "STALE_HANDLE"
	DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
	ACTION_FAILED = "ACTION_FAILED"


# -- DPI awareness: physical pixel coordinates --------------------------------
try:
	ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
except Exception:
	try:
		ctypes.windll.user32.SetProcessDPIAware()
	except Exception:
		pass

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.04

_INSTRUCTIONS = """
This server controls the local machine's mouse, keyboard, and screen.

Coordinate spaces:
- physical: device pixels (default for click/move/drag/screenshot).
- logical: 96-DPI logical units (DIP-like). Use convert_coordinates to convert.
Each coordinate-aware response returns coordinate metadata.

Preferred automation workflow:
1. list_windows
2. focus_window
3. ua_dump_tree / ua_find
4. click_in_window or ua_invoke / ua_set_value
5. wait_for_window / wait_for_control_change / wait_input_idle

PyAutoGUI failsafe is enabled: move mouse to top-left to abort.
""".strip()

app = Server("application-controller", instructions=_INSTRUCTIONS)


@dataclass
class ScopeResolution:
	success: bool
	window: Any | None
	error_code: str | None
	diagnostics: dict[str, Any]


# =============================================================================
# Helpers
# =============================================================================

def _now() -> float:
	return time.perf_counter()


def _ms(start: float) -> int:
	return int((time.perf_counter() - start) * 1000)


def _json_text(payload: Any) -> list[types.TextContent]:
	return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


def _err(code: str, message: str, diagnostics: dict[str, Any] | None = None, timings: dict[str, int] | None = None):
	resp = {
		"success": False,
		"error": {"code": code, "message": message},
	}
	if diagnostics:
		resp["diagnostics"] = diagnostics
	if timings:
		resp["timings_ms"] = timings
	return _json_text(resp)


def _require_pywinauto():
	if not _PYWINAUTO_AVAILABLE:
		raise RuntimeError("pywinauto is not installed. Run: pip install pywinauto")


def _require_win32():
	if not _WIN32_AVAILABLE:
		raise RuntimeError("pywin32 is not installed. Run: pip install pywin32")


def _coord_metadata(space: str = "physical", hwnd: int | None = None) -> dict[str, Any]:
	meta: dict[str, Any] = {"space": space}
	meta["dpi"] = _get_dpi(hwnd)
	meta["scale"] = meta["dpi"] / 96.0
	return meta


def _get_dpi(hwnd: int | None = None) -> int:
	try:
		if hwnd:
			return int(ctypes.windll.user32.GetDpiForWindow(hwnd))
	except Exception:
		pass
	try:
		return int(ctypes.windll.user32.GetDpiForSystem())
	except Exception:
		return 96


def _logical_to_physical(x: float, y: float, hwnd: int | None = None) -> tuple[int, int]:
	scale = _get_dpi(hwnd) / 96.0
	return int(round(x * scale)), int(round(y * scale))


def _physical_to_logical(x: float, y: float, hwnd: int | None = None) -> tuple[int, int]:
	scale = _get_dpi(hwnd) / 96.0
	return int(round(x / scale)), int(round(y / scale))


def _to_physical_point(x: float, y: float, coordinate_space: str, hwnd: int | None = None) -> tuple[int, int]:
	if coordinate_space == "physical":
		return int(x), int(y)
	if coordinate_space == "logical":
		return _logical_to_physical(x, y, hwnd)
	raise ValueError("coordinate_space must be 'physical' or 'logical'")


def _rect_to_space(rect: dict[str, int], coordinate_space: str, hwnd: int | None = None) -> dict[str, int]:
	if coordinate_space == "physical":
		return rect
	if coordinate_space == "logical":
		x, y = _physical_to_logical(rect["x"], rect["y"], hwnd)
		w, h = _physical_to_logical(rect["w"], rect["h"], hwnd)
		return {"x": x, "y": y, "w": w, "h": h}
	return rect


def _foreground_info():
	if not _WIN32_AVAILABLE:
		return {}
	try:
		hwnd = win32gui.GetForegroundWindow()
		return {"foreground_window_title": win32gui.GetWindowText(hwnd), "foreground_hwnd": hwnd}
	except Exception:
		return {}


def _list_windows_impl(pid: int | None = None):
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
				"isForeground": hwnd == fg,
			})

	win32gui.EnumWindows(cb, None)
	return result


def _resolve_scope_window(scope_window: str | None = None, scope_hwnd: int | None = None, pid: int | None = None) -> ScopeResolution:
	_require_pywinauto()
	diag: dict[str, Any] = {
		"input": {"scope_window": scope_window, "scope_hwnd": scope_hwnd, "pid": pid},
		"matched_windows": [],
	}
	desktop = Desktop(backend="uia")

	if scope_hwnd is not None:
		try:
			if _WIN32_AVAILABLE and (not win32gui.IsWindow(scope_hwnd) or not win32gui.IsWindowVisible(scope_hwnd)):
				return ScopeResolution(False, None, ErrorCode.STALE_HANDLE, {
					**diag, "reason": f"hwnd {scope_hwnd} is invalid or not visible"
				})
		except Exception:
			pass
		try:
			win = desktop.window(handle=scope_hwnd)
			diag["matched_windows"].append({"hwnd": scope_hwnd, "title": win.window_text(), "match": "hwnd_exact"})
			return ScopeResolution(True, win, None, diag)
		except Exception as e:
			return ScopeResolution(False, None, ErrorCode.STALE_HANDLE, {**diag, "reason": str(e)})

	if not scope_window and not pid:
		return ScopeResolution(True, desktop, None, diag)

	candidates = []
	for w in desktop.windows():
		try:
			title = w.window_text() or ""
			hwnd = int(w.handle)
			match_kind = None
			if scope_window:
				if title == scope_window:
					match_kind = "title_exact"
				elif title.lower() == scope_window.lower():
					match_kind = "title_case_insensitive_exact"
				elif scope_window.lower() in title.lower():
					match_kind = "title_contains"
			else:
				match_kind = "pid_only"
			if pid is not None:
				try:
					_, wpid = win32process.GetWindowThreadProcessId(hwnd)
					if wpid != pid:
						match_kind = None
				except Exception:
					match_kind = None
			if match_kind:
				candidates.append((match_kind, title, hwnd, w))
		except Exception:
			continue

	for c in candidates:
		diag["matched_windows"].append({"hwnd": c[2], "title": c[1], "match": c[0]})

	if not candidates:
		return ScopeResolution(False, None, ErrorCode.WINDOW_NOT_FOUND, {
			**diag,
			"reason": "no visible window matched scope filters",
			"visible_windows": _list_windows_impl(pid=pid) if _WIN32_AVAILABLE else [],
		})

	priority = {"title_exact": 0, "title_case_insensitive_exact": 1, "pid_only": 2, "title_contains": 3}
	candidates.sort(key=lambda c: (priority.get(c[0], 99), len(c[1]), c[2]))
	best = candidates[0]
	if len(candidates) > 1 and best[0] == "title_contains":
		return ScopeResolution(False, None, ErrorCode.WINDOW_AMBIGUOUS, {
			**diag,
			"reason": "multiple windows matched by partial title; provide scope_hwnd or exact title",
		})
	return ScopeResolution(True, best[3], None, diag)


def _elem_dict(elem, coordinate_space: str = "physical", hwnd: int | None = None):
	try:
		rect = elem.rectangle()
		physical_rect = {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()}
		d = {
			"automationId": elem.automation_id() or "",
			"name": elem.window_text() or "",
			"controlType": elem.friendly_class_name() or "",
			"rect": _rect_to_space(physical_rect, coordinate_space, hwnd),
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


def _invoke_elem(elem):
	ct = (elem.friendly_class_name() or "").lower()
	if ct in ("tabitem", "listitem", "radiobutton", "treeitem"):
		try:
			elem.select()
			return "SelectionItem", {"IsSelected": True}
		except Exception:
			pass
	if ct == "checkbox":
		try:
			elem.toggle()
			return "Toggle", {"ToggleState": str(elem.get_toggle_state())}
		except Exception:
			pass
	if ct in ("combobox", "splitbutton"):
		try:
			elem.expand()
			return "ExpandCollapse", {"Expanded": True}
		except Exception:
			pass
	try:
		elem.invoke()
		return "Invoke", {}
	except Exception:
		elem.click_input()
		return "click_input", {}


def _control_matches(elem, automation_id=None, name=None, control_type=None) -> tuple[bool, list[str]]:
	reasons = []
	try:
		if automation_id is not None and (elem.automation_id() or "") != automation_id:
			reasons.append("automation_id_mismatch")
		if name is not None and (elem.window_text() or "") != name:
			reasons.append("name_mismatch")
		if control_type is not None:
			if isinstance(control_type, list):
				if (elem.friendly_class_name() or "") not in control_type:
					reasons.append("control_type_mismatch")
			elif (elem.friendly_class_name() or "") != control_type:
				reasons.append("control_type_mismatch")
	except Exception:
		reasons.append("element_unreadable")
	return len(reasons) == 0, reasons


def _collect_controls(root, limit: int = 300):
	try:
		controls = list(root.descendants())[:limit]
		return controls
	except Exception:
		return []


def _resolve_controls(
	root,
	automation_id=None,
	name=None,
	control_type=None,
	coordinate_space="physical",
	scope_hwnd: int | None = None,
):
	if automation_id is None and name is None and control_type is None:
		raise ValueError("Provide at least one of: automation_id, name, control_type")

	candidates = []
	considered = []
	for elem in _collect_controls(root):
		ok, reasons = _control_matches(elem, automation_id=automation_id, name=name, control_type=control_type)
		entry = _elem_dict(elem, coordinate_space=coordinate_space, hwnd=scope_hwnd)
		if ok:
			candidates.append((elem, entry))
		else:
			entry["reasons"] = reasons
			considered.append(entry)

	def sort_key(item):
		e = item[1]
		rect = e.get("rect") or {}
		area = int(rect.get("w", 0)) * int(rect.get("h", 0))
		return (not bool(e.get("isVisible", False)), not bool(e.get("isEnabled", False)), -area, rect.get("y", 0), rect.get("x", 0))

	candidates.sort(key=sort_key)
	return candidates, considered


def _png_response(img: Image.Image, scale: float = 1.0) -> types.ImageContent:
	return _image_response(img, scale=scale)


def _image_response(img: Image.Image, scale: float = 1.0, fmt: str = "png", quality: int = 75) -> types.ImageContent:
	if scale != 1.0:
		w = max(1, int(img.width * scale))
		h = max(1, int(img.height * scale))
		img = img.resize((w, h), Image.LANCZOS)
	buf = io.BytesIO()
	if fmt.upper() == "JPEG":
		img = img.convert("RGB")
		img.save(buf, format="JPEG", quality=quality)
		mime = "image/jpeg"
	else:
		img.save(buf, format="PNG")
		mime = "image/png"
	encoded = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
	return types.ImageContent(type="image", mimeType=mime, data=encoded)


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
		}
		try:
			v = elem.get_value()
			if v is not None:
				node["value"] = v
		except Exception:
			pass
		if depth < max_depth:
			children = []
			for child in elem.children():
				child_node = _dump_element(child, max_depth, depth + 1)
				if child_node:
					children.append(child_node)
			node["children"] = children
		return node
	except Exception:
		return None


async def _wait_for_new_child_window(pid: int | None, before_hwnds: set[int], timeout: float = 2.0):
	if not _WIN32_AVAILABLE:
		return None
	loop = asyncio.get_running_loop()
	deadline = loop.time() + timeout
	while loop.time() < deadline:
		for w in _list_windows_impl(pid=pid):
			if w["hwnd"] not in before_hwnds:
				return w
		await asyncio.sleep(0.05)
	return None


def _batch_cache_get(cache: dict[str, Any], key: str):
	return cache.get(key)


def _batch_cache_set(cache: dict[str, Any], key: str, value: Any):
	cache[key] = value


def _cache_key_scope(scope_window: str | None, scope_hwnd: int | None, pid: int | None):
	return f"{scope_window}|{scope_hwnd}|{pid}"


# =============================================================================
# Tool definitions
# =============================================================================

@app.list_tools()
async def list_tools() -> list[types.Tool]:
	return [
		types.Tool(
			name="take_screenshot",
			description="Capture a screenshot as base64 image. Coordinates are physical pixels.",
			inputSchema={
				"type": "object",
				"properties": {
					"scale": {"type": "number", "description": "Resize factor 0-1 (default: 0.5)"},
					"format": {"type": "string", "enum": ["png", "jpeg"], "description": "Image format (default: png)"},
					"quality": {"type": "integer", "description": "JPEG quality 1-95 (default: 75, ignored for png)"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="capture_region",
			description="Capture a region by coordinates or automation_id bounding box.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer"},
					"y": {"type": "integer"},
					"w": {"type": "integer"},
					"h": {"type": "integer"},
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"coordinate_space": {"type": "string", "enum": ["physical", "logical"], "description": "Default: physical"},
					"scale": {"type": "number"},
					"format": {"type": "string", "enum": ["png", "jpeg"]},
					"quality": {"type": "integer"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="get_screen_size",
			description="Return screen size with coordinate metadata.",
			inputSchema={"type": "object", "properties": {"coordinate_space": {"type": "string", "enum": ["physical", "logical"]}}, "required": []},
		),
		types.Tool(
			name="get_mouse_position",
			description="Return mouse position with coordinate metadata.",
			inputSchema={"type": "object", "properties": {"coordinate_space": {"type": "string", "enum": ["physical", "logical"]}}, "required": []},
		),
		types.Tool(
			name="convert_coordinates",
			description="Convert coordinates between physical pixels and logical 96-DPI units.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "number"},
					"y": {"type": "number"},
					"from_space": {"type": "string", "enum": ["physical", "logical"]},
					"to_space": {"type": "string", "enum": ["physical", "logical"]},
					"hwnd": {"type": "integer", "description": "Optional window handle for per-window DPI"},
				},
				"required": ["x", "y", "from_space", "to_space"],
			},
		),
		types.Tool(
			name="click",
			description="Click by coordinates or automation_id.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer"},
					"y": {"type": "integer"},
					"button": {"type": "string", "enum": ["left", "right", "middle"]},
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"control_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"coordinate_space": {"type": "string", "enum": ["physical", "logical"]},
					"return_state": {"type": "boolean"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="double_click",
			description="Double-click coordinates.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer"},
					"y": {"type": "integer"},
					"coordinate_space": {"type": "string", "enum": ["physical", "logical"]},
				},
				"required": ["x", "y"],
			},
		),
		types.Tool(
			name="move_mouse",
			description="Move cursor to coordinates.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer"},
					"y": {"type": "integer"},
					"coordinate_space": {"type": "string", "enum": ["physical", "logical"]},
				},
				"required": ["x", "y"],
			},
		),
		types.Tool(
			name="drag",
			description="Drag from one point to another.",
			inputSchema={
				"type": "object",
				"properties": {
					"from_x": {"type": "integer"},
					"from_y": {"type": "integer"},
					"to_x": {"type": "integer"},
					"to_y": {"type": "integer"},
					"duration": {"type": "number"},
					"coordinate_space": {"type": "string", "enum": ["physical", "logical"]},
				},
				"required": ["from_x", "from_y", "to_x", "to_y"],
			},
		),
		types.Tool(
			name="scroll",
			description="Scroll at coordinates.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer"},
					"y": {"type": "integer"},
					"clicks": {"type": "integer"},
					"coordinate_space": {"type": "string", "enum": ["physical", "logical"]},
				},
				"required": ["x", "y", "clicks"],
			},
		),
		types.Tool(
			name="type_text",
			description="Type ASCII text.",
			inputSchema={
				"type": "object",
				"properties": {
					"text": {"type": "string"},
					"interval": {"type": "number"},
					"return_state": {"type": "boolean"},
				},
				"required": ["text"],
			},
		),
		types.Tool(
			name="press_key",
			description="Press a key or hotkey.",
			inputSchema={
				"type": "object",
				"properties": {
					"keys": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"presses": {"type": "integer"},
					"return_state": {"type": "boolean"},
				},
				"required": ["keys"],
			},
		),
		types.Tool(
			name="launch_app",
			description="Launch executable path.",
			inputSchema={
				"type": "object",
				"properties": {"path": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}},
				"required": ["path"],
			},
		),
		types.Tool(
			name="sleep",
			description="Pause for seconds.",
			inputSchema={"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]},
		),
		types.Tool(
			name="find_image_on_screen",
			description="Locate template on screen (opencv-python required).",
			inputSchema={
				"type": "object",
				"properties": {"image_base64": {"type": "string"}, "confidence": {"type": "number"}},
				"required": ["image_base64"],
			},
		),
		types.Tool(
			name="ua_dump_tree",
			description="Dump UIAutomation tree.",
			inputSchema={
				"type": "object",
				"properties": {
					"window_title": {"type": "string"},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"max_depth": {"type": "integer"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="ua_find",
			description="Find UIAutomation controls with deterministic scoping and diagnostics.",
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"control_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"coordinate_space": {"type": "string", "enum": ["physical", "logical"]},
				},
				"required": [],
			},
		),
		types.Tool(
			name="ua_invoke",
			description="Invoke control by automation_id/name with deterministic scope and timing diagnostics.",
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"control_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"strict_unique": {"type": "boolean"},
					"wait_until_idle": {"type": "boolean"},
					"idle_timeout": {"type": "number"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="ua_set_value",
			description="Set a control value.",
			inputSchema={
				"type": "object",
				"properties": {
					"value": {"type": "string"},
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"control_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"strict_unique": {"type": "boolean"},
				},
				"required": ["value"],
			},
		),
		types.Tool(
			name="ua_get_value",
			description="Get current value/text of a control.",
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"control_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"strict_unique": {"type": "boolean"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="click_in_window",
			description="Single-call deterministic click path inside target window with retries and post-action wait.",
			inputSchema={
				"type": "object",
				"properties": {
					"title": {"type": "string"},
					"hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"button": {"type": "string"},
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"control_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"wait_until_idle": {"type": "boolean"},
					"idle_timeout": {"type": "number"},
					"retries": {"type": "integer"},
					"detect_new_child_window": {"type": "boolean"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="wait_for_window",
			description="Wait for a window by title substring or regex to appear/disappear.",
			inputSchema={
				"type": "object",
				"properties": {
					"title": {"type": "string"},
					"title_regex": {"type": "string"},
					"timeout": {"type": "number"},
					"mode": {"type": "string", "enum": ["appear", "disappear"]},
					"pid": {"type": "integer"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="wait_for_control_change",
			description="Wait for control state/value/text change.",
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"control_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"state": {"type": "string", "enum": ["exists", "visible", "enabled", "value_equals", "value_changes", "text_equals", "text_changes"]},
					"expected_value": {"type": "string"},
					"expected_text": {"type": "string"},
					"timeout": {"type": "number"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="wait_for_element",
			description="Backward-compatible alias of wait_for_control_change (states: exists|visible|enabled).",
			inputSchema={
				"type": "object",
				"properties": {
					"automation_id": {"type": "string"},
					"name": {"type": "string"},
					"scope_window": {"type": "string"},
					"scope_hwnd": {"type": "integer"},
					"pid": {"type": "integer"},
					"state": {"type": "string", "enum": ["visible", "enabled", "exists"]},
					"timeout": {"type": "number"},
				},
				"required": [],
			},
		),
		types.Tool(
			name="wait_input_idle",
			description="Wait until process input queue is idle.",
			inputSchema={
				"type": "object",
				"properties": {"pid": {"type": "integer"}, "timeout": {"type": "number"}},
				"required": ["pid"],
			},
		),
		types.Tool(
			name="wait_ui_idle",
			description="Wait for UI idle/render settle using process idle + stable foreground window rect.",
			inputSchema={
				"type": "object",
				"properties": {"pid": {"type": "integer"}, "timeout": {"type": "number"}, "stable_ms": {"type": "integer"}},
				"required": [],
			},
		),
		types.Tool(
			name="list_windows",
			description="List visible top-level windows.",
			inputSchema={"type": "object", "properties": {"pid": {"type": "integer"}}, "required": []},
		),
		types.Tool(
			name="focus_window",
			description="Bring window to foreground.",
			inputSchema={
				"type": "object",
				"properties": {"hwnd": {"type": "integer"}, "title": {"type": "string"}, "pid": {"type": "integer"}},
				"required": [],
			},
		),
		types.Tool(
			name="close_foreground_dialog",
			description="Close active foreground dialog/modal using common button strategies.",
			inputSchema={
				"type": "object",
				"properties": {"wait_timeout": {"type": "number"}, "buttons": {"type": "array", "items": {"type": "string"}}},
				"required": [],
			},
		),
		types.Tool(
			name="dismiss_dialog",
			description="Dismiss dialog by title/hwnd and button text with wait timeout.",
			inputSchema={
				"type": "object",
				"properties": {
					"title": {"type": "string"},
					"hwnd": {"type": "integer"},
					"button": {"type": "string"},
					"wait_timeout": {"type": "number"},
					"pid": {"type": "integer"},
				},
				"required": ["button"],
			},
		),
		types.Tool(
			name="auto_dismiss_dialog",
			description="Backward-compatible alias for dismiss_dialog.",
			inputSchema={
				"type": "object",
				"properties": {"title": {"type": "string"}, "hwnd": {"type": "integer"}, "button": {"type": "string"}, "wait_timeout": {"type": "number"}, "pid": {"type": "integer"}},
				"required": ["button"],
			},
		),
		types.Tool(
			name="batch",
			description="Execute multiple tool calls in one round-trip with shared scope/control caches.",
			inputSchema={
				"type": "object",
				"properties": {
					"steps": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
							"required": ["tool", "arguments"],
						},
					},
				},
				"required": ["steps"],
			},
		),
	]


async def _dispatch(name: str, arguments: dict, batch_cache: dict[str, Any] | None = None) -> list[types.TextContent | types.ImageContent]:
	batch_cache = batch_cache or {}

	if name == "take_screenshot":
		screenshot = pyautogui.screenshot()
		meta = {
			"coordinate": _coord_metadata("physical"),
			"size": {"w": screenshot.width, "h": screenshot.height},
		}
		return [
			_image_response(
				screenshot,
				scale=arguments.get("scale", 0.5),
				fmt=arguments.get("format", "png"),
				quality=arguments.get("quality", 75),
			),
			types.TextContent(type="text", text=json.dumps(meta, indent=2)),
		]

	if name == "capture_region":
		coordinate_space = arguments.get("coordinate_space", "physical")
		scale = arguments.get("scale", 0.5)
		fmt = arguments.get("format", "png")
		quality = arguments.get("quality", 75)
		scope_hwnd = arguments.get("scope_hwnd")
		if "automation_id" in arguments or "name" in arguments:
			_require_pywinauto()
			scope = _resolve_scope_window(arguments.get("scope_window"), scope_hwnd, arguments.get("pid"))
			if not scope.success:
				return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Scope window resolution failed", scope.diagnostics)
			root = scope.window
			cands, considered = _resolve_controls(
				root,
				automation_id=arguments.get("automation_id"),
				name=arguments.get("name"),
				control_type=arguments.get("control_type"),
				coordinate_space="physical",
				scope_hwnd=scope_hwnd,
			)
			if not cands:
				return _err(ErrorCode.CONTROL_NOT_FOUND, "No matching control found", {"scope": scope.diagnostics, "controls_considered": considered[:40]})
			rect = cands[0][0].rectangle()
			x, y, w, h = rect.left, rect.top, rect.width(), rect.height()
		else:
			x = arguments["x"]
			y = arguments["y"]
			w = arguments["w"]
			h = arguments["h"]
			x, y = _to_physical_point(x, y, coordinate_space, scope_hwnd)
			if coordinate_space == "logical":
				w, h = _logical_to_physical(w, h, scope_hwnd)
		screenshot = pyautogui.screenshot()
		region = screenshot.crop((x, y, x + w, y + h))
		meta = {
			"success": True,
			"captured_rect": _rect_to_space({"x": x, "y": y, "w": w, "h": h}, coordinate_space, scope_hwnd),
			"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
		}
		return [types.TextContent(type="text", text=json.dumps(meta, indent=2)), _image_response(region, scale=scale, fmt=fmt, quality=quality)]

	if name == "get_screen_size":
		coordinate_space = arguments.get("coordinate_space", "physical")
		w, h = pyautogui.size()
		if coordinate_space == "logical":
			w, h = _physical_to_logical(w, h)
		return _json_text({"success": True, "size": {"w": int(w), "h": int(h)}, "coordinate": _coord_metadata(coordinate_space)})

	if name == "get_mouse_position":
		coordinate_space = arguments.get("coordinate_space", "physical")
		pos = pyautogui.position()
		x, y = pos.x, pos.y
		if coordinate_space == "logical":
			x, y = _physical_to_logical(x, y)
		return _json_text({"success": True, "position": {"x": int(x), "y": int(y)}, "coordinate": _coord_metadata(coordinate_space)})

	if name == "convert_coordinates":
		from_space = arguments["from_space"]
		to_space = arguments["to_space"]
		x = arguments["x"]
		y = arguments["y"]
		hwnd = arguments.get("hwnd")
		if from_space == to_space:
			out_x, out_y = int(x), int(y)
		elif from_space == "logical" and to_space == "physical":
			out_x, out_y = _logical_to_physical(x, y, hwnd)
		elif from_space == "physical" and to_space == "logical":
			out_x, out_y = _physical_to_logical(x, y, hwnd)
		else:
			return _err(ErrorCode.INVALID_ARGUMENT, "Invalid from_space/to_space")
		return _json_text({
			"success": True,
			"input": {"x": x, "y": y, "space": from_space},
			"output": {"x": out_x, "y": out_y, "space": to_space},
			"dpi": _get_dpi(hwnd),
			"scale": _get_dpi(hwnd) / 96.0,
		})

	if name == "click":
		t0 = _now()
		automation_id = arguments.get("automation_id")
		coordinate_space = arguments.get("coordinate_space", "physical")
		return_state = arguments.get("return_state", False)
		button = arguments.get("button", "left")
		scope_hwnd = arguments.get("scope_hwnd")
		if automation_id or arguments.get("name"):
			_require_pywinauto()
			t_lookup = _now()
			scope = _resolve_scope_window(arguments.get("scope_window"), scope_hwnd, arguments.get("pid"))
			if not scope.success:
				return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Scope window resolution failed", scope.diagnostics, {"lookup_ms": _ms(t_lookup), "total_ms": _ms(t0)})
			root = scope.window
			cands, considered = _resolve_controls(
				root,
				automation_id=automation_id,
				name=arguments.get("name"),
				control_type=arguments.get("control_type"),
				coordinate_space="physical",
				scope_hwnd=scope_hwnd,
			)
			lookup_ms = _ms(t_lookup)
			if not cands:
				return _err(ErrorCode.CONTROL_NOT_FOUND, "No matching control found", {"scope": scope.diagnostics, "controls_considered": considered[:40]}, {"lookup_ms": lookup_ms, "total_ms": _ms(t0)})
			elem = cands[0][0]
			t_action = _now()
			elem.click_input()
			action_ms = _ms(t_action)
			out = {
				"success": True,
				"method": "uia_click_input",
				"target": cands[0][1],
				"timings_ms": {"lookup_ms": lookup_ms, "action_ms": action_ms, "total_ms": _ms(t0)},
				"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
			}
		else:
			x = arguments["x"]
			y = arguments["y"]
			px, py = _to_physical_point(x, y, coordinate_space, scope_hwnd)
			t_action = _now()
			pyautogui.click(px, py, button=button)
			action_ms = _ms(t_action)
			out = {
				"success": True,
				"method": "mouse_click",
				"clicked": {"x": x, "y": y, "space": coordinate_space},
				"clicked_physical": {"x": px, "y": py},
				"timings_ms": {"action_ms": action_ms, "total_ms": _ms(t0)},
				"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
			}
		if return_state:
			out["foreground"] = _foreground_info()
		return _json_text(out)

	if name == "double_click":
		coordinate_space = arguments.get("coordinate_space", "physical")
		scope_hwnd = arguments.get("scope_hwnd")
		px, py = _to_physical_point(arguments["x"], arguments["y"], coordinate_space, scope_hwnd)
		pyautogui.doubleClick(px, py)
		return _json_text({
			"success": True,
			"clicked": {"x": arguments["x"], "y": arguments["y"], "space": coordinate_space},
			"clicked_physical": {"x": px, "y": py},
			"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
		})

	if name == "move_mouse":
		coordinate_space = arguments.get("coordinate_space", "physical")
		scope_hwnd = arguments.get("scope_hwnd")
		px, py = _to_physical_point(arguments["x"], arguments["y"], coordinate_space, scope_hwnd)
		pyautogui.moveTo(px, py)
		return _json_text({
			"success": True,
			"position": {"x": arguments["x"], "y": arguments["y"], "space": coordinate_space},
			"position_physical": {"x": px, "y": py},
			"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
		})

	if name == "drag":
		coordinate_space = arguments.get("coordinate_space", "physical")
		scope_hwnd = arguments.get("scope_hwnd")
		duration = arguments.get("duration", 0.5)
		fx, fy = _to_physical_point(arguments["from_x"], arguments["from_y"], coordinate_space, scope_hwnd)
		tx, ty = _to_physical_point(arguments["to_x"], arguments["to_y"], coordinate_space, scope_hwnd)
		pyautogui.moveTo(fx, fy)
		pyautogui.dragTo(tx, ty, duration=duration)
		return _json_text({
			"success": True,
			"from": {"x": arguments["from_x"], "y": arguments["from_y"], "space": coordinate_space},
			"to": {"x": arguments["to_x"], "y": arguments["to_y"], "space": coordinate_space},
			"from_physical": {"x": fx, "y": fy},
			"to_physical": {"x": tx, "y": ty},
			"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
		})

	if name == "scroll":
		coordinate_space = arguments.get("coordinate_space", "physical")
		scope_hwnd = arguments.get("scope_hwnd")
		px, py = _to_physical_point(arguments["x"], arguments["y"], coordinate_space, scope_hwnd)
		clicks = arguments["clicks"]
		pyautogui.scroll(clicks, x=px, y=py)
		return _json_text({
			"success": True,
			"clicks": clicks,
			"at": {"x": arguments["x"], "y": arguments["y"], "space": coordinate_space},
			"at_physical": {"x": px, "y": py},
			"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
		})

	if name == "type_text":
		text = arguments["text"]
		interval = arguments.get("interval", 0.03)
		pyautogui.write(text, interval=interval)
		resp = {"success": True, "typed": text, "interval": interval}
		if arguments.get("return_state"):
			resp["foreground"] = _foreground_info()
		return _json_text(resp)

	if name == "press_key":
		keys = arguments["keys"]
		presses = arguments.get("presses", 1)
		if isinstance(keys, list):
			pyautogui.hotkey(*keys)
			resp = {"success": True, "hotkey": keys}
		else:
			pyautogui.press(keys, presses=presses)
			resp = {"success": True, "key": keys, "presses": presses}
		if arguments.get("return_state"):
			resp["foreground"] = _foreground_info()
		return _json_text(resp)

	if name == "launch_app":
		path = arguments["path"]
		args = arguments.get("args", [])
		p = subprocess.Popen([path] + args)
		return _json_text({"success": True, "path": path, "pid": p.pid})

	if name == "sleep":
		seconds = arguments["seconds"]
		await asyncio.sleep(seconds)
		return _json_text({"success": True, "slept_seconds": seconds})

	if name == "find_image_on_screen":
		if not _CV2_AVAILABLE:
			return _err(ErrorCode.DEPENDENCY_MISSING, "opencv-python is not installed")
		confidence = arguments.get("confidence", 0.9)
		image_bytes = base64.standard_b64decode(arguments["image_base64"])
		template_arr = np.frombuffer(image_bytes, dtype=np.uint8)
		template = cv2.imdecode(template_arr, cv2.IMREAD_COLOR)
		if template is None:
			return _err(ErrorCode.INVALID_ARGUMENT, "could not decode the provided image")
		screenshot = pyautogui.screenshot()
		screen = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
		result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
		_, max_val, _, max_loc = cv2.minMaxLoc(result)
		if max_val < confidence:
			return _json_text({"success": False, "error": {"code": ErrorCode.CONTROL_NOT_FOUND, "message": "Image not found"}, "best_confidence": round(float(max_val), 4)})
		th, tw = template.shape[:2]
		cx = max_loc[0] + tw // 2
		cy = max_loc[1] + th // 2
		return _json_text({"success": True, "centre": {"x": cx, "y": cy}, "confidence": float(max_val), "coordinate": _coord_metadata("physical")})

	if name == "ua_dump_tree":
		_require_pywinauto()
		scope_title = arguments.get("scope_window") or arguments.get("window_title")
		scope = _resolve_scope_window(scope_title, arguments.get("scope_hwnd"), arguments.get("pid"))
		if not scope.success:
			return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Scope window resolution failed", scope.diagnostics)
		tree = _dump_element(scope.window, arguments.get("max_depth", 5))
		return _json_text({"success": True, "scope": scope.diagnostics, "tree": tree, "coordinate": _coord_metadata("physical", arguments.get("scope_hwnd"))})

	if name == "ua_find":
		_require_pywinauto()
		t0 = _now()
		coordinate_space = arguments.get("coordinate_space", "physical")
		scope_hwnd = arguments.get("scope_hwnd")
		scope_key = _cache_key_scope(arguments.get("scope_window"), scope_hwnd, arguments.get("pid"))
		scope = _batch_cache_get(batch_cache, f"scope:{scope_key}")
		if scope is None:
			scope = _resolve_scope_window(arguments.get("scope_window"), scope_hwnd, arguments.get("pid"))
			_batch_cache_set(batch_cache, f"scope:{scope_key}", scope)
		if not scope.success:
			return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Scope window resolution failed", scope.diagnostics)
		cands, considered = _resolve_controls(
			scope.window,
			automation_id=arguments.get("automation_id"),
			name=arguments.get("name"),
			control_type=arguments.get("control_type"),
			coordinate_space=coordinate_space,
			scope_hwnd=scope_hwnd,
		)
		return _json_text({
			"success": True,
			"count": len(cands),
			"matches": [c[1] for c in cands],
			"scope": scope.diagnostics,
			"controls_considered_count": len(considered) + len(cands),
			"timings_ms": {"lookup_ms": _ms(t0), "total_ms": _ms(t0)},
			"coordinate": _coord_metadata(coordinate_space, scope_hwnd),
		})

	if name in ("ua_invoke", "ua_set_value", "ua_get_value"):
		_require_pywinauto()
		t0 = _now()
		scope_hwnd = arguments.get("scope_hwnd")
		scope_key = _cache_key_scope(arguments.get("scope_window"), scope_hwnd, arguments.get("pid"))
		scope = _batch_cache_get(batch_cache, f"scope:{scope_key}")
		if scope is None:
			scope = _resolve_scope_window(arguments.get("scope_window"), scope_hwnd, arguments.get("pid"))
			_batch_cache_set(batch_cache, f"scope:{scope_key}", scope)
		if not scope.success:
			return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Scope window resolution failed", scope.diagnostics, {"lookup_ms": _ms(t0), "total_ms": _ms(t0)})
		root = scope.window
		t_lookup = _now()
		cands, considered = _resolve_controls(
			root,
			automation_id=arguments.get("automation_id"),
			name=arguments.get("name"),
			control_type=arguments.get("control_type"),
			coordinate_space="physical",
			scope_hwnd=scope_hwnd,
		)
		lookup_ms = _ms(t_lookup)
		if not cands:
			return _err(ErrorCode.CONTROL_NOT_FOUND, "No matching control found", {"scope": scope.diagnostics, "controls_considered": considered[:40]}, {"lookup_ms": lookup_ms, "total_ms": _ms(t0)})
		if arguments.get("strict_unique", False) and len(cands) > 1:
			return _err(ErrorCode.CONTROL_AMBIGUOUS, "Multiple controls matched criteria", {
				"scope": scope.diagnostics,
				"matches": [c[1] for c in cands[:10]],
			}, {"lookup_ms": lookup_ms, "total_ms": _ms(t0)})
		elem = cands[0][0]
		selected = cands[0][1]

		if name == "ua_invoke":
			t_action = _now()
			try:
				pattern_used, post_state = _invoke_elem(elem)
			except Exception as e:
				return _err(ErrorCode.ACTION_FAILED, str(e), {"selected": selected}, {"lookup_ms": lookup_ms, "action_ms": _ms(t_action), "total_ms": _ms(t0)})
			action_ms = _ms(t_action)
			if arguments.get("wait_until_idle", False):
				pid = arguments.get("pid")
				if pid is None and _WIN32_AVAILABLE:
					try:
						_, pid = win32process.GetWindowThreadProcessId(int(elem.top_level_parent().handle))
					except Exception:
						pid = None
				if pid is not None:
					await _dispatch("wait_input_idle", {"pid": pid, "timeout": arguments.get("idle_timeout", 3)}, batch_cache=batch_cache)
			return _json_text({
				"success": True,
				"pattern_used": pattern_used,
				"post_state": post_state,
				"selected": selected,
				"scope": scope.diagnostics,
				"timings_ms": {"lookup_ms": lookup_ms, "action_ms": action_ms, "total_ms": _ms(t0)},
			})

		if name == "ua_set_value":
			value = arguments["value"]
			t_action = _now()
			try:
				try:
					elem.set_edit_text(value)
					method = "set_edit_text"
				except Exception:
					try:
						elem.select(value)
						method = "select"
					except Exception:
						elem.set_value(value)
						method = "set_value"
			except Exception as e:
				return _err(ErrorCode.ACTION_FAILED, str(e), {"selected": selected}, {"lookup_ms": lookup_ms, "action_ms": _ms(t_action), "total_ms": _ms(t0)})
			return _json_text({
				"success": True,
				"method": method,
				"value": value,
				"selected": selected,
				"scope": scope.diagnostics,
				"timings_ms": {"lookup_ms": lookup_ms, "action_ms": _ms(t_action), "total_ms": _ms(t0)},
			})

		# ua_get_value
		t_action = _now()
		try:
			try:
				value = elem.get_value()
				method = "get_value"
			except Exception:
				value = elem.window_text()
				method = "window_text"
		except Exception as e:
			return _err(ErrorCode.ACTION_FAILED, str(e), {"selected": selected}, {"lookup_ms": lookup_ms, "action_ms": _ms(t_action), "total_ms": _ms(t0)})
		return _json_text({
			"success": True,
			"method": method,
			"value": value,
			"selected": selected,
			"scope": scope.diagnostics,
			"timings_ms": {"lookup_ms": lookup_ms, "action_ms": _ms(t_action), "total_ms": _ms(t0)},
		})

	if name == "click_in_window":
		_require_pywinauto()
		_require_win32()
		t0 = _now()
		retries = max(1, int(arguments.get("retries", 2)))
		wait_until_idle = bool(arguments.get("wait_until_idle", True))
		idle_timeout = float(arguments.get("idle_timeout", 3.0))
		detect_new_child_window = bool(arguments.get("detect_new_child_window", True))

		scope = _resolve_scope_window(arguments.get("title"), arguments.get("hwnd"), arguments.get("pid"))
		if not scope.success:
			return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Window resolution failed", scope.diagnostics, {"total_ms": _ms(t0)})
		window = scope.window
		target_hwnd = int(window.handle)
		win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)
		win32gui.SetForegroundWindow(target_hwnd)

		try:
			_, owner_pid = win32process.GetWindowThreadProcessId(target_hwnd)
		except Exception:
			owner_pid = arguments.get("pid")
		before_hwnds = set(w["hwnd"] for w in _list_windows_impl(pid=owner_pid)) if detect_new_child_window else set()

		control_type = arguments.get("control_type")
		button = arguments.get("button")
		if button and control_type is None:
			control_type = "Button"
		criteria = {
			"automation_id": arguments.get("automation_id"),
			"name": arguments.get("name") or button,
			"control_type": control_type,
		}

		last_diag = {}
		last_error = None
		t_lookup_total = 0
		t_action_total = 0
		selected = None
		pattern_used = None
		for attempt in range(1, retries + 1):
			t_lookup = _now()
			cands, considered = _resolve_controls(
				window,
				automation_id=criteria["automation_id"],
				name=criteria["name"],
				control_type=criteria["control_type"],
				coordinate_space="physical",
				scope_hwnd=target_hwnd,
			)
			t_lookup_total += _ms(t_lookup)
			last_diag = {"attempt": attempt, "criteria": criteria, "controls_considered": considered[:30], "scope": scope.diagnostics}
			if not cands:
				last_error = ErrorCode.CONTROL_NOT_FOUND
				await asyncio.sleep(0.05)
				continue
			elem = cands[0][0]
			selected = cands[0][1]
			try:
				t_action = _now()
				pattern_used, _ = _invoke_elem(elem)
				t_action_total += _ms(t_action)
				last_error = None
				break
			except Exception as e:
				last_error = str(e)
				await asyncio.sleep(0.05)
				continue

		if last_error:
			code = ErrorCode.CONTROL_NOT_FOUND if last_error == ErrorCode.CONTROL_NOT_FOUND else ErrorCode.ACTION_FAILED
			return _err(code, "Failed to click target control", last_diag, {"lookup_ms": t_lookup_total, "action_ms": t_action_total, "total_ms": _ms(t0)})

		if wait_until_idle and owner_pid is not None:
			await _dispatch("wait_input_idle", {"pid": owner_pid, "timeout": idle_timeout}, batch_cache=batch_cache)
		new_child = await _wait_for_new_child_window(owner_pid, before_hwnds, timeout=1.5) if detect_new_child_window else None

		return _json_text({
			"success": True,
			"pattern_used": pattern_used,
			"selected": selected,
			"scope": scope.diagnostics,
			"new_child_window": new_child,
			"timings_ms": {"lookup_ms": t_lookup_total, "action_ms": t_action_total, "total_ms": _ms(t0)},
		})

	if name == "wait_for_window":
		_require_win32()
		t0 = _now()
		title = arguments.get("title")
		title_regex = arguments.get("title_regex")
		timeout = float(arguments.get("timeout", 10))
		mode = arguments.get("mode", "appear")
		pid = arguments.get("pid")
		loop = asyncio.get_running_loop()
		deadline = loop.time() + timeout
		rgx = re.compile(title_regex) if title_regex else None

		def matches(window_title: str):
			if title and title.lower() in window_title.lower():
				return True
			if rgx and rgx.search(window_title):
				return True
			return False

		while loop.time() < deadline:
			matched = [w for w in _list_windows_impl(pid=pid) if matches(w["title"])]
			if mode == "appear" and matched:
				return _json_text({"success": True, "mode": mode, "window": matched[0], "timings_ms": {"total_ms": _ms(t0)}})
			if mode == "disappear" and not matched:
				return _json_text({"success": True, "mode": mode, "timings_ms": {"total_ms": _ms(t0)}})
			await asyncio.sleep(0.05)
		return _err(ErrorCode.TIMEOUT, f"Timeout after {timeout}s waiting for window to {mode}", {"title": title, "title_regex": title_regex}, {"total_ms": _ms(t0)})

	if name in ("wait_for_control_change", "wait_for_element"):
		_require_pywinauto()
		t0 = _now()
		state = arguments.get("state", "exists")
		timeout = float(arguments.get("timeout", 10))
		loop = asyncio.get_running_loop()
		deadline = loop.time() + timeout

		scope = _resolve_scope_window(arguments.get("scope_window"), arguments.get("scope_hwnd"), arguments.get("pid"))
		if not scope.success:
			return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Scope window resolution failed", scope.diagnostics)
		root = scope.window

		prev_value = None
		prev_text = None
		while loop.time() < deadline:
			cands, considered = _resolve_controls(
				root,
				automation_id=arguments.get("automation_id"),
				name=arguments.get("name"),
				control_type=arguments.get("control_type"),
				coordinate_space="physical",
				scope_hwnd=arguments.get("scope_hwnd"),
			)
			if cands:
				elem = cands[0][0]
				try:
					value = elem.get_value()
				except Exception:
					value = None
				text = elem.window_text()
				if state == "exists":
					return _json_text({"success": True, "state": state, "selected": cands[0][1], "timings_ms": {"total_ms": _ms(t0)}})
				if state == "visible" and elem.is_visible():
					return _json_text({"success": True, "state": state, "selected": cands[0][1], "timings_ms": {"total_ms": _ms(t0)}})
				if state == "enabled" and elem.is_enabled():
					return _json_text({"success": True, "state": state, "selected": cands[0][1], "timings_ms": {"total_ms": _ms(t0)}})
				if state == "value_equals" and value == arguments.get("expected_value"):
					return _json_text({"success": True, "state": state, "value": value, "timings_ms": {"total_ms": _ms(t0)}})
				if state == "value_changes":
					if prev_value is None:
						prev_value = value
					elif value != prev_value:
						return _json_text({"success": True, "state": state, "before": prev_value, "after": value, "timings_ms": {"total_ms": _ms(t0)}})
				if state == "text_equals" and text == arguments.get("expected_text"):
					return _json_text({"success": True, "state": state, "text": text, "timings_ms": {"total_ms": _ms(t0)}})
				if state == "text_changes":
					if prev_text is None:
						prev_text = text
					elif text != prev_text:
						return _json_text({"success": True, "state": state, "before": prev_text, "after": text, "timings_ms": {"total_ms": _ms(t0)}})
			await asyncio.sleep(0.05)

		return _err(
			ErrorCode.TIMEOUT,
			f"Timeout after {timeout}s waiting for state '{state}'",
			{
				"criteria": {"automation_id": arguments.get("automation_id"), "name": arguments.get("name"), "control_type": arguments.get("control_type")},
				"scope": scope.diagnostics,
				"controls_considered": considered[:30] if "considered" in locals() else [],
			},
			{"total_ms": _ms(t0)},
		)

	if name == "wait_input_idle":
		t0 = _now()
		pid = arguments["pid"]
		timeout_ms = int(float(arguments.get("timeout", 10)) * 1000)
		PROCESS_QUERY_INFORMATION = 0x0400
		SYNCHRONIZE = 0x00100000
		handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | SYNCHRONIZE, False, pid)
		if not handle:
			return _err(ErrorCode.STALE_HANDLE, f"Could not open process {pid}", timings={"total_ms": _ms(t0)})
		try:
			result = await asyncio.get_running_loop().run_in_executor(None, lambda: ctypes.windll.user32.WaitForInputIdle(handle, timeout_ms))
			if result == 0:
				return _json_text({"success": True, "idle": True, "timings_ms": {"total_ms": _ms(t0)}})
			if result == 0x102:
				return _err(ErrorCode.TIMEOUT, f"Timeout after {arguments.get('timeout', 10)}s", timings={"total_ms": _ms(t0)})
			return _err(ErrorCode.ACTION_FAILED, f"WaitForInputIdle returned {result}", timings={"total_ms": _ms(t0)})
		finally:
			ctypes.windll.kernel32.CloseHandle(handle)

	if name == "wait_ui_idle":
		_require_win32()
		t0 = _now()
		timeout = float(arguments.get("timeout", 5))
		stable_ms = int(arguments.get("stable_ms", 250))
		pid = arguments.get("pid")
		loop = asyncio.get_running_loop()
		deadline = loop.time() + timeout
		if pid:
			await _dispatch("wait_input_idle", {"pid": pid, "timeout": timeout}, batch_cache=batch_cache)
		last_rect = None
		stable_start = None
		while loop.time() < deadline:
			fg = _foreground_info().get("foreground_hwnd")
			if fg and win32gui.IsWindow(fg):
				r = win32gui.GetWindowRect(fg)
				rect = (r[0], r[1], r[2], r[3])
				if rect == last_rect:
					if stable_start is None:
						stable_start = loop.time()
					if (loop.time() - stable_start) * 1000 >= stable_ms:
						return _json_text({"success": True, "stable_window_rect": rect, "timings_ms": {"total_ms": _ms(t0)}})
				else:
					last_rect = rect
					stable_start = None
			await asyncio.sleep(0.05)
		return _err(ErrorCode.TIMEOUT, f"Timeout after {timeout}s waiting for UI idle", timings={"total_ms": _ms(t0)})

	if name == "list_windows":
		windows = _list_windows_impl(pid=arguments.get("pid"))
		return _json_text({"success": True, "count": len(windows), "windows": windows, "coordinate": _coord_metadata("physical")})

	if name == "focus_window":
		_require_win32()
		hwnd = arguments.get("hwnd")
		title = arguments.get("title")
		pid = arguments.get("pid")
		if hwnd is None and title:
			scope = _resolve_scope_window(title, None, pid)
			if not scope.success:
				return _err(scope.error_code or ErrorCode.WINDOW_NOT_FOUND, "Window resolution failed", scope.diagnostics)
			hwnd = int(scope.window.handle)
		if hwnd is None:
			return _err(ErrorCode.INVALID_ARGUMENT, "Provide hwnd or title")
		if not win32gui.IsWindow(hwnd):
			return _err(ErrorCode.STALE_HANDLE, f"hwnd {hwnd} is invalid")
		win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
		win32gui.SetForegroundWindow(hwnd)
		return _json_text({"success": True, "focused": {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd)}})

	if name in ("dismiss_dialog", "auto_dismiss_dialog"):
		_require_pywinauto()
		_require_win32()
		t0 = _now()
		button = arguments["button"]
		wait_timeout = float(arguments.get("wait_timeout", 0))
		loop = asyncio.get_running_loop()
		deadline = loop.time() + max(wait_timeout, 0)
		scope = None
		while True:
			scope = _resolve_scope_window(arguments.get("title"), arguments.get("hwnd"), arguments.get("pid"))
			if scope.success:
				break
			if loop.time() >= deadline:
				break
			await asyncio.sleep(0.05)
		if not scope or not scope.success:
			return _err(scope.error_code if scope else ErrorCode.WINDOW_NOT_FOUND, "Dialog not found", scope.diagnostics if scope else {}, {"total_ms": _ms(t0)})
		root = scope.window
		target_hwnd = int(root.handle)
		cands, considered = _resolve_controls(root, name=button, control_type="Button", coordinate_space="physical", scope_hwnd=target_hwnd)
		if not cands:
			return _err(ErrorCode.CONTROL_NOT_FOUND, "Dialog button not found", {"button": button, "scope": scope.diagnostics, "controls_considered": considered[:40]}, {"total_ms": _ms(t0)})
		elem = cands[0][0]
		try:
			pattern_used, _ = _invoke_elem(elem)
		except Exception as e:
			return _err(ErrorCode.ACTION_FAILED, str(e), {"button": button, "scope": scope.diagnostics}, {"total_ms": _ms(t0)})
		return _json_text({
			"success": True,
			"dialog": {"hwnd": target_hwnd, "title": root.window_text()},
			"button_clicked": button,
			"pattern_used": pattern_used,
			"timings_ms": {"total_ms": _ms(t0)},
		})

	if name == "close_foreground_dialog":
		_require_pywinauto()
		_require_win32()
		buttons = arguments.get("buttons", ["OK", "Yes", "Close", "Cancel", "No"])
		wait_timeout = float(arguments.get("wait_timeout", 0.8))
		loop = asyncio.get_running_loop()
		deadline = loop.time() + wait_timeout
		while loop.time() < deadline:
			fg = _foreground_info()
			hwnd = fg.get("foreground_hwnd")
			if hwnd and win32gui.IsWindow(hwnd):
				payload = await _dispatch("dismiss_dialog", {"hwnd": hwnd, "button": buttons[0], "wait_timeout": 0}, batch_cache=batch_cache)
				text = payload[0].text if payload and hasattr(payload[0], "text") else ""
				try:
					obj = json.loads(text)
					if obj.get("success"):
						return _json_text(obj)
				except Exception:
					pass
				for label in buttons[1:]:
					payload = await _dispatch("dismiss_dialog", {"hwnd": hwnd, "button": label, "wait_timeout": 0}, batch_cache=batch_cache)
					text = payload[0].text if payload and hasattr(payload[0], "text") else ""
					try:
						obj = json.loads(text)
						if obj.get("success"):
							return _json_text(obj)
					except Exception:
						continue
			await asyncio.sleep(0.05)
		try:
			pyautogui.press("esc")
			return _json_text({"success": True, "method": "escape_key_fallback"})
		except Exception as e:
			return _err(ErrorCode.ACTION_FAILED, str(e))

	if name == "batch":
		steps = arguments["steps"]
		results = []
		for i, step in enumerate(steps):
			tool_name = step["tool"]
			tool_args = step.get("arguments", {})
			try:
				step_results = await _dispatch(tool_name, tool_args, batch_cache=batch_cache)
				outputs = [r.text if hasattr(r, "text") else "[image]" for r in step_results]
				results.append({"step": i, "tool": tool_name, "success": True, "output": outputs})
			except Exception as e:
				results.append({"step": i, "tool": tool_name, "success": False, "error": {"code": ErrorCode.ACTION_FAILED, "message": str(e)}})
		return _json_text({"success": True, "results": results})

	return _json_text({"success": False, "error": {"code": ErrorCode.INVALID_ARGUMENT, "message": f"Unknown tool: {name}"}})


# =============================================================================
# Tool handler
# =============================================================================

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent]:
	try:
		return await _dispatch(name, arguments or {}, batch_cache={})
	except RuntimeError as e:
		msg = str(e)
		if "not installed" in msg.lower():
			return _err(ErrorCode.DEPENDENCY_MISSING, msg)
		return _err(ErrorCode.ACTION_FAILED, msg)
	except Exception as e:
		return _err(ErrorCode.ACTION_FAILED, str(e))


async def main():
	async with stdio_server() as (read_stream, write_stream):
		await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
	asyncio.run(main())
