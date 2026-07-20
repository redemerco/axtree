#!/usr/bin/env python3
"""Benchmark screen-capture: mismo flujo (crear página, escribir, borrar) pero con
las primitivas mecánicas de computer-use: screenshot + click por coordenadas +
keystrokes del sistema (no AX). Las coordenadas se leen de AX solo como el
"oráculo" (reemplaza al paso de visión que en un agente real cuesta latencia de
modelo aparte, no medida acá) — el click y el tipeo son ciegos, igual que
computer-use real.
"""
import subprocess
import sys
import time

import ax_core as ax
from Quartz import (
    CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap,
    kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft,
    CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
)

TIMES = []


def timed(label, fn, *a, **kw):
    t0 = time.time()
    result = fn(*a, **kw)
    dt = time.time() - t0
    TIMES.append((label, dt))
    print(f"{label}: {dt:.3f}s")
    return result


def screenshot(path="/tmp/bench_shot.png"):
    subprocess.run(["screencapture", "-x", path], check=True)


def click_at(x, y):
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.03)
    CGEventPost(kCGHIDEventTap, up)


def type_system(text):
    for ch in text:
        ev = CGEventCreateKeyboardEvent(None, 0, True)
        CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
        CGEventPost(kCGHIDEventTap, ev)
        ev2 = CGEventCreateKeyboardEvent(None, 0, False)
        CGEventKeyboardSetUnicodeString(ev2, len(ch), ch)
        CGEventPost(kCGHIDEventTap, ev2)
        time.sleep(0.01)


def ax_center(el):
    pos = ax.ax_attr(el, "AXPosition")
    size = ax.ax_attr(el, "AXSize")
    return pos.x + size.width / 2, pos.y + size.height / 2


def dump(app_el, max_nodes=800):
    w = ax.Walker(max_nodes=max_nodes)
    for win in (ax.ax_attr(app_el, "AXWindows") or []):
        w.walk(win, 0)
    return w


def find(w, *needles):
    for i, ln in enumerate(w.lines):
        if all(n.lower() in ln.lower() for n in needles):
            return i
    return None


def main():
    app = ax.find_app("Notion")
    if app is None:
        sys.exit("Notion no está corriendo")
    el = ax.AXUIElementCreateApplication(app.processIdentifier())

    # --- 1. screenshot + click "Agregar página nueva" ---
    timed("screenshot 1", screenshot)
    w = dump(el)
    idx = find(w, "agregar página nueva")
    if idx is None:
        sys.exit("No encontré 'Agregar página nueva' (¿sidebar cerrada?)")
    x, y = ax_center(w.elements[idx])
    timed("click 'Agregar página nueva' (coords vía AX, ciego)", click_at, x, y)
    time.sleep(0.5)

    # --- 2. screenshot + click "Página vacía" ---
    timed("screenshot 2", screenshot)
    w = dump(el)
    idx = find(w, "página vacía")
    if idx is None:
        sys.exit("No encontré 'Página vacía' en la galería de plantillas")
    x, y = ax_center(w.elements[idx])
    timed("click 'Página vacía'", click_at, x, y)
    time.sleep(0.5)

    # --- 3. screenshot + tipear (el título ya debería tener foco) ---
    timed("screenshot 3", screenshot)
    timed("tipear título (keystrokes del sistema)", type_system, "Benchmark screencap — borrar")
    time.sleep(0.3)

    # --- 4. screenshot + click "Acciones" ---
    timed("screenshot 4", screenshot)
    w = dump(el)
    idx = find(w, "popupbutton", "acciones")
    if idx is None:
        sys.exit("No encontré el menú Acciones")
    x, y = ax_center(w.elements[idx])
    timed("click 'Acciones'", click_at, x, y)
    time.sleep(0.4)

    # --- 5. screenshot + click "Mover a la Papelera" ---
    timed("screenshot 5", screenshot)
    w = dump(el)
    idx = find(w, "papelera") or find(w, "eliminar")
    if idx is None:
        print("No encontré la opción de borrar. Líneas:")
        for ln in w.lines:
            print(" ", ln.strip())
        sys.exit(1)
    x, y = ax_center(w.elements[idx])
    timed("click 'Mover a la Papelera'", click_at, x, y)
    time.sleep(0.3)
    timed("screenshot 6 (verificación final)", screenshot)

    total = sum(dt for _, dt in TIMES)
    print(f"\n=== TOTAL mecánico (screenshots + clicks + tipeo, SIN latencia de visión): {total:.3f}s ===")
    n_screens = sum(1 for label, _ in TIMES if "screenshot" in label)
    print(f"=== {n_screens} screenshots tomados — cada uno en un agente real necesita además una llamada")
    print(f"    al modelo de visión para decidir el próximo click (no medida acá, típicamente 1-4s c/u) ===")


if __name__ == "__main__":
    main()
