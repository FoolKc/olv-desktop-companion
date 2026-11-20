"""Desktop companion MCP server for Open-LLM-VTuber (Windows).

Open-LLM-VTuber starts this file as a separate process (stdio) for each session.
Note: stdout is the MCP channel, so never print() here.

Tools: get_watching, control_video, play_on_youtube, spotify, inspect_pc_state,
look_at_screen (off by default), launch_app, manage_workspace_file, remember_fact.
Tool descriptions only say what each tool does; how the character talks about results
belongs in your prompt (prompts/tool_rules.txt).

Safety (enforced in code, not only in the prompt):
- While the character answers a bridge event (a turn the user did not start), tools that
  change anything refuse to run.
- Files: only inside one folder (COMPANION_WORKSPACE, default Desktop\\Companion), only
  listed extensions, no "..", no drive letters, no links out of the folder.
- Deleting, overwriting, writing code files and saving permanent notes always open a
  Windows confirmation box. Only the user can click Yes; no answer within 25 s means No.
- "Untrusted" time: for 2 minutes after reading a video, a file or the screen, and for
  30 minutes after any bridge event (event text stays in the chat history), every write
  needs confirmation too.
- Titles of private windows (passwords, bank, mail, chat) are never sent, and no
  screenshot is taken while such a window is in front.

Environment variables (all optional):
  COMPANION_WORKSPACE   folder for manage_workspace_file
  COMPANION_CARD        user card file for remember_fact (default: prompts/user_card.md)
  COMPANION_SCREENSHOT  "1" turns look_at_screen on. With OpenAI-compatible models it only
                        works after applying olv_patch (see README).
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

# python -I does not put the script folder on sys.path; add it so pc_watcher.py is found.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage

from pc_watcher import _exe_name, capture_screen_jpeg, get_active_window_info, system_load

BRIDGE = "http://127.0.0.1:12450"

ENABLE_SCREENSHOT = os.environ.get("COMPANION_SCREENSHOT") == "1"
TAINT_S = 120                 # writes need confirmation for 2 min after reading a video, file or screen
TAINT_EVENT_S = 1800          # ...and for 30 min after any bridge event
CONFIRM_TIMEOUT_S = 25        # no answer in 25 s means No (Open-LLM-VTuber waits at most 30 s for a tool)

TEXT_EXT = {".txt", ".md", ".json", ".csv", ".log", ".yaml", ".yml", ".ini", ".xml"}
CODE_EXT = {".py", ".cs", ".js", ".ts", ".html", ".css", ".shader", ".lua"}   # writing these ALWAYS needs confirmation
MAX_WRITE_CHARS = 100_000
MAX_READ_BYTES = 1_000_000

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_tainted_until = 0.0

mcp = FastMCP("desktop-companion")

BLOCKED = ("Not allowed in this turn: it was started by the watch-along system, not by the user. "
           "Do not retry; just reply without tools.")


# ---------------- Helpers ----------------
def _bridge(path: str, body: dict | None = None, timeout: float = 3):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BRIDGE + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def _turn_info() -> dict:
    try:
        return _bridge("/turn", timeout=2)
    except Exception:
        return {}                               # bridge not running: no events either


def _system_turn() -> bool:
    return bool(_turn_info().get("system_turn"))


def _is_tainted() -> bool:
    if time.time() < _tainted_until:
        return True
    ago = _turn_info().get("last_event_ago_s")
    return ago is not None and ago < TAINT_EVENT_S


def _safe(value) -> str:
    """Text from the video: make sure it cannot open fake tags or fake events (same as the bridge)."""
    text = " ".join(str(value or "").split())
    return text.replace("<", "(").replace(">", ")").replace("[", "(").replace("]", ")")


def _taint() -> None:
    global _tainted_until
    _tainted_until = time.time() + TAINT_S


def _confirm(title: str, text: str) -> bool:
    """Windows confirmation box. Only the user can click Yes; no answer means No."""
    box = ctypes.windll.user32.MessageBoxTimeoutW
    box.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT, wt.WORD, wt.DWORD]
    box.restype = ctypes.c_int
    flags = 0x4 | 0x30 | 0x100 | 0x1000 | 0x10000 | 0x40000   # Yes/No, warning, default No, on top
    return box(None, text, title, flags, 0, CONFIRM_TIMEOUT_S * 1000) == 6   # 6 = Yes


def _wait_result(cid: int, tries: int) -> str | None:
    for _ in range(tries):
        time.sleep(0.5)
        try:
            result = _bridge(f"/command/result?id={cid}", timeout=2).get("result")
        except Exception:
            result = None
        if result:
            return result
    return None


def _send_command(body: dict) -> tuple[dict | None, str | None]:
    try:
        r = _bridge("/command", body)
    except Exception:
        return None, "The watch bridge is not running, so nothing happened in the browser."
    if not r.get("ok"):
        err = r.get("error", "")
        if "not connected" in err:
            return None, "The browser extension is not connected, so nothing happened in the browser."
        return None, f"Could not send the command: {err}"
    return r, None


# ---------------- Watching and playback ----------------
def _mmss(sec: int) -> str:
    """Seconds as 23:33 or 1:02:05."""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@mcp.tool()
def get_watching() -> str:
    """Get what the user is watching or listening to in their web browser right now: site, title,
    channel, playing or paused, how far into the video they are, the part/song playing now, the
    description and the subtitles on screen. Call it every time they ask about the song or part
    playing now (e.g. "this song", "what do you think of this one"), because in a long mix the song
    changes and the chat history only shows what was playing earlier."""
    try:
        s = _bridge("/state", timeout=2)
    except Exception:
        return "The watch bridge is not running, so it is unknown what the user is watching."
    if not s.get("watching"):
        return "The user is not watching anything in the browser right now."

    lines = [f"Title: {_safe(s.get('title'))}"]
    if s.get("site"):
        lines.append(f"Site: {_safe(s['site'])}")
    if s.get("channel"):
        lines.append(f"Channel: {_safe(s['channel'])}")
    if s.get("paused") is not None:
        lines.append("Status: paused" if s["paused"] else "Status: playing")
    if isinstance(s.get("position"), int):
        lines.append(f"Position: {_mmss(s['position'])}" + (f" of {_mmss(s['duration'])}" if s.get("duration") else ""))
    data = []
    if s.get("chapter"):
        data.append(f"PLAYING NOW (current chapter / song of this video): {_safe(s['chapter'])}. "
                    "The description may list many songs; only this one is playing right now.")
    if s.get("description"):
        data.append(f"Description: {_safe(s['description'])}")
    if s.get("subtitle"):
        data.append(f"Subtitles on screen (most recent last): {_safe(s['subtitle'])}")
    if data:
        lines += ["<video_data>", *data, "</video_data>"]
        _taint()
    else:
        lines.append("Subtitles: none (captions are off or nobody is talking).")
    age_min = s.get("age_seconds", 0) // 60
    if age_min >= 30:
        lines.append(f"(Last update was {age_min} minutes ago; they may have stopped watching.)")
    return "\n".join(lines)


@mcp.tool()
def control_video(action: str) -> str:
    """Control the video playing in the user's browser.
    action: "pause", "play" (start or resume what is already open), "next", "previous",
    "louder" (volume up) or "quieter" (volume down)."""
    if _system_turn():
        return BLOCKED
    action = (action or "").strip().lower()
    aliases = {"resume": "play", "continue": "play", "start": "play", "stop": "pause", "skip": "next",
               "back": "previous", "prev": "previous", "up": "louder", "volume_up": "louder",
               "down": "quieter", "volume_down": "quieter"}
    action = aliases.get(action, action)
    r, err = _send_command({"action": action})
    if err:
        return err
    result = _wait_result(r["id"], 12)
    return f"Result: {result}." if result else "The command was sent, but the browser did not confirm it yet."


@mcp.tool()
def play_on_youtube(query: str) -> str:
    """Search YouTube in the user's browser and play the first result.
    query: what to search, for example "artist name song title"."""
    if _system_turn():
        return BLOCKED
    query = " ".join((query or "").split())[:150]
    if not query:
        return "No search query was given."
    r, err = _send_command({"action": "search", "query": query})
    if err:
        return err
    result = _wait_result(r["id"], 30)
    return f"Result: {result}." if result else f"Searched YouTube for {query}, but the browser did not confirm what is playing yet."


# ---------------- Spotify ----------------
# Sends media keys to the Spotify desktop app's window, so it works while Spotify is minimized
# or in the tray, without touching other players. What is playing comes from the window title.
WM_APPCOMMAND = 0x0319
_APPCMD = {"toggle": 14, "next": 11, "previous": 12}     # APPCOMMAND_MEDIA_PLAY_PAUSE / NEXTTRACK / PREVIOUSTRACK
_IDLE_TITLES = {"spotify", "spotify premium", "spotify free", ""}
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_u32.EnumWindows.argtypes = [_ENUMPROC, wt.LPARAM]
_u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]


def _spotify_window():
    """(hwnd, title) of Spotify's main window, even if hidden in the tray, or (None, "")."""
    found = []

    def cb(hwnd, _):
        pid = wt.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_name(pid.value).casefold() != "spotify.exe":
            return True
        cls = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(hwnd, cls, 64)
        if not cls.value.startswith("Chrome_WidgetWin"):
            return True
        title = ctypes.create_unicode_buffer(300)
        _u32.GetWindowTextW(hwnd, title, 300)
        if title.value:
            found.append((hwnd, title.value))
        return True

    _u32.EnumWindows(_ENUMPROC(cb), 0)
    return found[0] if found else (None, "")


