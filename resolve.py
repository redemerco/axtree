"""resolve — direccionamiento de elementos AX por descripción en lenguaje natural,
en vez de por `eid` cacheado de un dump anterior.

Problema que ataca: los `eid` de axtree ([e42]) son índices dentro de UN walk
puntual. Son válidos SOLO contra ese dump — si la UI muta entre el dump y la
acción (aparece/desaparece un elemento, se abre un panel), el índice puede
terminar apuntando a otra cosa sin que haya ningún error.

`act()` resuelve y ejecuta la acción en la MISMA llamada (walk fresco → score →
press), así ningún índice se guarda entre turnos: no hay ventana para que la UI
mute entre la resolución y la acción.
"""
import difflib
import re
import time

import ax_core as ax
import atlas
from AppKit import NSApplicationActivateIgnoringOtherApps
from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType

ROLE_SYNONYMS = {
    "button": "button", "botón": "button", "boton": "button",
    "checkbox": "checkbox", "casilla": "checkbox",
    "textfield": "textfield", "campo": "textfield", "field": "textfield",
    "textarea": "textarea", "texto": "textarea",
    "combobox": "combobox",
    "popupbutton": "popupbutton", "menu": "popupbutton", "menú": "popupbutton",
    "colorwell": "colorwell", "color": "colorwell",
    "radiobutton": "radiobutton", "radio": "radiobutton",
    "tab": "tabbutton", "pestaña": "tabbutton", "pestana": "tabbutton",
    "link": "link", "enlace": "link",
    "cerrar": "closebutton", "close": "closebutton",
}

