# Prosper Track — Guía de referencia (HackSpain)

Documento de trabajo para el equipo. Resume la documentación oficial del reto: arquitectura, APIs, scoring, problemas y trampas.

**Clínica:** Clínica Arenal (EHR de solo lectura, idéntico para todos los equipos).

---

## 1. Qué hay que construir

Un **servidor WebSocket** que conteste llamadas entrantes como recepcionista de clínica.

- No hay número de teléfono ni Twilio en vuestro lado.
- Prosper os llama por WebSocket y simula al paciente.
- Al terminar la llamada, vuestro agente **POSTea la acción** que habría ejecutado (`book`, `cancel`, etc.).

El modelo de voz es **un componente**, no el sistema entero. Lo que puntúa:

- Lookups reales en el EHR (`/directory`, `/availability`, `/appointments`)
- Validaciones antes de escribir
- Estado por llamada (el caller cambia de opinión)
- Un pipeline por socket (el Switchboard sigue abriendo 5/10/20)
- Reporte correcto de acciones al final

**No hay starter kit.** Montar el servidor que contesta el teléfono es parte del reto.

---

## 2. Arranque rápido (Get on the phone)

### Credenciales (desk de registro)

| Qué | Para qué |
|---|---|
| Email | Login al dashboard |
| Password generada | Login al dashboard |
| API key `pk-…` | Header `X-Api-Key` en todas las peticiones |
| Tarjeta prepago €100 | Modelos, STT, TTS, ngrok, etc. |

```bash
export PLATFORM_API_KEY=pk-...
export PLATFORM_API_BASE_URL=https://<host que os den en el desk>

# Health (sin key)
curl -sS "$PLATFORM_API_BASE_URL/api/v1/health"

# Directory (con key)
curl -sS -H "X-Api-Key: $PLATFORM_API_KEY" \
  "$PLATFORM_API_BASE_URL/api/v1/directory?name=Marta%20Ruiz"
```

- Key inválida/revocada → `403 {"detail":"Invalid API key"}`
- Recurso de otro equipo → `404` (indistinguible de no existir)
- Todas las rutas excepto `/health` y el schema requieren `X-Api-Key`

### Stack recomendado

1. **WebSocket server** con protocolo **Twilio Media Streams** (sin cuenta Twilio)
2. **Pipecat** recomendado: pipeline STT → LLM → TTS + serializer/transport Twilio
3. **ngrok** para exponer el socket

```bash
ngrok http 7860
# Endpoint final: wss://a1b2c3d4.ngrok-free.app/ws
```

**Trampas ngrok:**

- URL free cambia al reiniciar → dominio estático con cuenta
- Región europea (audio en frames de 20 ms)
- El endpoint es `wss://`, no `https://`, **con path incluido**
- Mantener el túnel levantado; un scored run es un socket, el Switchboard abre hasta 20

### Registrar endpoint (dashboard)

Settings → Integration (no hace falta volver al desk):

| Campo | Valor |
|---|---|
| Endpoint | `wss://tu-dominio.ngrok-free.app/ws` |
| Headers | Opcional, uno por línea (`Authorization: Bearer …`) |

- Guardar **reemplaza** toda la config
- Valores de headers son write-only
- El cambio aplica al **siguiente** run (runs en cola usan el endpoint anterior)

### Probar

| Botón | Qué hace |
|---|---|
| **Call** (por caso publicado) | Práctica, no puntúa. Muestra respuesta, transcript, audio, campos fallidos |
| **Scored run** (el problema que eliges) | Un caso privado de ese problema. Lo que sube al leaderboard |

Límites:

- 1 run/practice activo a la vez
- 30 s entre practice calls
- **12 min** de cooldown tras un scored run
- Máx. 3 min por llamada
- Cada problema paga tus **primeras cuatro** llamadas aprobadas (un fallo no ocupa hueco)
- Los Run All anteriores siguen contando; ninguna puntuación ha bajado