def _spotify_status(title: str) -> str:
    if title.strip().casefold() in _IDLE_TITLES:
        return "Spotify is paused (or nothing is playing)."
    return f"Spotify is playing: {_safe(title)[:150]}."


@mcp.tool()
def spotify(action: str) -> str:
    """Control the Spotify desktop app on the user's PC.
    action: "play", "pause", "next", "previous" or "status" (what is playing now).
    Use this when they say Spotify, or when the music is playing on Spotify; for videos in the browser use control_video.
    If Spotify is not open, open it with launch_app first."""
    action = (action or "").strip().lower()
    action = {"resume": "play", "continue": "play", "start": "play", "stop": "pause", "skip": "next",
              "back": "previous", "prev": "previous", "now_playing": "status", "song": "status"}.get(action, action)
    if action not in ("play", "pause", "next", "previous", "status"):
        return "Invalid action. Use play, pause, next, previous or status."
    if action != "status" and _system_turn():
        return BLOCKED
    hwnd, title = _spotify_window()
    if not hwnd:
        return "Spotify is not open."
    if action == "status":
        return _spotify_status(title)
    paused = title.strip().casefold() in _IDLE_TITLES
    if (action == "play" and not paused) or (action == "pause" and paused):
        return "Nothing to do. " + _spotify_status(title)
    cmd = _APPCMD["toggle" if action in ("play", "pause") else action]
    _u32.PostMessageW(hwnd, WM_APPCOMMAND, 0, cmd << 16)
    time.sleep(1.2)                              # give Spotify time to update its title
    before, (_, title) = title, _spotify_window()
    if action == "previous" and title == before:   # after a few seconds, the first press only restarts the song
        _u32.PostMessageW(hwnd, WM_APPCOMMAND, 0, cmd << 16)
        time.sleep(1.2)
        _, title = _spotify_window()
    return "Done. " + _spotify_status(title)


