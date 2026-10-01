"""IG Cleaning: deja de seguir a quien no te sigue o con quien no hablas.

Dos ventanas: Instagram (inicias sesión tú) y el panel. Todas las llamadas a
Instagram se hacen DENTRO de la ventana de Instagram con tu sesión, como si
navegaras tú. Nada de contraseñas en el código.
"""
import json
import os
import random
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import certifi
import webview

# Código (dentro del .app, solo lectura) y datos (por usuario) van separados.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
DATA = Path.home() / "Library" / "Application Support" / "IG Cleaning"
_OLD = DATA.parent / "IG Limpieza"     # nombre anterior de la app: se trae sus datos y su sesión
if _OLD.exists() and not DATA.exists():
    _OLD.rename(DATA)
WEB = DATA / "web"          # el panel se sirve desde aquí para poder enseñar las fotos
PICS = WEB / "pics"
STATE_FILE = DATA / "state.json"
PICS.mkdir(parents=True, exist_ok=True)
shutil.copy2(BUNDLE / "web" / "index.html", WEB / "index.html")

PK_RE = re.compile(r"^\d{1,25}$")
PIC_HOSTS = (".cdninstagram.com", ".fbcdn.net")


def valid_pk(pk):
    return isinstance(pk, str) and bool(PK_RE.match(pk))

DEFAULTS = {
    # Ritmo: una cada 20-40 s, descanso de 10 min cada 25, máx. 60/hora y 150/día.
    "settings": {"min_delay": 20, "max_delay": 40, "daily_cap": 150, "hourly_cap": 60,
                 "burst": 25, "rest_min": 10},
    "warned": False,      # Instagram avisó alguna vez -> ritmo prudente para siempre
    "block_until": None,  # tras un aviso real de Instagram, nada se mueve hasta esta fecha
    "me": None,
    "accounts": {},      # pk -> {username, full_name, pic, verified, follows_me, last_dm, followers}
    "whitelist": [],
    "queue": [],
    "unfollowed": [],    # {pk, username, full_name, pic, at}
    "analyzed_at": None,
}

# Ritmo tras un aviso de Instagram: más lento y con menos tope.
SAFE = {"min_delay": 30, "max_delay": 60, "daily_cap": 100, "hourly_cap": 40, "burst": 20, "rest_min": 15}

# Helpers que se inyectan en la ventana de Instagram antes de cada llamada.
JS_HELPERS = r"""
const ck=Object.fromEntries(document.cookie.split('; ').filter(Boolean).map(c=>{const i=c.indexOf('=');return [c.slice(0,i),decodeURIComponent(c.slice(i+1))]}));
const H={'X-IG-App-ID':'936619743392459','X-Requested-With':'XMLHttpRequest','X-CSRFToken':ck.csrftoken||''};
async function g(u,opt={}){const r=await fetch(u,{credentials:'include',...opt,headers:{...H,...(opt.headers||{})}});
  const t=await r.text();let j=null;try{j=JSON.parse(t)}catch(e){}
  return {status:r.status,json:j};}
"""


def log(msg):
    """Registro de cada acción en data.log (para saber qué respondió Instagram)."""
    try:
        with open(DATA / "data.log", "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


class RateLimited(Exception):
    pass


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.d = json.loads(json.dumps(DEFAULTS))
        if STATE_FILE.exists():
            self.d.update(json.loads(STATE_FILE.read_text()))
        # El ritmo lo fija la app, no el usuario. Si Instagram ya avisó alguna vez,
        # se queda en el ritmo prudente para siempre.
        self.d["settings"] = dict(SAFE if self.d.get("warned") else DEFAULTS["settings"])

    def save(self):
        with self.lock:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, ensure_ascii=False))
            tmp.replace(STATE_FILE)


