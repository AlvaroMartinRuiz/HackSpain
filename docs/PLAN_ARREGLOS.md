# Plan a corto plazo — El Turno

**Sábado 19/09 · el marcador se congela el domingo 20 a las 06:00 (Madrid).**
Revisado sobre `main` @ `a7cb250` y sobre las 20 llamadas reales de la consola del ngrok.

> **Nota:** las llamadas en las que el llamante no llega a hablar son un fallo del
> simulador del sponsor, no nuestro, y lo arreglan ellos. Este plan no lo trata.

## Estado (rama `fix/plan-arreglos`)

| # | Estado | Commit | Cómo se ha comprobado |
|---|---|---|---|
| A1 | ✅ | `821f995` | *Más simple que el fragmento de abajo:* `_synthesize` ya no toca `_speaking_since` (la interrupción usa `is_speaking`, que ya cuenta la síntesis) y el `playhead` se ajusta en cada chunk. Simulación: con `main`, una respuesta de 2 s salía entera en 0,8 s y la interrupción durante la síntesis no cortaba; con el arreglo sale al ritmo real y la interrupción corta antes del primer frame |
| A2 | ✅ | `d52b0d5` | Simulación: 2 avisos y luego nada; ningún aviso si el llamante habla o el LLM piensa; en el idioma de la llamada |
| L1 | ✅ | `a210b27` | Herramienta: el rechazo por seguro espera a la pregunta; los demás motivos no |
| L2 + V1 | ✅ | `a210b27` + `a8d0b29` | Herramienta: el 1.er paso no envía, deletrea y marca la letra inferida; una corrección vuelve a la relectura |
| L3 | ✅ | `49fbc92` | El directorio real encuentra a un paciente buscando su DNI sin letra |
| S1 | ✅ | `5792e16` | 15 pruebas con TestClient (401 sin token; token, cookie y WebSocket; `/ws` abierto; loopback con cabeceras de ngrok → 401) |
| B1 + B2 | ✅ | `fa9872c` | Aura real: 9 frases precalentadas; el saludo sale de caché sin llamar al proveedor; semáforos separados |
| B3 | ✅ | `e1c74bf` | Clínica real: con `ca` solo PR01 (habla catalán); sin filtro, PR01/PR03/PR07 |
| Saludo bilingüe | ✅ | `d52b0d5` | Decisión del equipo |
| Cloudflare en `.env.example` | ✅ | docs | La URL del gateway (`/compat`) y la actual responden 200 con vuestra clave |
| **Extra** · motivo del `NO_ACTION` de seguridad | ✅ | `a194845` | Lo encontró el ensayo (también fallaba en `main`): si nadie buscó a ningún paciente, el motivo es `out_of_scope`, no `patient_not_found` (p14, peso 4) |

**Verificación de extremo a extremo** (servidor local con Aura):
- `mock_call.py --calls 10`: 10/10 llamadas aguantan y sin errores; el saludo sale de la caché en todas; el aviso de silencio salta a los 7 s justos.
- Primer audio medido con precisión: **0,75–1,0 s con esta rama frente a 1,3–1,5 s en `main`** (por el saludo cacheado).
- `rehearse.py`, los 17 escenarios con el LLM real: todos han pasado en alguna ejecución. `questions` a veces falla una comprobación porque el agente contesta el horario (bien) sin llamar a `clinic_facts`; la reserva sale correcta, que es lo que puntúa. `triage` y `real_call` fallaron una vez de cada 3–4 por variabilidad del modelo.

**Cada miembro del equipo debe añadir a su `.env`:** `CONSOLE_TOKEN` (nuevo, para abrir la consola por ngrok) y cambiar `AGENT_GREETING` al saludo bilingüe (o borrar la línea).

## Resumen

| # | Qué | Prioridad | Ficheros | Tiempo |
|---|---|---|---|---|
| A1 | El audio sale más rápido que el tiempo real | 🔴 | `session.py` | 30 min |
| A2 | "¿Sigue ahí?" cuando el llamante calla | 🔴 | `session.py`, `brain.py`, `config.py` | 45 min |
| L1 | Preguntar por un segundo seguro antes de rechazar (p17) | 🟠 | `tools.py` | 20 min |
| L2 | Releer los datos antes de dar de alta (p4) | 🟠 | `tools.py` | 30 min |
| L3 | Completar o comprobar la letra del DNI (p4) | 🟠 | `identity.py`, `tools.py` | 20 min |
| S1 | La consola es pública a través de ngrok | 🟠 | `main.py`, `obs/api.py`, `console.js` | 30 min |
| B1 | Caché del saludo y de las frases fijas | 🟡 | `tts.py`, `session.py`, `main.py` | 20 min |
| B2 | Un semáforo por proveedor de TTS | 🟡 | `tts.py` | 10 min |
| B3 | El idioma elige médico y voz | 🟡 | `tools.py`, `tts.py`, `session.py` | 20 min |
| V1 | Pedir los datos en bloque (velocidad) | 🟡 | `prompt.py` | 10 min |

