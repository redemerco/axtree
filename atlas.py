"""atlas — mapa de rutas de navegación por app, persistido en disco (atlas.json).

Por qué existe: el objeto de un botón NUNCA sobrevive entre lanzamientos de una
app (cada vez que se abre, la app crea sus controles de cero en memoria) — pero
el CAMINO en lenguaje natural para llegar a un control sí es estable ("Ajustes
del Sistema" -> "Accesibilidad" -> "Pantalla" -> scrollear -> "saturación").
El atlas guarda esos caminos, no referencias a elementos. Recorrerlo de nuevo
sigue requiriendo repetir los clicks reales (no hay teletransporte), pero ya no
hace falta redescubrir DÓNDE está ni CUÁNTO scrollear cada vez.
"""
import json
import os

ATLAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atlas.json")


def _load():
    if not os.path.exists(ATLAS_PATH):
        return {}
    with open(ATLAS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    with open(ATLAS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)


def recall(app, task):
    """Devuelve la ruta guardada (dict con 'breadcrumbs'/'scroll'/'query') para
    `task` en `app`, o None si nunca se recorrió."""
    return _load().get(app, {}).get(task)


def remember(app, task, route):
    """Guarda/actualiza la ruta descubierta para `task` en `app`."""
    data = _load()
    data.setdefault(app, {})[task] = route
    _save(data)


def forget(app, task=None):
    """Borra una ruta puntual, o todas las de una app si no se pasa `task` —
    para cuando una ruta guardada dejó de funcionar (la app cambió de versión)."""
    data = _load()
    if app not in data:
        return
    if task is None:
        del data[app]
    else:
        data[app].pop(task, None)
    _save(data)
