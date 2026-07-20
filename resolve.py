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
from AppKit import NSApplicationActivateIgnoringOtherApps

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
