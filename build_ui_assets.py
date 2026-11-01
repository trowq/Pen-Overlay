#!/usr/bin/env python3
"""Нарезка макетов в готовые ресурсы окна скинов.

Берёт PNG из папки src_png/ (layout, window, cells, buttons_normal, buttons_hover), находит
цветные зоны на макете, превращает белый фон рисунков в прозрачность и пишет в ui/:

    window.png                 рамка окна (с заливкой «бумагой» внутри контура)
    btn_<имя>_<состояние>.png  кнопки: min / max / close  x  normal / hover / pressed
    cell_1.png ... cell_N.png  обводки ячеек скинов (при показе выбираются случайно)
    layout.json                координаты всех зон (в пикселях окна, масштаб 1:1)

Запуск:  python build_ui_assets.py        (нужны Pillow, numpy, scipy - только для этого скрипта)
Если перерисуете макеты, просто положите новые PNG в src_png/ и запустите снова.
"""
import json
import os
import shutil
import sys

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src_png")
OUT = os.path.join(HERE, "ui")

EDGE = 3                       # скриншоты имеют тёмные пиксели по краю - срезаем
PAPER = (253, 251, 246)        # цвет заливки окна внутри рамки
PAPER_ALPHA = 0.97
FRAME_PAD = 3                  # запас вокруг рамки в итоговом window.png

ZONE_COLORS = {                # цвета зон макета
    "window": (0, 0, 0), "skin_area": (174, 173, 174), "cell": (215, 99, 213),
    "name": (251, 127, 13), "delete": (242, 255, 100),
    "min": (0, 182, 61), "max": (55, 166, 205), "close": (206, 27, 29),
}


def load(name):
    im = Image.open(os.path.join(SRC, name)).convert("RGB")
    a = np.array(im)
    a[:EDGE, :], a[-EDGE:, :], a[:, :EDGE], a[:, -EDGE:] = 255, 255, 255, 255
    return a


def to_alpha(rgb):
    """Белый фон -> прозрачность («color to alpha»). Возвращает float RGBA 0..1."""
    f = rgb.astype(np.float32) / 255.0
    a = np.clip((1.0 - f).max(axis=2), 0, 1)
    a[a < 0.03] = 0
    safe = np.where(a > 0, a, 1)[..., None]
    col = np.clip(1.0 - (1.0 - f) / safe, 0, 1)
    return np.dstack([col, a])


def bbox(mask, pad=0):
    ys, xs = np.where(mask)
    return (max(xs.min() - pad, 0), max(ys.min() - pad, 0),
            min(xs.max() + 1 + pad, mask.shape[1]), min(ys.max() + 1 + pad, mask.shape[0]))