# Números escritos con letras -> el glifo del botón. Sin esto, "botón cinco"
# nunca conecta con un label "5": no hay similitud de caracteres entre esas dos
# strings, y comparación letra a letra puede incluso dar falsos positivos con
# otro label que por casualidad comparta letras (confirmado en vivo: "cinco"
# empataba con "Cambiar signo" antes de este fix, por las letras compartidas
# c-i-n-o). Esto NO es fuzzy — es un match exacto de token, tratado igual que
# ROLE_SYNONYMS: prioridad categórica sobre la similitud de caracteres.
NUMBER_WORDS = {
    "cero": "0", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4",
    "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

_WORD_RE = re.compile(r"[a-záéíóúñü0-9]+", re.IGNORECASE)

# Palabras función: no aportan señal y diluyen el score si se comparan letra a
# letra contra un label corto (una query de 20 caracteres vs un label de 7 se
# castiga por diferencia de longitud aunque el match semántico sea perfecto).
STOPWORDS = {"el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
             "al", "en", "por", "para", "con", "que", "the", "a", "of", "to",
             "on", "in", "poner", "pone", "hacer"}


def _norm(s):
    return " ".join(_WORD_RE.findall(str(s).lower()))


def _candidate_texts(role, label, value, extras):
    role_short = role[2:].lower() if role.startswith("AX") else role.lower()
    texts = [t for t in (label, str(value) if value not in (None, "") else "") if t]
    texts.append(role_short)
    texts.extend(extras)
    return " ".join(texts), role_short


def score_element(query, role, label, value, extras):
    """Fuzzy word-level: compara cada palabra relevante de la query contra las
    palabras del candidato y promedia, en vez de comparar los strings enteros
    (que castiga queries largas en lenguaje natural contra labels cortos). Un
    sinónimo de rol conocido ("cerrar" -> closebutton) cuenta como señal
    categórica con piso propio, no se promedia con el resto (si no, se pierde
    contra labels parecidos letra a letra, ej. "cerrar" vs "centrar" ~0.77)."""
    combined, role_short = _candidate_texts(role, label, value, extras)
    ctoks = set(_norm(combined).split())

    qtoks_all = _norm(query).split()
    qtoks = [t for t in qtoks_all if t not in STOPWORDS] or qtoks_all

    role_hit = False
    number_hit = False
    remaining = []
    for qt in qtoks:
        target = ROLE_SYNONYMS.get(qt)
        digit = NUMBER_WORDS.get(qt)
        if digit and digit in ctoks:
            number_hit = True
        elif target and (target == role_short or target in extras):
            role_hit = True
        else:
            remaining.append(qt)

    def best_match(qt):
        return max((difflib.SequenceMatcher(None, qt, ct).ratio() for ct in ctoks), default=0.0)

    token_score = (sum(best_match(qt) for qt in remaining) / len(remaining)) if remaining else 1.0

    label_sim = difflib.SequenceMatcher(None, _norm(query), _norm(label)).ratio() if label else 0.0

    base = 0.75 * token_score + 0.25 * label_sim
    if role_hit:
        base = max(base, 0.65 + 0.25 * token_score)
    if number_hit:
        # match categórico exacto (dígito escrito == label del botón): piso más
        # alto que role_hit, porque acá no hay ambigüedad posible como con los
        # sinónimos de rol (un dígito solo puede ser ESE botón)
        base = max(base, 0.9 + 0.1 * token_score if remaining else 0.95)
    return min(1.0, base)


def walk_app(app_name, menus=False, activate=True, max_nodes=1500):
    app = ax.find_app(app_name)
    if app is None:
        raise ValueError(f"No encontré una app corriendo que matchee {app_name!r}")
    el = ax.AXUIElementCreateApplication(app.processIdentifier())
    ax.AXUIElementSetAttributeValue(el, "AXManualAccessibility", ax.kCFBooleanTrue)
    windows = ax.ax_attr(el, "AXWindows") or []
    # AXWindows puede venir vacío para apps que no están frontmost. Reactivar +
    # reintentar unas pocas veces evita falsos "0 ventanas".
    if not windows and activate:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        for _ in range(10):
            ax.pump_runloop(0.1)
            time.sleep(0.05)
            windows = ax.ax_attr(el, "AXWindows") or []
            if windows:
                break
    w = ax.Walker(flatten=True, max_nodes=max_nodes)
    for win in windows:
        w.walk(win, 0)
    if menus:
        mb = ax.ax_attr(el, "AXMenuBar")
        if mb is not None:
            w.walk(mb, 0)
    return app, w


def resolve(app_name, query, top_k=3, menus=False):
    """Devuelve (app, [(score, index, line, element), ...]) ordenado, walk fresco cada vez."""
    app, w = walk_app(app_name, menus=menus)
    scored = []
    for i, el in enumerate(w.elements):
        role, label, value, extras = ax.describe(el)
        s = score_element(query, role, label, value, extras)
        scored.append((s, i, w.lines[i].strip(), el))
    scored.sort(key=lambda t: -t[0])
    return app, scored[:top_k]


def act(app_name, query, action=None, text=None, key=None, min_score=0.35, menus=False):
    """Resuelve `query` y ejecuta en la MISMA llamada (walk fresco -> score -> act).
    No expone ni depende de ningún eid guardado de una llamada anterior."""
    app, top = resolve(app_name, query, top_k=3, menus=menus)
    if not top or top[0][0] < min_score:
        alts = "\n".join(f"  {s:.2f}  {line}" for s, _, line, _ in top)
        raise ValueError(f"Sin match confiable para {query!r} (mejor score "
                          f"{top[0][0]:.2f} < {min_score}).\nCandidatos:\n{alts}")
    score, idx, line, el = top[0]
    result = {"score": score, "line": line, "runner_up": top[1] if len(top) > 1 else None}
    if action:
        err = ax.AXUIElementPerformAction(el, action)
        result["action_err"] = err
    if text is not None:
        result["type_method"] = ax.type_into(el, app.processIdentifier(), text)
    if key:
        ax.cg_key(app.processIdentifier(), key)
    return result


def nav_step(app_name, query, min_score=0.5, menus=False):
    """Un paso de navegación intermedio (no el objetivo final): resuelve `query`
    y prueba AXPress; si el elemento no tiene esa acción (filas de sidebar/outline
    SIN Press, ej. la sidebar de Ajustes del Sistema), selecciona la fila de la
    forma correcta.

    BUG real que costó encontrar: la fila (AXRow) tiene un atributo `AXSelected`
    que parece el lugar obvio para escribir, pero setearlo ahí NO dispara la
    navegación (es de solo-reflejo en muchos NSOutlineView). La selección real
    vive en el CONTENEDOR (el AXOutline padre), vía su atributo `AXSelectedRows`
    — hay que pasarle una lista con la fila. Confirmado con Accessibility
    Inspector: la API sí lo expone bien, el bug estaba en escribir en el
    elemento equivocado, no en la API."""
    app, top = resolve(app_name, query, top_k=3, menus=menus)
    if not top or top[0][0] < min_score:
        alts = "\n".join(f"  {s:.2f}  {line}" for s, _, line, _ in top)
        raise ValueError(f"Sin match confiable para {query!r} (mejor score "
                          f"{top[0][0]:.2f} < {min_score}).\nCandidatos:\n{alts}")
    score, idx, line, el = top[0]
    if "AXPress" in ax.ax_actions(el):
        err = ax.AXUIElementPerformAction(el, "AXPress")
        return {"score": score, "line": line, "action_err": err}

    # buscar la fila (AXRow) ancestro más cercana y su AXOutline contenedor
    row = el
    while row is not None and ax.ax_attr(row, "AXRole") != "AXRow":
        row = ax.ax_attr(row, "AXParent")
    if row is None:
        err = ax.AXUIElementSetAttributeValue(el, "AXSelected", True)
        return {"score": score, "line": line, "action_err": err}

    outline = ax.ax_attr(row, "AXParent")
    while outline is not None and ax.ax_attr(outline, "AXRole") not in ("AXOutline", "AXTable"):
        outline = ax.ax_attr(outline, "AXParent")
    if outline is None:
        err = ax.AXUIElementSetAttributeValue(row, "AXSelected", True)
    else:
        err = ax.AXUIElementSetAttributeValue(outline, "AXSelectedRows", [row])
    return {"score": score, "line": line, "action_err": err}


def _scroll_point(app_name):
    """Punto (x, y) para scrollear el contenido de la ventana principal de la
    app — un poco más abajo del centro, para no caer sobre un header fijo."""
    app = ax.find_app(app_name)
    if app is None:
        return None
    el = ax.AXUIElementCreateApplication(app.processIdentifier())
    windows = ax.ax_attr(el, "AXWindows") or []
    if not windows:
        return None
    pos = ax.ax_attr(windows[0], "AXPosition")
    size = ax.ax_attr(windows[0], "AXSize")
    if pos is None or size is None:
        return None
    ok1, pt = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok2, sz = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (ok1 and ok2):
        return None
    return (pt.x + sz.width / 2, pt.y + sz.height * 0.6)


def discover_and_act(app_name, task, breadcrumbs, query, action="AXPress", text=None, key=None,
                      max_scrolls=8, min_score=0.5, remember_route=True):
    """Ejecuta `breadcrumbs` (pasos previos de navegación, en orden), busca
    `query` reintentando con scroll si hace falta (contenido perezoso: el
    control puede no existir todavía en el árbol hasta que se scrollea hasta
    ahí), actúa, y GUARDA la ruta completa en el atlas bajo `task` — la
    próxima vez que se pida ese `task` en esta app, `smart_act` la reproduce
    sin necesitar breadcrumbs/query de nuevo ni redescubrir el scroll."""
    for step in breadcrumbs:
        nav_step(app_name, step)
        time.sleep(0.6)

    scrolls_done = 0
    app, top = resolve(app_name, query, top_k=3)
    while (not top or top[0][0] < min_score) and scrolls_done < max_scrolls:
        pt = _scroll_point(app_name)
        if pt is None:
            break
        ax.scroll_at(*pt, clicks=10)
        time.sleep(0.4)
        scrolls_done += 1
        app, top = resolve(app_name, query, top_k=3)

    if not top or top[0][0] < min_score:
        alts = "\n".join(f"  {s:.2f}  {line}" for s, _, line, _ in top) if top else "(nada)"
        raise ValueError(f"Sin match confiable para {query!r} tras {scrolls_done} scrolls "
                          f"(mejor score {(top[0][0] if top else 0):.2f} < {min_score}).\n{alts}")

    score, idx, line, el = top[0]
    result = {"score": score, "line": line, "scrolls": scrolls_done}
    if action:
        result["action_err"] = ax.AXUIElementPerformAction(el, action)
    if text is not None:
        result["type_method"] = ax.type_into(el, app.processIdentifier(), text)
    if key:
        ax.cg_key(app.processIdentifier(), key)

    if remember_route:
        atlas.remember(app_name, task, {
            "breadcrumbs": breadcrumbs, "scrolls": scrolls_done, "query": query,
            "action": action, "key": key,
        })
    return result


def smart_act(app_name, task, breadcrumbs=None, query=None, action="AXPress", text=None, key=None,
              max_scrolls=8, min_score=0.5):
    """Punto de entrada recomendado para tareas recurrentes. Si `task` ya se
    recorrió antes en `app_name` (está en atlas.json), reproduce esa ruta
    directamente: no hace falta pasar breadcrumbs/query de nuevo. La primera
    vez que se pide un `task` nuevo, hace falta pasar `breadcrumbs` (la lista
    de pasos previos, ej. ["Accesibilidad", "Pantalla"]) y `query` (la
    descripción del control final) para descubrirla — a partir de ahí queda
    guardada para siempre (hasta que la app cambie de versión y deje de
    andar, en cuyo caso se re-descubre sola con lo que se haya pasado)."""
    route = atlas.recall(app_name, task)
    if route:
        try:
            return discover_and_act(
                app_name, task, route["breadcrumbs"], route["query"],
                action=route.get("action", action), text=text, key=route.get("key", key),
                max_scrolls=route.get("scrolls", 0) + 2, min_score=min_score, remember_route=True)
        except ValueError:
            pass  # la ruta guardada dejó de andar (la app cambió) -> redescubrir abajo
    if breadcrumbs is None or query is None:
        raise ValueError(f"No hay ruta guardada para {task!r} en {app_name!r} — "
                          f"pasá breadcrumbs y query la primera vez para descubrirla.")
    return discover_and_act(app_name, task, breadcrumbs, query, action=action, text=text,
                             key=key, max_scrolls=max_scrolls, min_score=min_score)
