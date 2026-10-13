"""Prueba rápida sin red: python test_app.py"""
import os
os.environ.update(ADMIN_PASSWORD="x", GEMINI_API_KEY="fake", DISCORD_WEBHOOK="https://example.invalid")
import json
from types import SimpleNamespace
import numpy as np
import app
from fastapi.testclient import TestClient

# Detector de inventario: mundo oscuro vs panel gris claro en el centro
world = np.full((app.H, app.W, 3), 60, np.uint8)
inv = world.copy(); inv[200:550, 450:830] = 198
assert not app.inventory_open(world) and app.inventory_open(inv)

# Filtro de la respuesta de la IA: fuera nombres que no están en la lista y poca seguridad
reply = {"items": [{"name": "elytra", "confidence": 0.95}, {"name": "Pechera de diamante", "confidence": 0.99},
                   {"name": "Spawner", "confidence": 0.4}]}
app.client = SimpleNamespace(models=SimpleNamespace(
    generate_content=lambda **k: SimpleNamespace(text=json.dumps(reply))))
assert [i["name"] for i in app.analyze(*app.crop(world, False))] == ["Elytra"]
assert app.debug[0]["kept"] == ["Elytra"] and len(app.debug[0]["raw"]) == 3   # el registro guarda todo lo que dijo

# Solo avisa si dos fotogramas seguidos ven lo mismo
app.is_live = lambda url: False
app.take_budget = lambda: True
sent, answers = [], [[{"name": "Elytra", "count": 1}],    # 1º: lo ve → pendiente
                     [],                                  # 2º: no lo confirma → descartado (falso positivo)
                     [{"name": "Elytra", "count": 1}],    # 3º: lo ve otra vez
                     [{"name": "Elytra", "count": 1}]]    # 4º: confirmado → aviso
app.analyze = lambda img, where: answers.pop(0)
app.send_discord = lambda st, found, jpg, reason: sent.append(reason)
sid = app.add_streamer("donutstreamer")
for _ in range(4):
    app.streamers[sid]["last_ai"] = 0          # fuerza revisión periódica en cada fotograma
    app.handle_frame(sid, world)
assert sent == ["revisión periódica"], sent
assert app.streamers[sid]["url"] == "https://www.twitch.tv/donutstreamer"

c = TestClient(app.app)
s = c.get("/api/state").json()
assert len(s["alerts"]) == 1 and s["alerts"][0]["items"][0]["name"] == "Elytra" and "jpg" not in s["alerts"][0]
assert c.get(f"/api/alert/{s['alerts'][0]['id']}").status_code == 200
assert s["version"] == app.VERSION and c.get(f"/api/debug/{s['debug'][0]['id']}").status_code == 200
assert c.post("/api/streamers", json={"url": "a"}, headers={"X-Password": "mal"}).status_code == 401
assert c.put("/api/items", json={"items": ["Elytra", " "]}, headers={"X-Password": "x"}).json()["items"] == ["Elytra"]
assert c.get("/").status_code == 200
print("OK")
