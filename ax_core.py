"""ax_core — lógica AX compartida entre axtree.py (CLI) y daemon.py (server persistente)."""
import subprocess
import time

import objc
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyActionNames,
    AXUIElementSetAttributeValue,
    AXUIElementIsAttributeSettable,
    AXUIElementPerformAction,
    AXIsProcessTrusted,
    AXObserverCreate,
    AXObserverAddNotification,
    AXObserverRemoveNotification,
    AXObserverGetRunLoopSource,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from AppKit import NSWorkspace
from CoreFoundation import (
    kCFBooleanTrue,
    CFRunLoopRunInMode,
    kCFRunLoopDefaultMode,
    CFRunLoopAddSource,
    CFRunLoopRemoveSource,
    CFRunLoopGetCurrent,
)
import Quartz
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


def wait_for_notification(pid, element, notification, action=None, timeout=2.0):
    """Bloquea hasta que `element` emita `notification` (ej. kAXValueChangedNotification,
    kAXUIElementDestroyedNotification, kAXFocusedUIElementChangedNotification) o hasta
    `timeout` segundos. Devuelve True si la notificación llegó, False si venció el timeout
    (o si la app ni siquiera soporta esa notificación en ese elemento — ver nota abajo).

    `action`, si se pasa, es un callable sin argumentos que dispara la mutación (típicamente
    `lambda: AXUIElementPerformAction(el, "AXPress")`). Es OBLIGATORIO pasarlo así en vez de
    hacer `perform_action(); wait_for_notification(...)` en dos pasos: medido contra apps
    reales (TextEdit), la notificación se postea de forma SÍNCRONA dentro del round-trip IPC
    de AXUIElementPerformAction — si el observer se registra después de que la acción ya
    volvió, la notificación ya pasó y se pierde para siempre (el valor terminaba cambiando
    igual, pero `wait_for_notification` colgaba hasta el timeout completo por escuchar tarde).
    Por eso acá se registra el observer y se agrega el run loop source ANTES de ejecutar
    `action`, y recién ahí se empieza a bombear el run loop.

    Reemplaza el patrón `perform_action(); time.sleep(N); re-leer estado`: en vez de
    adivinar cuánto tarda la app en re-renderizar, escuchamos el AXObserver real de esa
    app y volvemos apenas la UI confirma el cambio (o al timeout como red de seguridad).

    Cuándo conviene: esperar una mutación de UI específica y puntual disparada por la
    propia acción (un value que cambia, un elemento que se destruye, el foco que se
    mueve) sobre un elemento que ya tenés resuelto de antemano.
    Cuándo NO hace falta: una acción que no cambia nada observable via AX (ej. abrir un
    link externo, un botón "Compartir" que dispara una hoja de sistema fuera del árbol
    de esa app) — ahí un timeout fijo chico sigue siendo lo más simple, o directamente
    no hace falta esperar nada.

    Nota: no todas las apps postean todas las notificaciones para todos los elementos
    (algunos AX servers devuelven kAXErrorNotificationUnsupported, ej. los segmentos de
    un radiogroup de Finder) — ahí no hay señal real que escuchar y esto devuelve False
    de inmediato en vez de quemar el timeout completo.
    """
    fired = {"v": False}

    @objc.callbackFor(AXObserverCreate)
    def _callback(observer, elem, notif, refcon):
        fired["v"] = True

    err, observer = AXObserverCreate(pid, _callback, None)
    if err != 0 or observer is None:
        return False

    err = AXObserverAddNotification(observer, element, notification, None)
    if err != 0:
        return False  # esta app/elemento no postea esta notificación: no hay nada que esperar

    source = AXObserverGetRunLoopSource(observer)
    run_loop = CFRunLoopGetCurrent()
    CFRunLoopAddSource(run_loop, source, kCFRunLoopDefaultMode)
    try:
        if action is not None:
            action()
        deadline = time.time() + timeout
        while not fired["v"]:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, min(remaining, 0.05), False)
    finally:
        CFRunLoopRemoveSource(run_loop, source, kCFRunLoopDefaultMode)
        AXObserverRemoveNotification(observer, element, notification)

    return fired["v"]


