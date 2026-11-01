<div align="center">

# ✒️ Pen Overlay

**A tiny Windows overlay that draws a stylus image right at your pen tip and tilts it exactly like your real pen.**

![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white)
![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)
![Pillow](https://img.shields.io/badge/Pillow-required-green)

🇬🇧 English · [🇷🇺 Русский](README.ru.md)

<img src="docs/preview.png" alt="Skin picker window" width="520">

</div>

> [!WARNING]
> **IMPORTANT: it works through Windows Ink!**
> Tested **only on the XP-Pen Deco 01 V2**. It may not work on your device!

---

## ✨ What it does

I made this program for myself, and I'll be happy if it turns out useful to someone else too. 💜

Pen Overlay shows a PNG picture of a stylus (a *skin*) at the position of your graphics-tablet pen and rotates it in the direction the pen is tilted. It is handy for screen recordings, streams and tutorials, where viewers should see *where the pen is* and *how it is held*.

- 🪟 **Click-through overlay.** The picture is drawn in a transparent, always-on-top window that never steals focus or clicks.
- 🎨 **Skins.** Add as many stylus images as you like and switch between them in a hand-drawn skin picker window.
- 🧭 **Calibration.** Fixes an offset between the picture and the real pen tip, if there is one.
- 🫥 **Auto-hide.** The stylus appears when the pen starts working and disappears after 3 seconds without pen data.
- 🔔 **Tray icon.** `Settings`, `Reload` and `Exit` are one click away.

---

## ⚠️ Making a skin: the tip goes to the TOP-LEFT corner

> ### 🔴 The pen tip must be in the **top-left** corner of the picture, and the body must go towards the **bottom-right**.
>
> The program rotates the image around its tip. It finds the tip itself: it is the **opaque pixel closest to the top-left corner** of the image. If the tip is anywhere else, the skin will rotate around the wrong point and will not sit on your real pen tip.

**Checklist for a good skin**

- [x] **PNG with a transparent background** (empty margins are cropped automatically).
- [x] **Tip in the top-left corner**, the body runs diagonally to the bottom-right (45°).
- [x] **Recommended size: a square canvas of 520 × 520 px** (the stylus runs corner to corner). Any size works, but see the note below.
- [x] Big images are scaled down so the longest side is 260 px (`SPRITE_LENGTH_PX`). Small images are **never scaled up**, so below 260 px the skin is shown at its own size.
- [x] Do not put stray pixels, shadows or glow in the top-left corner: they would be mistaken for the tip.

> 💡 **Why 520 × 520?** On screen the stylus is 260 px long, so 520 px is exactly 2× that: the picture is scaled down cleanly and stays sharp, even on high-DPI screens or with `--scale 1.5`. If you plan to use `--size 400` or larger, draw the skin at least that big, because the program does not enlarge images. Anything above ~1000 px just wastes memory and gives no visible gain.
>
> Empty transparent margins are cropped, so the size that counts is the size of the stylus itself, not of the canvas.

**If your art is drawn differently**, you do not have to redraw it. In the settings block at the top of `pen_overlay.pyw` you can change:

| Setting | Meaning |
|---|---|
| `TIP_POINT = (x, y)` | tip position on the *original* image, instead of automatic detection |
| `BODY_DIRECTION_DEG` | where the back of the stylus points, clockwise from "right" (`45` = bottom-right) |

---

## 🚀 Installation

1. Install [Python 3.8+](https://www.python.org/downloads/) (tick *Add Python to PATH*).
2. Install Pillow:
   ```bash
   pip install pillow
   ```
3. Download the project and keep these files together:
   ```
   pen_overlay.pyw
   skin_window.py
   stylus.png          ← default skin (optional, a built-in placeholder is used without it)
   banner.png          ← tray icon, 400×400 (optional)
   ui/                 ← graphics and font of the skin window
   skins/              ← your skins (created automatically)
   ```
4. Double-click `pen_overlay.pyw` (or run `python pen_overlay.pyw`).

### Command-line options

```bash
python pen_overlay.pyw                # uses the skin chosen in Settings
python pen_overlay.pyw my.png         # one specific image
python pen_overlay.pyw --size 400     # on-screen length in pixels (default 260)
python pen_overlay.pyw --scale 1.2    # extra size multiplier
python pen_overlay.pyw --no-stretch   # do not change length with tilt
python pen_overlay.pyw --debug        # print pen data and write pen_overlay_log.txt
```

---

## 🎛️ Using it

### Hotkeys (work from any window)

| Keys | Action |
|---|---|
| `Ctrl` + `Alt` + `Q` | Exit |
| `Ctrl` + `Alt` + `1` | Calibration: put the pen in the **top-left** corner of the screen and press |
| `Ctrl` + `Alt` + `2` | Calibration: put the pen in the **bottom-right** corner of the screen and press |
| `Ctrl` + `Alt` + `0` | Reset calibration |
| `Ctrl` + `Alt` + `T` | Rotation test (the stylus spins by itself) |

Calibration is only needed if the picture is shifted relative to the real pen tip. It is saved automatically.

### Tray icon

Click the icon in the notification area for the menu: **Settings** (skin picker), **Reload** (restart the program), **Exit**.

### Skin picker window

| Action | How |
|---|---|
| Add a skin | drag and drop one or more **PNG** files into the window |
| Use a skin | click it (it is applied immediately and remembered) |
| Delete a skin | select it and press **Delete** (the default skin cannot be deleted) |
| Scroll | mouse wheel, `↑` `↓`, `PgUp` `PgDn`, `Home` `End` |
| Window | drag it by its frame; the buttons in the top-right corner minimize, maximize and close it (`Esc` also closes) |

The name under each skin is its file name without `.png`, so give your files nice names. Each skin gets a random hand-drawn frame.

> The skin names are drawn with the *PenHand* font, which has Latin letters and digits only. Other characters (for example Cyrillic) fall back to a system font.

---

## 🗂️ Project structure

```
pen_overlay.pyw        main program: pen input, overlay, tray, skins
skin_window.py         the skin picker window (hand-drawn, borderless)
build_ui_assets.py     optional: rebuilds ui/ from the mock-up pictures in src_png/
ui/                    window frame, button states, cell outlines, layout.json, PenHand.ttf
src_png/               original mock-ups the ui/ graphics are cut from
skins/                 your stylus skins (PNG)
stylus.png             default skin
banner.png             tray icon
pen_overlay_settings.json   chosen skin (created automatically)
pen_overlay_calib.json      calibration (created automatically)
```

**Redrawing the window.** Replace the pictures in `src_png/` (keep the colour zones of `layout.png`), then run `python build_ui_assets.py` (needs `numpy` and `scipy`, only for this step).

---

## 🔧 Tuning

All settings are at the top of `pen_overlay.pyw`, for example:

| Setting | Default | Meaning |
|---|---|---|
| `SPRITE_LENGTH_PX` | `260` | on-screen length of the stylus |
| `SMOOTHING_TAU` | `0.07` | tilt smoothing in seconds (`0` = none) |
| `HIDE_AFTER_SEC` | `3.0` | hide after this many seconds without pen data (`0` = always visible) |
| `INVERT_X` / `INVERT_Y` | `False` | mirror horizontally / vertically |
| `STRETCH_BY_TILT` | `False` | shorten the stylus when the pen is upright |

---

## ❓ Troubleshooting

- **Nothing appears.** Move the pen over the tablet first. Run with `--debug` and read `pen_overlay_log.txt`.
- **The picture sits next to the pen tip.** Check the skin (tip in the **top-left** corner), then try calibration (`Ctrl+Alt+1` / `Ctrl+Alt+2`).
- **The stylus rotates around the wrong point.** The top-left corner of your PNG contains extra pixels. Clean it up or set `TIP_POINT`.
- **Wrong direction.** Set `BODY_DIRECTION_DEG` or flip the picture.

---

## 📬 Contacts

Made by **trowq431**. 

- 	I stream my drawing somtimes - My twitch: https://www.twitch.tv/trowq431
