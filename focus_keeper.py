# -*- coding: utf-8 -*-
"""
Focus Keeper — держит несколько окон "в фокусе" (для локального кооператива
в двух экземплярах игры, например через Sandboxie Plus).

Как работает:
  1. Окно "клавиатура+мышь" получает НАСТОЯЩИЙ передний план (оно реально в фокусе).
  2. Окно с геймпадом дополнительно ПАТЧИТСЯ: GetForegroundWindow/GetFocus/GetActiveWindow
     в процессе игры всегда возвращают его окно. В окне клава+мышь XInput отключается.
     Патчи снимаются при СТОП / выходе.
     Остальные окна получают ПОДДЕЛЬНЫЙ фокус: программа постоянно шлёт им
     сообщения WM_ACTIVATE / WM_ACTIVATEAPP / WM_NCACTIVATE / WM_SETFOCUS,
     поэтому игра считает, что окно активно, и не ставит себя на паузу.
  3. Геймпады показываются в реальном времени (XInput), у каждого окна своё
     назначенное устройство.

Горячие клавиши (работают глобально):
  F8 — включить / выключить удержание фокуса
  F9 — поменять устройства местами (клава/мышь <-> геймпад)

Только Windows. Запускать лучше от имени администратора
(иначе нельзя слать сообщения окнам, запущенным с повышенными правами).
"""
import os
import sys
import re
import json
import struct
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox

if os.name != "nt":
    print("Эта программа работает только в Windows.")
    sys.exit(1)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
dwmapi = ctypes.windll.dwmapi

# ---------- сигнатуры WinAPI (важно для 64-бит) ----------
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

WM_ACTIVATE, WM_SETFOCUS, WM_NCACTIVATE, WM_ACTIVATEAPP = 0x0006, 0x0007, 0x0086, 0x001C
WA_ACTIVE = 1
SW_SHOWNOACTIVATE = 4

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "focus_keeper_config.json")