# ---------------- PC ----------------
@mcp.tool()
def inspect_pc_state() -> str:
    """See which app the user has in front on their PC, plus CPU and RAM usage."""
    win = get_active_window_info()
    load = system_load()
    if win["private"]:
        window = f"{win['app']} (a private window; do not ask about it)"
    else:
        window = f"{win['app']}" + (f", window title: {_safe(win['title'])}" if win["title"] else "")
    return f"Active app: {window}\nCategory: {win['category']}\nCPU: {load['cpu']}%, RAM: {load['ram']}%"


def look_at_screen():
    """Take a small screenshot of the user's main screen and see it.
    Use only when the user asks you to look at their screen, an error, or what they are doing or drawing."""
    if _system_turn():
        return BLOCKED
    if get_active_window_info()["private"]:
        return "A private window (passwords, bank, mail or chat) is in front, so no screenshot was taken."
    data = capture_screen_jpeg(max_width=1024)
    _taint()                                    # text on screen can be written by strangers too
    return MCPImage(data=data, format="jpeg")


if ENABLE_SCREENSHOT:
    mcp.tool()(look_at_screen)


# Open-LLM-VTuber starts MCP servers with a minimal environment: %ProgramFiles% is missing. Fill in the usual places.
_WIN_DEFAULTS = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
                 "SystemRoot": r"C:\Windows", "LOCALAPPDATA": os.path.join(os.path.expanduser("~"), "AppData", "Local")}