```bash
curl -sS -H "X-Api-Key: $PLATFORM_API_KEY" \
  "$PLATFORM_API_BASE_URL/api/v1/submissions?limit=50"
```

---

## 3. Contrato de llamada (Call contract)

### Flujo WebSocket (Twilio Media Streams)

Orden de mensajes entrantes:

1. `connected`
2. `start` → `start.callSid` = **`call_id`** (guardarlo)
   - `start.customParameters.call_id` (mismo id)
   - `start.customParameters.from_number` → E.164, hint para `/directory?phone=…` (puede faltar)
3. `media` → audio µ-law 8 kHz, base64, frames 20 ms
4. `stop` → cierre

**Importante:**

- JSON del handshake = **camelCase** (Twilio)
- `sequenceNumber`, `chunk`, `timestamp` son **strings**
- Vuestro agente responde con `media` (y opcionalmente `mark`/`clear`; barge-in es vuestro)
- **Un pipeline por socket** — nunca compartir sesión entre conexiones
- Un scored run = 1 socket; Switchboard = hasta 20

### Ventana de submission

- Abre al conectar la llamada
- Cierra **30 s después** de que Prosper cierre el socket
- Enviar antes de colgar **no es error**; solo importa no llegar tarde

| Situación | HTTP |
|---|---|
| `call_id` inválido / otro equipo | 404 |
| Llamada abierta o ≤30 s cerrada | 200 |
| >30 s desde cierre | 410 |
| Acción idéntica ya aceptada | 409 (retry OK) |
| Body malformado | 422 |

### POST `/api/v1/submit/<action>`

Header: `X-Api-Key`. El EHR es **read-only** — solo reportáis lo que habríais hecho.

#### `POST /submit/book`

```json
{
  "call_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "patient_id": "P00042",
  "provider_id": "PR05",
  "location_id": "sur",
  "appointment_type_id": "review",
  "slot": "2026-09-24T16:30:00+02:00",
  "policy_id": "sanitas"
}
```

#### Otras acciones

| Ruta | Campos (además de `call_id`) |
|---|---|
| `POST /submit/register` | `given_name`, `first_surname`, `second_surname`, `national_id`, `date_of_birth`, `phone`, `email`, `insurer` |
| `POST /submit/reschedule` | `appointment_id`, `provider_id`, `location_id`, `slot`, `policy_id` |
| `POST /submit/cancel` | `appointment_id` |
| `POST /submit/no-action` | `reason` |
| `POST /submit/escalate` | `reason` |

**Reglas clave:**

- `call_id` = exactamente `start.callSid` (no inventar)
- `slot` con **offset explícito**, comparado al minuto en **Europe/Madrid**
- `appointment_type_id` → usar el que devuelve `/availability`, no hardcodear `review`
- `policy_id` → plan concreto; puede haber 2 planes y solo 1 en el record
- Paciente no registrado → **REGISTER**, no BOOK
- Una acción por request; varias acciones = varios POST
- **No submitir nada = fallo siempre**

#### Valores de `reason` (vocabulario cerrado)

**Reglas de clínica:**

`not_eligible_age` · `referral_required` · `provider_not_in_network` · `specialty_not_covered` · `location_not_covered` · `insurer_referral_required` · `allowance_exhausted` · `provider_on_leave` · `location_hours` · `type_not_offered` · `patient_history`

**Otros:**

`no_availability` · `clinic_closed` · `patient_not_found` · `provider_not_found` · `caller_not_authorised` · `out_of_scope` · `medical_emergency`

Respuesta 200:

```json
{"call_id": "…", "received_at": "…", "record": {"actions": [...]}}
```

---

## 4. API de la clínica (read-only)

Cachear al arranque — no cambia durante el evento.

### Consultas por caller

| Endpoint | Uso |
|---|---|
| `GET /directory` | Identificar: `name`, `national_id`, `phone`, `date_of_birth` |
| `GET /availability` | Slots + restricciones: `date_from`, `date_to`, `provider_id` o `specialty_id`, opc. `location_id`, `patient_id`, `insurer` (repetible) |
| `GET /patients/{id}/appointments` | Citas (`when=upcoming\|past\|all`). **Única fuente de `appointment_id`** |

