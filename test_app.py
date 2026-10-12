"""Prueba rápida sin red: python test_app.py"""
import os
os.environ.update(ADMIN_PASSWORD="x", GEMINI_API_KEY="fake", DISCORD_WEBHOOK="https://example.invalid")
import numpy as np
import app
from fastapi.testclient import TestClient

# Detector de inventario: mundo oscuro vs panel gris claro en el centro
world = np.full((app.H, app.W, 3), 60, np.uint8)
inv = world.copy(); inv[150:400, 330:630] = 198
assert not app.inventory_open(world) and app.inventory_open(inv)

# Flujo completo con IA y Discord simulados
app.is_live = lambda url: False
sent = []
app.analyze = lambda jpg: [{"name": "Elytra", "count": 1, "where": "inventario"}]
app.send_discord = lambda st, found, jpg, reason: sent.append(reason)
app.take_budget = lambda: True
sid = app.add_streamer("donutstreamer")
app.handle_frame(sid, world)                       # primera revisión periódica → alerta
app.handle_frame(sid, inv)                         # inventario a los <15 s → espera, sin spamear
assert sent == ["revisión periódica"], sent
assert app.streamers[sid]["url"] == "https://www.twitch.tv/donutstreamer"

c = TestClient(app.app)
s = c.get("/api/state").json()
assert s["alerts"][0]["items"][0]["name"] == "Elytra" and "jpg" not in s["alerts"][0]
assert c.get(f"/api/alert/{s['alerts'][0]['id']}").status_code == 200
assert c.post("/api/streamers", json={"url": "a"}, headers={"X-Password": "mal"}).status_code == 401
assert c.put("/api/items", json={"items": ["Elytra", " "]}, headers={"X-Password": "x"}).json()["items"] == ["Elytra"]
assert c.get("/").status_code == 200
print("OK")