def _expand(path: str) -> str:
    for key, default in _WIN_DEFAULTS.items():
        path = path.replace(f"%{key}%", os.environ.get(key) or default)
    return os.path.expandvars(path)


def _exe(*candidates: str) -> str | None:
    for c in candidates:
        path = _expand(c)
        if os.path.isfile(path):
            return path
    return None


def _uri_registered(scheme: str) -> bool:
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme))
        return True
    except OSError:
        return False


# name -> ("exe", possible paths) or ("uri", scheme, address to open). No shell is ever used. Edit to taste.
APPS = {
    "unity": ("exe", [r"%ProgramFiles%\Unity Hub\Unity Hub.exe"]),
    "aseprite": ("exe", [r"%ProgramFiles%\Aseprite\Aseprite.exe",
                         r"%ProgramFiles(x86)%\Steam\steamapps\common\Aseprite\Aseprite.exe"]),
    "vscode": ("exe", [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
                       r"%ProgramFiles%\Microsoft VS Code\Code.exe"]),
    "spotify": ("uri", "spotify", "spotify:"),
    "discord": ("uri", "discord", "discord://"),
    "steam": ("uri", "steam", "steam://open/main"),
    "notepad": ("exe", [r"%SystemRoot%\System32\notepad.exe"]),
    "explorer": ("exe", [r"%SystemRoot%\explorer.exe"]),
}


@mcp.tool()
def launch_app(app_name: str) -> str:
    """Open an app on the user's PC.
    app_name: unity, aseprite, vscode, spotify, discord, steam, notepad or explorer."""
    if _system_turn():
        return BLOCKED
    key = (app_name or "").strip().lower().replace(" ", "")
    key = {"code": "vscode", "visualstudiocode": "vscode", "unityhub": "unity"}.get(key, key)
    entry = APPS.get(key)
    if not entry:
        return f"'{app_name}' is not on the allowed list: {', '.join(APPS)}."
    if entry[0] == "exe":
        target = _exe(*entry[1])
        if not target:
            return f"{key} was not found on this PC, so it was not opened."
    else:
        if not _uri_registered(entry[1]):
            return f"{key} does not seem to be installed, so it was not opened."
        target = entry[2]
    try:
        os.startfile(target)
        return f"Opened {key}."
    except OSError as e:
        return f"Could not open {key}: {e}"


# ---------------- Files (one folder only) ----------------
def _desktop() -> str:
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:   # right place even if moved to OneDrive
        return buf.value
    return os.path.join(os.path.expanduser("~"), "Desktop")


WORKSPACE = os.environ.get("COMPANION_WORKSPACE") or os.path.join(_desktop(), "Companion")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _resolve(filename: str) -> tuple[str | None, str | None]:
    """Safe full path, or (None, error)."""
    name = (filename or "").strip().replace("/", "\\")
    parts = [p for p in name.split("\\") if p]
    bad = (not parts or ":" in name or name.startswith("\\")
           or any(p in (".", "..") or p.endswith((" ", ".")) or p.split(".")[0].lower() in _RESERVED for p in parts))
    if bad:
        return None, "Invalid file name. Use a simple name like notes.txt or ideas\\game.md."
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext not in TEXT_EXT | CODE_EXT:
        return None, f"File type '{ext or 'none'}' is not allowed. Allowed: {', '.join(sorted(TEXT_EXT | CODE_EXT))}."
    os.makedirs(WORKSPACE, exist_ok=True)
    root = os.path.realpath(WORKSPACE)
    target = os.path.realpath(os.path.join(root, *parts))   # follow links/junctions to the real place
    try:
        inside = os.path.normcase(os.path.commonpath([root, target])) == os.path.normcase(root)
    except ValueError:                                        # different drive
        inside = False
    if not inside or target == root:
        return None, "Access denied: only files inside the companion folder."
    return target, None


