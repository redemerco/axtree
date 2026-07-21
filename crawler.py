"""crawler — descubrimiento HEURÍSTICO de rutas de navegación (sin LLM en el loop).

A diferencia de resolve.smart_act (donde YO decido qué apretar en cada paso,
mirando y razonando), esto es código puro: recorre los elementos de navegación
de una app, aprieta cada uno, compara el árbol antes/después para detectar si
realmente cambió de pantalla, y arma el atlas solo — sin que un modelo esté
mirando cada paso. Mucho más barato y rápido para mapear una app entera.

Diseño de seguridad (no negociable, ver TRAMPA de axtree del 2026-07-20 —
un click de baja confianza casi cambia un ajuste real del sistema):
- NUNCA explora checkboxes/switches/radiobuttons — esos cambian estado
  (justo lo que causó el casi-incidente), no navegan. Se excluyen por completo.
- Filtra por lista negra de palabras peligrosas en el label (eliminar, borrar,
  enviar, cerrar sesión, restablecer, etc.) antes de apretar CUALQUIER cosa.
- Solo sigue elementos con acción Press dentro de contenedores de navegación
  reconocibles (sidebar/outline, tabgroup de navegación principal) — no
  explora libremente cualquier botón del contenido.
- Detecta "¿esto realmente navegó?" comparando fingerprint de pantalla
  (título de ventana + headings) antes/después. Si no cambió, no lo cuenta
  como ruta y sigue de largo.
- Límites duros de profundidad y de pantallas totales exploradas, para que
  nunca corra sin fin.
- NUNCA corre sobre apps de mensajería/redes sociales (ver EXCLUDED_APPS):
  ahí un click heurístico sin supervisión podría enviar un mensaje, iniciar
  una llamada o publicar algo visible para otra persona real — un riesgo
  categóricamente distinto a tocar un ajuste propio. Esto es un bloqueo
  permanente en código, no un acuerdo de una sola vez.
"""
import re
import time

import ax_core as ax
import atlas
import resolve

DANGER_WORDS = [
    "eliminar", "borrar", "delete", "remove", "quitar",
    "enviar", "send", "publicar", "post", "share", "compartir",
    "cerrar sesión", "cerrar sesion", "log out", "logout", "sign out", "salir de",
    "restablecer", "reset", "restaurar", "reformatear", "format",
    "desinstalar", "uninstall", "vaciar", "empty", "papelera",
    "apagar", "shut down", "reiniciar", "restart", "reboot",
    "cancelar suscripción", "cancelar suscripcion", "unsubscribe",
    "cancelar cuenta", "eliminar cuenta", "delete account",
    "pagar", "pay", "comprar", "purchase", "buy", "checkout",
    "confirmar", "confirm", "aceptar", "llamar", "call", "marcar",
    "bloquear", "block", "denunciar", "report",
    # BUG real: el patrón #3 (elementos tempranos en el árbol) agarró los
    # controles del mini-reproductor de Podcasts — "Reproducir" arrancaría
    # audio de forma audible sin que nadie lo haya pedido. No es destructivo,
    # pero es una sorpresa perceptible que el crawler no debería causar solo.
    "reproducir", "play", "reproducción", "grabar", "record",
]
DANGER_RE = re.compile("|".join(re.escape(w) for w in DANGER_WORDS), re.IGNORECASE)

# Apps de mensajería/redes sociales: el crawler se niega a correr acá, sin
# excepción. Un click heurístico equivocado en una de estas apps puede tener
# consecuencias reales sobre OTRA persona (mandar algo, llamar, publicar) —
# no es lo mismo que tocar un ajuste propio por error.
EXCLUDED_APPS = [
    "whatsapp", "slack", "mensajes", "messages", "telegram", "signal",
    "discord", "teams", "instagram", "facebook", "messenger", "twitter",
    "x", "wechat", "line", "imessage", "skype", "zoom", "facetime",
]


def _is_excluded_app(app_name):
    # match EXACTO, no substring: "x" o "line" como substring bloquearían
    # de rebote apps sin relación (Xcode, cualquier "...line").
    return app_name.strip().lower() in EXCLUDED_APPS

