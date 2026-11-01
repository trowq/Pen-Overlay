# -*- coding: utf-8 -*-
"""
pen_overlay.py - рисует PNG-картинку стилуса в том месте экрана, где находится перо,
и поворачивает её в ту сторону, куда наклонено перо.

Позиция и наклон берутся прямо из данных планшета (HID), а не от системного курсора:
в программах с Windows Ink курсор Windows за пером не двигается.

Windows 10/11, Python 3.8+, библиотека Pillow (pip install pillow).

Запуск:
    python pen_overlay.py                  # берёт stylus.png рядом со скриптом
    python pen_overlay.py my.png           # или другой файл
    python pen_overlay.py --size 400       # длина картинки на экране в пикселях (по умолчанию 260)
    python pen_overlay.py --debug          # печатает данные пера (если что-то не работает)

Горячие клавиши (из любого окна):
    Ctrl+Alt+Q  - выход

Калибровка (нужна, только если картинка смещена относительно кончика пера):
    Ctrl+Alt+1  - поставьте перо в ЛЕВЫЙ ВЕРХНИЙ угол экрана и нажмите
    Ctrl+Alt+2  - поставьте перо в ПРАВЫЙ НИЖНИЙ угол экрана и нажмите
    Ctrl+Alt+0  - сбросить калибровку
"""
import sys
import os
import math
import time
import json
import struct
import ctypes
import argparse
import shutil
import subprocess
import traceback
from functools import lru_cache
from ctypes import wintypes as wt

from PIL import Image, ImageDraw

# ============================ НАСТРОЙКИ ============================
IMAGE_NAME = "stylus.png"
TRAY_ICON_NAME = "banner.png"   # значок в области уведомлений (скрытые значки), PNG 400x400, лежит рядом со скриптом

# Куда направлена задняя часть стилуса на вашей картинке, считая от кончика.
# Кончик слева сверху, задняя часть справа снизу -> 45
# (угол по часовой стрелке от направления "вправо": 0=вправо, 90=вниз, 180=влево, 270=вверх).
BODY_DIRECTION_DEG = 45

SPRITE_LENGTH_PX = 260  # большие картинки уменьшаются до такой длины (None = не уменьшать)
SCALE = 1.0             # дополнительный множитель размера
TIP_POINT = None        # (x, y) кончика на исходной картинке; None = найти автоматически
INVERT_X = False        # True = зеркально по горизонтали (меняется только здесь, горячей клавиши нет)
INVERT_Y = False        # True = зеркально по вертикали (меняется только здесь, горячей клавиши нет)
MIN_TILT_DEG = 1.5      # при почти вертикальном пере направление не определено - картинка не дёргается
SMOOTHING_TAU = 0.07    # сглаживание наклона, секунды: больше = плавнее, но с задержкой (0 = без сглаживания)
VERTICAL_ZONE_DEG = 35.0   # наклон, начиная с которого поворот ничем не гасится
VERTICAL_DAMP_TAU = 0.6    # у совсем вертикального пера поворот отстаёт на столько секунд (0 = выкл.)
                           # чем вертикальнее перо, тем сильнее гасится качание влево-вправо
POS_FOLLOW_CURSOR = True  # если данные пера пропали (Krita и т.п.), но системный курсор двигается - идти за ним

# Наклон -> точка на круге: направление наклона = куда уходит задняя часть стилуса,
# величина наклона = как далеко от кончика она находится (длина стилуса на экране).
STRETCH_BY_TILT = False  # False = всегда полная длина, меняется только направление
MAX_TILT_DEG = 60.0     # наклон, при котором стилус показывается в полную длину
MIN_LENGTH_FRAC = 0.2   # минимальная длина (доля от полной) при почти вертикальном пере
LENGTH_STEPS = 24       # сколько градаций длины (больше = плавнее, но больше памяти)
HIDE_AFTER_SEC = 3.0    # стилус появляется, когда перо работает, и пропадает через столько секунд без данных пера (0 = виден всегда)
RENDER_INTERVAL = 1 / 120.0
CALIB_FILE = "pen_overlay_calib.json"
CONFIG_FILE = "pen_overlay_settings.json"   # выбранный скин
SKINS_DIR = "skins"                          # папка со скинами (PNG), создаётся сама
UI_DIR = "ui"                                # графика окна скинов (рамка, кнопки, обводки, шрифт PenHand.ttf)
# ===================================================================


# ---------- Обычные функции (без Windows API) ----------

def placeholder_sprite():
    """Простая картинка-заглушка, если stylus.png не найден. Кончик - слева сверху."""
    img = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.line([(26, 26), (270, 270)], fill=(60, 60, 70, 255), width=22)
    d.line([(60, 60), (250, 250)], fill=(110, 110, 125, 255), width=8)
    d.polygon([(2, 2), (36, 16), (16, 36)], fill=(230, 80, 60, 255))
    return img


def find_tip(img, threshold=20):
    """Ближайший к левому верхнему углу непрозрачный пиксель = кончик."""
    w, h = img.size
    px = img.getchannel("A").load()
    for s in range(w + h - 1):
        for x in range(max(0, s - h + 1), min(s, w - 1) + 1):
            if px[x, s - x] > threshold:
                return x, s - x
    return 0, 0


def prepare_sprite(img, target_len=None, scale=1.0, tip_point=None):
    """Обрезает пустые поля, уменьшает до разумного размера, находит кончик."""
    img = img.convert("RGBA")
    ox = oy = 0
    bbox = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox:
        img = img.crop(bbox)
        ox, oy = bbox[0], bbox[1]
    w, h = img.size
    f = 1.0
    if target_len and max(w, h) > target_len:
        f = target_len / float(max(w, h))
    f *= scale
    if abs(f - 1.0) > 1e-6:
        img = img.resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)
    if tip_point:
        tip = (round((tip_point[0] - ox) * f), round((tip_point[1] - oy) * f))
        tip = (min(max(tip[0], 0), img.width - 1), min(max(tip[1], 0), img.height - 1))
    else:
        tip = find_tip(img)
    return img, tip


