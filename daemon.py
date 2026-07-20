#!/usr/bin/env python3
"""axtree daemon — mantiene el último árbol AX de cada app vivo en memoria entre llamadas.

get_tree camina el árbol (costoso: ~7 IPC calls por nodo). press/type_into/read/under
no necesitan volver a caminar nada: reusan los AXUIElement ya resueltos por la última
exploración de esa app. Un `dump` normal usa el cache; `--refresh` fuerza re-walk.

Arranca en foreground: .venv/bin/python daemon.py
Socket: /tmp/axtree-<uid>.sock — JSON, un mensaje por línea.
"""
import json
import os
import socket
import socketserver
import sys
import threading
import time

import ax_core as ax

SOCK_PATH = f"/tmp/axtree-{os.getuid()}.sock"

CACHE = {}  # app_name.lower() -> {"walker": Walker, "pid": int, "header": str, "ts": float}
LOCK = threading.Lock()


def full_walk(app, el, menus, raw, max_nodes):
    ax.AXUIElementSetAttributeValue(el, "AXManualAccessibility", ax.kCFBooleanTrue)
    w = ax.Walker(flatten=not raw, max_nodes=max_nodes)
    windows = ax.ax_attr(el, "AXWindows") or []
    header = f"# {app.localizedName()} (pid {app.processIdentifier()}) — {len(windows)} ventana(s)"
    for win in windows:
        w.walk(win, 0)
    if menus:
        mb = ax.ax_attr(el, "AXMenuBar")
        if mb is not None:
            w.walk(mb, 0)
    return w, header


def handle(req):
    cmd = req.get("cmd")

    if cmd == "list":
        ax.pump_runloop()
        names = [a.localizedName() for a in ax.NSWorkspace.sharedWorkspace().runningApplications()
                 if a.activationPolicy() == 0]
        return {"ok": True, "text": "\n".join(names)}

    if cmd == "ping":
        with LOCK:
            apps = list(CACHE.keys())
        return {"ok": True, "text": "pong", "cached_apps": apps}

    app_name = req.get("app")
    if not app_name:
        return {"ok": False, "error": "falta 'app'"}

    with LOCK:
        key = app_name.lower()
        app = ax.find_app(app_name)
        if app is None:
            return {"ok": False, "error": f"No encontré una app corriendo que matchee {app_name!r}"}
        pid = app.processIdentifier()
        el = ax.AXUIElementCreateApplication(pid)

        entry = CACHE.get(key)
        refresh = bool(req.get("refresh"))
        cached = entry is not None and not refresh
        if not cached:
            w, header = full_walk(app, el, req.get("menus", False), req.get("raw", False),
                                   req.get("max_nodes", 1500))
            entry = {"walker": w, "pid": pid, "header": header, "ts": time.time()}
            CACHE[key] = entry
        w = entry["walker"]
        header = entry["header"]

        def parse_eid(s):
            eid = int(str(s).lstrip("e"))
            if not 0 <= eid < len(w.elements):
                raise ValueError(f"e{eid} no existe en este dump ({len(w.elements)} elementos)")
            return eid

        under = req.get("under")
        if under is not None:
            try:
                root = w.elements[parse_eid(under)]
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            w = ax.Walker(flatten=not req.get("raw", False), max_nodes=req.get("max_nodes", 1500))
            w.walk(root, 0)
            entry["walker"] = w  # próximas acciones referencian esta vista (igual al modo one-shot)

        text = "\n".join(w.lines)
        quiet = bool(req.get("quiet"))
        out = (header + "\n" + text) if not quiet else ""
        note = " (TRUNCADO)" if w.truncated else ""
        stats = f"# {w.count} nodos, ~{len(text) // 4} tokens{note} [{'cache' if cached else 'walk'}]"

        if req.get("read") is not None:
            try:
                eid = parse_eid(req["read"])
            except ValueError as e:
                return {"ok": False, "error": str(e), "stats": stats}
            val = ax.ax_attr(w.elements[eid], "AXValue")
            return {"ok": True, "text": out, "stats": stats,
                     "result": "" if val is None else str(val)}

        if req.get("type_into") is not None:
            text_in = req.get("type_text")
            if not text_in:
                return {"ok": False, "error": "--type-into requiere --type TXT", "stats": stats}
            try:
                eid = parse_eid(req["type_into"])
            except ValueError as e:
                return {"ok": False, "error": str(e), "stats": stats}
            method = ax.type_into(w.elements[eid], pid, text_in)
            if req.get("key"):
                time.sleep(0.15)
                ax.cg_key(pid, req["key"])
            return {"ok": True, "text": out, "stats": stats,
                     "result": f"TYPE_INTO OK ({method}) → {w.lines[eid].strip()}"}

        if req.get("type_text") or req.get("key"):
            if req.get("type_text"):
                ax.cg_type(pid, req["type_text"])
            if req.get("key"):
                time.sleep(0.15)
                ax.cg_key(pid, req["key"])
            return {"ok": True, "text": out, "stats": stats,
                     "result": f"TYPE OK → {len(req.get('type_text') or '')} chars"}

        if req.get("press") is not None:
            try:
                eid = parse_eid(req["press"])
            except ValueError as e:
                return {"ok": False, "error": str(e), "stats": stats}
            target = w.elements[eid]
            err = ax.AXUIElementPerformAction(target, req.get("action", "AXPress"))
            line = w.lines[eid].strip()
            if err == 0:
                return {"ok": True, "text": out, "stats": stats, "result": f"PRESS OK → {line}"}
            return {"ok": False, "error": f"PRESS FALLÓ (err {err}) → {line}", "stats": stats}

        return {"ok": True, "text": out, "stats": stats}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        try:
            req = json.loads(line.decode())
            resp = handle(req)
        except Exception as e:
            resp = {"ok": False, "error": f"daemon exception: {type(e).__name__}: {e}"}
        self.wfile.write((json.dumps(resp) + "\n").encode())


def main():
    if not ax.AXIsProcessTrusted():
        sys.exit("Sin permiso de Accesibilidad (System Settings → Privacy → Accessibility).")
    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)
    server = socketserver.ThreadingUnixStreamServer(SOCK_PATH, Handler)
    print(f"axtree daemon escuchando en {SOCK_PATH}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)


if __name__ == "__main__":
    main()
