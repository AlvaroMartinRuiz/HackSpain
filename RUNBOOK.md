# Guion del fin de semana

## Poner una llamada en pie

```powershell
# 1. Dependencias (una vez)
.\.venv\Scripts\python -m pip install -r requirements.txt

# 2. Claves: copia .env.example a .env y rellena
#    PLATFORM_API_KEY ya está. Faltan DEEPGRAM_API_KEY, LLM_API_KEY, ELEVENLABS_API_KEY.

# 3. Comprobar que el núcleo está sano (no gasta nada, no usa modelo)
.\.venv\Scripts\python scripts\check_domain.py
.\.venv\Scripts\python scripts\check_engine.py

# 4. Arrancar
.\run.ps1
#    consola  -> http://localhost:7860/
#    endpoint -> ws://localhost:7860/ws
```

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

## Antes de cada Run All

```powershell
# El cable aguanta la ráfaga más grande del set (problema 2)
.\.venv\Scripts\python scripts\mock_call.py --calls 20 --seconds 6

# La lógica sigue dando el registro correcto
.\.venv\Scripts\python scripts\rehearse.py
```

Si `mock_call` reporta alguna llamada sin audio, para y arréglalo: una llamada
muda es un caso fallado haga lo que haga el resto.

Un Run All tarda unos dieciocho minutos y hay quince de espera después, así que
sale uno cada treinta y tres. Ensaya en texto entre medias, no con llamadas de
práctica.

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