def render_tile(sprite_pm, tip, phi_deg, k=1.0, body_deg=0.0):
    """Поворачивает картинку (в формате RGBa) на phi_deg градусов против часовой стрелки
    вокруг кончика и сжимает вдоль оси стилуса в k раз (k=1 - полная длина).
    body_deg - направление оси стилуса на исходной картинке (по часовой от 'вправо').
    Возвращает маленький кадр и положение кончика в нём (ox, oy)."""
    w, h = sprite_pm.size
    tx, ty = tip[0] + 0.5, tip[1] + 0.5
    c, s = math.cos(math.radians(phi_deg)), math.sin(math.radians(phi_deg))
    ux, uy = math.cos(math.radians(body_deg)), math.sin(math.radians(body_deg))
    k = max(0.02, k)

    def stretch(f):  # A = I + (f-1) * u*uT
        g = f - 1.0
        return (1 + g * ux * ux, g * ux * uy, g * ux * uy, 1 + g * uy * uy)

    a00, a01, a10, a11 = stretch(k)          # прямое преобразование (сжатие)
    i00, i01, i10, i11 = stretch(1.0 / k)    # обратное
    # прямое: F = Rot(phi) * A ; обратное: M = A^-1 * Rot(-phi)
    f00, f01 = c * a00 + s * a10, c * a01 + s * a11
    f10, f11 = -s * a00 + c * a10, -s * a01 + c * a11
    m00, m01 = i00 * c + i01 * s, -i00 * s + i01 * c
    m10, m11 = i10 * c + i11 * s, -i10 * s + i11 * c

    xs, ys = [], []
    for cx, cy in ((0, 0), (w, 0), (0, h), (w, h)):
        rx, ry = cx - tx, cy - ty
        xs.append(f00 * rx + f01 * ry)
        ys.append(f10 * rx + f11 * ry)
    ox = max(0, math.ceil(-min(xs) - 0.5)) + 1
    oy = max(0, math.ceil(-min(ys) - 0.5)) + 1
    tw = int(math.ceil(ox + 0.5 + max(xs))) + 2
    th = int(math.ceil(oy + 0.5 + max(ys))) + 2
    data = (m00, m01, tx - (m00 * (ox + 0.5) + m01 * (oy + 0.5)),
            m10, m11, ty - (m10 * (ox + 0.5) + m11 * (oy + 0.5)))
    return sprite_pm.transform((tw, th), Image.AFFINE, data, resample=Image.BILINEAR), ox, oy


def tilt_to_point(tilt_x_deg, tilt_y_deg, max_tilt=60.0):
    """Наклон пера -> точка на круге: (угол по часовой от 'вправо', расстояние 0..1).
    Центр круга - кончик пера, расстояние 1 = наклон max_tilt и больше."""
    tx = math.tan(math.radians(max(-89.0, min(89.0, tilt_x_deg))))
    ty = math.tan(math.radians(max(-89.0, min(89.0, tilt_y_deg))))
    alpha = math.degrees(math.atan(math.hypot(tx, ty)))   # полный угол от вертикали
    return math.degrees(math.atan2(ty, tx)), min(1.0, alpha / max_tilt), alpha


def tile_buffer_size(sprite, tip):
    """Сторона квадратного буфера, в который поместится кадр при любом повороте."""
    w, h = sprite.size
    r = math.ceil(max(math.hypot(cx - tip[0], cy - tip[1]) for cx in (0, w) for cy in (0, h)))
    return 2 * r + 12


def body_angle(tilt_x_deg, tilt_y_deg):
    """Угол (по часовой стрелке от 'вправо'), в какую сторону на экране уходит задняя часть пера."""
    tx = math.tan(math.radians(max(-89.0, min(89.0, tilt_x_deg))))
    ty = math.tan(math.radians(max(-89.0, min(89.0, tilt_y_deg))))
    return math.degrees(math.atan2(ty, tx))


def signed_value(v, bits, lmin):
    """HID отдаёт число без знака; если логический минимум отрицательный - восстанавливаем знак."""
    if lmin < 0 and bits and v >= (1 << (bits - 1)):
        v -= 1 << bits
    return v


def to_degrees(v, c):
    """Сырое значение HID -> градусы наклона."""
    v = signed_value(v, c["bits"], c["lmin"])
    # Некоторые планшеты (XP-Pen и др.) пишут в описании ерунду: логический диапазон
    # симметричный [-127..127], а физический [0..32767] (то есть 0..32.7 градуса).
    # Пересчёт по такой формуле сдвигает "ноль" наклона на ~16 градусов (сырое 0 -> 16.4),
    # из-за чего оба наклона всегда положительны, угол залипает около 45 градусов и
    # картинка поворачивается лишь на ~±25. Если физический диапазон не содержит нуля,
    # а логический содержит - физическому диапазону не верим: сырое значение = градусы.
    phys_ok = c["pmax"] != c["pmin"] and c["lmax"] != c["lmin"]
    if phys_ok and c["lmin"] < 0 < c["lmax"] and not (c["pmin"] < 0 < c["pmax"]):
        phys_ok = False
        return max(-90.0, min(90.0, float(v)))
    if phys_ok:
        d = c["pmin"] + (v - c["lmin"]) * (c["pmax"] - c["pmin"]) / (c["lmax"] - c["lmin"])
        d *= 10 ** c["exp"]
        if c["exp"] == 0 and max(abs(c["pmin"]), abs(c["pmax"])) > 180:
            d /= 100.0
    else:
        d = v * 90.0 / c["lmax"] if c["lmax"] > 90 else float(v)
    return max(-90.0, min(90.0, d))


def to_fraction(v, c):
    """Сырая координата HID -> число от 0 до 1 вдоль оси планшета."""
    v = signed_value(v, c["bits"], c["lmin"])
    span = c["lmax"] - c["lmin"]
    return (v - c["lmin"]) / float(span) if span else 0.0


def map_to_screen(fx, fy, rect, calib=None):
    """Доли планшета (0..1) -> пиксели экрана. rect = (left, top, right, bottom) монитора."""
    l, t, r, b = rect
    u, v = fx, fy
    if calib:
        (x1, y1), (x2, y2) = calib["tl"], calib["br"]
        if x2 != x1:
            u = (fx - x1) / (x2 - x1)
        if y2 != y1:
            v = (fy - y1) / (y2 - y1)
    return int(round(l + u * (r - l - 1))), int(round(t + v * (b - t - 1)))


def damped_angle(cur, target, alpha, dt):
    """Двигает угол cur к target, но тем медленнее, чем вертикальнее перо (меньше alpha).
    У вертикального пера направление наклона неустойчиво - малейшее дрожание руки
    даёт большой поворот, поэтому там картинка почти не качается."""
    w = min(1.0, max(0.0, alpha / VERTICAL_ZONE_DEG))
    tau = VERTICAL_DAMP_TAU * (1.0 - w) ** 2
    if tau < 1e-3:
        return target % 360
    k = 1.0 - math.exp(-max(0.0, dt) / tau)
    diff = ((target - cur + 180.0) % 360.0) - 180.0
    return (cur + k * diff) % 360


class TiltSmoother:
    """Сглаживание вектора наклона (tilt X, tilt Y) по времени. Сглаживается именно вектор,
    а не угол: у почти вертикального пера направление шумит (датчик отдаёт целые градусы),
    а у вектора малой длины шум не раздувается в скачки угла. Нет скачка на границе 360/0."""

    def __init__(self, tau):
        self.tau = tau
        self.x = self.y = None
        self.t = None

    def update(self, x, y, now):
        if self.x is None or self.tau <= 0:
            self.x, self.y = x, y
        else:
            dt = max(0.0, now - self.t)
            k = 1.0 - math.exp(-dt / self.tau)
            self.x += k * (x - self.x)
            self.y += k * (y - self.y)
        self.t = now
        return self.x, self.y


