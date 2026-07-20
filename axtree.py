#!/usr/bin/env python3
"""axtree — árbol de accesibilidad de macOS como texto compacto, para agentes.

Uso:
  axtree.py <app>              # árbol de las ventanas de la app
  axtree.py <app> --menus      # incluye la barra de menú
  axtree.py <app> --raw        # sin flatten de grupos
  axtree.py <app> --under eN   # solo el subárbol de eN (¡los eN se renumeran!)
  axtree.py <app> --type-into eN --type TXT   # tipear en un elemento (AX directo, fallback CGEvent)
  axtree.py <app> --press eN   # ejecutar una acción (default AXPress)
  axtree.py <app> --refresh    # ignorar el cache del daemon y re-caminar el árbol
  axtree.py --list             # apps corriendo con UI
  axtree.py --daemon-stop      # apagar el daemon (shutdown limpio, borra el socket)

Salida: una línea por elemento — rol "label" value=… (acciones) [eN]

Si el daemon (daemon.py) está corriendo, esta CLI es solo un cliente: press/read/
type_into/under reusan el último árbol cacheado por esa app sin volver a caminarlo.
Si no está corriendo, cae a modo standalone (cada llamada camina el árbol de cero).
"""
import argparse
import json
import os
import socket
import sys
import time

from ax_core import *  # noqa: F401,F403 — reexporta Walker/find_app/etc para uso standalone y para chat_driver.py

SOCK_PATH = f"/tmp/axtree-{os.getuid()}.sock"


def daemon_request(payload, timeout=10):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCK_PATH)
            s.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            return json.loads(data.decode()) if data else None
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
        return None


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("app", nargs="?", help="nombre de la app (match parcial)")
    ap.add_argument("--menus", action="store_true", help="incluir barra de menú")
    ap.add_argument("--raw", action="store_true", help="sin flatten de grupos")
    ap.add_argument("--max-nodes", type=int, default=1500)
    ap.add_argument("--list", action="store_true", help="listar apps corriendo")
    ap.add_argument("--press", metavar="EID", help="ejecutar acción sobre el elemento (ej: e20)")
    ap.add_argument("--action", default="AXPress", help="acción AX a ejecutar (default AXPress)")
    ap.add_argument("--quiet", action="store_true", help="no imprimir el árbol (útil con --press)")
    ap.add_argument("--read", metavar="EID", help="imprimir el AXValue completo del elemento, sin truncar")
    ap.add_argument("--type", dest="type_text", metavar="TXT", help="tipear texto en la app (va al elemento con foco)")
    ap.add_argument("--type-into", metavar="EID", help="tipear --type TXT en ese elemento (AX directo, fallback CGEvent)")
    ap.add_argument("--key", choices=["return", "escape", "tab"], help="tecla especial post --type")
    ap.add_argument("--under", metavar="EID", help="dumpear solo el subárbol de ese elemento (renumera los eN)")
    ap.add_argument("--refresh", action="store_true", help="ignorar cache del daemon, re-caminar el árbol")
    ap.add_argument("--no-daemon", action="store_true", help="ignorar el daemon aunque esté corriendo")
    ap.add_argument("--daemon-stop", action="store_true", help="apagar el daemon corriendo (shutdown limpio, borra el socket)")
    ap.add_argument("--no-fallback", action="store_true", help="desactivar el fallback a screenshot cuando el árbol AX viene vacío")
    return ap


