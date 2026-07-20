"""ax_core — lógica AX compartida entre axtree.py (CLI) y daemon.py (server persistente)."""
import subprocess
import time

from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyActionNames,
    AXUIElementSetAttributeValue,
    AXUIElementIsAttributeSettable,
    AXUIElementPerformAction,
    AXIsProcessTrusted,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from AppKit import NSWorkspace
from CoreFoundation import kCFBooleanTrue, CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPostToPid,
)

MAX_DEPTH = 30
MAX_TEXT = 80

# Roles contenedores sin semántica propia: se aplanan si no aportan label
FLATTEN_ROLES = {"AXGroup", "AXGenericElement", "AXUnknown", "AXSplitGroup",
                 "AXScrollArea", "AXLayoutArea", "AXLayoutItem"}
# Roles que nunca aportan al agente
SKIP_ROLES = {"AXSplitter", "AXGrowArea"}
# Acciones que no vale la pena listar (ruido)
BORING_ACTIONS = {"AXShowMenu", "AXScrollToVisible", "AXRaise", "AXShowAlternateUI",
                  "AXShowDefaultUI", "AXZoomWindow"}


def ax_attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def ax_actions(el):
    err, names = AXUIElementCopyActionNames(el, None)
    return list(names) if err == 0 and names else []


INVISIBLES = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩"))


def clean(s):
    s = str(s).translate(INVISIBLES).replace(" ", " ").replace("\n", "\\n").strip()
    return s[:MAX_TEXT] + "…" if len(s) > MAX_TEXT else s


def clean_action(a):
    # Catalyst devuelve custom actions como "me:Leído\nTarget:0x0\nSelector:(null)"
    a = str(a).split("\n")[0].removeprefix("me:").removeprefix("Name:").strip()
    return a.translate(INVISIBLES)


def describe(el):
    """(role, label, value, extras) de un elemento, ya limpio."""
    role = ax_attr(el, "AXRole") or "?"
    label = ax_attr(el, "AXTitle") or ax_attr(el, "AXDescription") \
        or ax_attr(el, "AXPlaceholderValue") or ""
    value = ax_attr(el, "AXValue")
    extras = []
    if ax_attr(el, "AXEnabled") is False:
        extras.append("disabled")
    if ax_attr(el, "AXFocused"):
        extras.append("focused")
    sub = ax_attr(el, "AXSubrole")
    if sub:
        extras.append(str(sub)[2:].lower())
    return str(role), clean(label), value, extras


def node_line(role, label, value, extras, actions, eid):
    parts = [f"- {role[2:].lower() if role.startswith('AX') else role}"]
    if label:
        parts.append(f'"{label}"')
    if value is not None and str(value) != "" and str(value) != label:
        parts.append(f"value={clean(value)!r}")
    if extras:
        parts.append(f"<{','.join(extras)}>")
    acts = []
    for a in actions:
        if a in BORING_ACTIONS or str(a).startswith("AXScroll"):
            continue
        acts.append(a[2:] if str(a).startswith("AX") else clean_action(a))
    if acts:
        parts.append(f"({','.join(acts)})")
    parts.append(f"[e{eid}]")
    return " ".join(parts)


class Walker:
    def __init__(self, flatten=True, max_nodes=1500):
        self.lines = []
        self.elements = []  # eid -> AXUIElement, alineado con las líneas
        self.count = 0
        self.max_nodes = max_nodes
        self.flatten = flatten
        self.truncated = False

    def walk(self, el, depth=0):
        if self.count >= self.max_nodes:
            self.truncated = True
            return
        if depth > MAX_DEPTH:
            return
        role, label, value, extras = describe(el)
        if role in SKIP_ROLES:
            return
        children = ax_attr(el, "AXChildren") or []

        # flatten: contenedor anónimo → pasar derecho a los hijos
        # (Electron devuelve AXValue='' en vez de None en grupos vacíos: "not value" cubre ambos)
        if self.flatten and role in FLATTEN_ROLES and not label and not value:
            for c in children:
                self.walk(c, depth)
            return

        actions = ax_actions(el)
        eid = self.count
        self.count += 1
        self.elements.append(el)
        self.lines.append("  " * depth + node_line(role, label, value, extras, actions, eid))
        for c in children:
            self.walk(c, depth + 1)


def ax_settable(el, attr):
    err, ok = AXUIElementIsAttributeSettable(el, attr, None)
    return err == 0 and ok


KEYCODES = {"return": 36, "escape": 53, "tab": 48}


def cg_type(pid, text):
    for i in range(0, len(text), 16):  # chunks: SetUnicodeString es frágil con strings largos
        chunk = text[i:i + 16]
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, 0, down)
            CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
            CGEventPostToPid(pid, ev)
        time.sleep(0.03)


