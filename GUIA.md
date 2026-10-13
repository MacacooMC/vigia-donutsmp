# Cómo montar el Vigía DonutSMP (gratis, sin tarjeta)

Necesitas 4 cuentas gratis: **Discord**, **Google**, **GitHub** y **Render**. Ninguna pide tarjeta.

## 1. Webhook de Discord (dónde llegan los avisos)
1. En tu servidor de Discord: clic derecho en el canal → **Editar canal** → **Integraciones** → **Webhooks** → **Nuevo webhook**.
2. Ponle nombre y pulsa **Copiar URL del webhook**. Guárdala.

## 2. Clave de la IA (Gemini)
1. Entra en **aistudio.google.com** con tu cuenta de Google.
2. **Get API key** → **Create API key**. Copia la clave (empieza por `AIza...`).

## 3. Subir el código a GitHub
1. Crea una cuenta en **github.com**.
2. Arriba a la derecha **+** → **New repository** → nombre `vigia-donutsmp` → **Public** → **Create repository**.
3. Pulsa **uploading an existing file** y arrastra TODO lo de dentro de la carpeta del zip
   (`app.py`, `Dockerfile`, `requirements.txt`, `README.md`, `GUIA.md`, `test_app.py` y la carpeta `static`).
   Arrastra la carpeta `static` entera, no solo el archivo de dentro. → **Commit changes**.
4. Comprueba que en el repo se ve `static/index.html`.

## 4. Ponerlo en marcha en Render
1. Entra en **render.com** → **Get Started** → **GitHub** (inicia sesión con tu cuenta de GitHub).
2. **New** → **Web Service** → elige el repo `vigia-donutsmp` (si no aparece, pulsa **Configure GitHub** y dale acceso).
3. Render detecta el Dockerfile solo. Rellena:
   - **Name**: `vigia-donutsmp` (será tu link: `vigia-donutsmp.onrender.com`; si está cogido, pon otro)
   - **Instance Type**: **Free**
4. En **Environment Variables** añade:

| Nombre | Valor |
|---|---|
| `GEMINI_API_KEY` | la clave del paso 2 |
| `DISCORD_WEBHOOK` | la URL del paso 1 |
| `ADMIN_PASSWORD` | una contraseña que solo sepas tú (y quien tú quieras) |
| `STREAMERS` | (opcional) directos a vigilar siempre, separados por comas. Ej: `https://www.twitch.tv/fulano,https://kick.com/mengano` |

5. **Deploy Web Service**. La primera vez tarda unos minutos. Cuando ponga **Live**, abre el link.

Los streamers que añadas desde la web se borran cada vez que Render reinicia el servicio; los de `STREAMERS` no.

## 5. Que no se duerma
Render apaga el servicio gratis si pasan **15 minutos sin visitas**, y entonces deja de vigilar.
1. Entra en **uptimerobot.com** (gratis, sin tarjeta) → **New monitor** → tipo **HTTP(s)**.
2. URL: `https://TU-NOMBRE.onrender.com/api/ping` · intervalo **5 minutos**.

Con eso se mantiene despierto. Render da 750 horas gratis al mes, que alcanzan para tenerlo encendido el mes entero (un mes tiene como mucho 744 horas), siempre que sea tu único servicio gratis.

## 6. Usarlo
- La web es `https://TU-NOMBRE.onrender.com` → pásasela a tus amigos.
- Abre **Panel de admin**, pon la contraseña, pega el link del directo y pulsa **Añadir**.
- Edita la lista de objetos valiosos ahí mismo (uno por línea).

## Ajustes opcionales (variables de entorno en Render)
| Variable | Por defecto | Qué hace |
|---|---|---|
| `MODELS` | gemini-flash-latest,gemini-flash-lite-latest,gemma-3-27b-it | las IAs que votan, separadas por comas. Cada una tiene su propio cupo gratis |
| `VOTES` | 2 | cuántas IAs tienen que estar de acuerdo para avisar. 1 = más avisos (y más falsos), 3 = casi sin falsos pero se le escapan cosas |
| `DAILY_LIMIT` | 400 | máximo de revisiones al día |
| `PERIODIC_CHECK` | 120 | cada cuántos segundos revisa aunque no pase nada |
| `BAR_DIFF` | 25 | sensibilidad del detector de cambios en la barra. Más bajo = revisa más a menudo (gasta más cupo) |
| `MIN_CONFIDENCE` | 0.8 | seguridad mínima de cada IA (0 a 1) |

## Si algo falla
- **"error IA: ... 429 / RESOURCE_EXHAUSTED"** → te pasaste del límite gratis. Baja `DAILY_LIMIT`, sube `PERIODIC_CHECK` o sube `BAR_DIFF`.
- **Una IA sale con ✗ "not found" en "Lo que ve la IA"** → ese modelo ya no existe; el bot la deja de usar solo. Puedes quitarla de `MODELS`.
- **Siempre "offline" aunque esté en directo** → en Render abre la pestaña **Logs** y pásale el error a Claude; puede que la plataforma bloquee el servidor.
- **La imagen se queda congelada o va muy atrasada** → la CPU gratis no da abasto; vigila menos streamers a la vez (1–2 es lo seguro).
- **No detecta cuando abre el inventario** → el streamer usa un texture pack que cambia el color de la GUI; seguirá avisando con la revisión periódica.
