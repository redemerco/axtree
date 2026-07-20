# axtree

El árbol de accesibilidad de macOS (el mismo que usa VoiceOver) como texto compacto, para que un agente opere apps nativas sin screenshots ni coordenadas de píxeles.

## Por qué

Los agentes que controlan una Mac hoy dependen de computer-use: screenshot → el modelo mira la imagen → decide coordenadas → click → repetir. Es lento (cada paso paga una inferencia de visión completa) y frágil (adivina posiciones sobre píxeles). macOS ya expone un árbol completo y estructurado de cada botón, campo y acción disponible en cualquier app — es la misma API que usa VoiceOver para que gente ciega use su Mac. axtree solo la vuelca a texto y la conecta con acciones (`press`, `type_into`).

## Medido, no prometido

Mismo flujo real (Notion: crear página, escribir, borrar), comparado con computer-use usando el modelo de visión real, no una simulación:

| | axtree | computer-use real |
|---|---|---|
| Tiempo total | 0.105s | 54.8s (~500x) |
| Tokens de imagen | ~1.500 (un dump completo) | ~8.530 (4 screenshots) (~5x) |

## Instalación

```bash
git clone <este repo>
cd axtree
python3 -m venv .venv
.venv/bin/pip install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa
```

Necesita permiso de Accesibilidad (System Settings → Privacy & Security → Accessibility) para el proceso que lo ejecuta.

## Uso

```bash
axtree.py <app>                              # árbol de las ventanas de la app
axtree.py <app> --under eN                   # solo el subárbol de eN (los eN se renumeran)
axtree.py <app> --press eN                   # ejecutar una acción (default AXPress)
axtree.py <app> --type-into eN --type TXT    # escribir en un elemento
axtree.py <app> --refresh                    # ignorar cache del daemon, re-caminar el árbol
axtree.py --list                             # apps corriendo con UI
```

Salida: una línea por elemento — rol, label, value, acciones disponibles y su referencia `[eN]`.

## Daemon (opcional pero recomendado)

`daemon.py` mantiene el último árbol de cada app vivo en memoria entre llamadas, para que `press`/`type_into`/`--under` no vuelvan a caminar el árbol completo cada vez:

```bash
.venv/bin/python daemon.py &
```

Si no está corriendo, `axtree.py` cae solo a modo standalone (cada llamada camina de cero).

## Esperar cambios reales (AXObserver) en vez de sleep a ciegas

Después de un `press` o `type_into`, buena parte del código (`benchmark.py`, `chat_driver.py`, `daemon.py`, `axtree.py`, `mcp_server.py`) usa `time.sleep(0.3..0.6)` a ciegas antes de volver a leer el estado: adivina cuánto tarda la app en re-renderizar. `ax_core.wait_for_notification(pid, element, notification, action=None, timeout=2.0)` reemplaza esa adivinanza por un `AXObserver` real: registra interés en una notificación AX (`AXValueChanged`, `AXUIElementDestroyed`, `AXFocusedUIElementChanged`, ...) sobre un elemento puntual, ejecuta `action` (típicamente el `AXUIElementPerformAction` que dispara el cambio) y bombea el run loop hasta que la propia app confirma el cambio o vence `timeout` (red de seguridad).

Ojo con el orden: hay que registrar el observer **antes** de disparar la acción, no después — medido contra TextEdit, la notificación se postea de forma síncrona dentro del round-trip IPC de `AXUIElementPerformAction`, así que si se llama `perform_action()` y recién después `wait_for_notification(...)`, la notificación ya pasó y nunca llega (por eso `action` se ejecuta *adentro* de la función, con el observer ya enganchado).

**Cuándo conviene**: esperar una mutación de UI puntual y específica disparada por la acción — un checkbox/radiobutton que cambia de valor, un elemento que se destruye, el foco que se mueve — sobre un elemento ya resuelto de antemano. Ahí el helper es correcto por construcción (no una lectura prematura) y normalmente más rápido que un sleep fijo pensado para el peor caso.

**Cuándo NO hace falta**: una acción que no cambia nada observable vía AX en ese elemento (abrir un link externo, un botón que dispara una hoja de sistema fuera del árbol de esa app) — ahí un timeout chico sigue siendo lo más simple. Tampoco todas las apps postean todas las notificaciones para todos los elementos: algunos AX servers devuelven `kAXErrorNotificationUnsupported` (ej. los segmentos de un radiogroup de Finder) — en ese caso no hay señal real que escuchar y `wait_for_notification` devuelve `False` de inmediato en vez de quemar el timeout completo.