class IG:
    """Ejecuta fetch() dentro de la ventana de Instagram y devuelve el JSON."""

    def __init__(self, win):
        self.win = win

    def run(self, body, timeout=90):
        done, box = threading.Event(), {}

        def cb(res):
            box["r"] = res
            done.set()

        script = "(async()=>{try{%s\n%s}catch(e){return JSON.stringify({error:String(e)})}})()" % (JS_HELPERS, body)
        self.win.evaluate_js(script, cb)
        if not done.wait(timeout):
            raise TimeoutError("Instagram no responde")
        r = box.get("r")
        while isinstance(r, str):
            r = json.loads(r)
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(r["error"])
        return r

    def get(self, url):
        r = self.run("return JSON.stringify(await g(%s));" % json.dumps(url))
        if r["status"] == 429:
            raise RateLimited()
        return r

    def post(self, url, body=None):
        """POST como lo hace la web: cuerpo de formulario + cabecera X-IG-WWW-Claim."""
        r = self.run(
            "const b=new URLSearchParams(%s).toString();"
            "const r=await fetch(%s,{method:'POST',credentials:'include',body:b,headers:{...H,"
            "'X-IG-WWW-Claim':sessionStorage.getItem('www-claim-v2')||'0',"
            "'Content-Type':'application/x-www-form-urlencoded'}});"
            "const t=await r.text();let j=null;try{j=JSON.parse(t)}catch(e){}"
            "return JSON.stringify({status:r.status,json:j,raw:j?null:t.slice(0,300)});"
            % (json.dumps(body or {}), json.dumps(url)))
        if r["status"] == 429:
            raise RateLimited()
        return r

    def friendship(self, action, pk):
        """action = 'destroy' (dejar de seguir) o 'create' (volver a seguir).
        Usa la misma función interna (PolarisInstapi) que la web al pulsar el botón,
        así la petición lleva exactamente las mismas cabeceras y sellos que un clic."""
        body = {"container_module": "profile", "user_id": pk}
        if action == "create":
            body["include_follow_friction_check"] = True
        r = self.run(
            "try{const res=await window.require('PolarisInstapi').apiPost(%s,{body:%s,path:{target_user_id:%s}});"
            "return JSON.stringify({status:200,json:res&&res.data!==undefined?res.data:res});"
            "}catch(e){const o=e&&(e.responseObject||e.data||e.response)||{};"
            "return JSON.stringify({status:e&&(e.statusCode||e.status)||0,json:typeof o==='object'?o:null,"
            "raw:String(e&&e.message||e).slice(0,300)})}"
            % (json.dumps(f"/api/v1/friendships/{action}/{{target_user_id}}/"), json.dumps(body), json.dumps(pk)))
        if r["status"] == 429:
            raise RateLimited()
        return r

    def whoami(self):
        return self.run("return JSON.stringify({uid: ck.ds_user_id||null, url: location.href});", timeout=5)

    def follower_count(self, username):
        """Número de seguidores leído de la descripción pública del perfil."""
        r = self.run(
            "const r=await fetch('/'+%s+'/',{credentials:'include'});"
            # leer a trozos y cortar en cuanto aparece la descripción (está al principio)
            "const rd=r.body.getReader(),dec=new TextDecoder();let h='',m=null;"
            "const re=/<meta[^>]+(?:og:description|name=\"description\")[^>]*content=\"([^\"]*)\"/;"
            "while(true){const {done,value}=await rd.read();if(done)break;h+=dec.decode(value,{stream:true});"
            "m=h.match(re);if(m||h.length>400000){rd.cancel();break}}"
            "return JSON.stringify({status:r.status,desc:m?m[1].slice(0,160):null});" % json.dumps(username)
        )
        if r["status"] == 429:
            raise RateLimited()
        return parse_followers(r.get("desc") or "")


def parse_followers(desc):
    """'687M seguidores' / '12,5 mil seguidores' / '1.234 Followers' -> int."""
    m = re.search(r"([\d.,]+)\s*(mil|millones|millón|K|M|B)?\s*(?:de\s+)?(?:seguidores|followers)", desc, re.I)
    if not m:
        return None
    num, suf = m.group(1), (m.group(2) or "").lower()
    mult = {"k": 1e3, "mil": 1e3, "m": 1e6, "millones": 1e6, "millón": 1e6, "b": 1e9}.get(suf)
    if mult:
        return int(float(num.replace(",", ".")) * mult)
    return int(re.sub(r"[.,]", "", num))