def to_img(rgba):
    return Image.fromarray((np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")


def zones(layout_rgb):
    """Прямоугольники цветных зон макета: {имя: [(x, y, w, h), ...]} (слева направо, сверху вниз)."""
    im = layout_rgb.astype(int)
    out = {}
    for name, c in ZONE_COLORS.items():
        m = np.abs(im - np.array(c)).sum(2) < 30
        lab, _ = ndi.label(m)
        found = []
        for s in ndi.find_objects(lab):
            h, w = s[0].stop - s[0].start, s[1].stop - s[1].start
            if w * h > (5000 if name in ("skin_area", "window") else 120):
                found.append((s[1].start, s[0].start, w, h))
        out[name] = sorted(found, key=lambda r: (r[1] // 40, r[0]))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    lay = load("layout.png")
    z = zones(lay)
    for k in ("skin_area", "delete", "min", "max", "close", "cell", "name"):
        if not z[k]:
            sys.exit("На макете не найдена зона: " + k)

    # ---- окно: рамка + заливка внутри контура ----
    win = to_alpha(load("window.png"))
    line = win[..., 3] > 0.25
    x0, y0, x1, y1 = (int(v) for v in bbox(line, FRAME_PAD))
    closed = ndi.binary_dilation(line, iterations=5)
    inside = ndi.binary_fill_holes(closed)
    if inside.sum() < 0.5 * (x1 - x0) * (y1 - y0) * 0.5:
        sys.exit("Контур окна не замкнут - заливку построить не удалось")
    inside = ndi.binary_erosion(inside, iterations=4)
    fill = ndi.gaussian_filter(inside.astype(np.float32), 0.8) * PAPER_ALPHA
    paper = np.dstack([np.full(fill.shape + (3,), 1, np.float32) * (np.array(PAPER) / 255.0), fill])
    # линии поверх заливки (обычное «over»)
    la, pa = win[..., 3:4], paper[..., 3:4]
    oa = la + pa * (1 - la)
    oc = (win[..., :3] * la + paper[..., :3] * pa * (1 - la)) / np.where(oa > 0, oa, 1)
    comp = np.dstack([oc, oa])[y0:y1, x0:x1]
    to_img(comp).save(os.path.join(OUT, "window.png"))
    W, H = int(x1 - x0), int(y1 - y0)
    print("window.png", W, "x", H, "(начало в координатах макета: %d,%d)" % (x0, y0))

    def rel(r):                      # координаты макета -> координаты окна
        return [int(r[0] - x0), int(r[1] - y0), int(r[2]), int(r[3])]

    # ---- кнопки ----
    for state in ("normal", "hover"):
        rgb = load("buttons_%s.png" % state)
        art = to_alpha(rgb)
        mask = art[..., 3] > 0.15
        r_, g_, b_ = (rgb[..., i].astype(int) for i in range(3))
        cls = {                                   # глифы различаем по цвету
            "close": (r_ > b_ + 40) & (r_ > g_ + 40),
            "min": (g_ >= r_ + 25) & (g_ >= b_ + 25),
            "max": (b_ > r_ + 40),
        }
        for name in ("min", "max", "close"):
            sub = mask & cls[name]
            lab_, n_ = ndi.label(ndi.binary_dilation(sub, iterations=3))
            if n_ > 1:                           # выбрасываем мелкий мусор (края других глифов)
                sizes_ = ndi.sum(sub, lab_, range(1, n_ + 1))
                sub = sub & np.isin(lab_, [i + 1 for i, v in enumerate(sizes_) if v > 0.2 * max(sizes_)])
            if sub.sum() < 100:
                sys.exit("Не нашёл кнопку %s в buttons_%s.png" % (name, state))
            bx0, by0, bx1, by1 = bbox(sub, 2)
            g = art[by0:by1, bx0:bx1].copy()
            keep = ndi.binary_dilation(sub[by0:by1, bx0:bx1], iterations=2)
            g[:, :, 3] *= keep
            to_img(g).save(os.path.join(OUT, "btn_%s_%s.png" % (name, state)))
    # нажатие = повтор обычного вида (по просьбе); отдельные файлы - чтобы можно было перерисовать
    for name in ("min", "max", "close"):
        shutil.copyfile(os.path.join(OUT, "btn_%s_normal.png" % name),
                        os.path.join(OUT, "btn_%s_pressed.png" % name))

    # ---- обводки ячеек ----
    cells = to_alpha(load("cells.png"))
    raw = cells[..., 3] > 0.2
    lab0, n0 = ndi.label(raw, structure=np.ones((3, 3)))        # убираем мелкие случайные штрихи (< 300 пикс.)
    areas = ndi.sum(raw, lab0, range(1, n0 + 1))
    for i, a in enumerate(areas, 1):
        if a < 300:
            cells[lab0 == i] = 0
    cm = ndi.binary_dilation(cells[..., 3] > 0.2, iterations=3)
    lab, _ = ndi.label(cm)
    boxes = []
    for s in ndi.find_objects(lab):
        h, w = s[0].stop - s[0].start, s[1].stop - s[1].start
        if w > 40 and h > 40:
            boxes.append((s[1].start, s[0].start, s[1].stop, s[0].stop))
    boxes.sort(key=lambda b: (b[1] // 60, b[0]))
    for f in os.listdir(OUT):
        if f.startswith("cell_"):
            os.remove(os.path.join(OUT, f))
    sizes = []
    for i, (a, b, c, d) in enumerate(boxes, 1):
        crop = cells[max(b - 2, 0):d + 2, max(a - 2, 0):c + 2]
        to_img(crop).save(os.path.join(OUT, "cell_%d.png" % i))
        sizes.append((int(c - a), int(d - b)))
    slot_w = int(round(sum(int(s[0]) for s in sizes) / len(sizes)))
    slot_h = int(round(sum(int(s[1]) for s in sizes) / len(sizes)))

    # ---- координаты ----
    cell_rects = z["cell"]
    thumb_w = int(round(sum(r[2] for r in cell_rects) / len(cell_rects)))
    thumb_h = int(round(sum(r[3] for r in cell_rects) / len(cell_rects)))
    sa = z["skin_area"][0]
    d = z["delete"][0]
    layout = {
        "_note": "Координаты [x, y, w, h] в пикселях окна при масштабе 1:1. Создано build_ui_assets.py.",
        "window": [W, H],
        "buttons": {n: rel(z[n][0]) for n in ("min", "max", "close")},   # глиф вписывается в прямоугольник
        "delete": rel(d),
        "skin_area": rel(sa),
        "slot": [slot_w, slot_h],            # размер обводки ячейки
        "thumb": [thumb_w, thumb_h],         # размер картинки скина внутри обводки
        "label_h": 18,                       # высота строки с названием под обводкой
        "pitch": [slot_w + 9, slot_h + 2 + 18 + 5],   # шаг сетки по x и y
        "slice": 60,                         # поля 9-slice рамки окна (для растягивания)
        "cells": len(boxes),
        "name_hint": rel(z["name"][0]),      # оранжевая метка макета (для справки)
        "min_window": [W, H],
    }
    with open(os.path.join(OUT, "layout.json"), "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
    print("layout.json готов; ячеек-обводок:", len(boxes), "слот", (slot_w, slot_h), "миниатюра", (thumb_w, thumb_h))


if __name__ == "__main__":
    main()
