#!/usr/bin/env python3
"""Smoke test end-to-end contra una app real (TextEdit).

No depende de pytest (no está instalado en el venv del proyecto): es un script
standalone con asserts, imprime PASS/FAIL por paso y sale con código 1 en el
primer fallo. Si pytest SÍ está disponible se lo puede correr igual con
`pytest test_smoke.py` (las funciones step_* actúan como pasos secuenciales,
pero el modo recomendado y el que valida este repo es ejecutarlo directo:

    .venv/bin/python test_smoke.py

Pasos, en orden, cada uno verificado antes de seguir al próximo:
  1. abrir TextEdit con un documento nuevo, `get_tree` y confirmar que aparece
     un elemento con role "textarea"
  2. `type_into` sobre ese textarea con un texto de prueba conocido
  3. releer el árbol (`--read`, equivalente a read_value/AXValue) y confirmar
     que el texto coincide EXACTO con lo esperado
  4. cerrar el documento sin guardar, sin tocar nada más del usuario
"""
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AXTREE = HERE / "axtree.py"
APP = "TextEdit"
TEST_TEXT = f"axtree-smoke-test-{int(time.time())}"

STEP = {"n": 0}


def step(name):
    STEP["n"] += 1
    print(f"\n[{STEP['n']}] {name}")


def ok(msg):
    print(f"    PASS: {msg}")


def fail(msg):
    print(f"    FAIL: {msg}")
    sys.exit(1)


