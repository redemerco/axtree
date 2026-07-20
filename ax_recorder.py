#!/usr/bin/env python3
"""ax_recorder — PROTOTIPO EXPERIMENTAL. Grabar una sesión de interacción humana
real (clicks, tipeo) como una secuencia de acciones axtree reproducibles
(`press`/`type_into`), sin coordenadas de pantalla en el resultado final.

Es el inverso de lo que hace axtree hoy: en vez de ejecutar acciones AX, ESCUCHA
lo que un humano hace y las reconstruye como script.

Dos fuentes de señal, combinadas:

1. AXObserver registrado UNA SOLA VEZ sobre el elemento APLICACIÓN (no sobre un
   control puntual) para kAXValueChangedNotification y
   kAXFocusedUIElementChangedNotification. Validado empíricamente (ver reporte):
   TextEdit postea estas notificaciones con el descendiente real como `elem` del
   callback, aunque el observer se haya registrado sobre el AXUIElement de la
   aplicación entera. Esto es lo que hace viable "escuchar toda la app" sin saber
   de antemano qué se va a tocar.

2. CGEventTap global (listen-only, no consume eventos) sobre kCGEventLeftMouseDown
   + AXUIElementCopyElementAtPosition (hit-testing por coordenadas de pantalla)
   para identificar QUÉ elemento fue clickeado — necesario porque un press de un
   AXButton plano no siempre dispara AXValueChanged en el propio botón, así que
   el observer solo no alcanza para "grabar clicks" en general.

Limitación conocida de este prototipo: el CGEventTap es sesión-wide, no filtrado
por pid — si el usuario clickea otra app durante la grabación, igual se hittestea
(pero probablemente contra la ventana equivocada si no es la app grabada; no se
filtra acá a propósito, para mantener el prototipo legible).
"""
import threading
import time

import objc
from ApplicationServices import (
    AXObserverCreate,
    AXObserverAddNotification,
    AXObserverRemoveNotification,
    AXObserverGetRunLoopSource,
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateSystemWide,
)
from CoreFoundation import (
    CFRunLoopAddSource,
    CFRunLoopRemoveSource,
    CFRunLoopGetCurrent,
    CFRunLoopRunInMode,
    kCFRunLoopDefaultMode,
)
from Quartz import (
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetLocation,
    CFMachPortCreateRunLoopSource,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventTapOptionListenOnly,
    kCGEventLeftMouseDown,
    CGEventMaskBit,
)

import ax_core as ax

SYSWIDE = AXUIElementCreateSystemWide()


def _describe_at(x, y):
    err, hit = AXUIElementCopyElementAtPosition(SYSWIDE, float(x), float(y), None)
    if err != 0 or hit is None:
        return None
    role, label, value, extras = ax.describe(hit)
    return {"element": hit, "role": role, "label": label, "value": value,
            "extras": extras, "actions": ax.ax_actions(hit)}