SSL = ssl.create_default_context(cafile=certifi.where())


def download_pic(pk, url):
    if not valid_pk(pk) or not url:
        return
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not (u.hostname or "").endswith(PIC_HOSTS):
        return
    path = PICS / f"{pk}.jpg"
    if path.exists():
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=SSL, timeout=20) as r:
            path.write_bytes(r.read())
    except Exception:
        pass


class Api:
    """Lo que llama el panel (window.pywebview.api.*)."""

    def __init__(self):
        self.state = State()
        self.ig = None
        self.task = {"running": False, "step": "", "done": 0, "total": 0, "error": None}
        self.worker_note = ""
        self.stop_analysis = False
        self.halt = False    # True tras una respuesta rara: la cola no se mueve hasta reabrir

    # ---------- estado ----------
    def status(self):
        s = self.state.d
        try:
            who = self.ig.whoami() if self.ig else {}
        except Exception as e:
            who = {}
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "logged_in": bool(who.get("uid")),
            "me": s["me"],
            "task": self.task,
            "queue": len(s["queue"]),
            "today": sum(1 for u in s["unfollowed"] if u["at"].startswith(today)),
            "settings": s["settings"],
            "worker": self.worker_note,
            "analyzed_at": s["analyzed_at"],
        }

    def data(self):
        s = self.state.d
        return {"accounts": s["accounts"], "whitelist": s["whitelist"], "queue": s["queue"],
                "unfollowed": s["unfollowed"]}

    def set_settings(self, settings):
        with self.state.lock:
            self.state.d["settings"].update({k: int(v) for k, v in settings.items()})
            self.state.save()
        return True

    def toggle_whitelist(self, pk):
        if not valid_pk(pk):
            return False
        with self.state.lock:
            wl = self.state.d["whitelist"]
            wl.remove(pk) if pk in wl else wl.append(pk)
            self.state.save()
        return pk in self.state.d["whitelist"]

    # ---------- análisis ----------
    def analyze(self, influencer_min=10000):
        if self.task["running"]:
            return False
        self.stop_analysis = False
        threading.Thread(target=self._analyze, args=(int(influencer_min),), daemon=True).start()
        return True

    def cancel(self):
        self.stop_analysis = True
        return True

    def _step(self, step, done=0, total=0):
        self.task.update(step=step, done=done, total=total)

    def _get_paced(self, url, pause=1.5):
        step = self.task["step"]
        for attempt in range(6):
            try:
                r = self.ig.get(url)
                time.sleep(pause + random.random())
                self.task["step"] = step
                return r
            except RateLimited:
                wait = 60 * (attempt + 1)
                self._step(f"Instagram pide calma: espero {wait}s…", self.task["done"], self.task["total"])
                time.sleep(wait)
            except Exception:
                # corte de red puntual ("Load failed", timeout): reintentar
                wait = 10 * (attempt + 1)
                self._step(f"Fallo de red, reintento en {wait}s…", self.task["done"], self.task["total"])
                time.sleep(wait)
        raise RuntimeError("Instagram no responde. Prueba dentro de un rato.")

    def _analyze(self, influencer_min):
        self.task.update(running=True, error=None)
        try:
            who = self.ig.whoami()
            uid = who.get("uid")
            if not uid:
                raise RuntimeError("Primero inicia sesión en la ventana de Instagram.")

            # 1. Quién soy
            self._step("Leyendo tu cuenta…")
            inbox = self._get_paced("/api/v1/direct_v2/inbox/?limit=20")["json"]
            me = (inbox or {}).get("viewer", {})
            self.state.d["me"] = {"pk": uid, "username": me.get("username"), "full_name": me.get("full_name")}

            # 2. A quién sigo
            following, max_id = [], ""
            while not self.stop_analysis:
                self._step("Leyendo a quién sigues…", len(following))
                j = self._get_paced(f"/api/v1/friendships/{uid}/following/?count=100" + (f"&max_id={max_id}" if max_id else ""))["json"]
                following += j.get("users", [])
                max_id = j.get("next_max_id")
                if not max_id:
                    break

            # 3. Quién me sigue
            followers, max_id = set(), ""
            while not self.stop_analysis:
                self._step("Leyendo quién te sigue…", len(followers))
                j = self._get_paced(f"/api/v1/friendships/{uid}/followers/?count=100" + (f"&max_id={max_id}" if max_id else ""))["json"]
                followers |= {str(u["pk"]) for u in j.get("users", [])}
                max_id = j.get("next_max_id")
                if not max_id:
                    break

            # 4. Con quién hablo por mensaje directo (solo chats 1 a 1)
            last_dm, cursor, pages = {}, None, 0
            cutoff_us = (time.time() - 3 * 365 * 86400) * 1e6
            while not self.stop_analysis and pages < 60:
                self._step("Leyendo tus chats…", len(last_dm))
                j = (self._get_paced("/api/v1/direct_v2/inbox/?limit=20" + (f"&cursor={cursor}" if cursor else ""))["json"] or {}).get("inbox", {})
                threads = j.get("threads", [])
                for t in threads:
                    users = t.get("users", [])
                    if len(users) == 1:
                        pk = str(users[0]["pk"])
                        last_dm[pk] = max(last_dm.get(pk, 0), int(t.get("last_activity_at") or 0))
                pages += 1
                cursor = j.get("oldest_cursor")
                oldest = min((int(t.get("last_activity_at") or 0) for t in threads), default=0)
                if not j.get("has_older") or not cursor or oldest < cutoff_us:
                    break

            if self.stop_analysis:
                raise RuntimeError("Análisis cancelado.")

            # 5. Juntar todo
            old = self.state.d["accounts"]
            accounts = {}
            for u in following:
                pk = str(u["pk"])
                accounts[pk] = {
                    "username": u["username"],
                    "full_name": u.get("full_name", ""),
                    "verified": bool(u.get("is_verified")),
                    "private": bool(u.get("is_private")),
                    "follows_me": pk in followers,
                    "last_dm": last_dm.get(pk, 0) // 1_000_000 or None,  # segundos
                    "followers": old.get(pk, {}).get("followers"),
                    "pic_url": u.get("profile_pic_url"),
                }
            with self.state.lock:
                self.state.d["accounts"] = accounts
                self.state.save()

            # 6. Fotos (CDN público, en paralelo)
            self._step("Descargando fotos de perfil…", 0, len(accounts))
            items = list(accounts.items())
            lock, counter = threading.Lock(), [0]

            def worker(chunk):
                for pk, a in chunk:
                    download_pic(pk, a["pic_url"])
                    with lock:
                        counter[0] += 1
                        self.task["done"] = counter[0]

            ths = [threading.Thread(target=worker, args=(items[i::8],)) for i in range(8)]
            [t.start() for t in ths]
            [t.join() for t in ths]

            # 7. Seguidores solo de quien podría salir (no me sigue o no hablamos)
            self._fill_followers(accounts)

            with self.state.lock:
                self.state.d["analyzed_at"] = datetime.now().isoformat(timespec="minutes")
                self.state.save()
            self._step("Listo", len(accounts), len(accounts))
        except Exception as e:
            self.task["error"] = str(e)
        finally:
            self.task["running"] = False

    def _fill_followers(self, accounts, workers=2):
        """Cuántos seguidores tiene cada cuenta candidata. Primero las que no te siguen."""
        year = time.time() - 365 * 86400
        todo = [pk for pk, a in accounts.items()
                if (not a["follows_me"] or not a["last_dm"] or a["last_dm"] < year)
                and not a["verified"] and a["followers"] is None]
        todo.sort(key=lambda pk: accounts[pk]["follows_me"])
        lock, it, done = threading.Lock(), iter(todo), [0]
        pause = [0.0]  # pausa compartida si Instagram pide calma

        def worker():
            while not self.stop_analysis:
                with lock:
                    pk = next(it, None)
                if pk is None:
                    return
                for attempt in range(6):
                    while pause[0] > time.time():
                        time.sleep(1)
                    try:
                        accounts[pk]["followers"] = self.ig.follower_count(accounts[pk]["username"])
                        break
                    except RateLimited:
                        wait = 60 * (attempt + 1)
                        pause[0] = max(pause[0], time.time() + wait)
                        self._step(f"Instagram pide calma: espero {wait}s…", done[0], len(todo))
                    except Exception:
                        time.sleep(5 * (attempt + 1))
                with lock:
                    done[0] += 1
                    self._step("Mirando quién es influencer…", done[0], len(todo))
                    if done[0] % 25 == 0:
                        with self.state.lock:
                            self.state.save()
                time.sleep(0.7 + random.random() * 0.8)

        ths = [threading.Thread(target=worker) for _ in range(workers)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        with self.state.lock:
            self.state.save()

    # ---------- dejar de seguir ----------
    def enqueue(self, pks):
        self.halt = False
        with self.state.lock:
            q = self.state.d["queue"]
            wl = set(self.state.d["whitelist"])
            for pk in pks:
                if valid_pk(pk) and pk not in q and pk not in wl and pk in self.state.d["accounts"]:
                    q.append(pk)
            self.state.save()
        return len(self.state.d["queue"])

    def clear_queue(self):
        with self.state.lock:
            self.state.d["queue"] = []
            self.state.save()
        return True

    def refollow(self, pk):
        if not valid_pk(pk):
            return False
        r = self.ig.friendship("create", pk)
        fs = (r["json"] or {}).get("friendship_status") or {}
        ok = r["status"] == 200 and (fs.get("following") or fs.get("outgoing_request")) is True
        log(f"create {pk} -> {r['status']} {json.dumps(r['json'])[:200] if r['json'] else r.get('raw')}")
        if ok:
            with self.state.lock:
                for u in self.state.d["unfollowed"]:
                    if u["pk"] == pk:
                        u["refollowed"] = True
                self.state.save()
        return ok

    def _blocked(self, hours, why):
        until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="minutes")
        with self.state.lock:
            self.state.d["block_until"] = until
            self.state.d["warned"] = True
            self.state.d["settings"] = dict(SAFE)
            self.state.save()
        log(f"BLOQUEO {why} -> parado hasta {until}")
        self.worker_note = f"{why}. Parado {hours} h para no arriesgar la cuenta."

    def worker_loop(self):
        while True:
            try:
                self._worker_tick()
            except Exception as e:
                self.worker_note = f"Error: {e}. Reintento en 5 min."
                time.sleep(300)

    def _worker_tick(self):
        s = self.state.d
        if not s["queue"] or not self.ig:
            self.worker_note = ""
            time.sleep(2)
            return
        st = s["settings"]
        if s.get("block_until") and datetime.now().isoformat() < s["block_until"]:
            self.worker_note = (f"Parado por aviso de Instagram hasta el "
                                f"{s['block_until'][8:10]}/{s['block_until'][5:7]} a las {s['block_until'][11:16]}. No insisto.")
            time.sleep(60)
            return
        if self.halt:
            time.sleep(2)
            return
        hour_ago = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        if sum(1 for u in s["unfollowed"] if u["at"] >= hour_ago) >= st["hourly_cap"]:
            self.worker_note = f"Tope de {st['hourly_cap']} por hora. Sigo en unos minutos."
            time.sleep(30)
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if sum(1 for u in s["unfollowed"] if u["at"].startswith(today)) >= s["settings"]["daily_cap"]:
            self.worker_note = f"Tope de {s['settings']['daily_cap']} por hoy. Sigo mañana (deja la app abierta)."
            time.sleep(60)
            return
        if not self.ig.whoami().get("uid"):
            self.worker_note = "Sesión de Instagram cerrada: vuelve a entrar."
            time.sleep(10)
            return

        pk = s["queue"][0]
        a = s["accounts"].get(pk)
        if not a or pk in s["whitelist"] or not valid_pk(pk):
            with self.state.lock:
                s["queue"].pop(0)
                self.state.save()
            return

        self.worker_note = f"Dejando de seguir a @{a['username']}…"
        try:
            r = self.ig.friendship("destroy", pk)
        except RateLimited:
            self._blocked(24, "Instagram ha frenado (429)")
            return
        j = r["json"] or {}
        log(f"destroy {pk} @{a['username']} -> {r['status']} {json.dumps(j)[:300] if j else r.get('raw')}")
        if r["status"] == 200 and (j.get("friendship_status") or {}).get("following") is False:
            with self.state.lock:
                s["queue"].pop(0)
                s["unfollowed"].insert(0, {"pk": pk, "username": a["username"], "full_name": a["full_name"],
                                           "followers": a.get("followers"), "at": datetime.now().isoformat(timespec="seconds")})
                s["accounts"].pop(pk, None)
                self.state.save()
        elif j.get("message") in ("feedback_required", "challenge_required", "checkpoint_required") \
                or j.get("spam") or r["status"] in (400, 403):
            # Aviso real de Instagram: parar 24 h y no reintentar nada hasta entonces.
            self._blocked(24, f"Instagram ha avisado ({j.get('message') or r['status']})")
            return
        else:
            # Respuesta rara (no es un bloqueo): parar la cola y no reintentar a ciegas.
            self.halt = True
            self.worker_note = (f"Respuesta inesperada de Instagram ({r['status']}). Cola parada, "
                                "nadie se ha dejado de seguir. Mira data.log o vuelve a abrir la app.")
            return
        done_now = sum(1 for u in s["unfollowed"] if u["at"].startswith(today))
        if done_now and done_now % st["burst"] == 0 and s["queue"]:
            end = time.time() + st["rest_min"] * 60 * random.uniform(1, 1.3)
            while time.time() < end and s["queue"]:
                self.worker_note = f"Descanso tras {st['burst']} seguidas: {int(end - time.time()) // 60 + 1} min"
                time.sleep(1)
            return
        delay = random.uniform(s["settings"]["min_delay"], s["settings"]["max_delay"])
        end = time.time() + delay
        while time.time() < end and s["queue"]:
            self.worker_note = f"Siguiente en {int(end - time.time())}s"
            time.sleep(1)