**Ya hecho en `main`:** registro doble a los 170 s, respaldo de voz ElevenLabs → Aura, Aura para prácticas, grabación de llamadas, keepalive de Deepgram y, de Alejandro, que un `provider_id` solo valga si lo ha devuelto una herramienta en esta llamada.

---

## 🔴 A1 · El audio sale más rápido que el tiempo real

**Qué pasa.** `_synthesize` marca `_speaking_since` **antes** de que llegue el audio, para que la interrupción funcione durante la síntesis. Pero `_player_loop` usa esa misma variable para detectar el inicio de una frase, y deja de ejecutar `playhead = max(playhead, time.monotonic())`. En cada respuesta, `playhead` se queda en el pasado (lo que duró el turno del llamante más el LLM), así que **los primeros segundos de audio salen de golpe**. Resultado: la interrupción no sirve (p13, peso 4), porque el audio ya está entero al otro lado. Se ve en la consola: en muchas frases falta el `agent_speaking`.

**Arreglo:** separar "voy a hablar" (para la interrupción) de "está sonando" (para el ritmo).
```python
# __init__
self._playing = False

# _player_loop: condición de inicio de frase
if not self._playing:
    self._playing = True
    self._speaking_since = self._speaking_since or time.monotonic()
    self._current_text = text
    self._current_sent = 0
    self._current_total = 0
    playhead = max(playhead, time.monotonic())
    await self.record("agent_speaking", {"text": text})
elif text != self._current_text:
    ...  # igual que ahora

# _finish_speaking y _interrupt: además de _speaking_since = None
self._playing = False

# _synthesize, en el except: no dejar el flag colgado si no llegó audio
if total == 0 and not self._playing and self._audio_queue.empty():
    self._speaking_since = None

# _interrupt
spoken = self._spoken_so_far() if self._playing else ""
self.agent.note_interruption(spoken or "(interrumpido antes de hablar)")
```
**Comprobar:** `agent_speaking` aparece en **cada** frase, y en la grabación de salida una respuesta de 4 s ocupa ~4 s.

---

## 🔴 A2 · "¿Sigue ahí?" cuando el llamante calla

**Qué queremos.** Si el agente ha terminado de hablar y el llamante no dice nada en unos segundos, el agente pregunta si sigue ahí, como haría una recepcionista.

**Por qué importa además de por naturalidad:**
- El p13 incluye *"ocho segundos de silencio"* del llamante a propósito.
- `rules.md` corta la llamada, y la cuenta como fallo nuestro, si nuestro agente pasa demasiado tiempo sin audio audible. Una pregunta a tiempo evita que los dos lados se queden callados.

**Comportamiento:**
- El temporizador **arranca** cuando el agente termina de hablar y **no** está procesando un turno (el LLM no está pensando y no hay audio en cola).
- Se **cancela** en cuanto el llamante empieza a hablar (`caller_speaking` o el primer `stt_partial`), o cuando el agente vuelve a hablar.
- A los **7 s** de silencio: primer aviso. Si sigue el silencio, a los **7 s** siguientes: segundo aviso. **Como máximo 2 seguidos**: después no se repite (no se queda en bucle). El contador se reinicia cuando el llamante vuelve a hablar.
- **En el idioma de la llamada** (`session.language`):
  - `en`: *"Are you still there?"* → *"Can you hear me? I'm still here whenever you're ready."*
  - `es`: *"¿Sigue ahí?"* → *"¿Me oye? Sigo aquí cuando quiera."*
  - `ca`: *"Encara hi és?"* → *"Em sent? Encara soc aquí quan vulgui."*
- **No** se activa con el registro ya cerrado (`_sealing` o `_frozen`), ni antes del saludo.
- El aviso se añade al historial del LLM como mensaje del asistente, para que sepa que lo ha dicho y no lo repita ni pierda el hilo.
- Se registra como decisión (`stage: "silence_prompt"`, con `attempt` y `silent_s`) para verlo en la consola.