def run_standalone(args):
    if not AXIsProcessTrusted():
        sys.exit("Este proceso no tiene permiso de Accesibilidad (System Settings → Privacy → Accessibility).")

    app = find_app(args.app)
    if app is None:
        sys.exit(f"No encontré una app corriendo que matchee {args.app!r} (probá --list).")

    el = AXUIElementCreateApplication(app.processIdentifier())
    AXUIElementSetAttributeValue(el, "AXManualAccessibility", kCFBooleanTrue)

    w = Walker(flatten=not args.raw, max_nodes=args.max_nodes)
    windows = ax_attr(el, "AXWindows") or []
    print(f"# {app.localizedName()} (pid {app.processIdentifier()}) — {len(windows)} ventana(s)")
    for win in windows:
        w.walk(win, 0)

    if args.menus:
        mb = ax_attr(el, "AXMenuBar")
        if mb is not None:
            print("# menubar")
            w.walk(mb, 0)

    # BUG real encontrado en revisión: este chequeo tiene que ir DESPUÉS de --menus,
    # no antes. Apps como Spotify no exponen nada en AXWindows pero SÍ tienen un
    # AXMenuBar real (8 items, confirmado a mano) — si el fallback corta acá antes de
    # walkear los menús, `--menus` queda completamente ignorado y silencioso.
    if not args.no_fallback and is_tree_empty(w):
        path = screenshot_fallback(app, el)
        print(f"# árbol AX vacío para {app.localizedName()} — fallback a screenshot: {path}")
        print(f"# {w.count} nodos, ~0 tokens [standalone, sin daemon]", file=sys.stderr)
        return

    def parse_eid(s):
        eid = int(str(s).lstrip("e"))
        if not 0 <= eid < len(w.elements):
            sys.exit(f"e{eid} no existe en este dump ({len(w.elements)} elementos)")
        return eid

    if args.under:
        root = w.elements[parse_eid(args.under)]
        w = Walker(flatten=not args.raw, max_nodes=args.max_nodes)
        w.walk(root, 0)

    out = "\n".join(w.lines)
    if not args.quiet:
        print(out)
    note = " (TRUNCADO)" if w.truncated else ""
    print(f"# {w.count} nodos, ~{len(out) // 4} tokens{note} [standalone, sin daemon]", file=sys.stderr)

    pid = app.processIdentifier()

    if args.read:
        val = ax_attr(w.elements[parse_eid(args.read)], "AXValue")
        print("" if val is None else str(val))
        return

    if args.type_into:
        if not args.type_text:
            sys.exit("--type-into requiere --type TXT")
        eid = parse_eid(args.type_into)
        method = type_into(w.elements[eid], pid, args.type_text)
        if args.key:
            time.sleep(0.15)
            cg_key(pid, args.key)
        print(f"TYPE_INTO OK ({method}) → {w.lines[eid].strip()}"
              + (f" + {args.key}" if args.key else ""))
        return

    if args.type_text or args.key:
        if args.type_text:
            cg_type(pid, args.type_text)
        if args.key:
            time.sleep(0.15)
            cg_key(pid, args.key)
        print(f"TYPE OK → {len(args.type_text or '')} chars" + (f" + {args.key}" if args.key else ""))
        return

    if args.press:
        eid = parse_eid(args.press)
        target = w.elements[eid]
        err = AXUIElementPerformAction(target, args.action)
        line = w.lines[eid].strip()
        if err == 0:
            print(f"PRESS OK → {line}")
        else:
            sys.exit(f"PRESS FALLÓ (err {err}) → {line}")


def main():
    args = build_parser().parse_args()

    if args.daemon_stop:
        resp = daemon_request({"cmd": "shutdown"})
        if resp and resp.get("ok"):
            print("daemon detenido")
            return
        sys.exit("no se pudo detener el daemon (¿está corriendo?)")

    if args.list:
        if not args.no_daemon:
            resp = daemon_request({"cmd": "list"})
            if resp and resp.get("ok"):
                print(resp["text"])
                return
        for a in NSWorkspace.sharedWorkspace().runningApplications():
            if a.activationPolicy() == 0:
                print(a.localizedName())
        return

    if not args.app:
        sys.exit("falta el nombre de la app (o --list)")

    if not args.no_daemon:
        payload = {
            "app": args.app, "menus": args.menus, "raw": args.raw,
            "max_nodes": args.max_nodes, "under": args.under, "refresh": args.refresh,
            "quiet": args.quiet, "read": args.read, "type_into": args.type_into,
            "type_text": args.type_text, "key": args.key, "press": args.press,
            "action": args.action,
        }
        resp = daemon_request(payload)
        if resp is not None:
            if resp.get("text"):
                print(resp["text"])
            if resp.get("ok"):
                if resp.get("result"):
                    print(resp["result"])
                if resp.get("stats"):
                    print(resp["stats"], file=sys.stderr)
                return
            print(resp.get("stats", ""), file=sys.stderr)
            sys.exit(resp.get("error", "daemon error"))
        # resp is None: el daemon no está corriendo, seguimos en modo standalone

    run_standalone(args)


if __name__ == "__main__":
    main()
