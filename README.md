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

## Limitaciones conocidas

- **Apps con renderizado propio no exponen nada** (ej. Spotify: solo la barra de menú). No hay fallback a screenshot todavía — es el próximo gap a cerrar.
- El daemon cachea la lista de apps corriendo al momento de resolver cada pedido; si una app tarda en terminar de arrancar puede no aparecer en el primer intento.
- `--under` reemplaza la vista cacheada de esa app hasta un `--refresh` — es intencional (permite drill-down y clickear varias veces seguidas dentro de un subárbol) pero hay que saber "salir".
- Escribir en campos que son WebView/contenteditable (ej. el editor de Notion) cae a simular teclado real, no a `AXValue` directo.

## Proyectos relacionados

No es una idea nueva — vale la pena mirar [macos-use](https://macos-use.dev/) (Swift, más maduro, sin daemon conocido), [macapptree](https://github.com/MacPaw/macapptree) (Python, solo lectura, con fallback a screenshot que a axtree le falta) y [Fazm](https://fazm.ai/) (híbrido AX+ScreenCaptureKit). El ángulo de axtree es la combinación leer+actuar+cache — no vimos ese combo resuelto en otro lado.
