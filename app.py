"""Vigía DonutSMP: mira directos, detecta objetos valiosos con Gemini y avisa por Discord."""
import base64, hashlib, io, json, os, re, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from datetime import datetime

import numpy as np
import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from google import genai
from google.genai import types
from PIL import Image

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# IAs gratis (sin tarjeta) de 3 proveedores. Formato "proveedor:modelo". Las que no tengan clave se ignoran.
KEYS = {"gemini": GEMINI_KEY, "groq": os.getenv("GROQ_API_KEY", ""), "github": os.getenv("GITHUB_TOKEN", ""),
        "openrouter": os.getenv("OPENROUTER_API_KEY", "")}
BASES = {"groq": "https://api.groq.com/openai/v1", "github": "https://models.github.ai/inference",
         "openrouter": "https://openrouter.ai/api/v1"}
csv = lambda name, default: [m.strip() for m in os.getenv(name, default).split(",") if m.strip()]
# 1º mira una IA rápida y con mucho cupo. Solo si ve algo valioso, las mejores IAs lo confirman.
SCREENERS = csv("SCREENERS", "groq:qwen/qwen3.8-27b,gemini:gemini-flash-lite-latest,github:openai/gpt-4.1-mini")
JUDGES = csv("JUDGES", "github:openai/gpt-4.1,gemini:gemini-flash-latest,github:openai/gpt-4.1-mini,"
                       "groq:qwen/qwen3.8-27b,gemini:gemini-flash-lite-latest")
VOTES = int(os.getenv("VOTES", "2"))                  # cuántas IAs tienen que estar de acuerdo (contando la 1ª)
JUDGES_PER_CHECK = int(os.getenv("JUDGES_PER_CHECK", "2"))  # cuántas IAs buenas confirman cada hallazgo
WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
PASSWORD = os.getenv("ADMIN_PASSWORD", "")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "900"))    # revisiones por día
PERIODIC = int(os.getenv("PERIODIC_CHECK", "120"))    # revisa la pantalla aunque no abra inventario
MIN_CONF = float(os.getenv("MIN_CONFIDENCE", "0.8"))  # seguridad mínima de la IA para contar un objeto
INV_COOLDOWN = 8                                      # mín. segundos entre consultas con inventario abierto
BAR_DIFF = float(os.getenv("BAR_DIFF", "25"))        # sensibilidad del detector de cambios en la barra
BAR_COOLDOWN = 8                                      # mín. segundos entre consultas por cambio en la barra
ALERT_COOLDOWN = 600                                  # no repetir el mismo aviso en 10 min
FRAME_EVERY = 2                                       # una imagen cada 2 s
W, H = 1280, 720

lock = threading.Lock()
streamers = {}                     # id -> dict
alerts = deque(maxlen=40)
items = [s.strip() for s in os.getenv("ITEMS",
    "Elytra, Armadura de Netherite, Espada de Netherite, Pico de Netherite, Hacha de Netherite, "
    "Lingote de Netherite, Bloque de Netherite, Tótem de la inmortalidad, Spawner, Shulker box, "
    "Manzana de oro encantada, Beacon").split(",") if s.strip()]
usage = {"day": "", "count": 0}
client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
last_call = [0.0]
dead = set()                       # modelos que no existen en tu cuenta (404): no se vuelven a llamar
cooling = {}                       # modelo -> hasta cuándo descansa porque se quedó sin cupo (429)
pool = ThreadPoolExecutor(8)

VERSION = "v9"
debug = deque(maxlen=20)           # últimas consultas a la IA: recorte + respuesta, para ver por qué falla

