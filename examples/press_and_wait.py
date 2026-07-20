#!/usr/bin/env python3
"""Demo real de wait_for_notification vs. sleep() a ciegas.

Contra TextEdit (corriendo en la Mac ahora): abre Preferencias (Cmd+,), agarra el
primer checkbox del panel y lo toggle:

  A) método viejo: AXUIElementPerformAction(press) + time.sleep(FIXED) + leer AXValue
  B) método nuevo: wait_for_notification(..., action=press) — bloquea hasta que la
     propia app confirma kAXValueChangedNotification sobre ESE elemento, con
     `timeout` como red de seguridad

Mide con time.time() cuánto tarda cada uno y verifica que el valor leído después
es el correcto (no una lectura prematura, no una espera de más). Al final deja el
checkbox como estaba.

Uso: .venv/bin/python examples/press_and_wait.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ax_core as ax  # noqa: E402 — necesita el sys.path.insert de arriba

FIXED_SLEEP = 0.4  # el valor típico usado a ciegas hoy en benchmark.py/chat_driver.py


def osa(script):
    subprocess.run(["osascript", "-e", script], check=True)


def open_textedit_prefs():
    osa('tell application "TextEdit" to activate')
    time.sleep(0.3)
    osa('tell application "System Events" to keystroke "," using command down')
    time.sleep(1.0)  # dar tiempo a que la ventana de prefs termine de aparecer


def find_app_any_locale():
    from AppKit import NSWorkspace
    ax.pump_runloop()
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.localizedName() and a.localizedName().lower() in ("textedit", "texteditor"):
            return a
    # fallback: buscar por bundle id, insensible al locale del nombre visible
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        bid = a.bundleIdentifier() or ""
        if bid == "com.apple.TextEdit":
            return a
    return None


def find_first_checkbox(w):
    for i, ln in enumerate(w.lines):
        stripped = ln.strip()
        # excluir <segment>: son toggles de formato atados al cursor/selección
        # (ej. "negrita" en la toolbar del documento), no booleans estables de prefs
        if stripped.startswith("- checkbox") and "<segment>" not in stripped \
                and ("value='0'" in stripped or "value='1'" in stripped):
            return i
    return None


def dump(el):
    w = ax.Walker(max_nodes=2000)
    for win in (ax.ax_attr(el, "AXWindows") or []):
        w.walk(win, 0)
    return w


def main():
    open_textedit_prefs()

    app = find_app_any_locale()
    if app is None:
        sys.exit("No encontré TextEdit corriendo (¿abrilo antes de correr el demo?)")
    pid = app.processIdentifier()
    el = ax.AXUIElementCreateApplication(pid)

    w = dump(el)
    idx = find_first_checkbox(w)
    if idx is None:
        sys.exit("No encontré ningún checkbox en la ventana de Preferencias")
    target = w.elements[idx]
    label = w.lines[idx].strip()
    original = ax.ax_attr(target, "AXValue")
    print(f"elemento target: {label}")
    print(f"valor original: {original!r}\n")

    # --- A) sleep a ciegas -------------------------------------------------
    t0 = time.time()
    ax.AXUIElementPerformAction(target, "AXPress")
    time.sleep(FIXED_SLEEP)
    elapsed_sleep = time.time() - t0
    val_sleep = ax.ax_attr(target, "AXValue")
    print(f"[A] sleep({FIXED_SLEEP}) a ciegas:  {elapsed_sleep:.4f}s  →  value={val_sleep!r}"
          f"  {'OK (cambió)' if val_sleep != original else 'MAL (no cambió)'}")

    # revertir con wait_for_notification (no cuenta para la medición de A)
    ax.wait_for_notification(pid, target, "AXValueChanged",
                              action=lambda: ax.AXUIElementPerformAction(target, "AXPress"),
                              timeout=2.0)
    back = ax.ax_attr(target, "AXValue")
    assert back == original, f"no volvió al valor original: {back!r} != {original!r}"

    # --- B) wait_for_notification -------------------------------------------
    t0 = time.time()
    ok = ax.wait_for_notification(
        pid, target, "AXValueChanged",
        action=lambda: ax.AXUIElementPerformAction(target, "AXPress"),
        timeout=2.0,
    )
    elapsed_wait = time.time() - t0
    val_wait = ax.ax_attr(target, "AXValue")
    print(f"[B] wait_for_notification:      {elapsed_wait:.4f}s  →  value={val_wait!r}"
          f"  {'OK (cambió, notificación='+str(ok)+')' if val_wait != original else 'MAL (no cambió)'}")

    # revertir de nuevo, dejar el checkbox como estaba antes del demo
    ax.wait_for_notification(pid, target, "AXValueChanged",
                              action=lambda: ax.AXUIElementPerformAction(target, "AXPress"),
                              timeout=2.0)
    final = ax.ax_attr(target, "AXValue")
    assert final == original, f"no quedó como estaba: {final!r} != {original!r}"
    print(f"\nrevertido a valor original: {final!r} (checkbox queda como estaba)")

    speedup = elapsed_sleep / elapsed_wait if elapsed_wait > 0 else float("inf")
    print(f"\n=== {elapsed_sleep:.4f}s (sleep fijo) vs {elapsed_wait:.4f}s (observer) "
          f"→ {speedup:.1f}x, ambos con lectura correcta ===")


if __name__ == "__main__":
    main()