def cg_key(pid, name):
    for down in (True, False):
        CGEventPostToPid(pid, CGEventCreateKeyboardEvent(None, KEYCODES[name], down))


def typed_ok(el, text):
    val = ax_attr(el, "AXValue")
    return isinstance(val, str) and text in val


def type_into(el, pid, text):
    """Tipea `text` en el elemento. Devuelve el método que funcionó.

    Orden: AXSelectedText (inserta en el caret) → AXValue (REEMPLAZA el contenido)
    → CGEvent al pid (necesita que el foco real esté en el elemento).
    Cada camino AX se verifica leyendo AXValue: hay apps (Catalyst/Electron)
    que devuelven err 0 sin escribir nada.
    """
    if ax_settable(el, "AXFocused"):
        AXUIElementSetAttributeValue(el, "AXFocused", kCFBooleanTrue)
        time.sleep(0.05)
    if ax_settable(el, "AXSelectedText"):
        if AXUIElementSetAttributeValue(el, "AXSelectedText", text) == 0 and typed_ok(el, text):
            return "AXSelectedText"
    if ax_settable(el, "AXValue"):
        if AXUIElementSetAttributeValue(el, "AXValue", text) == 0 and typed_ok(el, text):
            return "AXValue"
    cg_type(pid, text)
    return "CGEvent-fallback" + ("" if typed_ok(el, text) else " (sin verificar)")


def pump_runloop(seconds=0.05):
    """NSWorkspace.runningApplications() se alimenta de notificaciones distribuidas;
    un proceso de larga vida sin run loop propio (como el daemon) nunca las procesa
    y queda con la lista de apps congelada en el momento en que arrancó. Bombear el
    run loop brevemente le da tiempo a esas notificaciones de llegar."""
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, seconds, False)


def find_app(name):
    pump_runloop()
    apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications()
            if a.localizedName()]
    exact = [a for a in apps if a.localizedName().lower() == name.lower()]
    partial = [a for a in apps if name.lower() in a.localizedName().lower()]
    return (exact or partial or [None])[0]


# Subroles de chrome de ventana: no cuentan como "contenido" al evaluar si un
# árbol vino vacío (algunas apps sin soporte AX real igual exponen estos tres).
CHROME_SUBROLES = {"AXCloseButton", "AXFullScreenButton", "AXMinimizeButton", "AXZoomButton"}


def content_node_count(w, limit=3):
    """Cuenta nodos de contenido real en el walk `w` (excluye el propio AXWindow
    y los botones de chrome). Corta apenas llega a `limit`: solo nos importa si
    el árbol está vacío, no el conteo exacto."""
    n = 0
    for el in w.elements:
        if ax_attr(el, "AXRole") == "AXWindow":
            continue
        if ax_attr(el, "AXSubrole") in CHROME_SUBROLES:
            continue
        n += 1
        if n >= limit:
            break
    return n


def is_tree_empty(w, min_content=3):
    """True si el walk `w` tiene menos de `min_content` nodos de contenido real.
    Pensado para apps cuyo motor de renderizado propio nunca implementó soporte
    de accesibilidad (ej. Spotify): AXWindows viene vacío o solo trae los
    botones estándar de la ventana, sin nada útil para un agente."""
    return content_node_count(w, min_content) < min_content


def screenshot_fallback(app, el, path=None):
    """Fallback para cuando el árbol AX viene vacío: captura un screenshot
    recortado a los bounds de la primera ventana de `app` (AXPosition/AXSize,
    desempaquetados de su AXValue opaco vía AXValueGetValue). Si la app no
    tiene ninguna ventana en AXWindows, cae a un screenshot de pantalla
    completa. Devuelve la ruta del PNG guardado."""
    if path is None:
        safe_name = "".join(c if c.isalnum() else "_" for c in (app.localizedName() or "app"))
        path = f"/tmp/axtree_fallback_{safe_name}_{int(time.time())}.png"

    bounds = None
    windows = ax_attr(el, "AXWindows") or []
    if windows:
        pos = ax_attr(windows[0], "AXPosition")
        size = ax_attr(windows[0], "AXSize")
        if pos is not None and size is not None:
            ok1, pt = AXValueGetValue(pos, kAXValueCGPointType, None)
            ok2, sz = AXValueGetValue(size, kAXValueCGSizeType, None)
            if ok1 and ok2:
                bounds = (pt.x, pt.y, sz.width, sz.height)

    cmd = ["screencapture", "-x"]
    if bounds:
        x, y, w, h = bounds
        cmd += ["-R", f"{int(x)},{int(y)},{int(w)},{int(h)}"]
    cmd.append(path)
    subprocess.run(cmd, check=True)
    return path