# ---------- работа с окнами ----------
def is_admin():
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def get_exe(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return "?"


def clean_title(t):
    """Убирает маркеры Sandboxie вида [#] в начале/конце заголовка."""
    t = re.sub(r"^\s*\[[^\]]*\]\s*", "", t)
    t = re.sub(r"\s*\[[^\]]*\]\s*$", "", t)
    return t.strip()


def list_windows():
    result = []
    my_pid = os.getpid()

    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            cloaked = ctypes.c_int(0)
            dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            pid = wintypes.DWORD(0)
            tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == my_pid:
                return True
            result.append({"hwnd": int(hwnd), "title": buf.value, "pid": pid.value,
                           "tid": tid, "exe": get_exe(pid.value)})
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return result


def force_foreground(hwnd):
    """Надёжно выводит окно на передний план."""
    if (user32.GetForegroundWindow() or 0) == hwnd:
        return True
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    if (user32.GetForegroundWindow() or 0) == hwnd:
        return True
    cur = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow() or 0
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    user32.keybd_event(0x12, 0, 0, 0)  # нажать/отпустить ALT — снимает блокировку
    user32.keybd_event(0x12, 0, 2, 0)
    attached = []
    for t in (fg_tid, tgt_tid):
        if t and t != cur and user32.AttachThreadInput(cur, t, True):
            attached.append(t)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    for t in attached:
        user32.AttachThreadInput(cur, t, False)
    return (user32.GetForegroundWindow() or 0) == hwnd


def spoof_focus(hwnd, tid):
    """Говорит окну, что оно активно и в фокусе."""
    user32.PostMessageW(hwnd, WM_NCACTIVATE, 1, 0)
    user32.PostMessageW(hwnd, WM_ACTIVATEAPP, 1, tid)
    user32.PostMessageW(hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
    user32.PostMessageW(hwnd, WM_SETFOCUS, 0, 0)


# ---------- патч API внутри процесса игры ----------
# Подменяет GetForegroundWindow / GetActiveWindow / GetFocus в процессе игры,
# чтобы они всегда возвращали её окно (игра "думает", что в фокусе).
# Для окна клава+мышь отключает XInput (геймпад им не управляет).
# Оригинальные байты сохраняются и возвращаются при остановке.
psapi = ctypes.windll.psapi
_VP = ctypes.c_void_p
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, _VP, _VP, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, _VP, _VP, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.VirtualProtectEx.argtypes = [wintypes.HANDLE, _VP, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, _VP, ctypes.c_size_t]
kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
kernel32.LoadLibraryW.restype = wintypes.HMODULE
kernel32.GetModuleHandleExW.argtypes = [wintypes.DWORD, _VP, ctypes.POINTER(wintypes.HMODULE)]
kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
_GetProcAddress = ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.HMODULE, ctypes.c_void_p)(("GetProcAddress", kernel32))

FOCUS_FUNCS = [("user32.dll", b"GetForegroundWindow"), ("user32.dll", b"GetActiveWindow"),
               ("user32.dll", b"GetFocus")]
XINPUT_DLLS = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")
PAD_FUNCS = [(d, f) for d in XINPUT_DLLS for f in (b"XInputGetState", 100, b"XInputGetCapabilities")]
PAD_CODE = b"\xB8\x8F\x04\x00\x00\xC3"  # mov eax, ERROR_DEVICE_NOT_CONNECTED; ret


def focus_code(hwnd):
    return b"\xB8" + struct.pack("<I", hwnd & 0xFFFFFFFF) + b"\xC3"  # mov eax, hwnd; ret


def remote_modules(h):
    arr = (wintypes.HMODULE * 2048)()
    need = wintypes.DWORD(0)
    if not psapi.EnumProcessModulesEx(h, arr, ctypes.sizeof(arr), ctypes.byref(need), 3):
        return {}
    res = {}
    buf = ctypes.create_unicode_buffer(520)
    for i in range(min(need.value // ctypes.sizeof(wintypes.HMODULE), 2048)):
        if arr[i] and psapi.GetModuleFileNameExW(h, arr[i], buf, 520):
            res[os.path.basename(buf.value).lower()] = arr[i]
    return res


def resolve_remote(mods, dll, func):
    """Адрес функции в ЧУЖОМ процессе (база модуля там + смещение у нас)."""
    local = kernel32.LoadLibraryW(dll)
    if not local:
        return None
    p = _GetProcAddress(local, func)
    if not p:
        return None
    hmod = wintypes.HMODULE(0)
    if not kernel32.GetModuleHandleExW(0x6, p, ctypes.byref(hmod)) or not hmod.value:
        return None
    buf = ctypes.create_unicode_buffer(520)
    if not kernel32.GetModuleFileNameW(hmod, buf, 520):
        return None
    rbase = mods.get(os.path.basename(buf.value).lower())
    if not rbase:
        return None
    return rbase + (p - hmod.value)


def read_remote(h, addr, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, addr, buf, n, ctypes.byref(got)) and got.value == n:
        return buf.raw
    return None


def write_remote(h, addr, data):
    old = wintypes.DWORD(0)
    if not kernel32.VirtualProtectEx(h, addr, len(data), 0x40, ctypes.byref(old)):
        return False
    wrote = ctypes.c_size_t(0)
    ok = kernel32.WriteProcessMemory(h, addr, data, len(data), ctypes.byref(wrote))
    kernel32.VirtualProtectEx(h, addr, len(data), old.value, ctypes.byref(old))
    kernel32.FlushInstructionCache(h, addr, len(data))
    return bool(ok) and wrote.value == len(data)


class Patcher:
    def __init__(self):
        self.saved = {}  # pid -> {addr: (оригинальные байты, метка)}

    def apply(self, pid, hwnd, focus, block_pad):
        """Возвращает (готово, отчёт). готово=False — попробовать позже."""
        if sys.maxsize <= 2 ** 32:
            return True, "нужен 64-битный Python"
        h = kernel32.OpenProcess(0x0438, False, pid)
        if not h:
            return False, "нет доступа к процессу игры (нужны права администратора)"
        try:
            w = wintypes.BOOL(0)
            kernel32.IsWow64Process(h, ctypes.byref(w))
            if w.value:
                return True, "игра 32-битная — патч не поддерживается"
            mods = remote_modules(h)
            if not mods:
                return False, "не удалось прочитать модули процесса"
            n = {"focus": 0, "pad": 0}
            wanted = []
            if focus:
                wanted += [("focus", d, f, focus_code(hwnd)) for d, f in FOCUS_FUNCS]
            if block_pad:
                wanted += [("pad", d, f, PAD_CODE) for d, f in PAD_FUNCS if d in mods]
            saved = self.saved.setdefault(pid, {})
            for label, dll, fn, code in wanted:
                addr = resolve_remote(mods, dll, fn)
                if not addr:
                    continue
                if addr not in saved:
                    orig = read_remote(h, addr, len(code))
                    if orig is None:
                        continue
                    saved[addr] = (orig, label)
                if write_remote(h, addr, code):
                    n[label] += 1
            parts, done = [], True
            if focus:
                parts.append(f"фокус-патч: {n['focus']} ф-ций" + ("" if n["focus"] else " ✖"))
            if block_pad:
                if n["pad"]:
                    parts.append(f"XInput отключён ({n['pad']})")
                else:
                    parts.append("XInput в игре ещё не загружен")
                    done = False
            return done, "; ".join(parts)
        finally:
            kernel32.CloseHandle(h)

    def restore(self, pid):
        saved = self.saved.pop(pid, None)
        if not saved:
            return
        h = kernel32.OpenProcess(0x0438, False, pid)
        if not h:
            return
        try:
            for addr, (orig, _label) in saved.items():
                write_remote(h, addr, orig)
        finally:
            kernel32.CloseHandle(h)


INPUT_KEYS = ("xinput", "dinput", "hid.dll", "gaming", "gameinput", "steam_api", "gameoverlay",
              "sdl", "unityplayer", "gameassembly", "mono", "openvr", "rawinput")


# ---------- XInput (геймпады) ----------
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


_xinput = None
for _name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
    try:
        _xinput = ctypes.WinDLL(_name)
        break
    except OSError:
        continue

BUTTONS = {0x0001: "↑", 0x0002: "↓", 0x0004: "←", 0x0008: "→", 0x0010: "Start", 0x0020: "Back",
           0x0040: "L3", 0x0080: "R3", 0x0100: "LB", 0x0200: "RB", 0x1000: "A", 0x2000: "B",
           0x4000: "X", 0x8000: "Y"}


def pad_state(index):
    """None если геймпад не подключён, иначе строка с нажатыми кнопками."""
    if _xinput is None:
        return None
    st = XINPUT_STATE()
    if _xinput.XInputGetState(index, ctypes.byref(st)) != 0:
        return None
    g = st.Gamepad
    pressed = [n for m, n in BUTTONS.items() if g.wButtons & m]
    if g.bLeftTrigger > 60:
        pressed.append("LT")
    if g.bRightTrigger > 60:
        pressed.append("RT")
    if abs(g.sThumbLX) > 12000 or abs(g.sThumbLY) > 12000:
        pressed.append("LStick")
    if abs(g.sThumbRX) > 12000 or abs(g.sThumbRY) > 12000:
        pressed.append("RStick")
    return " ".join(pressed)


# ---------- интерфейс ----------
DEVICES = ["Клавиатура + мышь", "Геймпад 1", "Геймпад 2", "Геймпад 3", "Геймпад 4", "Без устройства"]
KBM = DEVICES[0]

BG, PANEL, FG, ACCENT, GOOD, BAD, MUTED = "#15171c", "#1f2229", "#e6e8ee", "#4f8cff", "#3ddc84", "#ff5d5d", "#8b90a0"


class Slot:
    def __init__(self, idx, device):
        self.idx = idx
        self.hwnd = 0
        self.tid = 0
        self.title = ""
        self.exe = ""
        self.pid = 0
        self.device = tk.StringVar(value=device)
        self.win_text = tk.StringVar(value="окно не выбрано")
        self.state_text = tk.StringVar(value="—")
        self.patch_key = None
        self.patch_info = ""
        self.next_try = 0
        self.last_report = ""


class App:
    def __init__(self, root):
        self.root = root
        root.title("Focus Keeper")
        root.geometry("1050x800")
        root.minsize(900, 640)
        root.configure(bg=BG)
        self.running = False
        self.windows = {}
        self.slots = [Slot(0, KBM), Slot(1, "Геймпад 1")]
        self.interval = tk.IntVar(value=250)
        self.opt_spoof = tk.BooleanVar(value=True)
        self.opt_grab = tk.BooleanVar(value=True)
        self.opt_restore = tk.BooleanVar(value=True)
        self.opt_patch_focus = tk.BooleanVar(value=True)
        self.opt_block_pad = tk.BooleanVar(value=True)
        self.patcher = Patcher()
        self.tick_n = 0
        self.filter_var = tk.StringVar()
        self.status = tk.StringVar(value="ОСТАНОВЛЕНО")
        self.keystate = {0x77: False, 0x78: False}
        self.rematch_counter = 0
        self.pad_labels = []

        self.style()
        self.build()
        self.load_config()
        self.refresh()
        self.rematch_all()
        self.log("Готово. Выбери окна справа, назначь устройства и нажми «СТАРТ» (или F8).")
        if _xinput is None:
            self.log("XInput не найден — отображение геймпадов недоступно.")
        if not is_admin():
            self.log("⚠ Нет прав администратора — окна с повышенными правами не получат сообщения.")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()
        self.poll_pads()
        self.poll_keys()

    # --- оформление ---
    def style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Card.TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("H.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 15))
        s.configure("CardH.TLabel", background=PANEL, foreground=ACCENT, font=("Segoe UI Semibold", 12))
        s.configure("TButton", background="#2b2f3a", foreground=FG, borderwidth=0, padding=6)
        s.map("TButton", background=[("active", "#3a4050")])
        s.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Segoe UI Semibold", 12), padding=10)
        s.map("Accent.TButton", background=[("active", "#6aa0ff")])
        s.configure("TCheckbutton", background=PANEL, foreground=FG)
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=24, borderwidth=0)
        s.configure("Treeview.Heading", background="#2b2f3a", foreground=FG, borderwidth=0)
        s.map("Treeview", background=[("selected", ACCENT)])
        s.configure("TCombobox", fieldbackground="#2b2f3a", background="#2b2f3a", foreground=FG, arrowcolor=FG)
        s.configure("TSpinbox", fieldbackground="#2b2f3a", foreground=FG, arrowcolor=FG)
        s.configure("TEntry", fieldbackground="#2b2f3a", foreground=FG)

    def build(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(top, text="🎮 Focus Keeper", style="H.TLabel").pack(side="left")
        adm = "администратор ✔" if is_admin() else "обычные права ✖"
        ttk.Label(top, text=f"  ({adm})", foreground=GOOD if is_admin() else BAD).pack(side="left")
        if not is_admin():
            ttk.Button(top, text="Перезапустить от администратора", command=self.relaunch_admin).pack(side="left", padx=10)
        self.status_lbl = ttk.Label(top, textvariable=self.status, foreground=BAD, font=("Segoe UI Semibold", 12))
        self.status_lbl.pack(side="right")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=14, pady=6)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        # левая панель — список окон
        left = ttk.Frame(body, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(left, text="Открытые окна", style="CardH.TLabel").pack(anchor="w", padx=10, pady=(10, 4))
        fr = ttk.Frame(left, style="Card.TFrame")
        fr.pack(fill="x", padx=10)
        ttk.Entry(fr, textvariable=self.filter_var).pack(side="left", fill="x", expand=True)
        self.filter_var.trace_add("write", lambda *a: self.refresh())
        ttk.Button(fr, text="⟳ Обновить", command=self.refresh).pack(side="left", padx=(6, 0))
        ttk.Button(fr, text="Автопоиск игры", command=self.auto_detect).pack(side="left", padx=(6, 0))

        cols = ("title", "exe", "pid")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("title", "Заголовок", 330), ("exe", "Процесс", 130), ("pid", "PID", 60)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("game", foreground=GOOD)
        self.tree.pack(fill="both", expand=True, padx=10, pady=8)
        self.tree.bind("<Double-1>", lambda e: self.assign_to_free())
        bf = ttk.Frame(left, style="Card.TFrame")
        bf.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bf, text="→ Окно 1", command=lambda: self.assign_selected(0)).pack(side="left")
        ttk.Button(bf, text="→ Окно 2", command=lambda: self.assign_selected(1)).pack(side="left", padx=6)
        ttk.Label(bf, text="(двойной клик — в первый свободный слот)", style="Muted.TLabel").pack(side="left", padx=6)

        # правая панель — слоты и настройки
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        for s in self.slots:
            self.slot_card(right, s)

        opt = ttk.Frame(right, style="Card.TFrame")
        opt.pack(fill="x", pady=(8, 0))
        ttk.Label(opt, text="Настройки", style="CardH.TLabel").pack(anchor="w", padx=10, pady=(10, 4))
        ttk.Checkbutton(opt, text="Поддельный фокус для окон без реального фокуса", variable=self.opt_spoof).pack(anchor="w", padx=10)
        ttk.Checkbutton(opt, text="Вернуть фокус окну «клава+мышь», если он ушёл на другое окно игры", variable=self.opt_grab).pack(anchor="w", padx=10)
        ttk.Checkbutton(opt, text="Разворачивать свёрнутые окна", variable=self.opt_restore).pack(anchor="w", padx=10)
        ttk.Checkbutton(opt, text="ПАТЧ игры: окно с геймпадом всегда «в фокусе»", variable=self.opt_patch_focus).pack(anchor="w", padx=10)
        ttk.Checkbutton(opt, text="ПАТЧ игры: окно клава+мышь НЕ видит геймпад", variable=self.opt_block_pad).pack(anchor="w", padx=10)
        f = ttk.Frame(opt, style="Card.TFrame")
        f.pack(fill="x", padx=10, pady=(6, 10))
        ttk.Label(f, text="Интервал, мс:", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(f, from_=50, to=2000, increment=50, width=7, textvariable=self.interval).pack(side="left", padx=8)

        pads = ttk.Frame(right, style="Card.TFrame")
        pads.pack(fill="x", pady=(8, 0))
        ttk.Label(pads, text="Геймпады (XInput)", style="CardH.TLabel").pack(anchor="w", padx=10, pady=(10, 4))
        for i in range(4):
            l = ttk.Label(pads, text="", style="Card.TLabel")
            l.pack(anchor="w", padx=10)
            self.pad_labels.append(l)
        ttk.Label(pads, text="").pack()

        # низ
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=14, pady=(4, 12))
        self.btn = ttk.Button(bottom, text="▶  СТАРТ  (F8)", style="Accent.TButton", command=self.toggle)
        self.btn.pack(side="left")
        ttk.Button(bottom, text="⇄ Поменять устройства (F9)", command=self.swap).pack(side="left", padx=10)
        ttk.Button(bottom, text="🔍 Диагностика", command=self.diagnose).pack(side="left")
        self.logbox = tk.Text(bottom, height=8, bg=PANEL, fg=MUTED, bd=0, font=("Consolas", 9), state="disabled")
        self.logbox.pack(side="left", fill="x", expand=True, padx=(10, 0))

    def slot_card(self, parent, s):
        card = ttk.Frame(parent, style="Card.TFrame")
        card.pack(fill="x", pady=(0, 8))
        ttk.Label(card, text=f"Окно {s.idx + 1}", style="CardH.TLabel").pack(anchor="w", padx=10, pady=(10, 2))
        ttk.Label(card, textvariable=s.win_text, style="Card.TLabel", wraplength=340).pack(anchor="w", padx=10)
        ttk.Label(card, textvariable=s.state_text, style="Muted.TLabel").pack(anchor="w", padx=10)
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", padx=10, pady=(6, 10))
        ttk.Label(row, text="Устройство:", style="Card.TLabel").pack(side="left")
        cb = ttk.Combobox(row, values=DEVICES, textvariable=s.device, state="readonly", width=20)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", lambda e, sl=s: self.device_changed(sl))
        ttk.Button(row, text="Сброс", command=lambda sl=s: self.clear_slot(sl)).pack(side="left")

    # --- утилиты ---
    def log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def relaunch_admin(self):
        try:
            script = os.path.abspath(sys.argv[0])
            shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}"', None, 1)
            self.root.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    # --- список окон ---
    def refresh(self):
        flt = self.filter_var.get().strip().lower()
        self.windows = {w["hwnd"]: w for w in list_windows()}
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for hwnd, w in sorted(self.windows.items(), key=lambda kv: kv[1]["title"].lower()):
            if flt and flt not in w["title"].lower() and flt not in w["exe"].lower():
                continue
            game = "gambling" in w["title"].lower() or "gambling" in w["exe"].lower()
            self.tree.insert("", "end", iid=str(hwnd), values=(w["title"], w["exe"], w["pid"]),
                             tags=("game",) if game else ())
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def auto_detect(self):
        self.refresh()
        found = [w for w in self.windows.values()
                 if "gambling" in w["title"].lower() or "gambling" in w["exe"].lower()]
        if len(found) < 2:
            self.log(f"Автопоиск: найдено окон игры — {len(found)}. Запусти оба экземпляра и повтори.")
            if not found:
                return
        for i, w in enumerate(found[:2]):
            self.assign(self.slots[i], w)
        self.slots[0].device.set(KBM)
        self.slots[1].device.set("Геймпад 1")

    def assign_selected(self, idx):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Выбор окна", "Сначала выбери окно в списке слева.")
            return
        self.assign(self.slots[idx], self.windows.get(int(sel[0])))

    def assign_to_free(self):
        sel = self.tree.selection()
        if not sel:
            return
        free = next((s for s in self.slots if not s.hwnd), self.slots[0])
        self.assign(free, self.windows.get(int(sel[0])))

    def assign(self, slot, w):
        if not w:
            return
        for o in self.slots:
            if o is not slot and o.hwnd == w["hwnd"]:
                self.clear_slot(o)
        slot.hwnd, slot.tid, slot.pid = w["hwnd"], w["tid"], w["pid"]
        slot.title, slot.exe = clean_title(w["title"]), w["exe"]
        slot.win_text.set(f"{w['title']}  [{w['exe']}, PID {w['pid']}]")
        slot.state_text.set("● назначено")
        self.log(f"Окно {slot.idx + 1} ← {w['title']} (PID {w['pid']})")

    def clear_slot(self, slot):
        if slot.pid:
            self.patcher.restore(slot.pid)
        slot.patch_key = None
        slot.patch_info = ""
        slot.hwnd = slot.tid = slot.pid = 0
        slot.win_text.set("окно не выбрано")
        slot.state_text.set("—")

    def device_changed(self, slot):
        other = self.slots[1 - slot.idx]
        d = slot.device.get()
        if d != "Без устройства" and other.device.get() == d:
            self.log(f"⚠ «{d}» назначено сразу двум окнам — это скорее всего не то, что нужно.")

    def swap(self):
        a, b = self.slots[0].device.get(), self.slots[1].device.get()
        self.slots[0].device.set(b)
        self.slots[1].device.set(a)
        self.log(f"Устройства поменяны: окно 1 → {b}, окно 2 → {a}")

    # --- переподбор потерянных окон ---
    def rematch_all(self):
        wins = list_windows()
        used = {s.hwnd for s in self.slots if s.hwnd and user32.IsWindow(s.hwnd)}
        for s in self.slots:
            if s.hwnd and user32.IsWindow(s.hwnd):
                continue
            if not s.exe or not s.title:
                continue
            for w in wins:
                if w["hwnd"] in used:
                    continue
                if w["exe"].lower() == s.exe.lower() and s.title.lower() in clean_title(w["title"]).lower():
                    self.assign(s, w)
                    used.add(w["hwnd"])
                    break

    # --- главный цикл ---
    def toggle(self):
        self.running = not self.running
        if self.running:
            if not any(s.hwnd for s in self.slots):
                self.log("Не выбрано ни одного окна.")
                self.running = False
                return
            self.status.set("РАБОТАЕТ")
            self.status_lbl.configure(foreground=GOOD)
            self.btn.configure(text="■  СТОП  (F8)")
            self.log("Удержание фокуса включено.")
        else:
            self.status.set("ОСТАНОВЛЕНО")
            self.status_lbl.configure(foreground=BAD)
            self.btn.configure(text="▶  СТАРТ  (F8)")
            self.restore_all()
            self.log("Удержание фокуса выключено (патчи сняты).")

    def tick(self):
        self.tick_n += 1
        try:
            self.rematch_counter += 1
            if self.rematch_counter % 8 == 0:
                for s in self.slots:
                    if s.hwnd and not user32.IsWindow(s.hwnd):
                        s.hwnd = 0
                        s.state_text.set("✖ окно закрыто — ищу заново…")
                if any((not s.hwnd) and s.exe for s in self.slots):
                    self.rematch_all()
            if self.running:
                self.keep_focus()
        except Exception as e:
            self.log(f"Ошибка: {e}")
        try:
            ms = max(50, int(self.interval.get()))
        except Exception:
            ms = 250
        self.root.after(ms, self.tick)

    def keep_focus(self):
        fg = user32.GetForegroundWindow() or 0
        live = [s for s in self.slots if s.hwnd and user32.IsWindow(s.hwnd)]
        self.sync_patches()
        if self.opt_restore.get():
            for s in live:
                if user32.IsIconic(s.hwnd):
                    user32.ShowWindow(s.hwnd, SW_SHOWNOACTIVATE)
        kb = next((s for s in live if s.device.get() == KBM), None)
        if kb and self.opt_grab.get():
            others = [s.hwnd for s in live if s is not kb]
            if fg in others:
                force_foreground(kb.hwnd)
                fg = user32.GetForegroundWindow() or 0
        for s in live:
            real = (s.hwnd == fg)
            if self.opt_spoof.get() and not real:
                spoof_focus(s.hwnd, s.tid)
            txt = ("● реальный фокус" if real else
                   ("● фокус (поддельный)" if self.opt_spoof.get() else "● без фокуса"))
            s.state_text.set(txt + (f"  |  {s.patch_info}" if s.patch_info else ""))

    def diagnose(self):
        """Показывает, какие библиотеки ввода загружены в процессах игры."""
        report = []
        for s in self.slots:
            if not s.pid or not user32.IsWindow(s.hwnd):
                self.log(f"Окно {s.idx + 1}: не назначено")
                continue
            h = kernel32.OpenProcess(0x0438, False, s.pid)
            if not h:
                self.log(f"Окно {s.idx + 1}: нет доступа к процессу")
                continue
            try:
                names = sorted(remote_modules(h))
            finally:
                kernel32.CloseHandle(h)
            hits = [n for n in names if any(k in n for k in INPUT_KEYS)]
            self.log(f"Окно {s.idx + 1} (PID {s.pid}): " + (", ".join(hits) or "ничего подходящего"))
            report.append(f"== Окно {s.idx + 1}, PID {s.pid}, {s.exe} ==\n" + "\n".join(names))
        if report:
            path = os.path.join(os.path.dirname(CONFIG_PATH), "focus_keeper_modules.txt")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n\n".join(report))
                self.log(f"Полный список модулей: {path}")
            except Exception as e:
                self.log(f"Не удалось записать файл: {e}")

    def sync_patches(self):
        pad_present = any(o.device.get().startswith("Геймпад") for o in self.slots)
        for s in self.slots:
            if not s.hwnd or not user32.IsWindow(s.hwnd):
                continue
            is_kbm = s.device.get() == KBM
            want_focus = self.opt_patch_focus.get() and not is_kbm
            want_pad = self.opt_block_pad.get() and is_kbm and pad_present
            key = (s.pid, s.hwnd, want_focus, want_pad)
            if key == s.patch_key or self.tick_n < s.next_try:
                continue
            self.patcher.restore(s.pid)  # чистый лист, затем применяем нужное
            s.patch_info = ""
            if not (want_focus or want_pad):
                s.patch_key = key
                continue
            done, rep = self.patcher.apply(s.pid, s.hwnd, want_focus, want_pad)
            s.patch_info = rep
            if rep != s.last_report:
                self.log(f"Окно {s.idx + 1}: {rep}")
                s.last_report = rep
            if done:
                s.patch_key = key
            else:
                s.patch_key = None
                s.next_try = self.tick_n + 12

    def restore_all(self):
        for s in self.slots:
            if s.pid:
                self.patcher.restore(s.pid)
            s.patch_key = None
            s.patch_info = ""
            s.last_report = ""

    def poll_pads(self):
        for i, lbl in enumerate(self.pad_labels):
            st = pad_state(i)
            owners = [f"окно {s.idx + 1}" for s in self.slots if s.device.get() == f"Геймпад {i + 1}"]
            who = f"  → {', '.join(owners)}" if owners else ""
            if st is None:
                lbl.configure(text=f"○ Геймпад {i + 1}: не подключён{who}", foreground=MUTED)
            else:
                lbl.configure(text=f"● Геймпад {i + 1}: {st or 'подключён'}{who}", foreground=GOOD)
        self.root.after(100, self.poll_pads)

    def poll_keys(self):
        for vk, fn in ((0x77, self.toggle), (0x78, self.swap)):
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not self.keystate[vk]:
                fn()
            self.keystate[vk] = down
        self.root.after(40, self.poll_keys)

    # --- конфиг ---
    def load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for s, c in zip(self.slots, cfg.get("slots", [])):
                s.title, s.exe = c.get("title", ""), c.get("exe", "")
                s.device.set(c.get("device", s.device.get()))
                if s.title:
                    s.win_text.set(f"(поиск) {s.title} [{s.exe}]")
            self.interval.set(cfg.get("interval", 250))
            self.opt_spoof.set(cfg.get("spoof", True))
            self.opt_grab.set(cfg.get("grab", True))
            self.opt_restore.set(cfg.get("restore", True))
            self.opt_patch_focus.set(cfg.get("patch_focus", True))
            self.opt_block_pad.set(cfg.get("block_pad", True))
        except Exception:
            pass

    def on_close(self):
        self.restore_all()
        try:
            cfg = {"slots": [{"title": s.title, "exe": s.exe, "device": s.device.get()} for s in self.slots],
                   "interval": self.interval.get(), "spoof": self.opt_spoof.get(),
                   "grab": self.opt_grab.get(), "restore": self.opt_restore.get(),
                   "patch_focus": self.opt_patch_focus.get(), "block_pad": self.opt_block_pad.get()}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()