# ===== WIN32 =====
if sys.platform != "win32":
    sys.exit("Эта программа работает только в Windows.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HANDLE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT), ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT), ("NumberInputButtonCaps", wt.USHORT),
                ("NumberInputValueCaps", wt.USHORT), ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT), ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT), ("NumberFeatureButtonCaps", wt.USHORT),
                ("NumberFeatureValueCaps", wt.USHORT), ("NumberFeatureDataIndices", wt.USHORT)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", wt.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wt.USHORT), ("LinkCollection", wt.USHORT), ("LinkUsage", wt.USHORT),
                ("LinkUsagePage", wt.USHORT), ("IsRange", ctypes.c_ubyte),
                ("IsStringRange", ctypes.c_ubyte), ("IsDesignatorRange", ctypes.c_ubyte),
                ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte), ("BitSize", wt.USHORT), ("ReportCount", wt.USHORT),
                ("Reserved2", wt.USHORT * 5), ("UnitsExp", wt.ULONG), ("Units", wt.ULONG),
                ("LogicalMin", wt.LONG), ("LogicalMax", wt.LONG),
                ("PhysicalMin", wt.LONG), ("PhysicalMax", wt.LONG),
                ("Usage0", wt.USHORT), ("Usage1", wt.USHORT), ("Rest", wt.USHORT * 6)]


class POINTER_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("displayOrientation", wt.DWORD), ("device", wt.HANDLE),
                ("pointerDeviceType", ctypes.c_int), ("monitor", wt.HANDLE),
                ("startingCursorId", wt.ULONG), ("maxActiveContacts", wt.USHORT),
                ("productString", wt.WCHAR * 520)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HANDLE), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uTimeoutOrVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64),
                ("dwInfoFlags", wt.DWORD), ("guidItem", ctypes.c_ubyte * 16),
                ("hBalloonIcon", wt.HANDLE)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HANDLE), ("hbmColor", wt.HANDLE)]


