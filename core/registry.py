import importlib
import pkgutil
import inspect
import sys
from pathlib import Path

# Caches
_tools = {}   # name -> (function, declaration)
_agents = {}  # name -> agent_instance

# Fallback catalog for built-in tools (originally hardcoded in main.py)
_BUILTIN_DECLARATIONS = {
    "open_app": {
        "name": "open_app",
        "description": "Opens any application on the computer. Use this whenever the user asks to open, launch, or start any app, website, or program. Always call this tool — never just say you opened it.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    "web_search": {
        "name": "web_search",
        "description": "Searches the web for any information.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) or compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "price | specs | reviews"}
            },
            "required": ["query"]
        }
    },
    "weather_report": {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    "send_message": {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    "reminder": {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    "youtube_video": {
        "name": "youtube_video",
        "description": "Controls YouTube. Use for: playing videos, summarizing a video's content, getting video info, showing trending videos, or saving videos into a playlist.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending | playlist (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action or playlist fallback query"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
                "playlist_name": {"type": "STRING", "description": "Name of the playlist to create (playlist only)"},
                "urls":   {"type": "ARRAY", "items": {"type": "STRING"}, "description": "List of video URLs to add to playlist (playlist only)"},
                "save_local": {"type": "BOOLEAN", "description": "Save playlist as a local markdown file (default: true)"},
            },
            "required": []
        }
    },
    "screen_process": {
        "name": "screen_process",
        "description": "Captures and analyzes the screen or webcam image. MUST be called when user asks what is on screen, what you see, analyze my screen, look at camera, etc. You have NO visual ability without this tool. After calling this tool, stay SILENT — the vision module speaks directly.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    "computer_settings": {
        "name": "computer_settings",
        "description": "Controls the computer: volume, brightness, window management, keyboard shortcuts, typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. Use for ANY single computer control command. NEVER route to agent_task.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    "desktop_control": {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    "file_controller": {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    "code_helper": {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    "dev_agent": {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    "computer_control": {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    "game_updater": {
        "name": "game_updater",
        "description": "THE ONLY tool for ANY Steam or Epic Games request. Use for: installing, downloading, updating games, listing installed games, checking download status, scheduling updates. ALWAYS call directly for any Steam/Epic/game request. NEVER use agent_task, browser_control, or web_search for Steam/Epic.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    "flight_finder": {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    "file_processor": {
        "name": "file_processor",
        "description": "Processes a file on the system, such as reading lines, searching text, or analyzing data.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_path": {"type": "STRING", "description": "Absolute path to the file"},
                "action": {"type": "STRING", "description": "The processing operation"}
            }
        }
    }
}


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def find_tool_func(module, module_name: str):
    # Check for function matching the module name
    if hasattr(module, module_name):
        return getattr(module, module_name)
    # Check for functions ending with _action / _control
    for name, obj in inspect.getmembers(module):
        if inspect.isfunction(obj):
            if name.endswith("_action") or name.endswith("_control") or name == "screen_process" or name == "youtube_video" or name == "flight_finder":
                return obj
    # Return first public function
    for name, obj in inspect.getmembers(module):
        if inspect.isfunction(obj) and not name.startswith("_"):
            return obj
    return None


def load_all():
    global _tools, _agents
    _tools.clear()
    _agents.clear()

    base_dir = get_base_dir()

    # 1. Load Shadow Agents from agents/
    agents_dir = base_dir / "agents"
    if agents_dir.exists():
        # Ensure agents package path is loaded in sys.path
        if str(agents_dir.parent) not in sys.path:
            sys.path.insert(0, str(agents_dir.parent))

        try:
            import agents
            importlib.reload(agents)
        except Exception:
            pass

        # Scan folder for agents
        for _, module_name, _ in pkgutil.iter_modules([str(agents_dir)]):
            if module_name in ("base_agent", "_llm", "coordinator"):
                continue
            try:
                module = importlib.import_module(f"agents.{module_name}")
                importlib.reload(module)

                from agents.base_agent import ShadowAgent
                for name, obj in inspect.getmembers(module):
                    if inspect.isclass(obj) and issubclass(obj, ShadowAgent) and obj is not ShadowAgent:
                        inst = obj()
                        _agents[inst.NAME.upper()] = inst
            except Exception as e:
                print(f"[Registry] ⚠️ Failed to load agent {module_name}: {e}")

    # 2. Load Actions from actions/
    actions_dir = base_dir / "actions"
    if actions_dir.exists():
        if str(actions_dir.parent) not in sys.path:
            sys.path.insert(0, str(actions_dir.parent))

        for _, module_name, _ in pkgutil.iter_modules([str(actions_dir)]):
            try:
                module = importlib.import_module(f"actions.{module_name}")
                importlib.reload(module)

                func = find_tool_func(module, module_name)
                if not func:
                    continue

                # Look for TOOL_DECLARATION in file
                decl = getattr(module, "TOOL_DECLARATION", None)
                if decl is None and hasattr(module, "get_tool_declaration"):
                    decl = module.get_tool_declaration()

                # If missing, try fallback
                if not decl:
                    decl = _BUILTIN_DECLARATIONS.get(module_name)

                # Final fallback
                if not decl:
                    decl = {
                        "name": module_name,
                        "description": func.__doc__ or f"Run action {module_name}.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {},
                            "required": []
                        }
                    }

                tool_name = decl.get("name", module_name)
                _tools[tool_name] = (func, decl)
            except Exception as e:
                print(f"[Registry] ⚠️ Failed to load action {module_name}: {e}")


def get_tool_declarations() -> list[dict]:
    return [decl for _, decl in _tools.values()]


def execute_tool(name: str, parameters: dict, player=None, speak=None) -> str:
    if name not in _tools:
        raise ValueError(f"Tool {name} is not registered.")
    func, _ = _tools[name]
    
    # Inspect arguments to handle different calling conventions dynamically
    sig = inspect.signature(func)
    kwargs = {}
    
    if "parameters" in sig.parameters:
        kwargs["parameters"] = parameters
    else:
        # Pass parameters as flat keyword arguments if function doesn't take 'parameters' dict
        kwargs.update(parameters)

    if "player" in sig.parameters:
        kwargs["player"] = player
    if "speak" in sig.parameters:
        kwargs["speak"] = speak
    if "session_memory" in sig.parameters:
        kwargs["session_memory"] = None
    if "response" in sig.parameters:
        kwargs["response"] = None

    # Strip any kwargs not accepted by the function signature to avoid TypeError
    final_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())}

    return func(**final_kwargs)


def get_agents() -> dict:
    return _agents


def get_agent(name: str):
    return _agents.get(name.upper())


# Initial load at import time
load_all()