class Recorder:
    """Graba clicks + cambios de foco/valor de `pid` durante `record()`."""

    def __init__(self, pid):
        self.pid = pid
        self.events = []  # dicts: {t, kind, role, label, value, actions}
        self._observer = None
        self._tap = None

    def record(self, duration, drive=None):
        """Bombea el run loop `duration` segundos. Si se pasa `drive` (callable),
        se corre en un thread aparte apenas arrancó a escuchar — pensado para que
        el propio test dispare las acciones "humanas" simuladas, ya con los
        listeners enganchados (mismo cuidado de orden que wait_for_notification).

        Nota pyobjc: `objc.callbackFor` no puede decorar un *bound method* (no
        tiene __dict__ propio para el atributo pyobjc_closure que agrega el
        decorador) — hace falta una función anidada plana que capture `self`
        por clausura, igual que hace ax_core.wait_for_notification."""
        events = self.events
        app_el = ax.AXUIElementCreateApplication(self.pid)

        @objc.callbackFor(AXObserverCreate)
        def cb_ax(observer, elem, notif, refcon):
            role, label, value, extras = ax.describe(elem)
            events.append({
                "t": time.time(), "kind": str(notif), "role": role, "label": label,
                "value": value, "element": elem,
            })

        err, observer = AXObserverCreate(self.pid, cb_ax, None)
        if err != 0:
            raise RuntimeError(f"AXObserverCreate falló: err {err}")
        self._observer = observer
        for notif in ("AXValueChanged", "AXFocusedUIElementChanged"):
            AXObserverAddNotification(observer, app_el, notif, None)
        ax_source = AXObserverGetRunLoopSource(observer)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), ax_source, kCFRunLoopDefaultMode)

        def cb_tap(proxy, etype, event, refcon):
            loc = CGEventGetLocation(event)
            info = _describe_at(loc.x, loc.y)
            if info is not None:
                events.append({
                    "t": time.time(), "kind": "click", "role": info["role"],
                    "label": info["label"], "value": info["value"],
                    "actions": info["actions"], "element": info["element"],
                    "pos": (loc.x, loc.y),
                })
            return event  # listen-only: hay que devolver el evento intacto igual

        mask = CGEventMaskBit(kCGEventLeftMouseDown)
        tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap,
                                kCGEventTapOptionListenOnly, mask, cb_tap, None)
        if tap is None:
            raise RuntimeError(
                "CGEventTapCreate devolvió None — falta permiso "
                "(Accessibility o Input Monitoring) para este proceso.")
        self._tap = tap
        tap_source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), tap_source, kCFRunLoopDefaultMode)
        CGEventTapEnable(tap, True)

        try:
            if drive is not None:
                threading.Thread(target=drive, daemon=True).start()
            deadline = time.time() + duration
            while time.time() < deadline:
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
        finally:
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), ax_source, kCFRunLoopDefaultMode)
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), tap_source, kCFRunLoopDefaultMode)
            for notif in ("AXValueChanged", "AXFocusedUIElementChanged"):
                AXObserverRemoveNotification(observer, app_el, notif)

        return self.events


def reconstruct_script(events):
    """De la lista cruda de eventos (click / AXValueChanged / AXFocusedUIElementChanged,
    ya en orden cronológico) arma líneas de script tipo axtree. Heurística simple:

    - un `click` sobre un elemento con acción AXPress y rol que no es de texto
      editable → `press "<role> label"`
    - un `click` sobre un campo de texto, seguido de AXValueChanged en ESE elemento
      → se toma el valor ANTES (al momento del click) y el valor DESPUÉS (el último
      AXValueChanged de ese elemento antes del próximo click/foco) y si uno es
      prefijo del otro se reporta el texto insertado → `type_into "<role> label" "texto"`
    - un `click` sobre un checkbox/botón cuyo propio valor cambia → se reporta el
      nuevo valor también, a modo informativo.
    """
    TEXTY_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}
    lines = []
    i = 0
    n = len(events)
    while i < n:
        ev = events[i]
        if ev["kind"] != "click":
            i += 1
            continue
        role, label = ev["role"], ev["label"]
        tag = f'{role[2:].lower() if role.startswith("AX") else role}' + (f' "{label}"' if label else "")
        if role in TEXTY_ROLES:
            before = ev.get("value")
            after = before
            j = i + 1
            while j < n and events[j]["kind"] != "click":
                if events[j]["kind"] == "AXValueChanged":
                    after = events[j]["value"]
                j += 1
            if isinstance(before, str) and isinstance(after, str) and after != before:
                inserted = after[len(before):] if after.startswith(before) else after
                lines.append(f'type_into {tag} "{inserted}"')
            i = j
            continue
        else:
            lines.append(f'press {tag}')
            # consumir los AXValueChanged asociados a este press (informativo, no genera línea aparte)
            j = i + 1
            while j < n and events[j]["kind"] != "click":
                j += 1
            i = j
            continue
    return lines
