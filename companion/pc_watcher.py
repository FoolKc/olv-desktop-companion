"""Windows helpers shared by the bridge and the MCP server.

No extra packages needed (ctypes only). Pillow is needed only for screenshots and is
imported only when capture_screen_jpeg() is called.

- get_active_window_info(): the window in front; titles of private windows are dropped
- idle_seconds(): seconds since the keyboard or mouse was last used
- system_load(): CPU and RAM percentage
- capture_screen_jpeg(): a downscaled screenshot of the main screen (JPEG bytes)
"""
import ctypes
import ctypes.wintypes as wt
import io
import os
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetTickCount.restype = wt.DWORD

# A window whose title contains one of these counts as "private": its title is never sent to
# the LLM and no screenshot is taken while it is in front. Add words for your language, bank,
# mail and chat apps.
PRIVATE_WORDS = (
    "password", "passcode", "login", "log in", "sign in", "2fa", "authenticator", "inprivate",
    "incognito", "private browsing", "bank", "paypal", "wallet", "crypto", "gmail", "outlook",
    "mail", "whatsapp", "telegram", "signal", "messenger", "instagram", "1password", "bitwarden",
    "keepass", "lastpass",
)
PRIVATE_EXES = {
    "keepass.exe", "keepassxc.exe", "bitwarden.exe", "1password.exe", "whatsapp.exe",
    "telegram.exe", "signal.exe", "outlook.exe", "olk.exe",
}

# exe name -> (readable name, category). The category helps the character react (work / game / browser / chat / music / other).
APPS = {
    "code.exe": ("VS Code", "work"),
    "unity.exe": ("Unity", "work"),
    "unity hub.exe": ("Unity Hub", "work"),
    "aseprite.exe": ("Aseprite", "work"),
    "devenv.exe": ("Visual Studio", "work"),
    "rider64.exe": ("Rider", "work"),
    "blender.exe": ("Blender", "work"),
    "windowsterminal.exe": ("Terminal", "work"),
    "powershell.exe": ("PowerShell", "work"),
    "zen.exe": ("Zen Browser", "browser"),
    "firefox.exe": ("Firefox", "browser"),
    "chrome.exe": ("Chrome", "browser"),
    "msedge.exe": ("Edge", "browser"),
    "discord.exe": ("Discord", "chat"),
    "spotify.exe": ("Spotify", "music"),
    "steam.exe": ("Steam", "game"),
    "steamwebhelper.exe": ("Steam", "game"),
    "explorer.exe": ("File Explorer", "other"),
    "notepad.exe": ("Notepad", "other"),
}


def _exe_name(pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def is_private(title: str, exe: str) -> bool:
    low = (title or "").casefold()
    return exe.casefold() in PRIVATE_EXES or any(w in low for w in PRIVATE_WORDS)


def get_active_window_info() -> dict:
    """{"app", "exe", "category", "title", "private"}; title is empty for private windows."""
    try:
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title, exe = buf.value.strip(), _exe_name(pid.value)
    except Exception:
        title, exe = "", ""
    app, category = APPS.get(exe.casefold(), (os.path.splitext(exe)[0] or "unknown", "other"))
    private = is_private(title, exe)
    return {"app": app, "exe": exe, "category": category,
            "title": "" if private else title[:120], "private": private}


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    """Seconds since the keyboard or mouse was last used (0 if unknown)."""
    info = _LastInput(ctypes.sizeof(_LastInput), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _cpu_times():
    idle, kern, usr = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(usr))
    as_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return as_int(idle), as_int(kern) + as_int(usr)   # kernel time includes idle time


def system_load() -> dict:
    """{"cpu": percent, "ram": percent}; measures for 0.2 s."""
    idle1, total1 = _cpu_times()
    time.sleep(0.2)
    idle2, total2 = _cpu_times()
    busy = total2 - total1
    cpu = round(100.0 * (1 - (idle2 - idle1) / busy), 1) if busy else 0.0
    mem = _MemStatus()
    mem.dwLength = ctypes.sizeof(_MemStatus)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    return {"cpu": max(0.0, cpu), "ram": float(mem.dwMemoryLoad)}


def capture_screen_jpeg(max_width: int = 1024, quality: int = 70) -> bytes:
    """Screenshot of the main screen, at most max_width wide, as JPEG bytes. Needs Pillow.
    A smaller image means fewer tokens and a faster upload; text is still readable at 1024 px."""
    from PIL import ImageGrab   # imported here only: everything else works without Pillow
    img = ImageGrab.grab()
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