**Cambio:**
```python
# config.py
silence_prompt_s: float = field(default_factory=lambda: _float("SILENCE_PROMPT_S", 7.0))
silence_prompt_max: int = field(default_factory=lambda: _int("SILENCE_PROMPT_MAX", 2))

# session.py
SILENCE_PROMPTS = {
    "en": ["Are you still there?", "Can you hear me? I'm still here whenever you're ready."],
    "es": ["¿Sigue ahí?", "¿Me oye? Sigo aquí cuando quiera."],
    "ca": ["Encara hi és?", "Em sent? Encara soc aquí quan vulgui."],
}

# __init__
self._silence_task: Optional[asyncio.Task] = None
self._silence_prompts = 0

def _arm_silence(self) -> None:
    self._disarm_silence()
    if self._sealing or self._frozen or self.text_mode:
        return
    if self._silence_prompts >= settings.silence_prompt_max:
        return
    self._silence_task = asyncio.create_task(self._silence_watch())

def _disarm_silence(self) -> None:
    if self._silence_task and not self._silence_task.done():
        self._silence_task.cancel()
    self._silence_task = None

async def _silence_watch(self) -> None:
    await asyncio.sleep(settings.silence_prompt_s)
    if self.is_speaking or self._turn_lock.locked() or self._sealing:
        return
    phrases = SILENCE_PROMPTS.get(self.language, SILENCE_PROMPTS["en"])
    text = phrases[min(self._silence_prompts, len(phrases) - 1)]
    self._silence_prompts += 1
    await self.record("decision", {"stage": "silence_prompt",
                                   "attempt": self._silence_prompts,
                                   "silent_s": settings.silence_prompt_s})
    self.agent.note_agent_line(text)      # nuevo en brain.py: añade {"role": "assistant", ...}
    await self.say(text)
    # al terminar de sonar, _finish_speaking vuelve a armar el temporizador

# _finish_speaking, al final
self._arm_silence()

# _on_speech_started y _on_partial, al principio
self._disarm_silence()
self._silence_prompts = 0

# say(), al principio
self._disarm_silence()

# finalize(), al principio
self._disarm_silence()
```
**Detalles:**
- `_silence_task` **no** va en `self._tasks`. Se cancela y se vuelve a crear cada vez, y `_disarm_silence()` en `finalize` es suficiente.
- Las 6 frases son fijas: se cachean con **B1**, así salen al instante y no gastan caracteres de ElevenLabs.

**Comprobar:**
- [ ] Práctica o `mock_call.py` sin hablar: a los ~7 s el agente dice *"Are you still there?"*, a los ~14 s el segundo aviso, y después silencio. En **Decisiones** salen dos `silence_prompt`.
- [ ] Hablar justo antes de los 7 s: **no** salta ningún aviso.
- [ ] Mientras el agente "piensa" (LLM lento), **no** salta.
- [ ] Tras un aviso, el llamante responde y la conversación sigue sin repetir preguntas.

---

## 🟠 Lógica: tres fallos vistos en llamadas reales

El prompt ya pide L1 y L2 y el modelo no los hizo. **Pasan a código.**

### L1 · Preguntar por un segundo seguro antes de rechazar → p17 (peso 4)
`79037c1f`: el plan no cubre fisioterapia, el agente rechaza (`specialty_not_covered`), el llamante insiste *"what would it cost me under my policy?"* y el agente nunca pregunta por otro seguro.
```python
# tools.py
INSURANCE_REASONS = {"specialty_not_covered", "location_not_covered",
                     "provider_not_in_network", "insurer_referral_required",
                     "allowance_exhausted"}

# end_without_booking: nuevo argumento booleano en el schema, "caller_has_no_other_plan"
if (args.get("reason") in INSURANCE_REASONS and not self.named_insurers
        and not args.get("caller_has_no_other_plan")):
    return {"error": "ask_second_plan",
            "guidance": "Before refusing, ask whether they hold another insurance plan. "
                        "If they name one, call find_appointments again with also_consider_insurer. "
                        "Only if they say they have no other plan, call end_without_booking "
                        "again with caller_has_no_other_plan=true."}
```
**Comprobar:** en `rehearse.py`, el escenario del segundo seguro reserva con el `policy_id` del segundo plan, y el de control (el primer plan ya cubre) no pregunta de más.

