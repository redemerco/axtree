#!/usr/bin/env python3
"""Demo real de ax_recorder.py contra TextEdit corriendo en esta Mac.

Simula una sesión "humana": clickear dentro del documento (foco real, no AXFocused
seteado a mano), tipear texto (CGEvent de teclado real a nivel de sesión, no
CGEventPostToPid ni AXValue directo), y clickear el botón de fullscreen de la
ventana — todo con clicks sintéticos en coordenadas de pantalla reales
(CGEventPost), igual que lo haría un humano con el mouse. El recorder NO sabe de
antemano qué se va a tocar: reconstruye el script solo a partir de lo que observa
(AXObserver a nivel-app + hit-testing de clicks).

Uso: .venv/bin/python demo_recorder.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ax_core as ax  # noqa: E402
from ax_recorder import Recorder, reconstruct_script  # noqa: E402

from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType  # noqa: E402
from Quartz import (  # noqa: E402
    CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap,
    kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft,
    CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
)


def osa(script):
    subprocess.run(["osascript", "-e", script], check=True)


def click_at(x, y):
    for etype in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
        ev = CGEventCreateMouseEvent(None, etype, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, ev)
    time.sleep(0.05)


def type_real(text):
    """Igual a ax_core.cg_type pero posteado a nivel de sesión (kCGHIDEventTap),
    no a un pid puntual — más fiel a un teclado físico real."""
    for ch in text:
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, 0, down)
            CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.02)


def center_of(el):
    pos = ax.ax_attr(el, "AXPosition")
    size = ax.ax_attr(el, "AXSize")
    ok1, pt = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok2, sz = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (ok1 and ok2):
        return None
    return (pt.x + sz.width / 2, pt.y + sz.height / 2)


def wait_windows(app_el, tries=20):
    wins = ax.ax_attr(app_el, "AXWindows") or []
    for _ in range(tries):
        if wins:
            return wins
        time.sleep(0.2)
        ax.pump_runloop()
        wins = ax.ax_attr(app_el, "AXWindows") or []
    return wins


def main():
    osa('tell application "TextEdit" to activate')
    time.sleep(0.3)
    osa('tell application "TextEdit" to make new document')  # doc descartable, dedicado al test
    time.sleep(0.6)

    app = ax.find_app("TextEdit")
    pid = app.processIdentifier()
    app_el = ax.AXUIElementCreateApplication(pid)
    windows = wait_windows(app_el)
    print(f"ventanas de TextEdit: {len(windows)} (pid {pid})")

    # el doc recien creado por "make new document" queda frontmost -> primera ventana
    win = windows[0]
    walker = ax.Walker(max_nodes=1000)
    walker.walk(win, 0)

    text_area = None
    fs_button = None
    for el in walker.elements:
        role = ax.ax_attr(el, "AXRole")
        if role == "AXTextArea" and text_area is None:
            text_area = el
        if role == "AXButton" and ax.ax_attr(el, "AXSubrole") == "AXFullScreenButton":
            fs_button = el

    if text_area is None or fs_button is None:
        sys.exit(f"no encontré targets: text_area={text_area is not None} fs_button={fs_button is not None}")

    doc_pt = center_of(text_area)
    fs_pt = center_of(fs_button)
    original_doc_value = ax.ax_attr(text_area, "AXValue") or ""
    print(f"doc textarea center: {doc_pt}  valor original: {original_doc_value!r}")
    print(f"fullscreen button center: {fs_pt}")

    typed_text = "hola recorder"

    def drive():
        click_at(*doc_pt)
        time.sleep(0.15)
        type_real(typed_text)
        time.sleep(0.3)
        click_at(*fs_pt)
        time.sleep(0.2)

    rec = Recorder(pid)
    print("\n=== grabando 5s (clicks + tipeo sintéticos, sesión-level, no AX directo) ===")
    events = rec.record(duration=5.0, drive=drive)

    print(f"\neventos crudos capturados: {len(events)}")
    for e in events:
        extra = f" value={e['value']!r}" if "value" in e else ""
        print(f"  t={e['t']:.3f} {e['kind']:28s} {e['role']:20s} {e['label']!r}{extra}")

    script = reconstruct_script(events)
    print("\n=== script reconstruido (SOLO a partir de lo observado, sin plan previo) ===")
    for ln in script:
        print(" ", ln)

    # --- verificación contra la realidad ---
    # ojo: TextEdit autocapitaliza la primera letra de la oración ("hola" -> "Hola")
    # -- eso es autocorrect de la APP, no un fallo del recorder: el recorder debe
    # reflejar fielmente el AXValue final, autocorrect incluido.
    new_doc_value = ax.ax_attr(text_area, "AXValue") or ""
    print(f"\nverificación: doc AXValue ahora = {new_doc_value!r}")
    assert typed_text.lower() in new_doc_value.lower(), "el tipeo sintético no llegó al documento"

    inferred_type_lines = [l for l in script if l.startswith("type_into")]
    ok_infer = any(typed_text.lower() in l.lower() for l in inferred_type_lines)
    press_lines = [l for l in script if l.startswith("press")]
    print(f"\n¿el script infirió el texto tipeado correctamente (case-insensitive, autocorrect de por medio)? {ok_infer}")
    print(f"¿el script infirió el press del click en el botón? {len(press_lines) > 0} -> {press_lines}")

    # cleanup: salir de fullscreen (esto es limpieza del entorno, no parte de lo grabado)
    time.sleep(0.5)
    osa('tell application "System Events" to keystroke "f" using {command down, control down}')


if __name__ == "__main__":
    main()
