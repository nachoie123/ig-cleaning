"""La app se actualiza sola. Al abrirla y una vez al día lee el appcast de su web; si hay una versión nueva, baja su
instalador (.pkg) de GitHub, saca la app de dentro, comprueba que la firmó el equipo 4HBV76K3X6 y que Apple la
notarizó, y la cambia por esta en cuanto la app se cierra (se cierre como se cierre). Sin ventanas ni contraseña: el
.pkg deja la app a nombre de quien la usa. Solo actúa en la app empaquetada, no al ejecutar el código a mano.

Prueba sin ventanas: ACTUALIZADOR_PRUEBA=1 "<App>.app/Contents/MacOS/<App>" hace una vuelta y sale.
"""
from __future__ import annotations

import datetime as dt
import os
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

EQUIPO = 'anchor apple generic and certificate leaf[subject.OU] = "4HBV76K3X6"'
SP = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
# Espera a que se cierre la app ($1 = su pid), aparta la vieja, pone la nueva; si algo falla, la vieja vuelve.
CAMBIO = ('while kill -0 "$1" 2>/dev/null; do sleep 1; done; rm -rf "$3.vieja"; mv "$3" "$3.vieja" || exit 1; '
          'if mv "$2" "$3"; then rm -rf "$3.vieja"; else mv "$3.vieja" "$3"; fi')
_preparada = False


def _v(texto: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", texto or "0"))


def _contexto():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def comprobar(feed: str, datos: Path) -> str:
    """Una vuelta. Devuelve qué hizo, en una frase."""
    global _preparada
    app = Path(sys.executable).resolve().parents[2]                     # .../X.app/Contents/MacOS/X
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    ctx = _contexto()
    item = ET.fromstring(urllib.request.urlopen(feed, timeout=30, context=ctx).read()).find("channel/item")
    nueva, actual = item.findtext(SP + "shortVersionString"), info["CFBundleShortVersionString"]
    if _preparada or _v(nueva) <= _v(actual):
        return f"al día ({actual}; en la web {nueva})"
    if not (os.access(app.parent, os.W_OK) and os.access(app, os.W_OK)):
        return f"hay {nueva}, pero no puedo escribir en {app.parent}: instálala desde la web"
    sitio = datos / "actualizacion"
    shutil.rmtree(sitio, ignore_errors=True)
    sitio.mkdir(parents=True)
    pkg = sitio / "nueva.pkg"
    with urllib.request.urlopen(item.find("enclosure").get("url"), timeout=900, context=ctx) as r, \
            pkg.open("wb") as f:
        shutil.copyfileobj(r, f)
    subprocess.run(["pkgutil", "--expand-full", pkg, sitio / "x"], check=True, capture_output=True)
    lista = sitio / "x" / "Payload" / app.name
    ni = plistlib.loads((lista / "Contents" / "Info.plist").read_bytes())
    if (ni["CFBundleIdentifier"], ni["CFBundleShortVersionString"]) != (info["CFBundleIdentifier"], nueva):
        raise ValueError(f"el instalador trae {ni['CFBundleIdentifier']} {ni['CFBundleShortVersionString']}")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", "-R=" + EQUIPO, lista], check=True,
                   capture_output=True)                                 # firmada por el equipo, sin tocar
    subprocess.run(["spctl", "--assess", "--type", "execute", lista], check=True, capture_output=True)  # notarizada
    pkg.unlink()
    subprocess.Popen(["/bin/sh", "-c", CAMBIO, "sh", str(os.getpid()), str(lista), str(app)], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _preparada = True
    return f"{nueva} preparada: se instala al cerrar la app"


def empezar(feed: str, datos: Path) -> None:
    """Comprueba al abrir y luego cada 24 h de reloj (aunque el Mac duerma entre medias), en segundo plano."""
    if not getattr(sys, "frozen", False):
        return
    prueba = bool(os.environ.get("ACTUALIZADOR_PRUEBA"))

    def vueltas():
        while True:
            try:
                r = comprobar(feed, datos)
            except Exception as e:                                       # sin red, GitHub caído…: otro día
                r = f"no se pudo ({type(e).__name__}: {str(e)[:200]})"
            datos.mkdir(parents=True, exist_ok=True)
            with (datos / "actualizacion.log").open("a") as f:
                f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {r}\n")
            if prueba:
                print(r, flush=True)
                os._exit(0)
            proxima = time.time() + 86400
            while time.time() < proxima:
                time.sleep(600)

    if prueba:
        vueltas()
    threading.Thread(target=vueltas, daemon=True).start()