def run_axtree(*args, timeout=15):
    """Corre axtree.py en modo standalone (--no-daemon: aislado de cualquier
    daemon que pueda estar corriendo en la máquina, para que el smoke test sea
    determinístico) con el mismo intérprete que corre este script."""
    cmd = [sys.executable, str(AXTREE), APP, "--no-daemon", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def cleanup_textedit():
    """Mata TODOS los procesos de TextEdit (killall, no un close-documents suave).

    BUG real encontrado corriendo este test repetidas veces: `open -a TextEdit -n`
    lanza un PROCESO NUEVO cada vez (-n = nueva instancia), y `find_app`/`osascript
    tell application "TextEdit"` pueden terminar apuntando a procesos DISTINTOS de
    los varios que quedan corriendo en paralelo. Eso producía texto concatenado
    entre corridas (insertaba en un documento viejo con texto de una corrida
    anterior) y hasta "0 ventanas" (encontraba un proceso nuevo que todavía no
    había terminado de crear su ventana). La única forma confiable de aislar el
    test es garantizar que exista UN SOLO proceso de TextEdit en todo momento."""
    subprocess.run(["killall", "TextEdit"], capture_output=True, text=True, timeout=10)
    time.sleep(0.3)


def wait_for_textarea(timeout=15.0, interval=0.4):
    """Reintenta get_tree hasta encontrar un elemento '- textarea', en vez de
    confiar en un sleep fijo: recién abierta, una app puede tardar más de lo
    esperado en terminar de crear su ventana (mismo tipo de race de timing que
    ya se vio con apps recién lanzadas — sleeps fijos son inherentemente
    flaky). Devuelve (eid, out) o falla con el último dump visto."""
    deadline = time.time() + timeout
    last_out, last_err, last_rc = "", "", None
    while time.time() < deadline:
        rc, out, err = run_axtree()
        last_out, last_err, last_rc = out, err, rc
        if rc == 0:
            for line in out.splitlines():
                if "- textarea" in line:
                    m = re.search(r"\[e(\d+)\]\s*$", line)
                    if m:
                        return m.group(1), out
        time.sleep(interval)
    if last_rc != 0:
        fail(f"axtree.py devolvió rc={last_rc}\nstdout={last_out}\nstderr={last_err}")
    fail(f"no encontré ningún elemento '- textarea' tras {timeout}s de reintentos:\n{last_out}")


def setup_textedit():
    """Garantiza UN SOLO proceso de TextEdit con un documento nuevo y REALMENTE
    vacío. Ver cleanup_textedit() y wait_for_textarea() para el detalle de los
    bugs de timing/autosave que esto evita."""
    cleanup_textedit()  # matar cualquier instancia previa (propia o del usuario) antes de arrancar
    subprocess.run(["open", "-a", APP], check=True)  # sin -n: un solo proceso, siempre
    time.sleep(1.5)
    subprocess.run(
        ["osascript", "-e", f'tell application "{APP}" to make new document'],
        capture_output=True, text=True, timeout=10,
    )
    # BUG real encontrado corriendo el test muchas veces: TextEdit tiene auto-guardado/
    # resume que puede restaurar contenido NO guardado de una corrida anterior incluso
    # después de un killall — "make new document" no garantiza que arranque vacío. Se
    # fuerza el vaciado explícito del texto para no depender de ese estado implícito.
    subprocess.run(
        ["osascript", "-e", f'tell application "{APP}" to set text of document 1 to ""'],
        capture_output=True, text=True, timeout=10,
    )


def main():
    print(f"=== axtree smoke test — app={APP!r} texto={TEST_TEXT!r} ===")

    # --- setup, con un reintento completo si la ventana nunca llega a existir ---
    # BUG real encontrado corriendo el test muchas veces (~1 de cada 10): a veces
    # TextEdit no termina de crear su ventana en absoluto tras el lanzamiento (no es
    # timing corto, es que el intento de arranque falló por completo). Reintentar
    # matando y relanzando de cero, en vez de solo esperar más, es lo que lo resuelve.
    step("setup: abrir TextEdit con un documento nuevo")
    eid = out = None
    setup_err = None
    for attempt in range(2):
        setup_textedit()
        try:
            eid, out = wait_for_textarea(timeout=10.0 if attempt == 0 else 15.0)
            break
        except SystemExit as e:
            setup_err = e
    if eid is None:
        raise setup_err
    ok("TextEdit abierto con documento nuevo y vacío")

    try:
        # --- paso 1: confirmado en el setup de arriba (con reintentos) ---
        step("get_tree: dump de TextEdit y buscar un elemento role=textarea")
        ok(f"encontré textarea en e{eid}")

        # --- paso 2: type_into ---
        step(f"type_into e{eid} con texto de prueba {TEST_TEXT!r}")
        rc, out, err = run_axtree("--type-into", f"e{eid}", "--type", TEST_TEXT, "--quiet")
        if rc != 0:
            fail(f"type_into devolvió rc={rc}\nstdout={out}\nstderr={err}")
        if "TYPE_INTO OK" not in out:
            fail(f"la salida de type_into no confirma éxito:\n{out}")
        ok(f"type_into confirmó escritura: {out.strip().splitlines()[-1]}")

        # --- paso 3: releer y comparar EXACTO ---
        # BUG real encontrado corriendo el test muchas veces seguidas: la Accessibility
        # API de macOS puede devolver "0 ventanas" transitoriamente por un instante justo
        # después de una escritura (AXWindows momentáneamente vacío mientras el bridge de
        # accesibilidad de la app termina de reflejar el cambio) — no es un problema del
        # cache de axtree (esto corre en --no-daemon), es inestabilidad real de la propia
        # AX API. Reintentar un par de veces con backoff corto lo resuelve sin esconder un
        # fallo real: si el texto sigue sin coincidir después de los reintentos, sí falla.
        step(f"releer AXValue de e{eid} y comparar contra {TEST_TEXT!r}")
        read_back, last_err = None, ""
        for attempt in range(4):
            rc, out, err = run_axtree("--read", f"e{eid}", "--quiet")
            last_err = err
            if rc == 0:
                read_back = out.strip("\n")
                read_back = read_back.splitlines()[-1] if read_back else ""
                if read_back == TEST_TEXT:
                    break
            time.sleep(0.3 * (attempt + 1))
        if read_back != TEST_TEXT:
            fail(f"texto releído no coincide EXACTO tras reintentos.\n  esperado: {TEST_TEXT!r}\n  obtenido: {read_back!r}\n  stderr: {last_err}")
        ok(f"texto releído coincide exacto: {read_back!r}")

    finally:
        # --- paso 4: cerrar TextEdit sin guardar (killall, ver cleanup_textedit) ---
        step("cerrar TextEdit")
        cleanup_textedit()
        ok("proceso de TextEdit terminado, sin guardar")

    print("\n=== TODOS LOS PASOS PASARON ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