PROMPT = """Eres experto en Minecraft. Esta imagen es un recorte ampliado del directo de un jugador de DonutSMP: {where}.

PASO 1 - Describe TODAS las casillas que veas, una por una, de izquierda a derecha y de arriba abajo:
qué objeto hay (o "vacía") y el número si lo tiene. Describe lo que VES (color, forma), no lo que te gustaría ver.
PASO 2 - Solo con esas casillas, di cuáles son EXACTAMENTE uno de estos objetos: {items}.

Confusiones típicas, NO las cometas:
- Obsidiana (negra/morada) NO es Bloque de Netherite (gris oscuro marrón con rayas).
- Herramientas/armadura de diamante (azul claro) NO son de Netherite (gris oscuro casi negro).
- La Elytra son DOS ALAS grises/moradas plegadas, con forma de V o de capa. Si sale en la barra, en el inventario
  o en el indicador de armadura (iconos a la derecha de la barra, en el hueco del pecho), SÍ cuenta: la lleva puesta.
  Una pechera normal (con mangas, color diamante/hierro/netherite) NO es una Elytra.
- Un objeto con brillo morado NO es Manzana de oro encantada salvo que sea una manzana dorada brillante.
- Ignora chat, webcam, carteles, marcos y bloques del mundo.
Si no estás seguro, NO lo pongas.
Copia "name" EXACTAMENTE como aparece en la lista.

Responde solo JSON:
{{"slots":["casilla 1: ...","casilla 2: ..."],"items":[{{"name":"...","count":numero_o_null,"confidence":0.0_a_1.0,"seen":"casilla N: qué ves"}}]}}"""


def crop(frame, inv):
    """Solo la zona que importa, a resolución completa: así los iconos no quedan diminutos."""
    h, w, _ = frame.shape
    if inv:
        return frame[int(h * .05):int(h * .95), int(w * .2):int(w * .8)], "su inventario o cofre abierto"
    strip = frame[int(h * .75):, int(w * .2):int(w * .8)]
    big = Image.fromarray(strip).resize((strip.shape[1] * 2, strip.shape[0] * 2), Image.NEAREST)
    return np.asarray(big), "su barra rápida (abajo) y el indicador de armadura"


def take_budget():
    with lock:
        day = datetime.now().strftime("%Y-%m-%d")
        if usage["day"] != day:
            usage.update(day=day, count=0)
        if usage["count"] >= DAILY_LIMIT:
            return False
        usage["count"] += 1
        # ponytail: espera global de 4 s entre llamadas para no pasar ~15/min del plan gratis
        wait = last_call[0] + 4 - time.time()
        last_call[0] = max(time.time(), last_call[0] + 4)
    if wait > 0:
        time.sleep(wait)
    return True


def inventory_open(frame):
    """El panel del inventario/cofre de Minecraft es gris claro (198,198,198) en el centro.
    ponytail: heurística de color; falla con texture packs que cambian la GUI → bajaría a revisión periódica."""
    h, w, _ = frame.shape
    c = frame[int(h * .2):int(h * .8), int(w * .3):int(w * .7)].astype(np.int16)
    gray = (np.abs(c - 198) < 14).all(axis=2)
    return gray.mean() > 0.12


def thumb(frame):
    return np.asarray(Image.fromarray(frame).convert("L").resize((64, 36)), dtype=np.int16)


def bar_thumb(frame):
    """Solo las casillas centrales de la barra rápida: si cambian, el streamer ha cogido/cambiado algo."""
    h, w, _ = frame.shape
    bar = frame[int(h * .95):, int(w * .38):int(w * .62)]
    return np.asarray(Image.fromarray(bar).convert("L").resize((72, 6)), dtype=np.int16)


def bar_changed(a, b):
    """Diferencia por trozos de la barra: basta con que UNA casilla cambie mucho (cogió otro objeto)."""
    # ponytail: 24 trozos fijos (~1 casilla = 3 trozos); con GUI scale muy grande o pequeña las casillas no caen justas, pero sigue detectando
    cols = np.abs(a - b).reshape(6, 24, 3).mean(axis=(0, 2))
    return cols.max() > BAR_DIFF


def to_jpg(frame):
    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def busy(e):
    return any(k in str(e) for k in ("503", "UNAVAILABLE", "overloaded"))


def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)       # Gemma a veces mete ```json ... ``` alrededor
    return json.loads(m.group(0) if m else "{}")


