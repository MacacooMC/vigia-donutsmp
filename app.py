"""Vigía DonutSMP: mira directos, detecta objetos valiosos con Gemini y avisa por Discord."""
import base64, hashlib, io, json, os, re, subprocess, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from datetime import datetime

import numpy as np
import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

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
MIN_CONF = float(os.getenv("MIN_CONFIDENCE", "0.7"))  # seguridad mínima para que una IA cuente como voto
SCREEN_CONF = float(os.getenv("SCREEN_CONFIDENCE", "0.35"))  # con esto la IA rápida ya pide confirmación
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
    "Lingote de Netherite, Bloque de Netherite, Spawner").split(",") if s.strip()]
usage = {"day": "", "count": 0}
client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
last_call = [0.0]
dead = set()                       # modelos que no existen en tu cuenta (404): no se vuelven a llamar
cooling = {}                       # modelo -> hasta cuándo descansa porque se quedó sin cupo (429)
pool = ThreadPoolExecutor(8)

VERSION = "v11"
debug = deque(maxlen=20)           # últimas consultas a la IA: recorte + respuesta, para ver por qué falla

PROMPT = """Eres experto en Minecraft. Te paso UNA imagen con paneles del directo de un jugador de DonutSMP:
[REFERENCIA] = las texturas REALES de los objetos que busco (verde, numeradas) y de objetos PARECIDOS que NO cuentan (rojo).
[ZOOM] = zoom de {where}. [PANTALLA ENTERA] = el directo completo.

Objetos que busco (usa el número):
{items}

Método obligatorio:
1. Describe cada casilla de la barra/inventario del ZOOM: forma y COLOR exactos de cada icono.
2. Compara cada icono con la REFERENCIA. Solo es un objeto de la lista si se parece MUCHO a su icono verde
   (misma forma Y mismo color) y NO se parece más a uno rojo.
   - Netherite es gris muy oscuro con toques morados. Si es azul/cian es DIAMANTE (rojo, no cuenta).
   - Bloque de Netherite es gris oscuro liso. Negro con motas moradas es OBSIDIANA (no cuenta).
   - Elytra son dos alas gris claro. Una pechera con mangas NO es Elytra.
3. Solo cuentan objetos del jugador: barra, mano, inventario/cofre abierto, armadura puesta o en el HUD.
   No cuentan otros jugadores, marcos, tiendas, subastas, chat, webcam ni el mundo.
Pon confidence alta (0.8-1) solo si coincide claramente con la referencia; 0.3-0.6 si te parece pero dudas.

Responde solo JSON:
{{"slots":["casilla 1: ...","..."],"items":[{{"id":numero,"count":numero_o_null,"confidence":0.0_a_1.0,"seen":"dónde y por qué coincide con la referencia"}}]}}"""

# Nombres alternativos que puede usar la IA (en minúsculas y sin tildes)
ALIASES = {"elytra": ["elitros", "elitro", "elytras", "alas"], "totem de la inmortalidad": ["totem", "totem of undying"],
           "shulker box": ["caja de shulker", "shulker"], "spawner": ["generador", "generador de monstruos", "mob spawner"],
           "beacon": ["faro"], "manzana de oro encantada": ["manzana de notch", "enchanted golden apple", "manzana encantada"]}


def norm(t):
    return unicodedata.normalize("NFD", str(t)).encode("ascii", "ignore").decode().lower().strip()


def match(i):
    """Qué objeto de tu lista es: por número, por nombre o por nombre alternativo (Élitros = Elytra)."""
    try:
        n = int(i.get("id"))
        if 1 <= n <= len(items):
            return items[n - 1]
    except (TypeError, ValueError):
        pass
    name = norm(i.get("name", ""))
    for it in items:
        if name and (name == norm(it) or name in ALIASES.get(norm(it), [])):
            return it
    return None


# ---------- Referencia visual: texturas reales de Minecraft que la IA usa para comparar ----------
TEX = "https://raw.githubusercontent.com/InventivetalentDev/minecraft-assets/1.21.4/assets/minecraft/textures/"
# objeto de tu lista (sin tildes, minúsculas) -> texturas. "b:" = bloque (se dibuja en 3D como en el inventario)
REF_ITEMS = {"elytra": ["elytra"], "armadura de netherite": ["netherite_helmet", "netherite_chestplate",
             "netherite_leggings", "netherite_boots"], "espada de netherite": ["netherite_sword"],
             "pico de netherite": ["netherite_pickaxe"], "hacha de netherite": ["netherite_axe"],
             "lingote de netherite": ["netherite_ingot"], "bloque de netherite": ["b:netherite_block"],
             "spawner": ["b:spawner"]}
