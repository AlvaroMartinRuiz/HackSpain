# Guion del fin de semana

## Desde cero en una máquina nueva

Python 3.11 o más nuevo. Nosotros corremos 3.14 y no hace falta igualarlo: en la
ruta configurada (ElevenLabs para escuchar y para hablar) el audio viaja en µ-law
de punta a punta y no pasa por `audioop`, que es lo único que cambió en 3.13.

```powershell
# Windows
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

Las versiones de `requirements.txt` están clavadas a las que hemos probado. No
las sueltes este fin de semana: dos portátiles con dos stacks distintos es el
fallo que nadie consigue depurar a las cuatro de la mañana.

Después, las claves. `.env` no está en el repo y no puede estarlo, así que
**pídeselo a alguien del equipo por un canal privado** en vez de reconstruirlo.
Si aun así toca reconstruirlo, `.env.example` tiene todos los nombres y qué hace
cada uno; las que no se adivinan son `PLATFORM_API_KEY`, `DEEPGRAM_API_KEY`,
`LLM_API_KEY` y `ELEVENLABS_API_KEY`.

Comprobar que el núcleo está sano — no gasta cuota y no usa modelo:

```powershell
.\.venv\Scripts\python scripts\smoke_test.py     # la clave y el host responden
.\.venv\Scripts\python scripts\check_domain.py   # fechas, DNI, tipos, cierres
.\.venv\Scripts\python scripts\check_engine.py   # el motor contra la clínica real
```

En macOS y Linux es el mismo comando con `./.venv/bin/python scripts/...`, y eso
vale para todos los scripts del resto de este documento.

## Arrancar

```powershell
.\run.ps1      # Windows
```

```bash
bash run.sh    # macOS / Linux
```

- consola  → <http://localhost:7860/>
- endpoint → `ws://localhost:7860/ws`

Los dos arrancan lo mismo (`python -m src.main`) y leen `HOST`, `PORT` y
`RELOAD` de `.env`, así que el puerto que anuncia la consola es siempre el
puerto en el que está escuchando. Está en pie cuando `/health` contesta:

```json
{"status": "ok", "voice_ready": true, "missing_keys": []}
```

`missing_keys` con algo dentro es un `.env` incompleto, y el agente se quedará
mudo justo en esa parte de la llamada.

**`RELOAD=true` sólo mientras editas.** Vigila el árbol y reinicia al guardar,
que durante un Run All es tirar las diez llamadas vivas a la vez.

## Exponerlo

```powershell
ngrok http 7860 --region eu
```

Europa, no otro continente: son frames de 20 ms en tiempo real y cada salto se
paga en todos ellos. Con cuenta, dominio fijo para no volver a tocar Settings:

```powershell
ngrok http --url=tu-nombre.ngrok-free.app 7860
```

En el dashboard, Settings → Integration:

| Campo | Valor |
| --- | --- |
| Endpoint | `wss://tu-nombre.ngrok-free.app/ws` — con esquema y con la ruta |
| Headers | vacío |

`https://` no es el endpoint y olvidar `/ws` es el fallo más común. Compruébalo
antes de entregarlo:

```powershell
.\.venv\Scripts\python scripts\mock_call.py --url wss://tu-nombre.ngrok-free.app/ws
```

### La consola a través del túnel

ngrok publica el puerto entero, y la consola enseña teléfonos, fichas,
transcripciones y grabaciones. Por eso, a través del túnel pide `CONSOLE_TOKEN`
(del `.env`). Ábrela una vez así y una cookie te mantiene dentro dos días:

```
https://tu-nombre.ngrok-free.app/?token=<CONSOLE_TOKEN>
```

Sin token responde 401. `/ws` y `/health` no lo necesitan (la plataforma solo usa
`/ws`), y en `http://localhost:7860/` desde la propia máquina tampoco. No
compartas la URL con el token fuera del equipo.

## Voz: ElevenLabs en el cable, Aura de reserva

STT y TTS van por ElevenLabs (`STT_PROVIDER=elevenlabs`, `TTS_PROVIDER=elevenlabs`):
Scribe v2 realtime en la escucha, Flash (y v3 conversacional en catalán) en la
voz. Aura sigue en el código como reserva si Flash responde 429, y se puede
volver a ella para practicar sin gastar el plan Creator:

```
STT_PROVIDER=deepgram
TTS_PROVIDER=deepgram
```

El plan Creator de ElevenLabs tiene **131.000 caracteres de TTS**, unos dos
Run All. No la uséis para ensayar frases. La lógica se itera en texto:

```powershell
.\.venv\Scripts\python scripts\rehearse.py
```

Llamadas de práctica **solo** para lo que depende del audio: ruido (p12),
interrupciones (p13), idiomas (p11).

## Antes de cada Run All

```powershell
# El cable aguanta la ráfaga más grande del set (problema 2)
.\.venv\Scripts\python scripts\mock_call.py --calls 10 --seconds 6

# Los arreglos de voz, consola y herramientas (sin modelo ni minutos de voz)
.\.venv\Scripts\python scripts\check_fixes.py

# La lógica sigue dando el registro correcto
.\.venv\Scripts\python scripts\rehearse.py
```

Pon `TTS_PROVIDER=elevenlabs` antes de puntuar. Si `mock_call` reporta alguna
llamada sin audio, o la consola muestra errores `tts`, para y arréglalo: una
llamada muda es un caso fallado haga lo que haga el resto.

Un Run All tarda unos dieciocho minutos y hay quince de espera después, así que
sale uno cada treinta y tres. Ensaya en texto entre medias. Vigilad los
caracteres de ElevenLabs entre un run y el siguiente.

## Cuando algo va mal

| Lo que ves | Dónde mirar |
| --- | --- |
| El caso falla y no sabes por qué | consola → la llamada → pestaña **Decisiones**: la traza dice qué ventana se pidió, cuántos huecos volvieron y qué regla bloqueó |
| El registro salió raro | pestaña **Registro**: el payload exacto que se envió y el HTTP que devolvió |
| El agente se inventó algo | pestaña **Herramientas**: si no hay una llamada a herramienta detrás de lo que dijo, el prompt necesita apretarse |
| `404` al enviar | el `call_id` no es el `start.callSid`, o la llamada era un ensayo local |
| `410` al enviar | llegó tarde: la ventana se cierra 30 s después de cerrarse el socket |
| `409` al enviar | acción idéntica repetida. Es lo esperado en un reintento, no un fallo |
| `422` al registrar | la letra del DNI no cuadra con los dígitos |
| El agente no habla | `GET /health` → `missing_keys` |

Una llamada terminada se puede volver a leer entera mucho después:

```
GET /api/console/calls/{call_id}/replay
```

## Para el jurado

La consola es la demo, y se conduce en directo, no en una diapositiva.

1. Abre `http://localhost:7860/` en grande. Arriba se ve qué modelo, qué voz y
   qué endpoint están activos.
2. Lanza `mock_call.py --calls 10`: diez llamadas aparecen a la vez y se ve el
   pico de concurrencia y la latencia mediana moverse.
3. Que llamen ellos desde la plataforma. Mientras hablan se ve la transcripción
   turno a turno, y el corte marcado en el punto exacto donde interrumpieron.
4. Al colgar, abre **Decisiones** y responde "¿por qué dijo eso?" con la traza
   delante: la ventana de fechas que pidió, los huecos que volvieron, la regla
   que bloqueó, el hueco que eligió y por qué.
5. Enseña `scripts/rehearse.py` corriendo: es cómo sabemos que funciona, aparte
   de que funcionara en su llamada.