def ask(spec, jpg, prompt):
    """Una IA ("proveedor:modelo"). Devuelve su JSON o lanza el error. Reintenta una vez si está saturada."""
    prov, model = spec.split(":", 1)
    for n in range(2):
        try:
            if prov == "gemini":
                cfg = types.GenerateContentConfig(temperature=0, **(
                    {"response_mime_type": "application/json"} if model.startswith("gemini") else {}))
                r = client.models.generate_content(
                    model=model, contents=[types.Part.from_bytes(data=jpg, mime_type="image/jpeg"), prompt], config=cfg)
                return parse(r.text)
            img = "data:image/jpeg;base64," + base64.b64encode(jpg).decode()
            r = requests.post(BASES[prov] + "/chat/completions", timeout=40,
                              headers={"Authorization": f"Bearer {KEYS[prov]}"},
                              json={"model": model, "temperature": 0, "max_tokens": 1500, "messages": [{"role": "user", "content": [
                                  {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": img}}]}]})
            if r.status_code >= 400:
                raise Exception(f"{r.status_code} {r.text[:200]}")
            text = r.json()["choices"][0]["message"]["content"] or ""
            return parse(re.sub(r"<think>.*?</think>", "", text, flags=re.S))   # Qwen piensa en voz alta
        except Exception as e:
            msg = str(e)
            if msg.startswith(("404", "400")) and ("model" in msg.lower() or "not found" in msg.lower()):
                dead.add(spec)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                cooling[spec] = time.time() + 15 * 60          # sin cupo: descansa 15 min y deja paso a otra
            if not busy(e) or n == 1:
                raise
            time.sleep(2)


def available(specs, skip=()):
    now = time.time()
    return [m for m in specs if m not in dead and m not in skip and KEYS.get(m.split(":", 1)[0])
            and cooling.get(m, 0) < now]


def vote(spec, data, allowed):
    mine = {}
    for i in data.get("items", []):
        name = allowed.get(str(i.get("name", "")).strip().lower())   # fuera lo que no esté en tu lista
        conf = float(i.get("confidence") or 0)
        if name and conf >= MIN_CONF and conf > mine.get(name, {}).get("confidence", -1):
            mine[name] = {**i, "name": name, "confidence": conf}
    return mine


def analyze(img, where):
    jpg, prompt = to_jpg(img), PROMPT.format(where=where, items=", ".join(items))
    allowed = {i.lower(): i for i in items}
    report, slots, results = [], [], {}
    # Paso 1: una IA rápida mira. Si una se queda sin cupo, prueba la siguiente.
    for spec in available(SCREENERS):
        try:
            data = ask(spec, jpg, prompt)
        except Exception as e:
            report.append({"model": spec, "error": str(e)[:90]})
            continue
        slots, results[spec] = data.get("slots", []), vote(spec, data, allowed)
        report.append({"model": spec, "items": list(results[spec])})
        break
    if not results:      # todas fallaron: se apunta igual en "Lo que ve la IA" para ver por qué
        debug.appendleft({"id": hashlib.md5(f"{time.time()}".encode()).hexdigest()[:10], "time": time.time(),
                          "where": where, "slots": [], "votes": report or [{"model": "—", "error":
                          "ninguna IA disponible: faltan claves o todas están sin cupo"}], "kept": [], "jpg": jpg})
        raise Exception(report[-1]["error"] if report else "429 no hay ninguna IA disponible (revisa las claves)")
    screener = next(iter(results))
    # Paso 2: solo si vio algo, las mejores IAs disponibles lo confirman a la vez
    if results[screener]:
        judges = available(JUDGES, skip={screener})[:JUDGES_PER_CHECK]
        futures = {m: pool.submit(ask, m, jpg, prompt) for m in judges}
        for m, f in futures.items():
            try:
                data = f.result(timeout=45)
            except Exception as e:
                report.append({"model": m, "error": str(e)[:90]})
                continue
            results[m] = vote(m, data, allowed)
            report.append({"model": m, "items": list(results[m])})
    votes, best = {}, {}
    for mine in results.values():
        for name, info in mine.items():
            votes[name] = votes.get(name, 0) + 1
            if info["confidence"] > best.get(name, {}).get("confidence", -1):
                best[name] = info
    # ponytail: si no queda ninguna IA para confirmar, decide la primera sola
    need = min(VOTES, len(results))
    out = [{**best[n], "votes": f"{v}/{len(results)}"} for n, v in votes.items() if v >= need]
    debug.appendleft({"id": hashlib.md5(f"{time.time()}".encode()).hexdigest()[:10], "time": time.time(),
                      "where": where, "slots": slots, "votes": report,
                      "kept": [i["name"] for i in out], "jpg": jpg})
    return out


def send_discord(st, found, jpg, reason):
    lines = "\n".join(f"• **{i['name']}**" + (f" x{i['count']}" if i.get("count") else "")
                      + (f" — {i['seen']}" if i.get("seen") else "") for i in found)
    payload = {"username": "Vigía DonutSMP", "embeds": [{
        "title": f"🚨 {st['name']} lleva algo valioso", "url": st["url"], "description": lines,
        "color": 0xE74C3C, "image": {"url": "attachment://captura.jpg"},
        "footer": {"text": f"Detectado por: {reason} · IAs de acuerdo: {found[0].get('votes', '?')}"}}]}
    if WEBHOOK:
        requests.post(WEBHOOK, data={"payload_json": json.dumps(payload)},
                      files={"files[0]": ("captura.jpg", jpg, "image/jpeg")}, timeout=20)


def handle_frame(sid, frame):
    st = streamers.get(sid)
    if not st:
        return
    jpg = to_jpg(frame)
    st["frame"] = jpg
    now, small, bar, inv = time.time(), thumb(frame), bar_thumb(frame), inventory_open(frame)
    since = now - st["last_ai"]
    if inv and (st["sent"] is None or np.abs(small - st["sent"]).mean() > 6) and since >= INV_COOLDOWN:
        reason = "inventario abierto"
    elif not inv and st["bar"] is not None and bar_changed(bar, st["bar"]) and since >= BAR_COOLDOWN:
        reason = "cambio en la barra"
    elif since >= PERIODIC:
        reason = "revisión periódica"
    else:
        return
    if not any(KEYS.values()):
        st["status"] = "falta una clave de IA"
        return
    if not take_budget():
        st["status"] = "límite diario de IA alcanzado"
        return
    st["last_ai"], st["sent"], st["bar"] = now, small, bar
    try:
        found = analyze(*crop(frame, inv))
    except Exception as e:  # todas las IAs fallaron: se registra y seguimos
        st["status"] = "IAs saturadas, reintento en breve" if busy(e) else \
            "límite gratis de IA agotado" if "429" in str(e) else f"error IA: {str(e)[:60]}"
        st["last_ai"] = now - PERIODIC + 30      # vuelve a intentarlo en ~30 s
        return
    st["status"], st["last_items"] = "en directo", found
    key = ",".join(sorted(i["name"] for i in found))
    if not found or (key == st["last_key"] and now - st["last_alert"] < ALERT_COOLDOWN):
        return
    st["last_key"], st["last_alert"] = key, now
    aid = hashlib.md5(f"{sid}{now}".encode()).hexdigest()[:10]
    alerts.appendleft({"id": aid, "streamer": st["name"], "url": st["url"], "time": now,
                       "items": found, "reason": reason, "jpg": jpg})
    try:
        send_discord(st, found, jpg, reason)
    except Exception as e:
        st["status"] = f"error Discord: {str(e)[:80]}"


def is_live(url):
    try:
        r = subprocess.run(["streamlink", "--json", url], capture_output=True, text=True, timeout=60)
        return "error" not in json.loads(r.stdout or "{}") and bool(r.stdout)
    except Exception:
        return False


def watch(sid):
    while sid in streamers:
        st = streamers[sid]
        st["status"] = "comprobando..."
        if not is_live(st["url"]):
            st["status"], st["frame"] = "offline", None
            for _ in range(60):          # vuelve a mirar en 2 min
                if sid not in streamers:
                    return
                time.sleep(2)
            continue
        st["status"] = "en directo"
        sl = subprocess.Popen(["streamlink", "--stdout", st["url"],
                               "720p,720p60,480p,best"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # -skip_frame nokey: solo decodifica fotogramas clave (cada ~2 s) → cabe en la CPU gratis de Render
        ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-skip_frame", "nokey", "-i", "pipe:0", "-vf",
                               f"fps=1/{FRAME_EVERY},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24",
                               "pipe:1"], stdin=sl.stdout, stdout=subprocess.PIPE)
        sl.stdout.close()
        try:
            while sid in streamers:
                buf = ff.stdout.read(W * H * 3)
                if len(buf) < W * H * 3:
                    break
                handle_frame(sid, np.frombuffer(buf, np.uint8).reshape(H, W, 3))
        finally:
            ff.kill(); sl.kill()
        time.sleep(10)


def add_streamer(url):
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://www.twitch.tv/" + url      # si ponen solo el nombre, se asume Twitch
    sid = hashlib.md5(url.lower().encode()).hexdigest()[:8]
    with lock:
        if sid in streamers:
            return sid
        streamers[sid] = {"url": url, "name": url.split("/")[-1].lstrip("@"), "status": "iniciando",
                          "frame": None, "sent": None, "last_ai": 0, "last_items": [],
                          "last_key": "", "last_alert": 0, "bar": None}
    threading.Thread(target=watch, args=(sid,), daemon=True).start()
    return sid


def check_pw(pw):
    if not PASSWORD or pw != PASSWORD:
        raise HTTPException(401, "Contraseña incorrecta (o falta ADMIN_PASSWORD en el Space)")


app = FastAPI()
for u in os.getenv("STREAMERS", "").split(","):
    if u.strip():
        add_streamer(u)


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.get("/api/state")
def state():
    return {
        "streamers": [{"id": k, "url": v["url"], "name": v["name"], "status": v["status"],
                       "has_frame": v["frame"] is not None, "last_items": v["last_items"]}
                      for k, v in list(streamers.items())],
        "alerts": [{k: v for k, v in a.items() if k != "jpg"} for a in alerts],
        "items": items, "usage": usage["count"], "limit": DAILY_LIMIT, "version": VERSION,
        "models": [{"name": m, "state": "dead" if m in dead else "cool" if cooling.get(m, 0) > time.time() else "ok"}
                   for m in dict.fromkeys(SCREENERS + JUDGES) if KEYS.get(m.split(":", 1)[0])], "need": VOTES,
        "debug": [{k: v for k, v in d.items() if k != "jpg"} for d in debug],
        "ready": {"ia": any(KEYS.values()), "discord": bool(WEBHOOK), "password": bool(PASSWORD)},
    }


@app.get("/api/frame/{sid}")
def frame(sid: str):
    st = streamers.get(sid)
    if not st or not st["frame"]:
        raise HTTPException(404)
    return Response(st["frame"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/alert/{aid}")
def alert_img(aid: str):
    for a in alerts:
        if a["id"] == aid:
            return Response(a["jpg"], media_type="image/jpeg")
    raise HTTPException(404)


@app.get("/api/debug/{did}")
def debug_img(did: str):
    for d in debug:
        if d["id"] == did:
            return Response(d["jpg"], media_type="image/jpeg")
    raise HTTPException(404)


@app.post("/api/streamers")
def post_streamer(body: dict, x_password: str = Header("")):
    check_pw(x_password)
    if not str(body.get("url", "")).strip():
        raise HTTPException(400, "Falta la URL")
    return {"id": add_streamer(str(body["url"]))}


@app.delete("/api/streamers/{sid}")
def del_streamer(sid: str, x_password: str = Header("")):
    check_pw(x_password)
    streamers.pop(sid, None)
    return {"ok": True}


@app.put("/api/items")
def put_items(body: dict, x_password: str = Header("")):
    check_pw(x_password)
    new = [str(i).strip()[:80] for i in body.get("items", []) if str(i).strip()][:40]
    if not new:
        raise HTTPException(400, "La lista no puede estar vacía")
    items[:] = new
    return {"items": items}