def scroll_at(x, y, clicks=10, direction="down"):
    """Scroll sintético (rueda del mouse) en (x, y). No hay acción AX universal
    para 'bajar la lista' — muchas apps ni siquiera exponen un AXScrollBar
    accionable — así que esto simula lo mismo que haría un humano con la rueda,
    igual que ya se usa CGEvent como fallback para tipear cuando AX no alcanza.

    BUG real encontrado en vivo: CGEventCreateScrollWheelEvent NO lleva
    coordenadas propias — el scroll se aplica donde esté el cursor REAL en ese
    momento (a diferencia de un click, que sí se postea a una posición
    explícita). Sin mover el mouse primero, terminaba scrolleando el panel
    equivocado (la sidebar en vez del contenido) porque el cursor había
    quedado ahí de una interacción anterior."""
    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.05)
    delta = -clicks if direction == "down" else clicks
    ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, delta)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


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


def _cgwindow_id(app_name):
    """windowNumber de la ventana on-screen de `app_name` de MAYOR ÁREA, vía
    CGWindowList — apps como Spotify no exponen NADA en AXWindows pero sí
    tienen una ventana real en pantalla que CGWindowList sí ve. Devuelve el
    kCGWindowNumber (no bounds: ver por qué en screenshot_fallback) o None.

    BUG real encontrado en revisión: la primera coincidencia layer=0 suele ser
    una franja angosta de 1470x33 (barra de estado/overlay), no la ventana de
    contenido — hay que elegir por tamaño, no por orden."""
    try:
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    except Exception:
        return None
    best = None
    for w in wins:
        if w.get("kCGWindowOwnerName") == app_name and w.get("kCGWindowLayer") == 0:
            b = w.get("kCGWindowBounds") or {}
            width, height = b.get("Width"), b.get("Height")
            if not width or not height:
                continue
            area = width * height
            if best is None or area > best[0]:
                best = (area, w.get("kCGWindowNumber"))
    return best[1] if best else None


def screenshot_fallback(app, el, path=None):
    """Fallback para cuando el árbol AX viene vacío: captura un screenshot de
    la ventana real de `app`. Preferencia de método:

    1. `screencapture -l <windowNumber>` contra el ID de ventana real de
       CGWindowList — BUG real encontrado en revisión: usar `-R x,y,w,h`
       (recorte por REGIÓN de pantalla) captura lo que sea que esté
       visualmente encima en esas coordenadas en ese instante, no el
       contenido de la ventana en sí — si la ventana de la app está tapada
       por otra (típico: la terminal desde la que se corre este comando
       encima), el screenshot termina siendo de la ventana que tapa, no de
       la app pedida. Capturar por windowNumber trae el contenido real de
       esa ventana sin importar qué haya encima.
    2. AXPosition/AXSize de AXWindows (desempaquetado vía AXValueGetValue)
       recortado por región, si CGWindowList no encontró nada pero AX sí
       reporta alguna ventana (mismo riesgo de oclusión que el punto 1, pero
       es mejor que nada).
    3. Pantalla completa como último recurso, si ninguna de las dos fuentes
       tiene una ventana real (la app genuinamente no tiene nada visible) —
       ojo que esto puede capturar contenido ajeno a la app pedida.

    Devuelve la ruta del PNG guardado."""
    if path is None:
        safe_name = "".join(c if c.isalnum() else "_" for c in (app.localizedName() or "app"))
        path = f"/tmp/axtree_fallback_{safe_name}_{int(time.time())}.png"

    win_id = _cgwindow_id(app.localizedName())
    cmd = ["screencapture", "-x"]
    if win_id is not None:
        cmd += ["-l", str(win_id)]
    else:
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
        if bounds:
            x, y, w, h = bounds
            cmd += ["-R", f"{int(x)},{int(y)},{int(w)},{int(h)}"]
    cmd.append(path)
    subprocess.run(cmd, check=True)
    return path