### Catálogo (pull once)

| Endpoint | Contenido |
|---|---|
| `GET /clinic` | Todo + ventana bookable + restricciones con reason |
| `GET /providers` | Especialidad, idiomas, horarios, planes, bajas |
| `GET /locations` | 3 sedes, horarios, cobertura |
| `GET /specialties` | Edad, referral, planes |
| `GET /appointment-types` | Duración, specialty, tipo de paciente |
| `GET /insurance-plans` | Cobertura por specialty/site |

**No hay endpoint de booking.** Otro equipo practicando no os quita slots.

### Seis reglas de identificación

1. Submitir **nombre e id del record**, no lo que dijo el caller
2. Campo exacto que no match → **excluye** paciente (no downrank)
3. `phone` normaliza a 9 dígitos nacionales (`+34…`, `0034…`, `612…` = mismo)
4. `/availability` devuelve `blocked` con la regla aunque no haya slots; slots vacíos + blocked vacío = agenda llena
5. Sin `insurer` en availability → cotiza contra el plan del record; 2º plan hay que preguntarlo
6. Cada paciente tiene **note** + historial → no scored en leaderboard, **sí en jurado**

---

## 5. Clínica Arenal — trampas importantes

### Sedes

- **Centro, Norte, Sur** (Madrid)
- Solo **Centro** abre sábado; **ninguna** abre domingo
- **12 oct 2026 (lun)** = Fiesta Nacional → todo cerrado ("first thing Monday" = trampa)

### Especialidades

- Límite edad: **14 años** (en meses, sin solapamiento)
- Referrals del paciente en su record de directory

### Proveedores (12)

- **Dr. Requena** de baja 14–30 sep (todo el evento)
- Pares ambiguos por voz: **Sáez/Sáenz**, **Iglesias/Iglesia**
- **D. Álvaro Cid** (fisioterapeuta, no "Dr.")
- Idioma solo restringe en **problema 11**

### Seguros (10 planes)

- **ASISA** + fisioterapia → imposible (fisio solo en Sur; ASISA physio solo Centro/Norte)
- **Adeslas** no cubre ginecología (solo hay 1 ginecóloga)
- **Dra. Iglesias** no toma DKV → redirigir a Dr. Vilar
- `privado` = plan explícito, no fallback

### Tipos de cita (11)

- El tipo correcto viene de **specialty + has_visited_before**, no de la conversación
- `/availability` devuelve `appointment_type` → **usar ese id exacto**
- Trampa: `review` vs `dermatology_review` (mismos minutos, id distinto)

### Calendario

- Slots: **7 sep – 16 oct 2026**, pasos 15 min
- Rango availability máx: **14 días**; fuera de ventana → 422
- Fechas relativas se resuelven al **momento de conectar** (Europe/Madrid)
- **Nada same-day**: "earliest" = desde el día **siguiente** a la llamada

---

## 6. Scoring

### Qué pasa un caso

- Binario: acciones submitidas ∈ acciones aceptadas (tras normalización)
- Sin crédito parcial
- Refusal correcta = `NO_ACTION(reason)`, no silencio
- Varios outcomes válidos (ej. 3 GPs libres a la misma hora)

### Puntos

```
puntos = Σ (fracción_pass_del_problema × peso)
```

- Peso por problema: 1–5
- Máximo roster completo: **49 puntos**
- Cada problema paga las **primeras 4 llamadas aprobadas** (los pases de un Run All antiguo cuentan)
- Leaderboard = esa suma, no el último run
- Problema 2 (Switchboard) **no puntúa**
- Practice **no puntúa**
- Cooldown scored: **12 min**

### Límites de llamada

- Máx. **3 min** por call
- Silencio del agente → corte (atribuido a agente)
- Conexión lenta → fallo

### Qué NO puntúa el leaderboard