@mcp.tool()
def manage_workspace_file(action: str, filename: str = "", content: str = "") -> str:
    """List, read, write or delete text files in the companion folder.
    action: "list", "read", "write" or "delete". filename: e.g. "notes.txt" or "ideas/game.md".
    Deleting, overwriting and some writes open a confirmation box on the user's screen; the user answers it."""
    if _system_turn():
        return BLOCKED
    action = (action or "").strip().lower()

    if action == "list":
        os.makedirs(WORKSPACE, exist_ok=True)
        files = []
        for base, _, names in os.walk(WORKSPACE):   # does not walk into linked folders
            for n in names:
                full = os.path.join(base, n)
                if os.path.islink(full) or os.path.splitext(n)[1].lower() not in TEXT_EXT | CODE_EXT:
                    continue
                files.append(os.path.relpath(full, WORKSPACE))
        return "Files in the companion folder: " + (", ".join(sorted(files)[:100]) if files else "none yet.")

    target, err = _resolve(filename)
    if err:
        return err
    shown = os.path.relpath(target, os.path.realpath(WORKSPACE))

    if action == "read":
        if not os.path.isfile(target):
            return f"File '{shown}' not found."
        if os.path.getsize(target) > MAX_READ_BYTES:
            return f"File '{shown}' is too big to read."
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(4000)
        _taint()
        text = text.replace("</file_data", "(/file_data")
        return f"<file_data name=\"{shown}\">\n{text}\n</file_data>"

    if action == "write":
        if len(content) > MAX_WRITE_CHARS:
            return f"Content is too long (max {MAX_WRITE_CHARS} characters)."
        exists = os.path.exists(target)
        ext = os.path.splitext(target)[1].lower()
        if exists or ext in CODE_EXT or _is_tainted():
            preview = content[:400] + ("..." if len(content) > 400 else "")
            verb = "OVERWRITE" if exists else "create"
            if not _confirm("Companion - write file", f"The companion wants to {verb}:\n{shown}\n\n{preview}\n\nAllow?"):
                return "The user said no in the confirmation box (or it timed out). Nothing was written."
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _write_atomic(target, content)
        return f"Saved '{shown}'."

    if action == "delete":
        if not os.path.isfile(target):
            return f"File '{shown}' does not exist."
        if not _confirm("Companion - delete file", f"The companion wants to PERMANENTLY DELETE:\n{shown}\n\nAllow?"):
            return "The user said no in the confirmation box (or it timed out). Nothing was deleted."
        os.remove(target)
        return f"Deleted '{shown}'."

    return "Invalid action. Use 'list', 'read', 'write' or 'delete'."


def _write_atomic(path: str, text: str) -> None:
    """Write via a new temporary file next to path, then swap it in.
    mkstemp creates a brand-new file (O_EXCL), so a link planted under a fixed temp name
    cannot redirect the write outside the folder."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".companion_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------- User card ----------------
CARD = os.environ.get("COMPANION_CARD") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "user_card.md")
LEARNED_HEADER = "Things you learned about the user:"
MAX_LEARNED = 40
CARD_MAX_CHARS = 4000          # the patch puts only this much of the card into the prompt (keep both the same)


@mcp.tool()
def remember_fact(fact: str) -> str:
    """Save one short, lasting fact about the user to your permanent notes, so you know it in every
    future chat. The user confirms it in a box on their screen. Use when they tell you something worth
    keeping (a preference, a project, a dislike) or say "remember this".
    fact: one sentence, for example: They do not like horror games."""
    if _system_turn():
        return BLOCKED
    fact = _safe(fact).strip(" -")[:200]
    if len(fact) < 3:
        return "Nothing to remember."
    try:
        with open(CARD, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    if LEARNED_HEADER not in text:
        text = text.rstrip() + f"\n\n{LEARNED_HEADER}\n"
    head, learned = text.split(LEARNED_HEADER, 1)
    lines = [l for l in learned.splitlines() if l.strip().startswith("- ")]
    if any(fact.casefold() == l.strip()[2:].casefold() for l in lines):
        return "You already knew that."
    if len(lines) >= MAX_LEARNED:
        return f"Your notes are full ({MAX_LEARNED} facts). Ask the user to clean up their user card file."
    new_text = head.rstrip() + f"\n\n{LEARNED_HEADER}\n" + "\n".join(lines + [f"- {fact}"]) + "\n"
    if len(new_text.strip()) > CARD_MAX_CHARS:
        return (f"Your notes card is full (about {CARD_MAX_CHARS} characters fit into your instructions). "
                "Ask the user to shorten their user card file. Nothing was saved.")
    if not _confirm("Companion - permanent note", f"The companion wants to remember this forever:\n{fact}\n\nAllow?"):
        return "The user said no in the confirmation box (or it timed out). Nothing was saved."
    _write_atomic(CARD, new_text)
    return f"Saved to your permanent notes: {fact}"


if __name__ == "__main__":
    mcp.run()
