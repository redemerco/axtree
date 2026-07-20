#!/usr/bin/env python3
"""Servidor MCP de axtree — expone get_tree/press/type_into/read como tools nativas.

El propio proceso del servidor es persistente por sesión (así funciona MCP sobre
stdio), así que la cache de árboles vivos vive directamente acá: no hace falta el
daemon.py separado cuando se usa vía MCP.
"""
import time

import ax_core as ax
import resolve as ax_resolve
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("axtree")

CACHE = {}  # app_name.lower() -> {"walker": Walker, "pid": int, "header": str}


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


def get_cached(app_name, menus=False, raw=False, max_nodes=1500, refresh=False):
    """Devuelve (walker, header, from_cache) para app_name, caminando el árbol si hace falta."""
    app = ax.find_app(app_name)
    if app is None:
        raise ValueError(f"No encontré una app corriendo que matchee {app_name!r}")
    key = app_name.lower()
    entry = CACHE.get(key)
    if entry is not None and not refresh:
        return entry["walker"], entry["header"], True, app
    el = ax.AXUIElementCreateApplication(app.processIdentifier())
    w, header = full_walk(app, el, menus, raw, max_nodes)
    CACHE[key] = {"walker": w, "pid": app.processIdentifier(), "header": header}
    return w, header, False, app


def parse_eid(w, eid):
    i = int(str(eid).lstrip("e"))
    if not 0 <= i < len(w.elements):
        raise ValueError(f"e{i} no existe en este dump ({len(w.elements)} elementos)")
    return i


def stats_line(w, cached):
    text = "\n".join(w.lines)
    note = " (TRUNCADO)" if w.truncated else ""
    return f"# {w.count} nodos, ~{len(text) // 4} tokens{note} [{'cache' if cached else 'walk'}]"


@mcp.tool()
def list_apps() -> str:
    """Lista las apps con interfaz gráfica corriendo ahora en la Mac."""
    ax.pump_runloop()
    names = [a.localizedName() for a in ax.NSWorkspace.sharedWorkspace().runningApplications()
             if a.activationPolicy() == 0]
    return "\n".join(names)


@mcp.tool()
def get_tree(app: str, under: str = "", menus: bool = False, raw: bool = False,
             max_nodes: int = 1500, refresh: bool = False, fallback: bool = True) -> str:
    """Vuelca el árbol de accesibilidad de una app como texto: una línea por
    elemento con rol, label, value, acciones disponibles y su referencia [eN].
    Usá `under` con un eN de un dump previo para ver solo ese subárbol (más barato
    en tokens); ojo que esto reemplaza la vista cacheada de esa app hasta el
    próximo `refresh=True`. Si el árbol viene vacío o casi vacío (apps cuyo motor
    de renderizado propio no expone accesibilidad, ej. Spotify), cae automáticamente
    a un screenshot de la ventana — pasá `fallback=False` para desactivarlo."""
    w, header, cached, app_obj = get_cached(app, menus, raw, max_nodes, refresh)
    if under:
        root = w.elements[parse_eid(w, under)]
        w2 = ax.Walker(flatten=not raw, max_nodes=max_nodes)
        w2.walk(root, 0)
        CACHE[app.lower()]["walker"] = w2
        w = w2
    if fallback and ax.is_tree_empty(w):
        el = ax.AXUIElementCreateApplication(app_obj.processIdentifier())
        path = ax.screenshot_fallback(app_obj, el)
        return f"{header}\n# árbol AX vacío para {app_obj.localizedName()} — fallback a screenshot: {path}"
    return f"{header}\n{chr(10).join(w.lines)}\n{stats_line(w, cached)}"


@mcp.tool()
def press(app: str, eid: str, action: str = "AXPress") -> str:
    """Ejecuta una acción (default AXPress = click) sobre el elemento eN del
    último dump de esa app. No re-camina el árbol: usa la referencia cacheada."""
    w, _, cached, _ = get_cached(app)
    i = parse_eid(w, eid)
    target = w.elements[i]
    err = ax.AXUIElementPerformAction(target, action)
    line = w.lines[i].strip()
    if err != 0:
        raise ValueError(f"PRESS falló (err {err}) → {line}")
    return f"PRESS OK → {line}"


@mcp.tool()
def type_into(app: str, eid: str, text: str, key: str = "") -> str:
    """Escribe `text` en el elemento eN. Prueba AXSelectedText → AXValue → CGEvent
    (fallback, para editores tipo webview que no aceptan escritura directa por AX),
    verificando en cada paso que el texto realmente quedó escrito. `key` opcional:
    return/escape/tab, para confirmar después de escribir."""
    w, _, cached, app_obj = get_cached(app)
    i = parse_eid(w, eid)
    method = ax.type_into(w.elements[i], app_obj.processIdentifier(), text)
    if key:
        time.sleep(0.15)
        ax.cg_key(app_obj.processIdentifier(), key)
    return f"TYPE_INTO OK ({method}) → {w.lines[i].strip()}" + (f" + {key}" if key else "")


@mcp.tool()
def read_value(app: str, eid: str) -> str:
    """Lee el AXValue completo (sin truncar) del elemento eN — para leer texto
    largo o confirmar el estado exacto de un campo/toggle tras una acción."""
    w, _, _, _ = get_cached(app)
    i = parse_eid(w, eid)
    val = ax.ax_attr(w.elements[i], "AXValue")
    return "" if val is None else str(val)


@mcp.tool()
def find(app: str, query: str, top_k: int = 3, menus: bool = False) -> str:
    """Busca un elemento por descripción en lenguaje natural (ej. "el botón de
    guardar", "campo de búsqueda") en vez de por eid. Camina el árbol fresco
    (no usa el cache de get_tree/press) y devuelve los top_k candidatos con su
    score — útil para explorar antes de decidir qué acción tomar con `act`."""
    app_obj, top = ax_resolve.resolve(app, query, top_k=top_k, menus=menus)
    return "\n".join(f"{s:.2f}  {line}" for s, _, line, _ in top)


@mcp.tool()
def act(app: str, query: str, action: str = "AXPress", text: str = "", key: str = "",
        min_score: float = 0.35, menus: bool = False) -> str:
    """Resuelve `query` (descripción en lenguaje natural) y ejecuta la acción en
    la MISMA llamada — walk fresco, sin depender de ningún eid cacheado de un
    turno anterior. Preferible a press/type_into cuando la UI pudo haber
    mutado desde el último get_tree, ya que evita el problema de que un eid
    quede apuntando a otra cosa. `text` (opcional) tipea en el elemento
    encontrado; `key` (opcional) manda una tecla después."""
    try:
        r = ax_resolve.act(app, query, action=action or None, text=text or None,
                            key=key or None, min_score=min_score, menus=menus)
    except ValueError as e:
        raise ValueError(str(e))
    return f"OK (score={r['score']:.2f}) → {r['line']}"


if __name__ == "__main__":
    mcp.run()