def api(lib, name, restype, *argtypes):
    f = getattr(lib, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


P = ctypes.POINTER
V = ctypes.c_void_p
GetModuleHandleW = api(kernel32, "GetModuleHandleW", wt.HANDLE, wt.LPCWSTR)
GetStdHandle = api(kernel32, "GetStdHandle", wt.HANDLE, wt.DWORD)
GetConsoleMode = api(kernel32, "GetConsoleMode", wt.BOOL, wt.HANDLE, P(wt.DWORD))
SetConsoleMode = api(kernel32, "SetConsoleMode", wt.BOOL, wt.HANDLE, wt.DWORD)
RegisterClassW = api(user32, "RegisterClassW", wt.ATOM, P(WNDCLASSW))
CreateWindowExW = api(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      wt.HWND, wt.HANDLE, wt.HANDLE, V)
DefWindowProcW = api(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
PeekMessageW = api(user32, "PeekMessageW", wt.BOOL, P(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
MsgWaitForMultipleObjects = api(user32, "MsgWaitForMultipleObjects", wt.DWORD, wt.DWORD, V,
                                wt.BOOL, wt.DWORD, wt.DWORD)
TranslateMessage = api(user32, "TranslateMessage", wt.BOOL, P(wt.MSG))
DispatchMessageW = api(user32, "DispatchMessageW", LRESULT, P(wt.MSG))
PostQuitMessage = api(user32, "PostQuitMessage", None, ctypes.c_int)
RegisterHotKey = api(user32, "RegisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
RegisterRawInputDevices = api(user32, "RegisterRawInputDevices", wt.BOOL, P(RAWINPUTDEVICE), wt.UINT, wt.UINT)
GetRawInputData = api(user32, "GetRawInputData", wt.UINT, V, wt.UINT, V, P(wt.UINT), wt.UINT)
GetRawInputDeviceInfoW = api(user32, "GetRawInputDeviceInfoW", wt.UINT, wt.HANDLE, wt.UINT, V, P(wt.UINT))
GetCursorPos = api(user32, "GetCursorPos", wt.BOOL, P(wt.POINT))
GetSystemMetrics = api(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetPointerDevices = api(user32, "GetPointerDevices", wt.BOOL, P(wt.UINT), P(POINTER_DEVICE_INFO))
GetPointerDeviceRects = api(user32, "GetPointerDeviceRects", wt.BOOL, wt.HANDLE, P(wt.RECT), P(wt.RECT))
ShowWindow = api(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
SetWindowPos = api(user32, "SetWindowPos", wt.BOOL, wt.HWND, V, ctypes.c_int, ctypes.c_int,
                   ctypes.c_int, ctypes.c_int, wt.UINT)
UpdateLayeredWindow = api(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HANDLE, P(wt.POINT),
                          P(wt.SIZE), wt.HANDLE, P(wt.POINT), wt.DWORD, P(BLENDFUNCTION), wt.DWORD)
GetDC = api(user32, "GetDC", wt.HANDLE, wt.HWND)
CreateCompatibleDC = api(gdi32, "CreateCompatibleDC", wt.HANDLE, wt.HANDLE)
CreateDIBSection = api(gdi32, "CreateDIBSection", wt.HANDLE, wt.HANDLE, V, wt.UINT, P(V), wt.HANDLE, wt.DWORD)
SelectObject = api(gdi32, "SelectObject", wt.HANDLE, wt.HANDLE, wt.HANDLE)
Shell_NotifyIconW = api(shell32, "Shell_NotifyIconW", wt.BOOL, wt.DWORD, P(NOTIFYICONDATAW))
CreatePopupMenu = api(user32, "CreatePopupMenu", wt.HANDLE)
AppendMenuW = api(user32, "AppendMenuW", wt.BOOL, wt.HANDLE, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
TrackPopupMenu = api(user32, "TrackPopupMenu", ctypes.c_int, wt.HANDLE, wt.UINT, ctypes.c_int,
                     ctypes.c_int, ctypes.c_int, wt.HWND, V)
DestroyMenu = api(user32, "DestroyMenu", wt.BOOL, wt.HANDLE)
SetForegroundWindow = api(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
PostMessageW = api(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
RegisterWindowMessageW = api(user32, "RegisterWindowMessageW", wt.UINT, wt.LPCWSTR)
CreateIconIndirect = api(user32, "CreateIconIndirect", wt.HANDLE, P(ICONINFO))
DestroyIcon = api(user32, "DestroyIcon", wt.BOOL, wt.HANDLE)
LoadIconW = api(user32, "LoadIconW", wt.HANDLE, wt.HANDLE, V)
CreateBitmap = api(gdi32, "CreateBitmap", wt.HANDLE, ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, V)
DeleteObject = api(gdi32, "DeleteObject", wt.BOOL, wt.HANDLE)
SendMessageW = api(user32, "SendMessageW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, V)
DestroyWindow = api(user32, "DestroyWindow", wt.BOOL, wt.HWND)
UnregisterHotKey = api(user32, "UnregisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int)
LoadCursorW = api(user32, "LoadCursorW", wt.HANDLE, wt.HANDLE, V)
SetWindowTextW = api(user32, "SetWindowTextW", wt.BOOL, wt.HWND, wt.LPCWSTR)
AdjustWindowRectEx = api(user32, "AdjustWindowRectEx", wt.BOOL, P(wt.RECT), wt.DWORD, wt.BOOL, wt.DWORD)
CreateFontW = api(gdi32, "CreateFontW", wt.HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                  ctypes.c_int, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                  wt.DWORD, wt.LPCWSTR)
DragQueryFileW = api(shell32, "DragQueryFileW", wt.UINT, wt.HANDLE, wt.UINT, wt.LPWSTR, wt.UINT)
DragFinish = api(shell32, "DragFinish", None, wt.HANDLE)
HidP_GetCaps = api(hid, "HidP_GetCaps", ctypes.c_long, V, P(HIDP_CAPS))
HidP_GetValueCaps = api(hid, "HidP_GetValueCaps", ctypes.c_long, ctypes.c_int, V, P(wt.USHORT), V)
HidP_GetUsageValue = api(hid, "HidP_GetUsageValue", ctypes.c_long, ctypes.c_int, wt.USHORT, wt.USHORT,
                         wt.USHORT, P(wt.ULONG), V, V, wt.ULONG)

WM_DESTROY, WM_INPUT, WM_HOTKEY, WM_QUIT = 0x0002, 0x00FF, 0x0312, 0x0012
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
ULW_ALPHA = 2
PM_REMOVE, QS_ALLINPUT = 1, 0x04FF
RID_INPUT, RIM_TYPEHID = 0x10000003, 2
RIDI_PREPARSEDDATA, RIDI_DEVICENAME = 0x20000005, 0x20000007
RIDEV_INPUTSINK = 0x100
HIDP_STATUS_SUCCESS = 0x110000
PAGE_GENERIC, USAGE_X, USAGE_Y = 0x01, 0x30, 0x31
PAGE_DIGITIZER, USAGE_TILT_X, USAGE_TILT_Y = 0x0D, 0x3D, 0x3E
HDR_SIZE = ctypes.sizeof(RAWINPUTHEADER)
WM_NULL, WM_LBUTTONUP, WM_RBUTTONUP = 0x0000, 0x0202, 0x0205
WM_TRAY = 0x0401                      # WM_USER + 1: сообщения от значка в трее
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_BOTTOMALIGN = 0x0, 0x2, 0x100, 0x20
SM_CXSMICON = 49
MENU_EXIT, MENU_SETTINGS, MENU_RELOAD = 1, 2, 3
MF_SEPARATOR = 0x800
WM_COMMAND, WM_CLOSE, WM_SETFONT, WM_DROPFILES = 0x0111, 0x0010, 0x0030, 0x0233
LB_RESETCONTENT, LB_ADDSTRING, LB_SETCURSEL, LB_GETCURSEL = 0x0184, 0x0180, 0x0186, 0x0188
LBN_SELCHANGE = 1
ID_LIST, ID_DELETE, ID_STATUS = 101, 102, 103
SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
LOG_FILE = "pen_overlay_log.txt"
LOGF = None
_print = print


def log(*args):
    """Печать в консоль и (в режиме --debug) в файл pen_overlay_log.txt."""
    msg = " ".join(str(a) for a in args)
    _print(msg)
    if LOGF:
        try:
            LOGF.write(msg + "\n")
            LOGF.flush()
        except OSError:
            pass


class State:
    fx = fy = 0.0          # положение пера на планшете, доли 0..1
    have_pos = False
    tilt_x = tilt_y = 0.0
    theta = 0.0            # угол, куда смотрит задняя часть пера
    length = 1.0           # длина стилуса, доля 0..1 (по величине наклона)
    offset = 0             # доворот картинки в градусах (горячей клавиши нет)
    last_seen = 0.0
    seen = False
    shown = False
    last_key = None
    last_top = 0.0
    last_debug = 0.0
    debug = False
    invert_x = INVERT_X
    invert_y = INVERT_Y
    rect = (0, 0, 1920, 1080)
    calib = None
    pending = {}           # углы, записанные при калибровке
    drawn_key = None       # (угол, длина), кадр которых сейчас лежит в буфере
    stretch = STRETCH_BY_TILT
    demo = False           # тест отрисовки: Ctrl+Alt+T
    last_stats = 0.0
    stats = None
    smoother = TiltSmoother(SMOOTHING_TAU)
    stale = False
    restart = False        # выход нужен для перезапуска (Reload)
    skin = ""              # ключ текущего скина: "" = стандартный stylus.png, иначе имя файла в skins/
    skin_keys = []         # ключи скинов в том же порядке, что и строки списка
    size_px = None
    scale = 1.0
    hbmp = None
    settings_win = None    # окно скинов (skin_window.SkinWindow)
    font = None
    settings_class = False
    hicon = None           # значок в трее
    taskbar_msg = 0        # сообщение "панель задач перезапущена"
    tray_added = False
    theta_t = None         # время последнего обновления угла
    cur_at_hid = None      # положение системного курсора в момент последних данных пера
    last_xy = (0, 0)       # последняя позиция пера на экране


S = State()
devices = {}


def device_name(hdev):
    size = wt.UINT(0)
    GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, None, ctypes.byref(size))
    if not size.value:
        return "?"
    buf = ctypes.create_unicode_buffer(size.value)
    GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, buf, ctypes.byref(size))
    return buf.value


def get_device(hdev):
    """Один раз разбирает описание данных устройства: ищет координаты и наклон."""
    if hdev in devices:
        return devices[hdev]
    dev = {"pp": None, "tilt": [], "pos": [], "all": []}
    devices[hdev] = dev
    size = wt.UINT(0)
    GetRawInputDeviceInfoW(hdev, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
    if size.value:
        buf = ctypes.create_string_buffer(size.value)
        if GetRawInputDeviceInfoW(hdev, RIDI_PREPARSEDDATA, buf, ctypes.byref(size)) != 0xFFFFFFFF:
            dev["pp"] = buf
            caps = HIDP_CAPS()
            if HidP_GetCaps(buf, ctypes.byref(caps)) == HIDP_STATUS_SUCCESS and caps.NumberInputValueCaps:
                n = wt.USHORT(caps.NumberInputValueCaps)
                arr = (HIDP_VALUE_CAPS * n.value)()
                if HidP_GetValueCaps(0, arr, ctypes.byref(n), buf) == HIDP_STATUS_SUCCESS:
                    for vc in arr[:n.value]:
                        usages = range(vc.Usage0, vc.Usage1 + 1) if vc.IsRange else [vc.Usage0]
                        for u in usages:
                            dev["all"].append((vc.UsagePage, u))
                            e = vc.UnitsExp & 0xF
                            cap = {"usage": u, "page": vc.UsagePage, "link": vc.LinkCollection,
                                   "bits": vc.BitSize, "lmin": vc.LogicalMin, "lmax": vc.LogicalMax,
                                   "pmin": vc.PhysicalMin, "pmax": vc.PhysicalMax,
                                   "exp": e - 16 if e > 7 else e,
                                   # перо/стилус в приоритете над пальцем и т.п.
                                   "pref": 0 if (vc.LinkUsagePage == PAGE_DIGITIZER and
                                                 vc.LinkUsage in (0x02, 0x20)) else 1}
                            if vc.UsagePage == PAGE_DIGITIZER and u in (USAGE_TILT_X, USAGE_TILT_Y):
                                dev["tilt"].append(cap)
                            elif vc.UsagePage == PAGE_GENERIC and u in (USAGE_X, USAGE_Y):
                                dev["pos"].append(cap)
    dev["tilt"].sort(key=lambda c: c["pref"])
    dev["pos"].sort(key=lambda c: c["pref"])
    if S.debug:
        log("\nУстройство:", device_name(hdev))
        for title, key in (("координаты", "pos"), ("наклон", "tilt")):
            if not dev[key]:
                log("  %s: НЕ найдены" % title)
            for c in dev[key]:
                log("  %s usage 0x%X: бит=%d логич=[%d..%d] физич=[%d..%d] exp=%d" % (
                    title, c["usage"], c["bits"], c["lmin"], c["lmax"], c["pmin"], c["pmax"], c["exp"]))
        if not dev["tilt"] or not dev["pos"]:
            log("  Все значения (страница/usage):",
                  ", ".join("0x%X/0x%X" % pu for pu in dev["all"]) or "нет")
    return dev


def read_cap(dev, c, rep, n):
    v = wt.ULONG()
    if HidP_GetUsageValue(0, c["page"], c["link"], c["usage"], ctypes.byref(v),
                          dev["pp"], rep, n) != HIDP_STATUS_SUCCESS:
        return None
    return v.value


def handle_raw_input(lparam):
    size = wt.UINT(0)
    GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), HDR_SIZE)
    if not size.value:
        return
    buf = ctypes.create_string_buffer(size.value)
    if GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), HDR_SIZE) != size.value:
        return
    hdr = RAWINPUTHEADER.from_buffer(buf)
    if hdr.dwType != RIM_TYPEHID:
        return
    raw = buf.raw
    size_hid, count = struct.unpack_from("<II", raw, HDR_SIZE)
    base = HDR_SIZE + 8
    dev = get_device(hdr.hDevice)
    if not dev["pp"] or not (dev["tilt"] or dev["pos"]):
        return
    for i in range(count):
        chunk = raw[base + i * size_hid: base + (i + 1) * size_hid]
        n = len(chunk)
        rep = (ctypes.c_char * n).from_buffer_copy(chunk)

        px = py = None   # (сырое значение, описание)
        for c in dev["pos"]:
            v = read_cap(dev, c, rep, n)
            if v is None:
                continue
            if c["usage"] == USAGE_X and px is None:
                px = (v, c)
            elif c["usage"] == USAGE_Y and py is None:
                py = (v, c)
        tx = ty = None
        rtx = rty = None   # сырые значения (для отладки)
        for c in dev["tilt"]:
            v = read_cap(dev, c, rep, n)
            if v is None:
                continue
            if c["usage"] == USAGE_TILT_X and tx is None:
                tx = to_degrees(v, c)
                rtx = v
            elif c["usage"] == USAGE_TILT_Y and ty is None:
                ty = to_degrees(v, c)
                rty = v

        if px and py:
            if px[0] == 0 and py[0] == 0:
                continue  # пустой отчёт при уходе пера из зоны
            S.fx, S.fy = to_fraction(px[0], px[1]), to_fraction(py[0], py[1])
            S.have_pos = True
        elif tx is None and ty is None:
            continue
        if tx is not None:
            S.tilt_x = tx
        if ty is not None:
            S.tilt_y = ty
        S.seen = True
        S.last_seen = time.perf_counter()

        if tx is not None or ty is not None:
            ax = -S.tilt_x if S.invert_x else S.tilt_x
            ay = -S.tilt_y if S.invert_y else S.tilt_y
            sax, say = S.smoother.update(ax, ay, S.last_seen)
            ang, dist, alpha = tilt_to_point(sax, say, MAX_TILT_DEG)
            S.length = dist
            if alpha >= MIN_TILT_DEG:
                if S.theta_t is None:
                    S.theta = ang
                else:
                    S.theta = damped_angle(S.theta, ang, alpha, S.last_seen - S.theta_t)
                S.theta_t = S.last_seen

        if S.debug:
            st = S.stats
            if st is None:
                st = S.stats = {"n": 0, "nt": 0, "tx": [1e9, -1e9], "ty": [1e9, -1e9],
                                "th": [1e9, -1e9], "fx": [1e9, -1e9], "devs": set()}
            st["n"] += 1
            st["devs"].add(device_name(hdr.hDevice)[-40:])
            if tx is not None or ty is not None:
                st["nt"] += 1
                st["tx"] = [min(st["tx"][0], S.tilt_x), max(st["tx"][1], S.tilt_x)]
                st["ty"] = [min(st["ty"][0], S.tilt_y), max(st["ty"][1], S.tilt_y)]
                st["th"] = [min(st["th"][0], S.theta), max(st["th"][1], S.theta)]
            st["fx"] = [min(st["fx"][0], S.fx), max(st["fx"][1], S.fx)]
        if S.debug and S.last_seen - S.last_debug > 0.25:
            S.last_debug = S.last_seen
            sx, sy = map_to_screen(S.fx, S.fy, S.rect, S.calib)
            log("перо: экран (%d, %d) | наклон X=%6.1f Y=%6.1f (сырые %s/%s) | угол %6.1f длина %.2f | отчёт %s" % (
                sx, sy, S.tilt_x, S.tilt_y, rtx, rty, S.theta, S.length, chunk.hex()))


SPRITE = {}


@lru_cache(maxsize=800)
def frame_data(angle2, step):
    """Готовый кадр: (буфер BGRA, ширина, высота, кончик x, кончик y).
    step - номер градации длины (LENGTH_STEPS = полная длина)."""
    k = MIN_LENGTH_FRAC + (1.0 - MIN_LENGTH_FRAC) * step / float(LENGTH_STEPS)
    # PIL крутит против часовой стрелки; угол пера и угол картинки считаются по часовой
    angle = angle2 / 2.0   # угол хранится в половинках градуса - поворот идёт мельче
    tile, ox, oy = render_tile(SPRITE["pm"], SPRITE["tip"], BODY_DIRECTION_DEG - angle, k, BODY_DIRECTION_DEG)
    r, g, b, a = tile.split()
    data = Image.merge("RGBA", (b, g, r, a)).tobytes()
    return (ctypes.create_string_buffer(data, len(data)), tile.width, tile.height, ox, oy)


def render(now):
    active = True if HIDE_AFTER_SEC <= 0 else (S.demo or (S.seen and now - S.last_seen < HIDE_AFTER_SEC))
    if not active:
        if S.shown:
            ShowWindow(S.hwnd, SW_HIDE)
            S.shown = False
            S.last_key = None
            S.drawn_key = None
        return
    pt = wt.POINT()
    GetCursorPos(ctypes.byref(pt))
    if S.have_pos and now - S.last_seen < 0.25:          # данные пера свежие - берём их
        S.cur_at_hid = (pt.x, pt.y)
        S.last_xy = map_to_screen(S.fx, S.fy, S.rect, S.calib)
    elif not S.have_pos:                                   # данных пера ещё не было
        S.last_xy = (pt.x, pt.y)
    elif POS_FOLLOW_CURSOR and S.cur_at_hid and (abs(pt.x - S.cur_at_hid[0]) > 2 or
                                                  abs(pt.y - S.cur_at_hid[1]) > 2):
        S.last_xy = (pt.x, pt.y)                           # данные пера пропали, а курсор едет - идём за ним
    sx, sy = S.last_xy
    angle = int(round((S.theta + S.offset) * 2)) % 720    # в половинках градуса
    step = int(round(S.length * LENGTH_STEPS)) if S.stretch else LENGTH_STEPS
    if S.demo:  # тест отрисовки: стилус сам крутится и меняет длину, данные пера не нужны
        angle = int(now * 180) % 720
        step = int((0.6 + 0.4 * math.sin(now * 2.0)) * LENGTH_STEPS)
    step = max(0, min(LENGTH_STEPS, step))
    if now - S.last_top > 0.5:  # дважды в секунду заново поднимаем окно над остальными и перерисовываем
        S.last_top = now
        SetWindowPos(S.hwnd, -1, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | 0x40)
        S.last_key = None
    key = (sx, sy, angle, step)
    if S.shown and key == S.last_key:
        return
    S.last_key = key
    buf, tw, th, ox, oy = frame_data(angle, step)
    if S.drawn_key != (angle, step):  # содержимое буфера меняем только при смене кадра
        S.drawn_key = (angle, step)
        stride, rowb, base = S.n * 4, tw * 4, S.bits.value
        src = ctypes.addressof(buf)
        if rowb == stride:
            ctypes.memmove(base, src, rowb * th)
        else:
            for y in range(th):
                ctypes.memmove(base + y * stride, src + y * rowb, rowb)
    if not S.shown:
        ShowWindow(S.hwnd, SW_SHOWNOACTIVATE)
        S.shown = True
    dst = wt.POINT(sx - ox, sy - oy)
    size = wt.SIZE(tw, th)
    src_pt = wt.POINT(0, 0)
    blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER + попиксельная прозрачность
    UpdateLayeredWindow(S.hwnd, S.hdc_screen, ctypes.byref(dst), ctypes.byref(size),
                        S.hdc_mem, ctypes.byref(src_pt), 0, ctypes.byref(blend), ULW_ALPHA)


def print_stats(now):
    st, S.stats = S.stats, None
    S.last_stats = now
    if not st:
        log("[за 3 с] данных от пера нет (перо вне зоны планшета или данные не приходят)")
        return
    log("[за 3 с] отчётов %d, из них с наклоном %d | наклон X %.1f..%.1f, Y %.1f..%.1f | угол %.0f..%.0f | позиция X %.3f..%.3f | устройств: %d" % (
        st["n"], st["nt"], st["tx"][0], st["tx"][1], st["ty"][0], st["ty"][1],
        st["th"][0], st["th"][1], st["fx"][0], st["fx"][1], len(st["devs"])))


def save_calib():
    try:
        with open(os.path.join(SCRIPT_DIR, CALIB_FILE), "w", encoding="utf-8") as f:
            json.dump(S.calib, f)
    except OSError as e:
        log("Не удалось сохранить калибровку:", e)


def load_calib():
    try:
        with open(os.path.join(SCRIPT_DIR, CALIB_FILE), "r", encoding="utf-8") as f:
            d = json.load(f)
        if "tl" in d and "br" in d:
            S.calib = {"tl": tuple(d["tl"]), "br": tuple(d["br"])}
    except (OSError, ValueError):
        pass


def handle_hotkey(hk):
    if hk == 1:
        PostQuitMessage(0)
    elif hk in (5, 6):
        if not S.have_pos:
            log("Нет данных о позиции пера - сначала поводите пером над планшетом.")
            return
        S.pending["tl" if hk == 5 else "br"] = (S.fx, S.fy)
        if "tl" in S.pending and "br" in S.pending:
            S.calib = dict(S.pending)
            save_calib()
            S.last_key = None
            log("Калибровка сохранена.")
        else:
            log("Угол записан. Теперь запишите второй (Ctrl+Alt+%d)." % (2 if hk == 5 else 1))
    elif hk == 8:
        S.demo = not S.demo
        S.last_key = None
        log("Демо-вращение:", "вкл" if S.demo else "выкл")
    elif hk == 7:
        S.calib = None
        S.pending = {}
        try:
            os.remove(os.path.join(SCRIPT_DIR, CALIB_FILE))
        except OSError:
            pass
        S.last_key = None
        log("Калибровка сброшена.")


# ===== СКИНЫ И ОКНО НАСТРОЕК =====

def load_config():
    try:
        with open(os.path.join(SCRIPT_DIR, CONFIG_FILE), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config():
    try:
        with open(os.path.join(SCRIPT_DIR, CONFIG_FILE), "w", encoding="utf-8") as f:
            json.dump({"skin": S.skin}, f, ensure_ascii=False)
    except OSError:
        log("Не удалось сохранить настройки.")


def skins_dir():
    return os.path.join(SCRIPT_DIR, SKINS_DIR)


def skin_path(key):
    return os.path.join(SCRIPT_DIR, IMAGE_NAME) if not key else os.path.join(skins_dir(), key)


def list_skins():
    """[(название, ключ)]: первым стандартный скин (stylus.png), дальше PNG из папки skins."""
    items = [("Default", "")]
    try:
        names = sorted((f for f in os.listdir(skins_dir()) if f.lower().endswith(".png")), key=str.lower)
    except OSError:
        names = []
    return items + [(os.path.splitext(f)[0], f) for f in names]


def set_sprite(sprite, tip):
    """Подменяет картинку стилуса на лету (при необходимости пересоздаёт буфер окна)."""
    n = tile_buffer_size(sprite, tip)
    SPRITE["pm"] = sprite.convert("RGBa")
    SPRITE["tip"] = tip
    if n != S.n:
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = n, -n
        bmi.biPlanes, bmi.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        hbmp = CreateDIBSection(S.hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbmp or not bits.value:
            raise ctypes.WinError(ctypes.get_last_error())
        SelectObject(S.hdc_mem, hbmp)
        if S.hbmp:
            DeleteObject(S.hbmp)
        S.hbmp, S.bits, S.n = hbmp, bits, n
    frame_data.cache_clear()
    S.last_key = None
    S.drawn_key = None


def set_status(text):
    """Короткое сообщение в шапке окна скинов (если оно открыто)."""
    if S.settings_win and S.settings_win.alive:
        S.settings_win.set_status(text)


def apply_skin(key):
    path = skin_path(key)
    try:
        img = Image.open(path) if os.path.exists(path) else placeholder_sprite()
        sprite, tip = prepare_sprite(img, S.size_px, S.scale, TIP_POINT)
        set_sprite(sprite, tip)
    except Exception as e:
        log("Не удалось загрузить скин '%s': %s" % (path, e))
        set_status("Failed to load this skin")
        return False
    S.skin = key
    save_config()
    set_status("Active skin: %s" % (os.path.splitext(key)[0] if key else "Default"))
    return True


def add_skin(path):
    """Копирует PNG в папку skins под свободным именем. Возвращает ключ или None."""
    if not path.lower().endswith(".png") or not os.path.isfile(path):
        return None
    try:
        with Image.open(path) as im:
            im.load()
            if im.width < 2 or im.height < 2:
                return None
        os.makedirs(skins_dir(), exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(path))
        name, i = base + ext, 2
        while os.path.exists(os.path.join(skins_dir(), name)):
            name = "%s_%d%s" % (base, i, ext)
            i += 1
        shutil.copyfile(path, os.path.join(skins_dir(), name))
        return name
    except (OSError, ValueError):
        return None


def delete_skin(key):
    """Удаляет файл скина. Возвращает (успех, сообщение для окна)."""
    if not key:
        return False, "The default skin can't be deleted"
    try:
        os.remove(skin_path(key))
    except OSError:
        return False, "Could not delete the file"
    if S.skin == key:
        apply_skin("")
    return True, "Skin deleted"


class SkinHooks:
    """Связь окна скинов (skin_window.py) с остальной программой."""

    @staticmethod
    def list_skins():
        return list_skins()

    @staticmethod
    def skin_path(key):
        return skin_path(key)

    @staticmethod
    def current_key():
        return S.skin

    @staticmethod
    def select(key):
        apply_skin(key)

    @staticmethod
    def delete(key):
        return delete_skin(key)

    @staticmethod
    def add(path):
        return add_skin(path)

    @staticmethod
    def closed():
        S.settings_win = None


def open_settings():
    """Окно скинов: рисованное, с вашей графикой из папки ui/."""
    if S.settings_win and S.settings_win.alive:       # уже открыто - просто показываем
        S.settings_win.show()
        return
    try:
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)
        import skin_window
        S.settings_win = skin_window.SkinWindow(os.path.join(SCRIPT_DIR, UI_DIR), SkinHooks, S.hicon)
    except Exception:
        S.settings_win = None
        log("Не удалось открыть окно скинов:\n" + traceback.format_exc())
        traceback.print_exc()


def restart_app():
    S.restart = True
    PostQuitMessage(0)


def make_tray_hicon():
    """HICON из banner.png (любого размера, лучше квадратного 400x400) нужного размера."""
    path = os.path.join(SCRIPT_DIR, TRAY_ICON_NAME)
    try:
        img = Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        log("Файл значка '%s' не найден - использую стандартный значок Windows." % path)
        return LoadIconW(None, ctypes.c_void_p(32512))   # IDI_APPLICATION
    sz = max(16, GetSystemMetrics(SM_CXSMICON))
    img = img.resize((sz, sz), Image.LANCZOS)
    r, g, b, a = img.split()
    data = Image.merge("RGBA", (b, g, r, a)).tobytes()
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight = sz, -sz
    bmi.biPlanes, bmi.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    hcolor = CreateDIBSection(S.hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not hcolor or not bits.value:
        return LoadIconW(None, ctypes.c_void_p(32512))
    ctypes.memmove(bits.value, data, len(data))
    hmask = CreateBitmap(sz, sz, 1, 1, None)
    ii = ICONINFO(True, 0, 0, hmask, hcolor)
    hicon = CreateIconIndirect(ctypes.byref(ii))
    DeleteObject(hcolor)
    DeleteObject(hmask)
    return hicon or LoadIconW(None, ctypes.c_void_p(32512))


def tray_data():
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = S.hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = S.hicon
    nid.szTip = "Pen overlay"
    return nid


def tray_add():
    if Shell_NotifyIconW(NIM_ADD, ctypes.byref(tray_data())):
        S.tray_added = True
    else:
        log("Не удалось добавить значок в область уведомлений.")


def tray_remove():
    if S.tray_added:
        Shell_NotifyIconW(NIM_DELETE, ctypes.byref(tray_data()))
        S.tray_added = False
    if S.hicon:
        DestroyIcon(S.hicon)
        S.hicon = None


def tray_menu():
    """Меню значка: Settings, Reload, Exit."""
    menu = CreatePopupMenu()
    AppendMenuW(menu, MF_STRING, MENU_SETTINGS, "Settings")
    AppendMenuW(menu, MF_STRING, MENU_RELOAD, "Reload")
    AppendMenuW(menu, MF_SEPARATOR, 0, None)
    AppendMenuW(menu, MF_STRING, MENU_EXIT, "Exit")
    pt = wt.POINT()
    GetCursorPos(ctypes.byref(pt))
    SetForegroundWindow(S.hwnd)   # без этого меню не закрывается кликом мимо
    cmd = TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_BOTTOMALIGN,
                         pt.x, pt.y, 0, S.hwnd, None)
    PostMessageW(S.hwnd, WM_NULL, 0, 0)
    DestroyMenu(menu)
    if cmd == MENU_EXIT:
        PostQuitMessage(0)
    elif cmd == MENU_RELOAD:
        restart_app()
    elif cmd == MENU_SETTINGS:
        open_settings()


def wndproc(hwnd, msg, wparam, lparam):
    try:
        if msg == WM_INPUT:
            handle_raw_input(lparam)
        elif msg == WM_HOTKEY:
            handle_hotkey(int(wparam))
        elif msg == WM_TRAY:
            if (lparam & 0xFFFF) in (WM_LBUTTONUP, WM_RBUTTONUP):   # клик по значку
                tray_menu()
        elif S.taskbar_msg and msg == S.taskbar_msg:                # Проводник перезапущен
            tray_add()
        elif msg == WM_DESTROY:
            PostQuitMessage(0)
    except Exception:
        traceback.print_exc()
    return DefWindowProcW(hwnd, msg, wparam, lparam)


WNDPROC_REF = WNDPROC(wndproc)  # ссылка, чтобы функцию не удалил сборщик мусора


def pen_display_rect():
    """Монитор, на который Windows отображает перо: ((l, t, r, b), название) или None."""
    try:
        n = wt.UINT(0)
        if not GetPointerDevices(ctypes.byref(n), None) or n.value == 0:
            return None
        arr = (POINTER_DEVICE_INFO * n.value)()
        if not GetPointerDevices(ctypes.byref(n), arr):
            return None
        pens = [d for d in arr[:n.value] if d.pointerDeviceType in (1, 2)]
        pens.sort(key=lambda d: 0 if d.pointerDeviceType == 2 else 1)  # внешнее перо первым
        for d in pens:
            dr, sr = wt.RECT(), wt.RECT()
            if GetPointerDeviceRects(d.device, ctypes.byref(dr), ctypes.byref(sr)) and sr.right > sr.left:
                return (sr.left, sr.top, sr.right, sr.bottom), d.productString
    except Exception:
        pass
    return None


def disable_quickedit():
    """Клик в окне консоли иначе замораживает программу (режим выделения)."""
    try:
        h = GetStdHandle(0xFFFFFFF6)  # STD_INPUT_HANDLE
        mode = wt.DWORD()
        if GetConsoleMode(h, ctypes.byref(mode)):
            SetConsoleMode(h, (mode.value & ~0x40) | 0x80)
    except Exception:
        pass


def run(image_path, size_px, scale):
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        user32.SetProcessDPIAware()
    disable_quickedit()

    if image_path and os.path.exists(image_path):
        img = Image.open(image_path)
    else:
        log("Файл '%s' не найден - использую простую картинку-заглушку." % image_path)
        img = placeholder_sprite()
    sprite, tip = prepare_sprite(img, size_px, scale, TIP_POINT)
    SPRITE["pm"] = sprite.convert("RGBa")   # предумноженная прозрачность - без тёмной каймы
    SPRITE["tip"] = tip
    S.n = tile_buffer_size(sprite, tip)
    S.size_px, S.scale = size_px, scale

    found = pen_display_rect()
    if found:
        S.rect = found[0]
        log("Перо отображается на монитор:", S.rect, "(%s)" % found[1])
    else:
        S.rect = (0, 0, GetSystemMetrics(0), GetSystemMetrics(1))
        log("Монитор пера определить не удалось, беру основной экран:", S.rect)
    load_calib()
    if S.calib:
        log("Загружена сохранённая калибровка.")

    hinst = GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC_REF
    wc.hInstance = hinst
    wc.lpszClassName = "PenOverlayWindow"
    if not RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    S.hwnd = CreateWindowExW(ex, wc.lpszClassName, "Pen overlay", WS_POPUP, 0, 0, S.n, S.n,
                             None, None, hinst, None)
    if not S.hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    # Буфер-картинка для прозрачного окна
    S.hdc_screen = GetDC(None)
    S.hdc_mem = CreateCompatibleDC(S.hdc_screen)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight = S.n, -S.n  # минус = строки сверху вниз
    bmi.biPlanes, bmi.biBitCount = 1, 32
    S.bits = ctypes.c_void_p()
    hbmp = CreateDIBSection(S.hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(S.bits), None, 0)
    if not hbmp or not S.bits.value:
        raise ctypes.WinError(ctypes.get_last_error())
    SelectObject(S.hdc_mem, hbmp)
    S.hbmp = hbmp

    # Подписка на данные пера (даже когда окно не в фокусе)
    rids = (RAWINPUTDEVICE * 2)(
        RAWINPUTDEVICE(PAGE_DIGITIZER, 0x02, RIDEV_INPUTSINK, S.hwnd),   # перо
        RAWINPUTDEVICE(PAGE_DIGITIZER, 0x01, RIDEV_INPUTSINK, S.hwnd))   # планшет/дигитайзер
    if not RegisterRawInputDevices(rids, 2, ctypes.sizeof(RAWINPUTDEVICE)):
        raise ctypes.WinError(ctypes.get_last_error())

    # Q, 1, 2, 0, T  (Ctrl+Alt+...)
    for hk_id, vk in ((1, 0x51), (5, 0x31), (6, 0x32), (7, 0x30), (8, 0x54)):
        if not RegisterHotKey(S.hwnd, hk_id, 0x1 | 0x2, vk):
            log("Не удалось занять горячую клавишу Ctrl+Alt+%s (занята другой программой)" % chr(vk))

    # Значок в области уведомлений (скрытые значки): по клику - меню с пунктом "Выход"
    S.taskbar_msg = RegisterWindowMessageW("TaskbarCreated")
    S.hicon = make_tray_hicon()
    tray_add()

    log("Размер картинки на экране: %dx%d. Работает - подвигайте пером над планшетом. "
          "Выход: Ctrl+Alt+Q. Тест отрисовки (стилус крутится сам): Ctrl+Alt+T." % sprite.size)
    if S.debug:
        log("Режим отладки: ниже появятся данные пера.")

    # Свой цикл сообщений: отрисовка по времени, а не по WM_TIMER (таймер голодает
    # при плотном потоке данных от пера и картинка начинает дёргаться).
    msg = wt.MSG()
    next_render = 0.0
    running = True
    while running:
        MsgWaitForMultipleObjects(0, None, False, 4, QS_ALLINPUT)
        while PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                running = False
                break
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))
            now = time.perf_counter()
            if now >= next_render:
                render(now)
                next_render = now + RENDER_INTERVAL
        now = time.perf_counter()
        if S.debug and now - S.last_stats > 3.0:
            print_stats(now)
        if running and now >= next_render:
            render(now)
            next_render = now + RENDER_INTERVAL
    tray_remove()
    if S.restart:                       # Reload: освобождаем клавиши и окна, запускаем новую копию
        for hk_id in (1, 5, 6, 7, 8):
            UnregisterHotKey(S.hwnd, hk_id)
        if S.settings_win and S.settings_win.alive:
            S.settings_win.destroy()
        DestroyWindow(S.hwnd)
        if LOGF:
            LOGF.close()
        subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:], cwd=SCRIPT_DIR)


def main():
    ap = argparse.ArgumentParser(description="Оверлей со стилусом, повторяющий положение и наклон пера")
    ap.add_argument("image", nargs="?", default=None, help="PNG со стилусом (по умолчанию - скин из настроек)")
    ap.add_argument("--size", type=int, default=SPRITE_LENGTH_PX,
                    help="длина картинки на экране, пикс. (по умолчанию %d; 0 = как в файле)" % SPRITE_LENGTH_PX)
    ap.add_argument("--scale", type=float, default=SCALE, help="дополнительный множитель размера")
    ap.add_argument("--no-stretch", action="store_true", help="не менять длину стилуса по величине наклона")
    ap.add_argument("--debug", action="store_true", help="печатать данные пера")
    args = ap.parse_args()
    S.debug = args.debug
    if S.debug:
        global LOGF
        try:
            LOGF = open(os.path.join(SCRIPT_DIR, LOG_FILE), "w", encoding="utf-8")
        except OSError:
            LOGF = None
    S.stretch = STRETCH_BY_TILT and not args.no_stretch
    S.skin = load_config().get("skin", "")
    if S.skin and not os.path.isfile(skin_path(S.skin)):
        S.skin = ""
    try:
        run(args.image or skin_path(S.skin), args.size or None, args.scale)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
