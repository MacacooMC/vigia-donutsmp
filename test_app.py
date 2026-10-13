"""Prueba rápida sin red: python test_app.py"""
import os
os.environ.update(ADMIN_PASSWORD="x", GEMINI_API_KEY="fake", DISCORD_WEBHOOK="https://example.invalid",
                  MODELS="a,b,c")
import json
from types import SimpleNamespace
import numpy as np
import app
from fastapi.testclient import TestClient

app.time.sleep = lambda s: None
fake = lambda fn: SimpleNamespace(models=SimpleNamespace(generate_content=fn))

# Detector de inventario: mundo oscuro vs panel gris claro en el centro
world = np.full((app.H, app.W, 3), 60, np.uint8)
inv = world.copy(); inv[200:550, 450:830] = 198
assert not app.inventory_open(world) and app.inventory_open(inv)

# Votación: Elytra la ven 2 IAs → cuenta. Spawner solo 1 → no. "c" no existe (404) → se descarta para siempre
def gen(model, contents, config):
    if model == "c":
        raise Exception("404 NOT_FOUND model c not found")
    found = [{"name": "elytra", "confidence": 0.95}, {"name": "Pechera de diamante", "confidence": 0.99}]
    if model == "a":
        found.append({"name": "Spawner", "confidence": 0.9})
    return SimpleNamespace(text="```json\n" + json.dumps({"slots": ["casilla 1: alas"], "items": found}) + "\n```")
app.client = fake(gen)
out = app.analyze(*app.crop(world, False))
assert [(i["name"], i["votes"]) for i in out] == [("Elytra", "2/2")], out
assert "c" in app.dead and app.debug[0]["kept"] == ["Elytra"]

# Si solo responde una IA, decide ella sola
app.dead.add("b")
assert [i["name"] for i in app.analyze(*app.crop(world, False))] == ["Elytra", "Spawner"]
app.dead.clear(); app.dead.add("c")

# IA saturada (503): reintenta y sigue
calls = []
def flaky(model, contents, config):
    calls.append(model)
    if calls.count(model) == 1:
        raise Exception("503 UNAVAILABLE")
    return SimpleNamespace(text=json.dumps({"items": []}))
app.client = fake(flaky)
assert app.analyze(*app.crop(world, False)) == [] and sorted(calls) == ["a", "a", "b", "b"]

# Disparadores: revisión periódica, luego solo si cambia la barra (y pasado el enfriamiento)
app.is_live = lambda url: False
app.take_budget = lambda: True
sent, reasons = [], []
real_analyze = app.analyze
def spy(img, where):
    reasons.append(where)
    return [{"name": "Elytra", "count": 1, "votes": "2/2"}]
app.analyze = spy
app.send_discord = lambda st, found, jpg, reason: sent.append(reason)
sid = app.add_streamer("donutstreamer")
st = app.streamers[sid]
app.handle_frame(sid, world)                       # 1º: revisión periódica → alerta
app.handle_frame(sid, world)                       # misma barra → nada
bar = world.copy(); bar[690:, 500:780] = 230       # cambia algo en la barra rápida
app.handle_frame(sid, bar)                         # cambio, pero < 8 s → nada
st["last_ai"] -= 10
app.handle_frame(sid, bar)                         # cambio y pasado el enfriamiento → revisa (mismo objeto: no repite aviso)
assert len(reasons) == 2 and sent == ["revisión periódica"], (reasons, sent)
assert st["url"] == "https://www.twitch.tv/donutstreamer"

c = TestClient(app.app)
s = c.get("/api/state").json()
assert len(s["alerts"]) == 1 and s["alerts"][0]["items"][0]["name"] == "Elytra" and "jpg" not in s["alerts"][0]
assert s["models"] == ["a", "b"] and s["version"] == app.VERSION
assert c.get(f"/api/alert/{s['alerts'][0]['id']}").status_code == 200
assert c.get(f"/api/debug/{s['debug'][0]['id']}").status_code == 200
assert c.post("/api/streamers", json={"url": "a"}, headers={"X-Password": "mal"}).status_code == 401
assert c.put("/api/items", json={"items": ["Elytra", " "]}, headers={"X-Password": "x"}).json()["items"] == ["Elytra"]
assert c.get("/").status_code == 200
print("OK")
