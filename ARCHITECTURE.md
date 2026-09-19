# El Turno — cómo está construido

El reto dice, con todas las letras, que el modelo de voz es *un componente* de un
sistema que diseñas tú, no el sistema. Esa frase es la que decide esta
arquitectura: **el modelo conduce la conversación, pero no decide nada que acabe
en el registro.**

Ningún id, ningún minuto, ningún tipo de cita, ninguna póliza y ningún motivo de
rechazo sale de lo que el modelo "cree". Todos salen de lo que la clínica ha
devuelto, pasando por código determinista.

```
        el harness llama
               │
               ▼
   ┌───────────────────────────┐
   │  /ws  Twilio Media Streams│  src/telephony/twilio_ws.py
   │  una sesión por socket    │
   └─────────────┬─────────────┘
                 ▼
   ┌───────────────────────────┐      ┌──────────────────────────┐
   │      CallSession          │─────▶│  consola en directo      │
   │  audio, turnos, submits   │      │  src/obs + src/web       │
   └──┬────────────────┬───────┘      └──────────────────────────┘
      │                │
      ▼                ▼
  ┌────────┐      ┌──────────────┐
  │ voz    │      │  Agent       │  bucle de turnos con herramientas
  │ STT/TTS│      │ src/agent    │
  └────────┘      └──────┬───────┘
                         │ sólo herramientas, nunca datos inventados
                         ▼
              ┌────────────────────────┐
              │  SchedulingEngine      │  núcleo determinista
              │  src/domain            │
              └──────────┬─────────────┘
                         ▼
              ┌────────────────────────┐
              │  API de la clínica     │  lectura + submits
              │  src/platform_api      │
              └────────────────────────┘
```

## La regla que ordena todo lo demás

El modelo elige **entre opciones numeradas** que le da el motor. Cuando dice
"reserva la dos", el motor coge el hueco número dos —el que la API devolvió— y
construye el payload con los ids exactos de ese hueco. El modelo nunca escribe
una fecha, un `provider_id` ni un `appointment_type_id`.

Por eso las trampas del reto no dependen de que el modelo "se acuerde":

| Trampa del reto | Quién la resuelve |
| --- | --- |
| El tipo de cita sigue al historial, no a la petición | `availability` lo devuelve; el motor lo copia tal cual |
| El minuto exacto con offset de Europe/Madrid | `timeref.format_slot` |
| Nunca el mismo día de la llamada | `engine._apply_floor` |
| "El jueves que viene", "a primera hora del lunes 12" | `timeref.resolve_when` |
| Domingos, sábados sólo en Centro, 12 de octubre cerrado | `catalog.is_open` + `next_open_day` |
| El motivo de rechazo debe nombrar la regla | `outcomes.pick_blocking_reason` desde `blocked` |
| La letra del DNI se recalcula desde los dígitos | `identity.parse_national_id` |
| Sáez / Sáenz, Iglesias / Iglesia | `catalog.find_providers_by_name` devuelve las dos |
| La sede más cercana que *pueda* atender | `engine.nearest_site` + `gazetteer` |
| Repartir carga entre médicos | desempate por diario más libre en `engine._pick` |
| Segunda póliza que hay que preguntar | `find_appointments(also_consider_insurer=…)` |

## Las tres garantías que evitan un cero

1. **Nunca una llamada muda.** El silencio siempre está mal, así que
   `CallSession.finalize` envía un `NO_ACTION` con el motivo más plausible si la
   llamada termina sin registro. Un agente que se cae no puntúa igual que uno
   que rechaza bien, pero al menos no puntúa como uno que calló.
2. **Se envía en cuanto se decide, no al colgar.** Llegar temprano nunca es
   motivo de rechazo; llegar tarde sí. La ventana se cierra 30 s después de que
   cierre el socket, y no se apura.
3. **Nada compartido entre sockets.** Cada conexión crea su propio
   transcriptor, su propia voz, su propia conversación y su propio cliente HTTP.
   Es el error que este reto busca.

## Barge-in

Deepgram manda resultados parciales. El primer parcial con contenido suficiente
mientras el agente habla corta la reproducción: se vacían las dos colas, se sube
un contador de generación (lo que hace que el audio en vuelo se descarte), se
manda `clear` por el cable y **se recorta el último turno del agente a lo que el
paciente realmente llegó a oír**, proporcional a los frames enviados. Sin ese
recorte el modelo cree haber dicho cosas que nadie escuchó.

Un parcial de menos de siete caracteres no interrumpe, y tampoco los primeros
350 ms de habla del agente: con ruido a 5 dB de SNR (problema 12) un gate más
sensible corta la llamada cada dos frases.

## La consola

La mitad del reto que no tiene solucionario. En `http://localhost:7860/`:

- llamadas en curso con su estado, y el histórico
- transcripción en directo, con los cortes marcados donde ocurrieron
- **"por qué dijo eso"**: cada decisión con su traza — qué ventana de fechas se
  pidió, cuántos huecos volvieron, qué regla bloqueó, por qué se eligió ese hueco
- cada consulta al EHR con sus parámetros, su resumen y su latencia
- el registro enviado, con el payload exacto y el código HTTP
- latencia de respuesta p50/p90, concurrencia máxima, llamadas sin registro

Todo se escribe también en SQLite (`data/calls.db`), así que una llamada se
puede volver a explicar mucho después de que el socket se cerrara:
`GET /api/console/calls/{id}/replay`.

## El ensayo, que es donde de verdad se itera

`POST /api/console/rehearse` corre una llamada completa **en texto**: el mismo
cerebro, las mismas herramientas, la misma clínica, sin audio y sin cuota. Con
`dry_run` el registro se queda en local, así que se puede correr sin límite.

`scripts/rehearse.py` lleva escenarios con la forma de los problemas puntuados y
comprueba el registro campo a campo, construyéndolos sobre pacientes reales
sacados del directorio en el momento. Un caso puntuado no te dice qué campo
perdiste hasta el lunes; esto sí.

## Qué falta y se sabe

- Hay una llamada con ruido de fondo real que no se ha probado todavía: el gate
  de barge-in está ajustado a mano, no medido contra las cuatro texturas.
- El problema 16 (el paciente actúa según lo que le digas) depende de que el
  modelo use `clinic_facts` en vez de recordar: está en el prompt y en la
  herramienta, pero no hay escenario que lo verifique aún.
- El gazetteer resuelve municipios y distritos, no calles. Las coordenadas
  publicadas dan margen de sobra, pero una dirección de un pueblo pequeño que no
  esté en la lista se queda sin resolver y el agente pregunta.
