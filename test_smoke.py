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
    """Cierra los documentos de TextEdit sin guardar, sin matar el proceso
    (por si el usuario ya lo tenía abierto con otra cosa)."""
    subprocess.run(
        ["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
        capture_output=True, text=True, timeout=10,
    )


def main():
    print(f"=== axtree smoke test — app={APP!r} texto={TEST_TEXT!r} ===")

    # --- setup: TextEdit con un documento nuevo ---
    step("setup: abrir TextEdit con un documento nuevo")
    subprocess.run(["open", "-a", APP, "-n"], check=True)
    time.sleep(1.5)
    subprocess.run(
        ["osascript", "-e", f'tell application "{APP}" to make new document'],
        capture_output=True, text=True, timeout=10,
    )
    time.sleep(1.0)
    ok("TextEdit abierto con documento nuevo")

    try:
        # --- paso 1: get_tree / dump, confirmar role textarea ---
        step("get_tree: dump de TextEdit y buscar un elemento role=textarea")
        rc, out, err = run_axtree()
        if rc != 0:
            fail(f"axtree.py devolvió rc={rc}\nstdout={out}\nstderr={err}")

        eid = None
        for line in out.splitlines():
            if "- textarea" in line:
                m = re.search(r"\[e(\d+)\]\s*$", line)
                if m:
                    eid = m.group(1)
                    break
        if eid is None:
            fail(f"no encontré ningún elemento '- textarea' en el dump:\n{out}")
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
        step(f"releer AXValue de e{eid} y comparar contra {TEST_TEXT!r}")
        rc, out, err = run_axtree("--read", f"e{eid}", "--quiet")
        if rc != 0:
            fail(f"--read devolvió rc={rc}\nstdout={out}\nstderr={err}")
        read_back = out.strip("\n")
        # --read imprime el AXValue completo tal cual, sin nada más en stdout
        read_back = read_back.splitlines()[-1] if read_back else ""
        if read_back != TEST_TEXT:
            fail(f"texto releído no coincide EXACTO.\n  esperado: {TEST_TEXT!r}\n  obtenido: {read_back!r}")
        ok(f"texto releído coincide exacto: {read_back!r}")

    finally:
        # --- paso 4: cerrar el documento sin guardar ---
        step("cerrar el documento de TextEdit sin guardar")
        cleanup_textedit()
        ok("documento cerrado (saving no)")

    print("\n=== TODOS LOS PASOS PASARON ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
