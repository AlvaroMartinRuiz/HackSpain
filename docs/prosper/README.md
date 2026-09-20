# Prosper track — official docs (local copy)

Everything here was pulled from the official platform on 2026-09-18 so we can
read it offline and grep it, then patched on 2026-09-19 when scored runs
became **one problem, one call**. **The live platform is the source of truth**
— if something here disagrees with it, the platform wins.

Host: `https://hackspain.getprosperapp.com` (note: **not** `voice.getprosperapp.com`,
which returns 403). All `/api/v1/*` routes need the team key in `X-Api-Key`.

## The docs

| File | What it covers |
|---|---|
| `overview.md` | One-page summary of the whole track ("El Turno") |
| `challenge.md` | Who Prosper is, what we're building, how it's won |
| `quickstart.md` | Account → key → tunnel → endpoint → first judged call |
| `contract.md` | **The wire protocol, the 30s submission window, the 6 submit routes.** Read this first. |
| `clinic-api.md` | The read-only EHR, the rules it doesn't expose, scheduling guidelines |
| `rules.md` | Scoring: what passes a case, points, limits, call failure attribution |
| `problems.md` | All 18 problems with weights and what decides each one |
| `scoring-normalization.md` | The scorer's own normalization table |
| `api.md` | Pointer to the generated API reference |

## The data

| File | What it is |
|---|---|
| `openapi.json` | Full API spec — 17 endpoints, all schemas and enums |
| `public-cases.json` | **73 published practice cases** with personas, objectives and expected answers |
| `clinic-snapshot.json` | `GET /api/v1/clinic` response: 12 providers, 6 specialties, 3 sites, 10 plans, 11 restrictions, calendar window |

The clinic data is generated once and is identical for the whole event, for
every team and every call — so `clinic-snapshot.json` will not go stale. Cache
it freely.

## Live versions

- Swagger UI: `https://hackspain.getprosperapp.com/api/docs`
- ReDoc: `https://hackspain.getprosperapp.com/api/redoc`
- Raw spec: `https://hackspain.getprosperapp.com/api/openapi.json`
- Dashboard + docs: `https://hackspain.getprosperapp.com/leaderboard/docs`
