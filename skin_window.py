"""Окно выбора скинов для pen_overlay.pyw: рисованное, без системной рамки.

Окно - слой с попиксельной прозрачностью (UpdateLayeredWindow), кадр рисует Pillow, поэтому форма
окна - это форма вашей нарисованной рамки. Ресурсы лежат в папке ui/ (их делает build_ui_assets.py).

Модуль состоит из двух частей:
  * SkinUI     - чистый Pillow: геометрия, отрисовка кадра, поиск элемента под курсором (без Windows API);
  * SkinWindow - Win32: само окно, мышь, перетаскивание, Drag&Drop PNG, сворачивание/разворачивание.
"""
import ctypes
import json
import os
import random
import sys
import threading
import traceback
from ctypes import wintypes as wt

from PIL import Image, ImageDraw, ImageFont

# ------------------------------ внешний вид ------------------------------
FONT_FILE = "PenHand.ttf"
FALLBACK_FONTS = ("segoeui.ttf", "arial.ttf", "tahoma.ttf", "DejaVuSans.ttf")   # для символов, которых нет в PenHand
# символы, которые есть в PenHand.ttf (кириллицы и многих знаков там нет)
PENHAND_CHARS = set(" !'()+,-./0123456789:;?ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz\u00a0")

NAME_SIZE = 15              # размер названия скина (в пикселях при масштабе 1:1)
BUTTON_SIZE = 19            # размер надписи Delete
HINT_SIZE = 16              # подсказка/статус в шапке
INK = (58, 58, 58, 255)               # обычный цвет текста
INK_SELECTED = (150, 40, 150, 255)    # название выбранного скина
INK_HOVER = (205, 45, 45, 255)        # Delete при наведении
INK_DISABLED = (165, 165, 165, 255)
LILAC = (215, 99, 213)                # цвет выбранной ячейки (как на макете)
SELECT_FILL_ALPHA = 70
HOVER_FILL_ALPHA = 28
OUTLINE_GAIN = {"normal": 1.0, "hover": 1.7, "selected": 2.6}
OUTLINE_TINT_SELECTED = (170, 45, 170)
STATUS_SECONDS = 3.5
MAX_SCALE = 3.0


# ============================== ЧАСТЬ 1: Pillow ==============================

def rgba_resize(img, size):
    """Изменение размера с предумноженной прозрачностью (без цветной каймы)."""
    size = (max(1, int(size[0])), max(1, int(size[1])))
    if img.size == size:
        return img.copy()
    return img.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")