### L2 · Releer los datos antes de dar de alta → p4
`44d25fd5`: el llamante dice *"Nuria"*, el STT oye *"Nario"*, y se registra "Nario". **Un carácter mal = caso fallado.**
**Arreglo:** `register_new_patient` en dos pasos.
1. Sin `confirmed`: **no envía nada**. Normaliza y devuelve `{"read_back": ...}` con el nombre y los dos apellidos **deletreados**, el DNI con su letra, la fecha, el teléfono y el email **deletreado**. La instrucción: *"Read these back and ask the caller to confirm or correct."* Guarda los datos en `self.pending_registration`.
2. Con `confirmed: true`: solo envía si los datos coinciden con `pending_registration`. Si el llamante corrigió algo, se vuelve al paso 1.

### L3 · Completar o comprobar la letra del DNI → p4
`2c1df33b`: el llamante dice *"48924647"* (el STT pierde la letra) y el agente sigue adelante.
**Arreglo** (en `identity.py`, si no existe ya), usado por `register_new_patient` y `lookup_patient`:
```python
LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"

def check_national_id(raw: str) -> dict:
    # DNI: 8 dígitos + letra. NIE: X/Y/Z + 7 dígitos + letra (X=0, Y=1, Z=2).
    # - sin letra              -> {"id": completo, "letter_added": True}  (releerlo)
    # - letra que no cuadra    -> {"error": "letter_mismatch"}            (pedir repetir los números)
    # - correcto               -> {"id": ...}
```

---

## 🟠 S1 · La consola es pública a través de ngrok

`GET https://…ngrok-free.dev/api/console/overview` responde **200 sin credenciales**: cualquiera con la URL ve teléfonos, transcripciones, fichas de pacientes y grabaciones. `HOST=127.0.0.1` no lo evita, porque ngrok reenvía todo el puerto. El p14 penaliza filtrar datos de pacientes y el jurado puntúa la seguridad.

**Arreglo:**
- Nuevo `CONSOLE_TOKEN` en `.env`.
- Un middleware en `main.py` exige el token (cabecera `X-Console-Token`, o `?token=` para abrirla en el navegador) en `/`, `/static/*` y `/api/console/*`.
- **Sin token:** `/ws` (lo necesita la plataforma) y `/health`.
- `console.js` lee `?token=` de la URL y lo manda en cada petición y en su WebSocket.
- Si `CONSOLE_TOKEN` está vacío, la consola solo responde desde `127.0.0.1` (sin pasar por ngrok).

**Comprobar:** a través del ngrok, sin token → 401; con `?token=…` → la consola funciona; `/ws` sigue aceptando llamadas.

---

## 🟡 Voz (lo de Emma)

- **B1 · Caché de frases fijas.** El saludo, la frase de error del LLM y las 6 de A2. Se sintetizan al arrancar y se reutilizan: quita el pico de concurrencia al empezar un Run All y ahorra caracteres. La clave de caché incluye el proveedor, el modelo, la voz, el idioma y el texto.
- **B2 · Un semáforo por proveedor.** `_TTS_GATE` es uno solo para ElevenLabs y Aura, así que cuando ElevenLabs satura, el respaldo espera en la misma cola.
- **B3 · El idioma elige médico y voz.** `find_appointments` filtra con `ca` cuando `session.language == "ca"` (solo con `ca`); `eleven_v3_conversational` para catalán (comprobado: 200 en `ulaw_8000`); traducir los códigos de 3 letras (`cat/spa/eng`) para cuando entre Scribe.

## 🟡 V1 · Velocidad

`2c1df33b` llegó a 171 s pidiendo el email, con los datos de uno en uno (~20 s por turno). **Una llamada que llega a los 3 min falla igualmente.** En `prompt.py`: pedir los datos de alta **en bloque** y releerlos **una sola vez** al final (que encaja con L2). Así lo hizo `44d25fd5`, que registró en 111 s.

---

## Orden de implementación (una rama, un commit por punto)

1. **A1**, porque A2 depende de que `_finish_speaking` funcione bien.
2. **A2**
3. **L1, L3, L2**
4. **S1**
5. **B2, B1** (B1 incluye las frases de A2)
6. **B3, V1**

Después de cada punto: `check_domain.py`, `check_engine.py` y `rehearse.py` en verde. Al final: `mock_call.py --calls 10` y una práctica real antes de mergear en `main`.

## Pendiente de decidir (equipo)

- **Idioma del saludo.** 69 de los 73 casos públicos son en inglés y los llamantes siguen en inglés. Opción: *"Clínica Arenal, good morning — buenos días. How can I help?"*
- **Cloudflare.** La URL actual del LLM funciona (probada con streaming y tools); no cambiarla. Solo confirmar en Cloudflare → AI Gateway → Logs que las peticiones pasan por el gateway.