DECOYS = [("Pico de diamante", "diamond_pickaxe"), ("Espada de diamante", "diamond_sword"), ("Hacha de diamante", "diamond_axe"),
          ("Pechera de diamante", "diamond_chestplate"), ("Pechera de hierro", "iron_chestplate"),
          ("Membrana de phantom", "phantom_membrane"), ("Chatarra de netherite", "netherite_scrap"),
          ("Obsidiana", "b:obsidian"), ("Obsidiana llorona", "b:crying_obsidian")]
_tex = {}


def texture(name):
    """Descarga (una vez) la textura de 16x16 y la devuelve en RGBA; los bloques se dibujan en 3D isométrico."""
    if name not in _tex:
        block = name.startswith("b:")
        url = TEX + ("block/" + name[2:] if block else "item/" + name) + ".png"
        try:
            im = Image.open(io.BytesIO(requests.get(url, timeout=10).content)).convert("RGBA").crop((0, 0, 16, 16))
            _tex[name] = iso(im) if block else im.resize((48, 48), Image.NEAREST)
        except Exception:
            _tex[name] = None
    return _tex[name]


def iso(face, n=48):
    """Cubo isométrico como los bloques en el inventario (arriba claro, izquierda media, derecha oscura)."""
    src, out = np.asarray(face).astype(np.float32), np.zeros((n, n, 4), np.float32)
    faces = [((0, n / 4), (n / 2, -n / 4), (n / 2, n / 4), 1.0), ((0, n / 4), (n / 2, n / 4), (0, n / 2), .8),
             ((n / 2, n / 2), (n / 2, -n / 4), (0, n / 2), .6)]
    ys, xs = np.mgrid[0:n, 0:n] + .5
    for (ox, oy), (ux, uy), (vx, vy), shade in faces:
        det = ux * vy - uy * vx
        u = ((xs - ox) * vy - (ys - oy) * vx) / det
        v = ((ys - oy) * ux - (xs - ox) * uy) / det
        inside = (u >= 0) & (u < 1) & (v >= 0) & (v < 1)
        px = src[(v[inside] * 16).astype(int).clip(0, 15), (u[inside] * 16).astype(int).clip(0, 15)]
        out[inside] = np.concatenate([px[:, :3] * shade, px[:, 3:]], axis=1)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def ref_cell(label, icons, color):
    """Una casilla de la hoja: los iconos sobre fondo oscuro (barra) y claro (inventario) + el nombre."""
    icons = [i for i in icons if i is not None]
    w = max(166, 58 * len(icons) + 10)
    cell = Image.new("RGB", (w, 140), (30, 30, 36))
    for n, ic in enumerate(icons):
        for row, bg in ((0, (60, 60, 64)), (1, (139, 139, 139))):
            box = Image.new("RGBA", (54, 54), bg + (255,))
            box.alpha_composite(ic, (3, 3))
            cell.paste(box.convert("RGB"), (6 + n * 58, 6 + row * 58))
    ImageDraw.Draw(cell).text((6, 120), label, fill=color, font=font(14))
    return cell


def reference():
    """Hoja con TUS objetos (numerados) y los que se les parecen pero NO son. Se rehace si cambias la lista."""
    key = tuple(items)
    if getattr(reference, "key", None) == key:
        return reference.img
    good = [ref_cell(f"{n}. {it}", [texture(t) for t in REF_ITEMS.get(norm(it), [])], (120, 255, 160))
            for n, it in enumerate(items, 1) if REF_ITEMS.get(norm(it))]
    bad = [ref_cell(f"NO: {name}", [texture(t)], (255, 120, 140)) for name, t in DECOYS]
    rows = []
    for title, cells, color in (("LO QUE BUSCO (texturas reales):", good, (120, 255, 160)),
                                ("PARECIDOS QUE NO CUENTAN:", bad, (255, 120, 140))):
        x, y, line = 0, 30, Image.new("RGB", (1280, 30 + 150 * 2), (20, 20, 26))
        ImageDraw.Draw(line).text((8, 6), title, fill=color, font=font(18))
        for c in cells:
            if x + c.width > 1280:
                x, y = 0, y + 150
            line.paste(c, (x, y)); x += c.width + 6
        rows.append(line.crop((0, 0, 1280, y + 150)))
    sheet = Image.new("RGB", (1280, sum(r.height for r in rows)), (20, 20, 26))
    y = 0
    for r in rows:
        sheet.paste(r, (0, y)); y += r.height
    reference.key, reference.img = key, (sheet if good else None)
    return reference.img


