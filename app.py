"""Vigía DonutSMP: mira directos, detecta objetos valiosos con Gemini y avisa por Discord."""
import hashlib, io, json, os, subprocess, threading, time
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")  # Flash (no Lite): se inventa mucho menos
WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
PASSWORD = os.getenv("ADMIN_PASSWORD", "")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "250"))    # consultas a Gemini por día (Flash gratis da menos que Lite)
PERIODIC = int(os.getenv("PERIODIC_CHECK", "120"))    # revisa la pantalla aunque no abra inventario
MIN_CONF = float(os.getenv("MIN_CONFIDENCE", "0.8"))  # seguridad mínima de la IA para contar un objeto
INV_COOLDOWN = 15                                     # mín. segundos entre consultas con inventario abierto
ALERT_COOLDOWN = 600                                  # no repetir el mismo aviso en 10 min
FRAME_EVERY = 3                                       # una imagen cada 3 s
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

PROMPT = """Eres experto en Minecraft. Esta imagen es un recorte del directo de un jugador de DonutSMP: {where}.
Busca SOLO estos objetos: {items}.
Cuidado: muchos jugadores usan texture packs y mods de HUD. Los iconos pequeños a la derecha de la barra rápida
son el indicador de armadura puesta: una pechera NO es una Elytra. Ignora el chat, la webcam, carteles, bloques del mundo
y objetos de otros jugadores. Si un icono no es inconfundiblemente uno de la lista, NO lo pongas.
Lo normal es que la lista salga vacía.
Copia "name" EXACTAMENTE como aparece en la lista.
Responde solo JSON: {{"items":[{{"name":"...","count":numero_o_null,"confidence":0.0_a_1.0,"seen":"qué icono ves y dónde"}}]}}"""


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


def to_jpg(frame):
    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def analyze(img, where):
    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Part.from_bytes(data=to_jpg(img), mime_type="image/jpeg"),
                  PROMPT.format(where=where, items=", ".join(items))],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
    allowed = {i.lower(): i for i in items}
    out = []
    for i in json.loads(r.text).get("items", []):
        name = allowed.get(str(i.get("name", "")).strip().lower())   # fuera lo que no esté en tu lista
        if name and float(i.get("confidence") or 0) >= MIN_CONF:
            out.append({**i, "name": name})
    return out


def send_discord(st, found, jpg, reason):
    lines = "\n".join(f"• **{i['name']}**" + (f" x{i['count']}" if i.get("count") else "")
                      + (f" — {i['seen']}" if i.get("seen") else "") for i in found)
    payload = {"username": "Vigía DonutSMP", "embeds": [{
        "title": f"🚨 {st['name']} lleva algo valioso", "url": st["url"], "description": lines,
        "color": 0xE74C3C, "image": {"url": "attachment://captura.jpg"},
        "footer": {"text": f"Detectado por: {reason} (confirmado en 2 fotogramas)"}}]}
    if WEBHOOK:
        requests.post(WEBHOOK, data={"payload_json": json.dumps(payload)},
                      files={"files[0]": ("captura.jpg", jpg, "image/jpeg")}, timeout=20)


def handle_frame(sid, frame):
    st = streamers.get(sid)
    if not st:
        return
    jpg = to_jpg(frame)
    st["frame"] = jpg
    now, small, inv = time.time(), thumb(frame), inventory_open(frame)
    changed = st["sent"] is None or np.abs(small - st["sent"]).mean() > 6
    pend = st["pending"]
    if pend:                                   # algo visto en el fotograma anterior → confirmar con este
        reason, inv = pend["reason"], pend["inv"]
    elif inv and changed and now - st["last_ai"] >= INV_COOLDOWN:
        reason = "inventario abierto"
    elif now - st["last_ai"] >= PERIODIC:
        reason = "revisión periódica"
    else:
        return
    if not client:
        st["status"] = "falta GEMINI_API_KEY"
        return
    if not take_budget():
        st["status"] = "límite diario de IA alcanzado"
        return
    st["last_ai"], st["sent"], st["pending"] = now, small, None
    try:
        found = analyze(*crop(frame, inv))
    except Exception as e:  # cuota, red, JSON raro: se registra y seguimos
        st["status"] = f"error IA: {str(e)[:80]}"
        return
    st["status"] = "en directo"
    if not pend:
        if found:
            st["pending"] = {"reason": reason, "inv": inv, "names": {i["name"] for i in found}}
        else:
            st["last_items"] = []
        return
    found = [i for i in found if i["name"] in pend["names"]]   # solo lo que salió las dos veces
    st["last_items"] = found
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
                          "last_key": "", "last_alert": 0, "pending": None}
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
        "items": items, "usage": usage["count"], "limit": DAILY_LIMIT,
        "ready": {"gemini": bool(client), "discord": bool(WEBHOOK), "password": bool(PASSWORD)},
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
