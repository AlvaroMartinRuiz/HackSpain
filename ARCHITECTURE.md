# Socket Wizard architecture

V2 is the sole runtime. Domain rules and utilities have been migrated into its package; there is no dependency on the removed v1 application.

```text
Carrier / browser audio
  -> authenticated FastAPI WebSocket
  -> Pipecat transport and Deepgram recognition
  -> caller-turn aggregation
  -> LangGraph CallController
       -> Vercel model: bounded, typed interpretation
       -> ClinicBoundary: identity, availability, validated offers
            -> domain rules + Prosper read API
       -> Dispatcher: idempotent action receipts
  -> deterministic response text
  -> Cartesia (English/Spanish) or ElevenLabs (Catalan)
  -> paced audio on the caller socket

Events/state/receipts/budget -> SQLite -> operator API -> dashboard
Successful socket audio -> local WAV files -> authenticated playback
Synthetic conversation -> optional Jev service -> model assessment
```

## Conversation and business boundaries

`CallState` owns an independent intent for each request and patient. An intent collects identity and criteria, prepares offers, waits for confirmation, and completes only after an accepted action receipt. Offer revisions invalidate outdated confirmations. Presentation and explicit acceptance are separate from model interpretation.

The model does not construct submission payloads or decide the server's execution mode. The scheduling engine resolves spoken times in Europe/Madrid and uses identifiers, appointment types, insurance coverage, and availability from the clinic. Emergency handling can bypass model interpretation.

`CallController` runs its interpret/apply graph per turn. Epochs reject obsolete responses after interruptions. An in-flight action is settled rather than blindly cancelled and retried. Per-call state is not shared between sockets.

## External providers

- Prosper supplies the read-only clinic and accepts reported appointment outcomes. No Twilio account is required: Prosper speaks Twilio's Media Streams protocol.
- Deepgram Nova-3 transcribes streaming speech.
- Cartesia synthesizes English/Spanish stock voices; ElevenLabs supplies Catalan speech.
- Vercel AI Gateway interprets caller turns and can power a synthetic caller in bounded evaluations.
- Jev is a separate authenticated TypeScript service using the AI SDK evaluation interface. Its fixed rubric checks unanswered requests, repeated questions, and premature success claims. It is not a conversation generator or an official grader.
- Quiver is an optional offline asset-generation tool. The operator interface serves committed SVG files without making Quiver requests during calls.

## Storage and evidence

`RunStore` persists state, ordered events, action receipts, and budget reservations in `v2/.data/runs.db`. Recordings live under `v2/.data/audio/`. The code/catalog fingerprint and provider manifest distinguish executions. Runtime data and credentials are excluded from Git and container builds.

Simulated receipts, HTTP acknowledgements, deterministic fixture grades, and model assessments are not interchangeable. Official grade remains unknown unless authoritative evidence is imported. A successful audio send does not prove audible caller playback.

The local ledger has a $30 maximum reservation cap. It is not provider-enforced and cannot see teammates' unrelated account spending. Unknown charges remain reserved until reconciliation.

## Release boundary

Simulation is the default. Practice uses clinic reads but still records actions locally. Paid execution needs explicit configuration. Live operation requires provider/carrier acceptance and explicit approval; the current integration evidence is maintained in `docs/V2-STATUS.md`. This implementation effort is code delivery, not permission to deploy or change a remote endpoint.