def composite(ref, zoom, full):
    """Una sola imagen con 3 paneles (las IAs gratis cobran por imagen): referencia, zoom y pantalla."""
    panels = [p for p in (ref, Image.fromarray(zoom), Image.fromarray(full).resize((960, 540))) if p is not None]
    titles = (["REFERENCIA"] if ref is not None else []) + ["ZOOM", "PANTALLA ENTERA"]
    scaled = [p.resize((1280, round(p.height * 1280 / p.width))) if p.width > 1280 else p for p in panels]
    out = Image.new("RGB", (1280, sum(p.height + 28 for p in scaled)), (10, 10, 14))
    y = 0
    for t, p in zip(titles, scaled):
        ImageDraw.Draw(out).text((8, y + 5), f"[{t}]", fill=(255, 210, 90), font=font(18))
        out.paste(p, (0, y + 28)); y += p.height + 28
    return out


def crop(frame, inv):
    """Pantalla entera (para ver la Elytra puesta, la mano, HUDs raros) + zoom de la zona importante."""
    h, w, _ = frame.shape
    full = np.asarray(Image.fromarray(frame).resize((960, 540)))
    if inv:
        return [full, frame[int(h * .05):int(h * .95), int(w * .2):int(w * .8)]], "su inventario o cofre abierto"
    strip = frame[int(h * .75):, int(w * .2):int(w * .8)]
    big = Image.fromarray(strip).resize((strip.shape[1] * 2, strip.shape[0] * 2), Image.NEAREST)
    return [full, np.asarray(big)], "su barra rápida (abajo) y el indicador de armadura"


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


