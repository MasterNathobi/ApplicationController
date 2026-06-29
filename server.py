import asyncio
import base64
import io
import subprocess
import pyautogui
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1

app = Server("application-controller")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
	return [
		types.Tool(
			name="take_screenshot",
			description="Capture a screenshot of the current screen and return it as a base64-encoded PNG image.",
			inputSchema={"type": "object", "properties": {}, "required": []},
		),
		types.Tool(
			name="get_screen_size",
			description="Return the screen width and height in pixels.",
			inputSchema={"type": "object", "properties": {}, "required": []},
		),
		types.Tool(
			name="click",
			description="Click the mouse at the given screen coordinates.",
			inputSchema={
				"type": "object",
				"properties": {
					"x": {"type": "integer", "description": "X coordinate"},
					"y": {"type": "integer", "description": "Y coordinate"},
					"button": {
						"type": "string",
						"enum": ["left", "right", "middle"],
						"description": "Mouse button to click (default: left)",
					},
				},
				"required": ["x", "y"],
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
					"duration": {
						"type": "number",
						"description": "Duration of drag in seconds (default: 0.5)",
					},
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
					"clicks": {
						"type": "integer",
						"description": "Number of scroll clicks. Positive = up, negative = down.",
					},
				},
				"required": ["x", "y", "clicks"],
			},
		),
		types.Tool(
			name="type_text",
			description="Type a string of text using the keyboard.",
			inputSchema={
				"type": "object",
				"properties": {
					"text": {"type": "string", "description": "Text to type"},
					"interval": {
						"type": "number",
						"description": "Seconds between each keypress (default: 0.05)",
					},
				},
				"required": ["text"],
			},
		),
		types.Tool(
			name="press_key",
			description=(
				"Press one or more keys. Use a single key name (e.g. 'enter', 'tab', 'escape') "
				"or a hotkey combination as a list (e.g. ['ctrl', 'c'])."
			),
			inputSchema={
				"type": "object",
				"properties": {
					"keys": {
						"oneOf": [
							{"type": "string", "description": "Single key name"},
							{
								"type": "array",
								"items": {"type": "string"},
								"description": "Hotkey combination, e.g. ['ctrl', 'alt', 'del']",
							},
						]
					},
					"presses": {
						"type": "integer",
						"description": "Number of times to press the key (default: 1)",
					},
				},
				"required": ["keys"],
			},
		),
		types.Tool(
			name="launch_app",
			description="Launch an application by its executable path.",
			inputSchema={
				"type": "object",
				"properties": {
					"path": {
						"type": "string",
						"description": "Full path to the executable, e.g. C:\\\\Windows\\\\notepad.exe",
					},
					"args": {
						"type": "array",
						"items": {"type": "string"},
						"description": "Optional list of command-line arguments to pass",
					},
				},
				"required": ["path"],
			},
		),
	]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent]:
	if name == "take_screenshot":
		screenshot = pyautogui.screenshot()
		buffer = io.BytesIO()
		screenshot.save(buffer, format="PNG")
		encoded = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")
		return [types.ImageContent(type="image", mimeType="image/png", data=encoded)]

	elif name == "get_screen_size":
		w, h = pyautogui.size()
		return [types.TextContent(type="text", text=f"Screen size: {w}x{h} pixels")]

	elif name == "click":
		x = arguments["x"]
		y = arguments["y"]
		button = arguments.get("button", "left")
		pyautogui.click(x, y, button=button)
		return [types.TextContent(type="text", text=f"Clicked {button} at ({x}, {y})")]

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
		pyautogui.dragTo(to_x, to_y, duration=duration, startX=from_x, startY=from_y, mouseDownUp=True)
		return [types.TextContent(type="text", text=f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})")]

	elif name == "scroll":
		x = arguments["x"]
		y = arguments["y"]
		clicks = arguments["clicks"]
		pyautogui.scroll(clicks, x=x, y=y)
		direction = "up" if clicks > 0 else "down"
		return [types.TextContent(type="text", text=f"Scrolled {direction} {abs(clicks)} clicks at ({x}, {y})")]

	elif name == "type_text":
		text = arguments["text"]
		interval = arguments.get("interval", 0.05)
		pyautogui.typewrite(text, interval=interval)
		return [types.TextContent(type="text", text=f"Typed: {text!r}")]

	elif name == "press_key":
		keys = arguments["keys"]
		presses = arguments.get("presses", 1)
		if isinstance(keys, list):
			pyautogui.hotkey(*keys)
			return [types.TextContent(type="text", text=f"Pressed hotkey: {'+'.join(keys)}")]
		else:
			pyautogui.press(keys, presses=presses)
			return [types.TextContent(type="text", text=f"Pressed '{keys}' {presses} time(s)")]

	elif name == "launch_app":
		path = arguments["path"]
		args = arguments.get("args", [])
		subprocess.Popen([path] + args)
		return [types.TextContent(type="text", text=f"Launched: {path}")]

	else:
		return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
	async with stdio_server() as (read_stream, write_stream):
		await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
	asyncio.run(main())