Voz, estilo, velocidad, arquitectura, coste, orden de preguntas.

**Excepción:** problema 14 comprueba transcript por datos filtrados (DNI/teléfono de otro paciente).

### Jurado (final boss) — criterios extra

- Experiencia de llamada (interrupciones, reparar mishearings)
- Personalización (chart antes de preguntar)
- Plataforma (console en vivo, observabilidad, 10 concurrent)
- Seguridad y límites
- Idiomas (code-switching, nombres españoles)
- Rigor de ingeniería (eval harness, coste, failure modes)

### Wall freeze

**Domingo 20 sep, 06:00 Europe/Madrid** — solo runs completados antes cuentan.

Reveal casos privados: **Lunes 21 sep, 00:00 Europe/Madrid**.

---

## 7. Los 18 problemas

| # | Problema | ID | Peso | Open |
|---|---|---|---|---|
| 1 | The Simple Booking | `simple_booking` | 1 | yes |
| 2 | The Switchboard (5/10/20 concurrent) | `switchboard` | — | yes |
| 3 | The Doctor and the Site | `doctor_and_site` | 2 | yes |
| 4 | The New Patient | `the_new_patient` | 2 | not yet |
| 5 | When Exactly | `when_exactly` | 2 | not yet |
| 6 | The Rules | `the_rules` | 3 | not yet |
| 7 | No Slot Free | `no_slot_free` | 2 | not yet |
| 8 | Change and Cancel | `change_and_cancel` | 2 | not yet |
| 9 | The Third Party | `third_party` | 3 | not yet |
| 10 | Triage | `triage` | 3 | not yet |
| 11 | Languages | `languages` | 3 | not yet |
| 12 | Noise | `noise` | 3 | not yet |
| 13 | The Difficult Caller | `difficult_caller` | 4 | not yet |
| 14 | Adversarial and Privacy | `adversarial` | 4 | not yet |
| 15 | The Nearest Site | `nearest_site` | 3 | not yet |
| 16 | The Questions | `the_questions` | 3 | not yet |
| 17 | The Second Policy | `second_policy` | 4 | not yet |
| 18 | The Real Call | `the_real_call` | 5 | not yet |

### Resumen por problema

| # | Qué prueba | Acción esperada |
|---|---|---|
| 1 | Booking baseline, earliest | `BOOK` |
| 2 | Concurrencia (diagnóstico) | Frac. éxito de #1 |
| 3 | Doctor + sede concretos | `BOOK` o `NO_ACTION` |
| 4 | Paciente nuevo | `REGISTER` (sin BOOK) |
| 5 | Fechas vagas ("next Thursday", etc.) | `BOOK` slot exacto |
| 6 | Reglas seguro/edad/referral | `NO_ACTION(reason)` o redirect `BOOK` |
| 7 | Agenda llena | `BOOK` alternativa o `NO_ACTION(no_availability)` |
| 8 | Mover/cancelar citas | `CANCEL` / `RESCHEDULE` |
| 9 | Llama tercero (madre/hija/cuidador) | `BOOK` para el paciente |
| 10 | Síntomas → specialty; red flags | `BOOK` o `ESCALATE(medical_emergency)` |
| 11 | Español, catalán, etc. | `BOOK` con provider que hable el idioma |
| 12 | Ruido (SNR 5 dB) | `BOOK` (fallo = audio, no reasoning) |
| 13 | Correcciones, interrupciones | `BOOK` petición **final** |
| 14 | Injection, privacidad | `NO_ACTION(out_of_scope)` + transcript limpio |
| 15 | Sede más cercana (distancia recta) | `BOOK` sede correcta |
| 16 | Preguntas sobre la clínica | `BOOK` según info dada |
| 17 | Segundo plan no en record | `BOOK` con `policy_id` correcto |
| 18 | Multi-intent (abuela + nieto + ruido) | Multi-acción, todo correcto |

---

## 8. Prioridades de implementación (puntos/hora)

Orden sugerido por la documentación:

1. **Identificación** — 2º identificador, campos exactos de `/directory`, DNI completo con letra
2. **Refusals** — leer `blocked` de `/availability`, reason correcto
3. **Slot exacto** — minuto + timezone Europe/Madrid
4. **appointment_type_id** — del response de availability, no hardcode
5. **Concurrencia** — pipeline aislado por WebSocket
6. **Turn-taking** — interrupciones 100% vuestras

---

## 9. Scheduling guidelines (jurado, no leaderboard)

- Registrar antes de book (paciente desconocido)
- Leer chart antes de preguntar (nota + historial)
- Personal pero respetar lo que pide el caller (no bookar "su doctor habitual" si pidió "lo antes posible")
- Respetar reglas siempre
- Repartir carga entre proveedores cuando hay empate

---

## 10. Estado de nuestro repo

El agente está construido. Cómo, y qué resuelve cada pieza, está en
[ARCHITECTURE.md](./ARCHITECTURE.md); cómo arrancarlo, en [RUNBOOK.md](./RUNBOOK.md).

| Componente | Estado |
|---|---|
| WebSocket Twilio Media Streams (`/ws`) | ✅ `src/telephony` |
| Pipeline de voz propio (Deepgram → LLM → ElevenLabs) | ✅ `src/voice`, sin Pipecat |
| Cliente del EHR real | ✅ `src/platform_api/client.py` |
| Motor determinista (huecos, reglas, rechazos) | ✅ `src/domain` |
| Submits `/submit/*` con reintentos | ✅ dentro de la ventana de 30 s |
| Consola en vivo + histórico en SQLite | ✅ `src/obs`, `src/web` |
| Ensayo en texto y arnés de escenarios | ✅ `scripts/rehearse.py` |
| ngrok + endpoint registrado en el dashboard | ⚠️ por sesión, no vive en el repo |

### Variables de entorno

Todas están en [.env.example](./.env.example), que es la referencia buena. Las
que no se pueden adivinar:

```env
PLATFORM_API_KEY=pk-...          # del desk
PLATFORM_API_BASE_URL=https://hackspain.getprosperapp.com/api/v1
DEEPGRAM_API_KEY=                # sin ella no hay barge-in, se cae a Whisper
LLM_API_KEY=                     # sin ella no hay agente
LLM_BASE_URL=                    # el gateway, si se usa uno
LLM_EXTRA_HEADERS=               # p. ej. cf-aig-gateway-id: <id> en Cloudflare
ELEVENLABS_API_KEY=
```

### Qué falta

1. Repartir el `.env` al resto del equipo por un canal privado. El repo ya
   arranca en cualquier máquina (`run.ps1` / `run.sh`, versiones clavadas), y
   las claves son lo único que sigue viviendo en un solo portátil.
2. Escenarios de ensayo para el problema 12 (ruido), que necesita audio real.
3. Scored runs de los problemas que ya pasamos en rehearsal, con margen antes del cierre del domingo a las 06:00.

---

## 11. Links útiles

- Dashboard Prosper (Settings → Integration)
- API reference (ReDoc): `<BASE_URL>/api/docs` o ReDoc en el host del desk
- OpenAPI: `<BASE_URL>/api/openapi.json`
- Problems page → Call (práctica) / scored run (el problema que eliges)
- `public-cases.json` — casos publicados con respuestas

---

## 12. Checklist fin de semana

- [ ] API key + base URL del desk configuradas
- [ ] WebSocket server respondiendo en `wss://…/ws`
- [ ] ngrok estable (dominio fijo)
- [ ] Endpoint registrado en dashboard
- [ ] Practice call problema 1 pasa
- [ ] Submit book/no-action funciona dentro de ventana 30 s
- [ ] 1 scored call a la vez, sin mezclar estado si el Switchboard abre 10/20
- [ ] Switchboard 10/20 OK (readiness)
- [ ] Primer scored run de un problema que rehearsal ya pasa
- [ ] Demo para jurado (console, logs, replay)