# Roles que cambian estado (NO son navegación) — nunca se exploran.
STATE_ROLES = {"AXCheckBox", "AXRadioButton", "AXSlider", "AXStepper"}
# Subroles que también indican un control de estado, no de navegación.
STATE_SUBROLES = {"AXSwitch", "AXToggle"}


def is_dangerous(label):
    return bool(label) and DANGER_RE.search(label)


def is_nav_candidate(role, subrole, extras, actions, label):
    if role in STATE_ROLES:
        return False
    if subrole in STATE_SUBROLES or any(s in STATE_SUBROLES for s in extras):
        return False
    if "AXPress" not in actions:
        return False
    if not label:
        return False  # sin label no hay forma de describir la ruta después
    if is_dangerous(label):
        return False
    return True


def screen_fingerprint(w):
    """Huella de la pantalla actual: título de ventana + headings visibles.
    Se usa para detectar si un click realmente cambió de pantalla, y para no
    re-explorar una pantalla ya vista (evita loops)."""
    title = w.lines[0].strip() if w.lines else ""
    headings = tuple(sorted(
        ln.strip() for ln in w.lines if ln.strip().startswith("- heading")
    ))
    return (title, headings)


def _has_nav_ancestor(el, max_depth=6):
    """¿Tiene un ancestro tipo outline/sidebar/"navegación"? Cubre tanto el
    patrón con AXPress (WhatsApp/Podcasts) como el de solas filas de outline
    sin Press (Ajustes del Sistema, Notas — ver nav_step en resolve.py)."""
    parent = ax.ax_attr(el, "AXParent")
    depth = 0
    while parent is not None and depth < max_depth:
        prole = ax.ax_attr(parent, "AXRole")
        plabel = ax.ax_attr(parent, "AXTitle") or ax.ax_attr(parent, "AXDescription") or ""
        if prole == "AXOutline" or "navegaci" in (plabel or "").lower() \
                or "sidebar" in (plabel or "").lower() or "barra lateral" in (plabel or "").lower():
            return True
        parent = ax.ax_attr(parent, "AXParent")
        depth += 1
    return False


def _row_label(el):
    """Mejor label disponible para una AXRow: su propio título/descripción, o
    si no tiene, el primer texto entre sus descendientes (BUG real encontrado
    en vivo, dos variantes distintas del mismo problema de fondo — la fila no
    tiene label propio:
    - Notas: el label vivía en AXTitle/AXDescription de un descendiente.
    - Finder: el label vivía en AXVALUE de un statictext hijo (describe()
      separa label y value; mirar solo `label` los deja afuera). Hay que
      revisar las dos cosas, no solo `label`."""
    label = ax.ax_attr(el, "AXTitle") or ax.ax_attr(el, "AXDescription")
    if label:
        return label
    stack = list(ax.ax_attr(el, "AXChildren") or [])
    seen = 0
    while stack and seen < 20:
        child = stack.pop(0)
        seen += 1
        role, clabel, cvalue, extras = ax.describe(child)
        text = clabel or (cvalue if isinstance(cvalue, str) else None)
        if text:
            return text
        stack.extend(ax.ax_attr(child, "AXChildren") or [])
    return None


EARLY_POSITION_LIMIT = 11  # ver 3er patrón abajo