def main():
    api = Api()
    panel = webview.create_window("IG Cleaning", str(WEB / "index.html"), js_api=api, text_select=True,
                                  width=1180, height=820, min_size=(820, 600))
    igwin = webview.create_window("Instagram", "https://www.instagram.com/",
                                  width=480, height=780, x=40, y=60)
    api.ig = IG(igwin)
    threading.Thread(target=api.worker_loop, daemon=True).start()
    func = None
    if os.environ.get("IGL_SELFTEST"):
        # Prueba de humo: ¿arranca el panel y habla con Python? Escribe el resultado y sale.
        def func():
            time.sleep(12)
            r = panel.evaluate_js("JSON.stringify({api: !!(window.pywebview && window.pywebview.api),"
                                  "sess: document.querySelector('#sess').textContent,"
                                  "empty: document.querySelector('#empty').textContent.slice(0, 60),"
                                  "cards: document.querySelectorAll('#grid .card').length,"
                                  "imgs: [...document.querySelectorAll('#grid img')].filter(i=>i.naturalWidth>0).length})")
            (DATA / "selftest.json").write_text(r if isinstance(r, str) else json.dumps(r))
            for w in list(webview.windows):
                w.destroy()
    # private_mode=False: la sesión de Instagram se recuerda entre aperturas
    webview.start(func, private_mode=False, storage_path=str(DATA / "webview"))


if __name__ == "__main__":
    main()