def ask(spec, jpgs, prompt):
    """Una IA ("proveedor:modelo"). Devuelve su JSON o lanza el error. Reintenta una vez si está saturada."""
    prov, model = spec.split(":", 1)
    for n in range(2):
        try:
            if prov == "gemini":
                cfg = types.GenerateContentConfig(temperature=0, **(
                    {"response_mime_type": "application/json"} if model.startswith("gemini") else {}))
                parts = [types.Part.from_bytes(data=j, mime_type="image/jpeg") for j in jpgs]
                r = client.models.generate_content(model=model, contents=parts + [prompt], config=cfg)
                return parse(r.text)
            imgs = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(j).decode()}}
                    for j in jpgs]
            body = {"model": model, "temperature": 0, "max_tokens": 1500, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt}] + imgs}]}
            headers = {"Authorization": f"Bearer {KEYS[prov]}", "Content-Type": "application/json",
                       **({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"} if prov == "github" else {})}
            url = BASES[prov] + "/chat/completions"
            for _ in range(3):           # requests convierte el POST en GET al redirigir: lo seguimos a mano
                r = requests.post(url, timeout=45, headers=headers, json=body, allow_redirects=False)
                if r.status_code not in (301, 302, 307, 308):
                    break
                url = r.headers["Location"]
            if r.status_code == 429:
                wait = r.headers.get("retry-after")
                cooling[spec] = time.time() + (float(wait) if wait and wait.replace(".", "").isdigit() else 90)
            if r.status_code >= 400:
                raise Exception(f"{r.status_code} {r.text[:160]}")
            try:
                text = r.json()["choices"][0]["message"]["content"] or ""
            except Exception:
                raise Exception(f"respuesta rara ({r.status_code}, {r.headers.get('content-type')}): {r.text[:120]!r}")
            return parse(re.sub(r"<think>.*?</think>", "", text, flags=re.S))   # Qwen piensa en voz alta
        except Exception as e:
            msg = str(e)
            if msg.startswith(("404", "400")) and ("model" in msg.lower() or "not found" in msg.lower()):
                dead.add(spec)
            if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and cooling.get(spec, 0) < time.time():
                daily = any(k in msg.lower() for k in ("per day", "daily", "quota", "rpd"))
                cooling[spec] = time.time() + (3600 if daily else 90)   # sin cupo: descansa y deja paso a otra
            if not busy(e) or n == 1:
                raise
            time.sleep(2)


def available(specs, skip=()):
    now = time.time()
    return [m for m in specs if m not in dead and m not in skip and KEYS.get(m.split(":", 1)[0])
            and cooling.get(m, 0) < now]


def vote(data, min_conf):
    mine = {}
    for i in data.get("items", []):
        name, conf = match(i), float(i.get("confidence") or 0)
        if name and conf >= min_conf and conf > mine.get(name, {}).get("confidence", -1):
            mine[name] = {**i, "name": name, "confidence": conf}
    return mine


def analyze(frame, inv):
    (full, zoom), where = crop(frame, inv)
    jpgs = [to_jpg(np.asarray(composite(reference(), zoom, full)))]
    prompt = PROMPT.format(where=where, items="\n".join(f"{n}. {it}" for n, it in enumerate(items, 1)))
    report, slots, results = [], [], {}
    # Paso 1: una IA rápida mira. Es generosa (umbral bajo): mejor que pregunte de más a que se le escape.
    for spec in available(SCREENERS):
        try:
            data = ask(spec, jpgs, prompt)
        except Exception as e:
            report.append({"model": spec, "error": str(e)[:90]})
            continue
        slots, results[spec] = data.get("slots", []), vote(data, SCREEN_CONF)
        report.append({"model": spec, "items": list(results[spec])})
        break
    if not results:      # todas fallaron: se apunta igual en "Lo que ve la IA" para ver por qué
        debug.appendleft({"id": hashlib.md5(f"{time.time()}".encode()).hexdigest()[:10], "time": time.time(),
                          "where": where, "slots": [], "votes": report or [{"model": "—", "error":
                          "ninguna IA disponible: faltan claves o todas están sin cupo"}], "kept": [], "jpg": to_jpg(zoom)})
        raise Exception(report[-1]["error"] if report else "429 no hay ninguna IA disponible (revisa las claves)")
    screener = next(iter(results))
    # Paso 2: solo si vio algo (aunque sea con dudas), las mejores IAs lo revisan a la vez
    if results[screener]:
        judges = available(JUDGES, skip={screener})[:JUDGES_PER_CHECK]
        futures = {m: pool.submit(ask, m, jpgs, prompt) for m in judges}
        for m, f in futures.items():
            try:
                data = f.result(timeout=45)
            except Exception as e:
                report.append({"model": m, "error": str(e)[:90]})
                continue
            results[m] = vote(data, MIN_CONF)
            report.append({"model": m, "items": list(results[m])})
    votes, best = {}, {}
    for mine in results.values():
        for name, info in mine.items():
            if info["confidence"] < MIN_CONF:       # las dudas de la rápida no cuentan como voto
                continue
            votes[name] = votes.get(name, 0) + 1
            if info["confidence"] > best.get(name, {}).get("confidence", -1):
                best[name] = info
    need = VOTES                     # nunca decide una IA sola: si no hay quien confirme, no avisa
    out = [{**best[n], "votes": f"{v}/{len(results)}"} for n, v in votes.items() if v >= need]
    debug.appendleft({"id": hashlib.md5(f"{time.time()}".encode()).hexdigest()[:10], "time": time.time(),
                      "where": where, "slots": slots, "votes": report,
                      "kept": [i["name"] for i in out], "jpg": to_jpg(zoom), "full": jpgs[0]})
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
        found = analyze(frame, inv)
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
        "debug": [{k: v for k, v in d.items() if k not in ("jpg", "full")} | {"has_full": "full" in d} for d in debug],
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
def debug_img(did: str, full: int = 0):
    for d in debug:
        if d["id"] == did:      # ?full=1 → la imagen exacta que recibió la IA (con la referencia)
            return Response(d.get("full") if full and d.get("full") else d["jpg"], media_type="image/jpeg")
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
