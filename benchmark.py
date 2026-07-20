#!/usr/bin/env python3
"""Benchmark: crear página en Notion, escribir texto, borrarla. Timing interno con
time.time() (nada de wrappers de shell por paso, para no ensuciar la medición ni
disparar un permiso nuevo por cada línea)."""
import sys
import time

import ax_core as ax

TIMES = {}


def tic(label):
    TIMES[label] = time.time()


def toc(label, start_label):
    dt = time.time() - TIMES[start_label]
    print(f"{label}: {dt:.3f}s")
    return dt


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
    pid = app.processIdentifier()

    w = dump(el)
    focused_idx = find(w, "textarea", "<focused>")
    if focused_idx is None:
        sys.exit("No encontré un textarea con foco (¿la página en blanco sigue abierta?)")
    print(f"elemento con foco: {w.lines[focused_idx].strip()}")

    tic("t0")
    method = ax.type_into(w.elements[focused_idx], pid, "Benchmark axtree — borrar")
    total = time.time() - TIMES["t0"]
    print(f"escribir ({method}): {total:.3f}s")

    time.sleep(0.4)
    w = dump(el)
    acciones_idx = find(w, "popupbutton", "acciones")
    if acciones_idx is None:
        sys.exit("No encontré el menú 'Acciones' para borrar la página")

    tic("t1")
    err = ax.AXUIElementPerformAction(w.elements[acciones_idx], "AXPress")
    open_menu = time.time() - TIMES["t1"]
    print(f"abrir menú Acciones: {open_menu:.3f}s (err={err})")

    time.sleep(0.4)
    w = dump(el)
    delete_idx = find(w, "eliminar") or find(w, "papelera") or find(w, "delete") or find(w, "trash")
    if delete_idx is None:
        print("No encontré la opción de borrar en el menú. Líneas visibles:")
        for ln in w.lines:
            print(" ", ln.strip())
        sys.exit(1)
    print(f"opción encontrada: {w.lines[delete_idx].strip()}")

    tic("t2")
    err = ax.AXUIElementPerformAction(w.elements[delete_idx], "AXPress")
    delete_time = time.time() - TIMES["t2"]
    print(f"borrar página: {delete_time:.3f}s (err={err})")

    total_flow = total + open_menu + delete_time
    print(f"\n=== TOTAL flujo (escribir+abrir menú+borrar), sin contar crear página: {total_flow:.3f}s ===")


if __name__ == "__main__":
    main()