Demo real en `examples/press_and_wait.py` (TextEdit corriendo, toggle de un checkbox de Preferencias, medido con `time.time()`):

```
[A] sleep(0.4) a ciegas:  0.5044s  →  value='1'  OK (cambió)
[B] wait_for_notification:      0.1575s  →  value='1'  OK (cambió, notificación=True)

=== 0.5044s (sleep fijo) vs 0.1575s (observer) → 3.2x, ambos con lectura correcta ===
```

## Resolver por descripción, no por eid (`find`/`act`)

Los `eid` ([e42]) son índices de UN dump puntual — si la UI muta entre el dump y la acción, pueden terminar apuntando a otra cosa sin ningún error. `resolve.py` ataca esto resolviendo por descripción en lenguaje natural ("el botón de sumar", "campo de búsqueda") en vez de por posición:

```python
import resolve
resolve.act("Calculadora", "el botón de sumar", action="AXPress")
```

`act()` camina el árbol fresco, puntúa cada nodo (fuzzy word-level + sinónimos de rol + números escritos con letras → dígito) y ejecuta en la MISMA llamada — nunca guarda un eid entre turnos, así que no hay ventana para que la UI mute en el medio. Expuesto también como tools MCP: `find(app, query)` para explorar candidatos, `act(app, query, action=..., text=..., key=...)` para resolver+actuar. Validado en vivo: `5 + 3 =` completo en Calculadora usando solo `"botón cinco"`, `"el botón de sumar"`, `"botón tres"`, `"el botón de igual"`.

Límite conocido: controles solo-ícono sin ningún label de texto quedan a merced de heurísticas de sinónimo de rol — para esos casos, un eid de un dump fresco sigue siendo más preciso.

## Grabar interacción humana (`ax_recorder.py`)

El inverso de lo que hace axtree: en vez de ejecutar acciones, escucha lo que un humano hace de verdad (clicks + tipeo) y lo reconstruye como script `press`/`type_into` reproducible. Combina un `AXObserver` registrado sobre la app entera (no un control puntual) con un `CGEventTap` global para identificar qué se clickeó por hit-testing de coordenadas.

```python
from ax_recorder import Recorder, reconstruct_script
events = Recorder(pid).record(duration=5.0)
script = reconstruct_script(events)
```

Demo real en `examples/demo_recorder.py`: graba una sesión sintética en TextEdit (click + tipeo + click en un botón, todo con eventos de sesión reales, no AX directo) y reconstruye exactamente `type_into textarea "Hola recorder"` / `press button`, capturando fielmente hasta el autocorrect de la app. Ningún competidor mencionado abajo ofrece esto — todos son solo-ejecución.

Limitaciones: el `CGEventTap` es session-wide (no filtrado por pid — un click en otra app también se hit-testea), y una app en fullscreen nativo mueve su ventana a otro Space, lo que puede confundir el hit-testing.

## Limitaciones conocidas

- **Apps con renderizado propio no exponen nada** (ej. Spotify: solo la barra de menú, o directamente 0 ventanas en `AXWindows`). `get_tree` detecta el árbol vacío/casi vacío (menos de 3 nodos de contenido real, sin contar los botones de chrome de ventana) y cae automáticamente a un screenshot recortado a la ventana (`screenshot_fallback` en `ax_core.py`). Desactivable con `--no-fallback` (CLI) o `fallback=False` (MCP). Esto cubre `axtree.py` y `mcp_server.py`; si usás el daemon (`daemon.py`), ese camino todavía no tiene el fallback.
- El daemon cachea la lista de apps corriendo al momento de resolver cada pedido; si una app tarda en terminar de arrancar puede no aparecer en el primer intento.
- `--under` reemplaza la vista cacheada de esa app hasta un `--refresh` — es intencional (permite drill-down y clickear varias veces seguidas dentro de un subárbol) pero hay que saber "salir".
- Escribir en campos que son WebView/contenteditable (ej. el editor de Notion) cae a simular teclado real, no a `AXValue` directo.

## Proyectos relacionados

No es una idea nueva — vale la pena mirar [macos-use](https://macos-use.dev/) (Swift, más maduro, sin daemon conocido), [macapptree](https://github.com/MacPaw/macapptree) (Python, solo lectura, con fallback a screenshot que a axtree le falta) y [Fazm](https://fazm.ai/) (híbrido AX+ScreenCaptureKit). El ángulo de axtree es la combinación leer+actuar+cache — no vimos ese combo resuelto en otro lado.
