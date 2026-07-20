#!/usr/bin/env python3
"""Driver de la demo: opera la sesión de Claude de la otra pestaña de Terminal vía AX.

Uso:
  chat_driver.py send "texto"   # selecciona la pestaña -zsh, verifica foco y manda el texto + Enter
  chat_driver.py read           # lee la pantalla completa de esa pestaña
  chat_driver.py launch         # tipea "claude" + Enter (arranca la sesión)
"""
import sys
import time

SRC = open(__file__.replace("chat_driver.py", "axtree.py")).read().split("if __name__")[0]
ns = {}
exec(compile(SRC, "axtree.py", "exec"), ns)

app = ns["find_app"]("Terminal")
PID = app.processIdentifier()
APP_EL = ns["AXUIElementCreateApplication"](PID)


def walk():
    w = ns["Walker"]()
    for win in (ns["ax_attr"](APP_EL, "AXWindows") or []):
        w.walk(win, 0)
    return w


def target_tab(w):
    """La pestaña de la demo: última tabbutton de la lista."""
    cands = [i for i, ln in enumerate(w.lines) if "tabbutton" in ln]
    return cands[-1] if cands else None


def ensure_selected():
    w = walk()
    t = target_tab(w)
    if t is None:
        sys.exit("no encontré la pestaña target")
    if "value='True'" not in w.lines[t]:
        ns["AXUIElementPerformAction"](w.elements[t], "AXPress")
        time.sleep(0.6)
        w = walk()
        t = target_tab(w)
        if "value='True'" not in w.lines[t]:
            sys.exit("ABORT: no pude dejar la pestaña seleccionada")
    return w


def read_screen(w=None):
    w = w or ensure_selected()
    ta = [i for i, ln in enumerate(w.lines) if "textarea" in ln]
    if not ta:
        sys.exit("no hay textarea")
    return str(ns["ax_attr"](w.elements[ta[0]], "AXValue") or "")


def type_text(text, enter=True):
    ensure_selected()
    # re-verificación inmediatamente antes de tipear (anti race de foco):
    # walk fresco e índice recalculado sobre ESE walk (no mezclar walks)
    w = walk()
    t = target_tab(w)
    if t is None or "value='True'" not in w.lines[t]:
        sys.exit("ABORT: el foco se movió justo antes de tipear")
    for i in range(0, len(text), 16):
        chunk = text[i:i + 16]
        for down in (True, False):
            ev = ns["CGEventCreateKeyboardEvent"](None, 0, down)
            ns["CGEventKeyboardSetUnicodeString"](ev, len(chunk), chunk)
            ns["CGEventPostToPid"](PID, ev)
        time.sleep(0.04)
    if enter:
        time.sleep(0.3)
        for down in (True, False):
            ns["CGEventPostToPid"](PID, ns["CGEventCreateKeyboardEvent"](None, 36, down))
    # verificar que el tipeo cayó donde debía
    time.sleep(0.4)
    return read_screen(walk())


cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
if cmd == "read":
    print(read_screen()[-2500:])
elif cmd == "launch":
    print(type_text("claude")[-600:])
elif cmd == "send":
    print(type_text(sys.argv[2])[-1200:])