def find_nav_containers(w):
    """Índices (y su label de navegación) de elementos dentro de un contenedor
    de navegación reconocible (sidebar/outline o el grupo de navegación
    principal) — es lo único que el crawler recorre, no cualquier botón del
    contenido. Tres patrones cubiertos:
    1. Elemento con acción Press directamente, con un ancestro etiquetado
       como nav (botones de tab, ej. WhatsApp).
    2. AXRow sin Press (outline de sidebar, ej. Ajustes del Sistema / Notas) —
       nav_step ya sabe navegar esto vía AXSelectedRows en el contenedor.
    3. Elemento con Press que aparece TEMPRANO en el árbol (primeros
       EARLY_POSITION_LIMIT nodos) pero sin ancestro etiquetado — BUG real
       encontrado en Podcasts: el grupo que agrupa "Buscar"/"Inicio"/
       "Novedades" es anónimo (axtree lo aplana en el texto, pero a nivel AX
       real sigue sin label ni rol reconocible). En casi toda app de escritorio
       la barra de nav se renderiza primero en el árbol — es una señal
       posicional, no estructural, y por eso más débil: si el click no
       produce un cambio real de pantalla (verificado después por
       screen_fingerprint), simplemente no cuenta como ruta y no hace daño."""
    candidates = []
    seen_labels = set()
    for i, el in enumerate(w.elements):
        role, label, value, extras = ax.describe(el)
        actions = ax.ax_actions(el)

        if role == "AXRow":
            row_label = _row_label(el)
            if not row_label or is_dangerous(row_label) or row_label in seen_labels:
                continue
            if _has_nav_ancestor(el):
                candidates.append((i, row_label))
                seen_labels.add(row_label)
            continue

        if not is_nav_candidate(role, ax.ax_attr(el, "AXSubrole"), extras, actions, label):
            continue
        if label in seen_labels:
            continue
        if _has_nav_ancestor(el) or i < EARLY_POSITION_LIMIT:
            candidates.append((i, label))
            seen_labels.add(label)
    return candidates


def crawl(app_name, max_depth=2, max_screens=25, pause=0.5):
    """Recorre `app_name` heurísticamente. Devuelve un reporte: pantallas
    encontradas, rutas descubiertas, y elementos evitados por peligrosos."""
    if _is_excluded_app(app_name):
        raise ValueError(
            f"{app_name!r} está en EXCLUDED_APPS (mensajería/redes sociales) — "
            f"el crawler no corre ahí, sin excepción.")
    visited = set()
    discovered = []  # [{"breadcrumbs": [...], "screen": (title, headings)}]
    skipped_dangerous = []
    home = None

    def explore(breadcrumbs, depth):
        if len(discovered) >= max_screens or depth > max_depth:
            return
        app, w = resolve.walk_app(app_name, max_nodes=300)
        fp = screen_fingerprint(w)
        if fp in visited:
            return
        visited.add(fp)
        discovered.append({"breadcrumbs": list(breadcrumbs), "screen": fp})

        candidates = find_nav_containers(w)
        for i, label in candidates:
            if is_dangerous(label):
                skipped_dangerous.append(label)
                continue
            app2, w2 = resolve.walk_app(app_name, max_nodes=300)
            before_fp = screen_fingerprint(w2)
            try:
                r = resolve.nav_step(app_name, label, min_score=0.65)
            except ValueError:
                continue
            time.sleep(pause)
            app3, w3 = resolve.walk_app(app_name, max_nodes=300)
            after_fp = screen_fingerprint(w3)
            if after_fp != before_fp and after_fp not in visited:
                explore(breadcrumbs + [label], depth + 1)
                # volver a home antes de seguir con el próximo hermano
                if home:
                    for step in home:
                        try:
                            resolve.nav_step(app_name, step, min_score=0.65)
                            time.sleep(pause)
                        except ValueError:
                            break

    app0, w0 = resolve.walk_app(app_name, max_nodes=300)
    home_fp = screen_fingerprint(w0)
    home = []  # desde la pantalla inicial, sin breadcrumbs previos
    explore([], 0)

    for d in discovered:
        atlas.remember(app_name, f"_auto:{' > '.join(d['breadcrumbs']) or 'home'}",
                        {"breadcrumbs": d["breadcrumbs"], "screen_title": d["screen"][0],
                         "discovered_by": "crawler"})

    return {
        "pantallas_encontradas": len(discovered),
        "rutas": [d["breadcrumbs"] for d in discovered],
        "evitados_por_peligrosos": sorted(set(skipped_dangerous)),
    }


if __name__ == "__main__":
    import sys
    app_name = sys.argv[1] if len(sys.argv) > 1 else "WhatsApp"
    report = crawl(app_name)
    print(f"=== crawl de {app_name!r} ===")
    print(f"pantallas encontradas: {report['pantallas_encontradas']}")
    for r in report["rutas"]:
        print("  ", " > ".join(r) or "(home)")
    if report["evitados_por_peligrosos"]:
        print("evitados por peligrosos:", report["evitados_por_peligrosos"])