def nine_slice(img, W, H, s):
    """Растягивает рамку до W x H, оставляя углы (s x s) без искажений."""
    w, h = img.size
    if (W, H) == (w, h):
        return img.copy()
    s = max(1, min(int(s), w // 2 - 1, h // 2 - 1, W // 2, H // 2))
    xs, ys = [0, s, w - s, w], [0, s, h - s, h]
    xd, yd = [0, s, W - s, W], [0, s, H - s, H]
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for j in range(3):
        for i in range(3):
            size = (xd[i + 1] - xd[i], yd[j + 1] - yd[j])
            if size[0] <= 0 or size[1] <= 0:
                continue
            part = img.crop((xs[i], ys[j], xs[i + 1], ys[j + 1]))
            out.paste(rgba_resize(part, size), (xd[i], yd[j]))
    return out


def tint(img, rgb=None, gain=1.0):
    """Копия картинки с изменённой непрозрачностью (gain) и, при желании, другим цветом."""
    r, g, b, a = img.split()
    a = a.point(lambda v: min(255, int(v * gain + 0.5)))
    if rgb is not None:
        r, g, b = (Image.new("L", img.size, c) for c in rgb)
    return Image.merge("RGBA", (r, g, b, a))


class FontSet:
    """PenHand для всего, что в нём есть; остальные символы - из запасного шрифта."""

    def __init__(self, ui_dir):
        self.main = os.path.join(ui_dir, FONT_FILE)
        self.fallback = None
        dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]
        for name in FALLBACK_FONTS:
            for d in dirs:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    self.fallback = p
                    break
            if self.fallback:
                break
        self._cache = {}

    def font(self, path, size):
        key = (path, int(size))
        f = self._cache.get(key)
        if f is None:
            try:
                f = ImageFont.truetype(path, int(size)) if path else ImageFont.load_default()
            except OSError:
                f = ImageFont.load_default()
            self._cache[key] = f
        return f

    def runs(self, text, size):
        """[(шрифт, кусок текста)]: подряд идущие символы одного шрифта."""
        out = []
        for ch in text:
            path = self.main if (ch in PENHAND_CHARS or not self.fallback) else self.fallback
            if out and out[-1][0] == path:
                out[-1][1] += ch
            else:
                out.append([path, ch])
        return [(self.font(p, size), t) for p, t in out]

    def width(self, text, size):
        return sum(f.getlength(t) for f, t in self.runs(text, size))

    def fit(self, text, size, max_w):
        """Укорачивает текст до max_w, добавляя '..' (в PenHand нет знака многоточия)."""
        if self.width(text, size) <= max_w:
            return text
        while len(text) > 1 and self.width(text + "..", size) > max_w:
            text = text[:-1]
        return text + ".."

    def draw(self, dst, cx, baseline, text, size, color, anchor="m"):
        """Рисует строку на RGBA-картинке (cx - центр или левый край, baseline - линия строки)."""
        w = int(self.width(text, size)) + 6
        h = int(size * 1.7) + 4
        mask = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(mask)
        x = 3.0
        base = int(size * 1.15)
        for f, t in self.runs(text, size):
            d.text((x, base), t, font=f, fill=255, anchor="ls")
            x += f.getlength(t)
        if color[3] < 255:
            mask = mask.point(lambda v: v * color[3] // 255)
        solid = Image.new("RGBA", (w, h), color[:3] + (255,))
        solid.putalpha(mask)
        left = cx - (w - 6) / 2 - 3 if anchor == "m" else cx - 3
        dst.alpha_composite(solid, (int(round(left)), int(round(baseline - base))))
        return w - 6


class SkinUI:
    """Геометрия и картинка окна. Не знает про Windows."""

    def __init__(self, ui_dir, scale=1.0, rng=None):
        self.ui_dir = ui_dir
        with open(os.path.join(ui_dir, "layout.json"), "r", encoding="utf-8") as f:
            self.L = json.load(f)
        self.u = max(0.5, min(MAX_SCALE, float(scale)))
        self.rng = rng or random.Random()
        self.fonts = FontSet(ui_dir)
        self.bw, self.bh = self.L["window"]
        self.window_art = Image.open(os.path.join(ui_dir, "window.png")).convert("RGBA")
        self.outlines = []
        i = 1
        while os.path.isfile(os.path.join(ui_dir, "cell_%d.png" % i)):
            self.outlines.append(Image.open(os.path.join(ui_dir, "cell_%d.png" % i)).convert("RGBA"))
            i += 1
        self._frame_cache = {}
        self._glyph_cache = {}
        self._outline_cache = {}
        self._thumb_cache = {}
        self.assigned = {}          # ключ скина -> (номер обводки, отражение по горизонтали)

    # ---------- размеры ----------
    def px(self, v):
        return int(round(v * self.u))

    def base_size(self):
        return self.px(self.bw), self.px(self.bh)

    def geometry(self, W, H, n_items, scroll):
        L, u = self.L, self.u
        sx, sy, sw, sh = L["skin_area"]
        vp = (self.px(sx), self.px(sy),
              W - self.px(sx) - self.px(self.bw - sx - sw), H - self.px(sy) - self.px(self.bh - sy - sh))
        btn = {}
        for name, (x, y, w, h) in L["buttons"].items():
            btn[name] = (W - self.px(self.bw - x), self.px(y), self.px(w), self.px(h))
        dx, dy, dw, dh = L["delete"]
        delete = (W - self.px(self.bw - dx), H - self.px(self.bh - dy), self.px(dw), self.px(dh))
        slot_w, slot_h = self.px(L["slot"][0]), self.px(L["slot"][1])
        label_gap, label_h = self.px(2), self.px(L["label_h"])
        pitch_x, pitch_y = self.px(L["pitch"][0]), self.px(L["pitch"][1])
        cols = max(1, (vp[2] - slot_w) // pitch_x + 1)
        rows = (n_items + cols - 1) // cols
        total_w = (cols - 1) * pitch_x + slot_w
        pad = self.px(6)
        content_h = max(0, rows * pitch_y - (pitch_y - slot_h - label_gap - label_h)) + 2 * pad
        max_scroll = max(0, content_h - vp[3])
        return {"vp": vp, "btn": btn, "delete": delete, "slot": (slot_w, slot_h), "label_h": label_h,
                "label_gap": label_gap, "pitch": (pitch_x, pitch_y), "cols": cols, "rows": rows,
                "x0": vp[0] + (vp[2] - total_w) // 2, "y0": vp[1] + pad, "max_scroll": max_scroll,
                "content_h": content_h, "pad": pad}

    @staticmethod
    def cell_rect(g, i, scroll):
        col, row = i % g["cols"], i // g["cols"]
        x = g["x0"] + col * g["pitch"][0]
        y = g["y0"] + row * g["pitch"][1] - int(scroll)
        return x, y, g["slot"][0], g["slot"][1] + g["label_gap"] + g["label_h"]

    def hit(self, x, y, W, H, n_items, scroll):
        """Элемент под точкой: ('btn', имя) | ('delete',) | ('cell', i) | ('view',) | None."""
        g = self.geometry(W, H, n_items, scroll)
        for name, (bx, by, bw, bh) in g["btn"].items():
            if bx <= x < bx + bw and by <= y < by + bh:
                return ("btn", name)
        dx, dy, dw, dh = g["delete"]
        if dx <= x < dx + dw and dy <= y < dy + dh:
            return ("delete",)
        vx, vy, vw, vh = g["vp"]
        if vx <= x < vx + vw and vy <= y < vy + vh:
            for i in range(n_items):
                cx, cy, cw, ch = self.cell_rect(g, i, scroll)
                if cy + ch < vy or cy > vy + vh:
                    continue
                if cx <= x < cx + cw and cy <= y < cy + ch:
                    return ("cell", i)
            return ("view",)
        return None

    # ---------- обводки ----------
    def assign_outlines(self, keys):
        """Каждому скину - случайная обводка (и случайное отражение), соседние не повторяются."""
        old = self.assigned
        self.assigned = {}
        prev = None
        n = len(self.outlines)
        for k in keys:
            if k in old:
                self.assigned[k] = old[k]
            elif n:
                choices = [i for i in range(n) if n < 2 or prev is None or i != prev] or list(range(n))
                self.assigned[k] = (self.rng.choice(choices), self.rng.random() < 0.5)
            prev = self.assigned.get(k, (None, 0))[0]

    def outline(self, key, mode, slot):
        idx, flip = self.assigned.get(key, (0, False))
        ck = (idx, flip, mode, slot)
        im = self._outline_cache.get(ck)
        if im is None:
            if not self.outlines:
                return None
            im = rgba_resize(self.outlines[idx % len(self.outlines)], (slot[0] * 1.0, slot[1] * 1.0))
            if flip:
                im = im.transpose(Image.FLIP_LEFT_RIGHT)
            im = tint(im, OUTLINE_TINT_SELECTED if mode == "selected" else None, OUTLINE_GAIN[mode])
            self._outline_cache[ck] = im
        return im

    # ---------- картинки ----------
    def frame(self, W, H):
        ck = (W, H)
        im = self._frame_cache.get(ck)
        if im is None:
            if len(self._frame_cache) > 4:
                self._frame_cache.clear()
            base = rgba_resize(self.window_art, self.base_size())
            im = nine_slice(base, W, H, self.px(self.L.get("slice", 60)))
            self._frame_cache[ck] = im
        return im

    def glyph(self, name, state, rect_wh):
        ck = (name, state, rect_wh)
        im = self._glyph_cache.get(ck)
        if im is None:
            path = os.path.join(self.ui_dir, "btn_%s_%s.png" % (name, state))
            if not os.path.isfile(path):
                path = os.path.join(self.ui_dir, "btn_%s_normal.png" % name)
            g = Image.open(path).convert("RGBA")
            sc = min(rect_wh[0] / g.width, rect_wh[1] / g.height)
            im = rgba_resize(g, (g.width * sc, g.height * sc))
            self._glyph_cache[ck] = im
        return im

    def thumb(self, key, path, size):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        ck = (key, size)
        hit = self._thumb_cache.get(ck)
        if hit and hit[0] == mtime:
            return hit[1]
        im = None
        try:
            src = Image.open(path).convert("RGBA")
            box = src.getchannel("A").getbbox()
            if box:
                src = src.crop(box)
            sc = min(size[0] / src.width, size[1] / src.height)
            im = rgba_resize(src, (src.width * sc, src.height * sc))
        except Exception:
            im = None
        if len(self._thumb_cache) > 400:
            self._thumb_cache.clear()
        self._thumb_cache[ck] = (mtime, im)
        return im

    # ---------- отрисовка ----------
    def render(self, W, H, st, skin_path_of):
        """Кадр окна. st: items [(имя, ключ)], selected (ключ), hover, pressed, scroll, status.
        skin_path_of(ключ) -> путь к PNG скина."""
        u = self.u
        items = st["items"]
        g = self.geometry(W, H, len(items), st["scroll"])
        img = self.frame(W, H).copy()
        fonts = self.fonts
        hover, pressed = st.get("hover"), st.get("pressed")

        # --- область скинов: рисуем на отдельном слое и обрезаем по её краям ---
        vx, vy, vw, vh = g["vp"]
        layer = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
        fills = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fills)
        scroll = min(st["scroll"], g["max_scroll"])
        slot_w, slot_h = g["slot"]
        tw, th = self.px(self.L["thumb"][0]), self.px(self.L["thumb"][1])
        for i, (name, key) in enumerate(items):
            cx, cy, cw, ch = self.cell_rect(g, i, scroll)
            cx -= vx
            cy -= vy
            if cy + ch < 0 or cy > vh:
                continue
            is_sel = key == st["selected"]
            is_hov = hover == ("cell", i)
            mode = "selected" if is_sel else "hover" if is_hov else "normal"
            tx, ty = cx + (slot_w - tw) // 2, cy + (slot_h - th) // 2
            if is_sel or is_hov:
                a = SELECT_FILL_ALPHA if is_sel else HOVER_FILL_ALPHA
                fd.rounded_rectangle((tx - self.px(3), ty - self.px(3), tx + tw + self.px(3), ty + th + self.px(3)),
                                     radius=self.px(8), fill=LILAC + (a,))
            th_img = self.thumb(key, skin_path_of(key), (tw, th))
            if th_img is not None:
                self._paste(layer, th_img, tx + (tw - th_img.width) // 2, ty + (th - th_img.height) // 2)
            else:                                   # скин не загрузился - рисуем крестик-заглушку
                d = ImageDraw.Draw(layer)
                d.line((tx, ty, tx + tw, ty + th), fill=INK_DISABLED, width=max(1, self.px(2)))
                d.line((tx, ty + th, tx + tw, ty), fill=INK_DISABLED, width=max(1, self.px(2)))
            ol = self.outline(key, mode, (slot_w, slot_h))
            if ol is not None:
                self._paste(layer, ol, cx, cy)
            label = fonts.fit(name, self.px(NAME_SIZE), g["pitch"][0] - self.px(2))
            color = INK_SELECTED if is_sel else INK
            base = cy + slot_h + g["label_gap"] + int(self.px(NAME_SIZE) * 0.95)
            fonts.draw(layer, cx + slot_w / 2, base, label, self.px(NAME_SIZE), color)
        fills.alpha_composite(layer)
        # полоса прокрутки
        if g["max_scroll"] > 0:
            bar_h = max(self.px(24), int(vh * vh / (vh + g["max_scroll"])))
            bar_y = int((vh - bar_h) * scroll / g["max_scroll"])
            ImageDraw.Draw(fills).rounded_rectangle((vw - self.px(7), bar_y, vw - self.px(3), bar_y + bar_h),
                                                    radius=self.px(2), fill=(90, 90, 90, 120))
        img.alpha_composite(fills, (vx, vy))

        # --- кнопки окна ---
        for name, (bx, by, bw, bh) in g["btn"].items():
            el = ("btn", name)
            state = "pressed" if (pressed == el and hover == el) else "hover" if hover == el else "normal"
            gl = self.glyph(name, state, (bw, bh))
            img.alpha_composite(gl, (bx + (bw - gl.width) // 2, by + (bh - gl.height) // 2))

        # --- Delete ---
        dx, dy, dw, dh = g["delete"]
        can_delete = bool(st["selected"])
        hov = hover == ("delete",) and can_delete
        color = INK_HOVER if hov else INK if can_delete else INK_DISABLED
        size = self.px(BUTTON_SIZE)
        base = dy + dh // 2 + int(size * 0.33)
        tw_ = fonts.draw(img, dx + dw / 2, base, "Delete", size, color)
        if hov:
            self._wobble(img, dx + dw / 2 - tw_ / 2, dx + dw / 2 + tw_ / 2, base + self.px(3), color)

        # --- шапка: статус или подсказка ---
        text = st.get("status") or "drop a PNG here to add a skin"
        left = self.px(40)
        right = g["btn"]["min"][0] - self.px(12)
        hint = fonts.fit(text, self.px(HINT_SIZE), max(10, right - left))
        fonts.draw(img, left, self.px(34), hint, self.px(HINT_SIZE),
                   (58, 58, 58, 255) if st.get("status") else (150, 150, 150, 255), anchor="l")
        return img

    @staticmethod
    def _paste(dst, src, x, y):
        """alpha_composite, но с обрезкой по краям dst (src может выходить за границы)."""
        x, y = int(x), int(y)
        if x >= dst.width or y >= dst.height or x + src.width <= 0 or y + src.height <= 0:
            return
        sx, sy = max(0, -x), max(0, -y)
        crop = src.crop((sx, sy, min(src.width, dst.width - x), min(src.height, dst.height - y)))
        dst.alpha_composite(crop, (x + sx, y + sy))

    def _wobble(self, dst, x0, x1, y, color):
        """Рисованное подчёркивание (слегка волнистая линия), со сглаживанием."""
        import math
        w, h, k = int(x1 - x0) + 8, self.px(10), 3
        mask = Image.new("L", (w * k, h * k), 0)
        d = ImageDraw.Draw(mask)
        pts = [(4 * k + i * k, (h / 2 + math.sin(i / 7.0) * self.u * 1.4) * k) for i in range(0, int(x1 - x0) + 1, 2)]
        d.line(pts, fill=255, width=max(2, int(self.u * 2 * k)), joint="curve")
        mask = mask.resize((w, h), Image.LANCZOS)
        solid = Image.new("RGBA", (w, h), color[:3] + (255,))
        solid.putalpha(mask)
        dst.alpha_composite(solid, (int(x0) - 4, int(y) - h // 2))


# ============================== ЧАСТЬ 2: Windows ==============================

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    P = ctypes.POINTER

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HANDLE), ("hIcon", wt.HANDLE),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class TRACKMOUSEEVENT(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND),
                    ("dwHoverTime", wt.DWORD)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    def _api(lib, name, restype, *argtypes):
        f = getattr(lib, name)
        f.restype = restype
        f.argtypes = list(argtypes)
        return f

    V = ctypes.c_void_p
    RegisterClassW = _api(user32, "RegisterClassW", wt.ATOM, P(WNDCLASSW))
    CreateWindowExW = _api(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           wt.HWND, wt.HANDLE, wt.HANDLE, V)
    DefWindowProcW = _api(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    DestroyWindow = _api(user32, "DestroyWindow", wt.BOOL, wt.HWND)
    ShowWindow = _api(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
    GetWindowRect = _api(user32, "GetWindowRect", wt.BOOL, wt.HWND, P(wt.RECT))
    UpdateLayeredWindow = _api(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HANDLE, P(wt.POINT),
                               P(wt.SIZE), wt.HANDLE, P(wt.POINT), wt.DWORD, P(BLENDFUNCTION), wt.DWORD)
    GetDC = _api(user32, "GetDC", wt.HANDLE, wt.HWND)
    ReleaseDC = _api(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HANDLE)
    SetCapture = _api(user32, "SetCapture", wt.HWND, wt.HWND)
    ReleaseCapture = _api(user32, "ReleaseCapture", wt.BOOL)
    TrackMouseEvent = _api(user32, "TrackMouseEvent", wt.BOOL, P(TRACKMOUSEEVENT))
    SetTimer = _api(user32, "SetTimer", ctypes.c_size_t, wt.HWND, ctypes.c_size_t, wt.UINT, V)
    KillTimer = _api(user32, "KillTimer", wt.BOOL, wt.HWND, ctypes.c_size_t)
    SetForegroundWindow = _api(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
    LoadCursorW = _api(user32, "LoadCursorW", wt.HANDLE, wt.HANDLE, V)
    SetCursor = _api(user32, "SetCursor", wt.HANDLE, wt.HANDLE)
    MonitorFromWindow = _api(user32, "MonitorFromWindow", wt.HANDLE, wt.HWND, wt.DWORD)
    MonitorFromPoint = _api(user32, "MonitorFromPoint", wt.HANDLE, wt.POINT, wt.DWORD)
    GetMonitorInfoW = _api(user32, "GetMonitorInfoW", wt.BOOL, wt.HANDLE, P(MONITORINFO))
    GetCursorPos = _api(user32, "GetCursorPos", wt.BOOL, P(wt.POINT))
    GetModuleHandleW = _api(kernel32, "GetModuleHandleW", wt.HANDLE, wt.LPCWSTR)
    CreateCompatibleDC = _api(gdi32, "CreateCompatibleDC", wt.HANDLE, wt.HANDLE)
    CreateDIBSection = _api(gdi32, "CreateDIBSection", wt.HANDLE, wt.HANDLE, V, wt.UINT, P(V), wt.HANDLE, wt.DWORD)
    SelectObject = _api(gdi32, "SelectObject", wt.HANDLE, wt.HANDLE, wt.HANDLE)
    DeleteObject = _api(gdi32, "DeleteObject", wt.BOOL, wt.HANDLE)
    DeleteDC = _api(gdi32, "DeleteDC", wt.BOOL, wt.HANDLE)
    DragQueryFileW = _api(shell32, "DragQueryFileW", wt.UINT, wt.HANDLE, wt.UINT, wt.LPWSTR, wt.UINT)
    DragFinish = _api(shell32, "DragFinish", None, wt.HANDLE)
    PostMessageW = _api(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    WM_DESTROY, WM_CLOSE, WM_NCHITTEST, WM_SETCURSOR = 0x0002, 0x0010, 0x0084, 0x0020
    WM_KEYDOWN, WM_TIMER, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0100, 0x0113, 0x0200, 0x0201, 0x0202
    WM_MOUSEWHEEL, WM_MOUSELEAVE, WM_DROPFILES, WM_CAPTURECHANGED = 0x020A, 0x02A3, 0x0233, 0x0215
    WS_POPUP, WS_MINIMIZEBOX, WS_SYSMENU = 0x80000000, 0x00020000, 0x00080000
    WS_EX_LAYERED, WS_EX_ACCEPTFILES = 0x00080000, 0x10
    HTCLIENT, HTCAPTION = 1, 2
    SW_SHOW, SW_MINIMIZE, SW_RESTORE = 5, 6, 9
    ULW_ALPHA = 2
    TME_LEAVE = 2
    TIMER_STATUS = 1
    WM_FRAME = 0x8001                   # WM_APP + 1: фоновый поток дорисовал кадр
    IDC_ARROW, IDC_HAND = 32512, 32649
    CLASS_NAME = "PenOverlaySkins"
    _class_registered = False
    _wndproc_ref = None
    _windows = {}                       # hwnd -> SkinWindow

    def _s16(v):
        return ctypes.c_short(v & 0xFFFF).value

    def _dispatch(hwnd, msg, wparam, lparam):
        win = _windows.get(hwnd)
        if win is not None:
            try:
                res = win.on_message(msg, wparam, lparam)
                if res is not None:
                    return res
            except Exception:
                import traceback
                traceback.print_exc()
        return DefWindowProcW(hwnd, msg, wparam, lparam)

    class SkinWindow:
        """Окно скинов. hooks - объект с методами:
            list_skins() -> [(название, ключ)]     skin_path(ключ) -> путь к PNG
            current_key() -> ключ активного скина   select(ключ)   - применить скин
            delete(ключ) -> (успех, сообщение)      add(путь) -> ключ или None
            closed()                                - окно закрыто
        """

        def __init__(self, ui_dir, hooks, hicon=None):
            global _class_registered, _wndproc_ref
            self.hooks = hooks
            try:
                dpi = ctypes.windll.user32.GetDpiForSystem()
            except Exception:
                dpi = 96
            self.ui = SkinUI(ui_dir, dpi / 96.0)
            self.st = {"items": [], "selected": "", "hover": None, "pressed": None, "scroll": 0.0, "status": ""}
            self.maxed = False
            self.restore_pos = (0, 0)
            self.size = self.ui.base_size()
            self._tracking = False
            self._buf = None
            # Рисование кадров идёт в фоновом потоке, чтобы не тормозить главный цикл (а с ним - стилус).
            self._cv = threading.Condition()
            self._req = None            # последний запрос на кадр: (размер, снимок состояния, позиция)
            self._ready = None          # готовый кадр: (W, H, позиция, байты BGRA)
            self._stop = False
            self._pending_pos = None    # куда переместить окно, когда придёт кадр нового размера
            self._worker = None
            self._hdc_screen = GetDC(None)
            self._hdc_mem = CreateCompatibleDC(self._hdc_screen)
            self.hwnd = None

            hinst = GetModuleHandleW(None)
            if not _class_registered:
                _wndproc_ref = WNDPROC(_dispatch)
                wc = WNDCLASSW()
                wc.lpfnWndProc = _wndproc_ref
                wc.hInstance = hinst
                wc.hIcon = hicon
                wc.hCursor = LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
                wc.lpszClassName = CLASS_NAME
                if not RegisterClassW(ctypes.byref(wc)):
                    raise ctypes.WinError(ctypes.get_last_error())
                _class_registered = True
            self.hwnd = CreateWindowExW(WS_EX_LAYERED | WS_EX_ACCEPTFILES, CLASS_NAME, "Pen overlay - Skins",
                                        WS_POPUP | WS_MINIMIZEBOX | WS_SYSMENU, 0, 0, 16, 16,
                                        None, None, hinst, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            _windows[self.hwnd] = self
            self.refresh()
            area = self._work_area(None)
            W, H = self.size
            self.restore_pos = (area[0] + (area[2] - area[0] - W) // 2, area[1] + (area[3] - area[1] - H) // 3)
            with self._cv:
                self._req = None
            self._present(self.restore_pos)              # первый кадр - сразу, дальше - в фоне
            self._worker = threading.Thread(target=self._render_loop, args=(self.hwnd,), daemon=True)
            self._worker.start()
            self.show()

        # ---------- публичное ----------
        @property
        def alive(self):
            return bool(self.hwnd) and self.hwnd in _windows

        def show(self):
            ShowWindow(self.hwnd, SW_SHOW)
            ShowWindow(self.hwnd, SW_RESTORE)
            SetForegroundWindow(self.hwnd)

        def destroy(self):
            if self.alive:
                DestroyWindow(self.hwnd)

        def refresh(self, select_key=None):
            """Перечитывает список скинов и перерисовывает окно."""
            items = list(self.hooks.list_skins())
            self.st["items"] = items
            self.st["selected"] = self.hooks.current_key() if select_key is None else select_key
            self.ui.assign_outlines([k for _, k in items])
            self._clamp_scroll()
            self.redraw()

        def set_status(self, text):
            self.st["status"] = text or ""
            if self.alive:
                SetTimer(self.hwnd, TIMER_STATUS, int(STATUS_SECONDS * 1000), None)
                self.redraw()

        def redraw(self, pos=None):
            """Просит фоновый поток нарисовать новый кадр (частые запросы сливаются в один)."""
            if not self.alive:
                return
            if pos is not None:
                self._pending_pos = pos
            with self._cv:
                self._req = (self.size, dict(self.st), self._pending_pos)
                self._cv.notify()

        def _render_loop(self, hwnd):
            try:
                sys.setswitchinterval(0.001)             # главный поток быстрее получает управление обратно
            except Exception:
                pass
            while True:
                with self._cv:
                    while self._req is None and not self._stop:
                        self._cv.wait()
                    if self._stop:
                        return
                    req, self._req = self._req, None
                (W, H), st, pos = req
                try:
                    img = self.ui.render(W, H, st, self.hooks.skin_path)
                    r, g, b, a = img.convert("RGBa").split()
                    data = Image.merge("RGBA", (b, g, r, a)).tobytes()
                except Exception:
                    traceback.print_exc()
                    continue
                with self._cv:
                    self._ready = (W, H, pos, data)
                PostMessageW(hwnd, WM_FRAME, 0, 0)

        def _show_ready_frame(self):
            with self._cv:
                frame, self._ready = self._ready, None
            if frame is None:
                return
            W, H, pos, data = frame
            if (W, H) != tuple(self.size):               # устаревший кадр: следом придёт актуальный
                return
            self._blit(W, H, pos, data)
            if pos is not None and pos == self._pending_pos:
                self._pending_pos = None

        # ---------- вывод кадра ----------
        def _make_buffer(self, W, H):
            if self._buf:
                SelectObject(self._hdc_mem, self._old_bmp)
                DeleteObject(self._buf[0])
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth, bmi.biHeight = W, -H
            bmi.biPlanes, bmi.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            hbmp = CreateDIBSection(self._hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not hbmp or not bits.value:
                raise ctypes.WinError(ctypes.get_last_error())
            self._old_bmp = SelectObject(self._hdc_mem, hbmp)
            self._buf = (hbmp, bits, W, H)

        def _present(self, pos):
            """Нарисовать и показать кадр прямо сейчас (только для первого кадра)."""
            W, H = self.size
            img = self.ui.render(W, H, self.st, self.hooks.skin_path)
            r, g, b, a = img.convert("RGBa").split()
            self._blit(W, H, pos, Image.merge("RGBA", (b, g, r, a)).tobytes())

        def _blit(self, W, H, pos, data):
            if self._buf is None or self._buf[2:] != (W, H):
                self._make_buffer(W, H)
            ctypes.memmove(self._buf[1].value, data, len(data))
            size, src = wt.SIZE(W, H), wt.POINT(0, 0)
            blend = BLENDFUNCTION(0, 0, 255, 1)         # AC_SRC_OVER, AC_SRC_ALPHA
            dst = wt.POINT(pos[0], pos[1]) if pos else None
            UpdateLayeredWindow(self.hwnd, self._hdc_screen, ctypes.byref(dst) if dst else None,
                                ctypes.byref(size), self._hdc_mem, ctypes.byref(src), 0,
                                ctypes.byref(blend), ULW_ALPHA)

        def _rect(self):
            r = wt.RECT()
            GetWindowRect(self.hwnd, ctypes.byref(r))
            return r

        def _work_area(self, hwnd):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if hwnd:
                mon = MonitorFromWindow(hwnd, 2)         # MONITOR_DEFAULTTONEAREST
            else:
                mon = MonitorFromPoint(wt.POINT(0, 0), 1)   # основной монитор
            GetMonitorInfoW(mon, ctypes.byref(mi))
            w = mi.rcWork
            return w.left, w.top, w.right, w.bottom

        # ---------- прокрутка и действия ----------
        def _geo(self):
            return self.ui.geometry(self.size[0], self.size[1], len(self.st["items"]), self.st["scroll"])

        def _clamp_scroll(self):
            self.st["scroll"] = max(0.0, min(self.st["scroll"], self._geo()["max_scroll"]))

        def _scroll_by(self, dy):
            old = self.st["scroll"]
            self.st["scroll"] = old + dy
            self._clamp_scroll()
            if self.st["scroll"] != old:
                self.st["hover"] = self._hit_cursor()
                self.redraw()

        def _hit_cursor(self):
            pt, r = wt.POINT(), self._rect()
            GetCursorPos(ctypes.byref(pt))
            return self.ui.hit(pt.x - r.left, pt.y - r.top, self.size[0], self.size[1],
                               len(self.st["items"]), self.st["scroll"])

        def _toggle_max(self):
            if not self.maxed:
                r = self._rect()
                self.restore_pos = (r.left, r.top)
                a = self._work_area(self.hwnd)
                self.size = (a[2] - a[0], a[3] - a[1])
                self.maxed = True
                self._clamp_scroll()
                self.redraw(pos=(a[0], a[1]))
            else:
                self.size = self.ui.base_size()
                self.maxed = False
                self._clamp_scroll()
                self.redraw(pos=self.restore_pos)

        def _activate(self, el):
            kind = el[0]
            if kind == "btn":
                if el[1] == "close":
                    DestroyWindow(self.hwnd)
                elif el[1] == "min":
                    ShowWindow(self.hwnd, SW_MINIMIZE)
                elif el[1] == "max":
                    self._toggle_max()
            elif kind == "delete":
                key = self.st["selected"]
                if key:
                    ok, msg = self.hooks.delete(key)
                    self.refresh()
                    self.set_status(msg)
            elif kind == "cell":
                key = self.st["items"][el[1]][1]
                self.hooks.select(key)
                self.refresh()

        def _on_drop(self, hdrop):
            added = None
            count = DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            for i in range(count):
                n = DragQueryFileW(hdrop, i, None, 0)
                buf = ctypes.create_unicode_buffer(n + 1)
                DragQueryFileW(hdrop, i, buf, n + 1)
                key = self.hooks.add(buf.value)
                if key:
                    added = key
            DragFinish(hdrop)
            if added:
                self.hooks.select(added)
                self.refresh()
                self._scroll_to(added)
                self.set_status("Skin added")
            else:
                self.set_status("Only PNG images can be added")

        def _scroll_to(self, key):
            keys = [k for _, k in self.st["items"]]
            if key not in keys:
                return
            g = self._geo()
            x, y, w, h = self.ui.cell_rect(g, keys.index(key), 0)
            vy, vh = g["vp"][1], g["vp"][3]
            top = y - vy - g["pad"]
            if top < self.st["scroll"]:
                self.st["scroll"] = top
            elif top + h > self.st["scroll"] + vh - 2 * g["pad"]:
                self.st["scroll"] = top + h - vh + 2 * g["pad"]
            self._clamp_scroll()
            self.redraw()

        # ---------- сообщения окна ----------
        def on_message(self, msg, wparam, lparam):
            st = self.st
            if msg == WM_NCHITTEST:
                r = self._rect()
                el = self.ui.hit(_s16(lparam) - r.left, _s16(lparam >> 16) - r.top, self.size[0], self.size[1],
                                 len(st["items"]), st["scroll"])
                if el is not None or self.maxed:
                    return HTCLIENT
                return HTCAPTION                     # рамка окна - таскаем за неё
            if msg == WM_SETCURSOR:
                if (lparam & 0xFFFF) == HTCLIENT:
                    hand = st["hover"] is not None and st["hover"][0] in ("btn", "cell") or \
                        (st["hover"] == ("delete",) and bool(st["selected"]))
                    SetCursor(LoadCursorW(None, ctypes.c_void_p(IDC_HAND if hand else IDC_ARROW)))
                    return 1
                return None
            if msg == WM_MOUSEMOVE:
                if not self._tracking:
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, self.hwnd, 0)
                    TrackMouseEvent(ctypes.byref(tme))
                    self._tracking = True
                el = self.ui.hit(_s16(lparam), _s16(lparam >> 16), self.size[0], self.size[1],
                                 len(st["items"]), st["scroll"])
                if el != st["hover"]:
                    st["hover"] = el
                    self.redraw()
                return 0
            if msg == WM_MOUSELEAVE:
                self._tracking = False
                if st["hover"] is not None and not st["pressed"]:
                    st["hover"] = None
                    self.redraw()
                return 0
            if msg == WM_LBUTTONDOWN:
                el = self.ui.hit(_s16(lparam), _s16(lparam >> 16), self.size[0], self.size[1],
                                 len(st["items"]), st["scroll"])
                if el is not None and el[0] != "view":
                    st["pressed"] = el
                    SetCapture(self.hwnd)
                    self.redraw()
                SetForegroundWindow(self.hwnd)
                return 0
            if msg == WM_LBUTTONUP:
                el = self.ui.hit(_s16(lparam), _s16(lparam >> 16), self.size[0], self.size[1],
                                 len(st["items"]), st["scroll"])
                pressed, st["pressed"] = st["pressed"], None
                ReleaseCapture()
                if pressed is not None and pressed == el:
                    self._activate(el)
                if self.alive:
                    st["hover"] = el
                    self.redraw()
                return 0
            if msg == WM_CAPTURECHANGED:
                if st["pressed"] is not None:
                    st["pressed"] = None
                    self.redraw()
                return 0
            if msg == WM_MOUSEWHEEL:
                delta = _s16(wparam >> 16) / 120.0
                self._scroll_by(-delta * self._geo()["pitch"][1] * 0.6)
                return 0
            if msg == WM_KEYDOWN:
                step = self._geo()["pitch"][1]
                if wparam == 0x1B:                                     # Esc
                    DestroyWindow(self.hwnd)
                elif wparam == 0x26:                                   # вверх
                    self._scroll_by(-step * 0.5)
                elif wparam == 0x28:                                   # вниз
                    self._scroll_by(step * 0.5)
                elif wparam == 0x21:                                   # PgUp
                    self._scroll_by(-self._geo()["vp"][3] * 0.8)
                elif wparam == 0x22:                                   # PgDn
                    self._scroll_by(self._geo()["vp"][3] * 0.8)
                elif wparam == 0x24:                                   # Home
                    self._scroll_by(-1e9)
                elif wparam == 0x23:                                   # End
                    self._scroll_by(1e9)
                return 0
            if msg == WM_FRAME:
                self._show_ready_frame()
                return 0
            if msg == WM_DROPFILES:
                self._on_drop(wparam)
                return 0
            if msg == WM_TIMER and wparam == TIMER_STATUS:
                KillTimer(self.hwnd, TIMER_STATUS)
                st["status"] = ""
                self.redraw()
                return 0
            if msg == WM_CLOSE:
                DestroyWindow(self.hwnd)
                return 0
            if msg == WM_DESTROY:
                with self._cv:
                    self._stop = True
                    self._cv.notify()
                _windows.pop(self.hwnd, None)
                if self._buf:
                    SelectObject(self._hdc_mem, self._old_bmp)
                    DeleteObject(self._buf[0])
                    self._buf = None
                DeleteDC(self._hdc_mem)
                ReleaseDC(None, self._hdc_screen)
                self.hwnd = None
                try:
                    self.hooks.closed()
                except Exception:
                    pass
                return 0
            return None
