# Radio Control — API v1

All endpoints under `/api/v1/` proxy to AzuraCast using a server-held bearer token. The frontend never sees the AzuraCast API key.

Standard envelope:

```json
{ "ok": true, "data": ..., "error": null, "meta": {} }
```

Errors:

```json
{ "ok": false, "data": null, "error": "message", "code": "ERROR_CODE" }
```

## Endpoints

### `GET /api/v1/config`
Returns the configured AzuraCast base URL and whether an API key is loaded.

### `GET /api/v1/stations`
Lists all stations (proxies `GET /api/admin/stations`).

### `POST /api/v1/stations`
Body: `{ "name": "Studio One", "short_name": "studio_one", "description": "...", "max_listeners": 250 }`. Creates a station with Icecast + Liquidsoap defaults.

### `GET /api/v1/stations/{id}/files`
Lists media files for the station.

### `POST /api/v1/stations/{id}/upload`
Multipart `file` field. Uploads a file to the station's media library.

### `GET /api/v1/stations/{id}/playlists`
Lists playlists.

### `POST /api/v1/stations/{id}/playlists`
Body: `{ "name": "Gulf Hits", "type": "default", "weight": 3 }`.

### `GET /api/v1/nowplaying`
Public AzuraCast nowplaying snapshot for all stations.

### `GET /healthz`
Liveness probe.

## Show personas (agents)

The 4 station × 4 show personas (`system_prompt` + `user_prompt_template` + times)
live as **editable files** under `agents/<station>/<show>.md` — not hardcoded. They are
rebuilt at request time, so editing a file takes effect on the next call with no restart.

### `GET /api/v1/templates`
Returns the 4 station templates with their shows — **built from the persona files**.
Each show: `description, start_time, end_time, system_prompt, user_prompt_template,
model, temperature`. Consumed by the station-creation flow and by `/generate-script`.

### `GET /api/v1/agents`
Flat list of all 16 personas: `{ station, station_name, show, description, start_time,
end_time, model, temperature }`.

### `GET /api/v1/agents/{station}/{show}`
Raw file content of one persona (frontmatter + system_prompt body), for an editor.

### `PUT /api/v1/agents/{station}/{show}`
Body: `{ "content": "---\n...frontmatter...\n---\nsystem prompt" }`. Validates the
frontmatter, then writes the file. Takes effect immediately (hot reload).
Errors: `BAD_NAME`, `EMPTY`, `BAD_FRONTMATTER`.

### `POST /api/v1/generate-script`
Body: `{ "show": { ...one show object from /templates... }, "date": "2026-06-08" }`.
Generates the show script via FreeLLM → OpenAI fallback, honoring the show's
`temperature` and optional `model` override.

> **Persistence:** personas + `broadcasts.yaml` are files under `agents/`. To keep
> UI/API edits across redeploys, mount a Coolify **persistent volume at `/app/agents`**.
> `docker-entrypoint.sh` seeds a *fresh* volume from the image's `agents_seed` on first
> run (so an empty volume doesn't start blank) and never overwrites an initialized one.

## Telegram broadcasts (to followers)

Flexible, event-configured Telegram posting. Rules live in the editable file
`agents/broadcasts.yaml`. Dormant until `TELEGRAM_BOT_TOKEN` is set.

A rule's `trigger` is either `show_start` (auto-posts when that show's `start_time`
hits — needs the scheduler + token) or `manual` (only fires on demand). Template
placeholders: `{script} {description} {station_name} {station} {show} {date}`.

### `GET /api/v1/telegram/status`
`{ configured, default_chat, rules, enabled_rules }`.

### `GET /api/v1/telegram/rules` · `PUT /api/v1/telegram/rules`
Read / replace `agents/broadcasts.yaml`. PUT body: `{ "content": "<yaml>" }` (validated).

### `POST /api/v1/telegram/send`
Manual post. Body: `{ "text": "..." }` for raw text, **or**
`{ "station": "islamic", "show": "morning", "date": "2026-06-08" }` to generate the
show script and post it. Optional `chat_id` override.

### `POST /api/v1/telegram/rules/{id}/fire`
Fire one rule now (test button). `?date=YYYY-MM-DD` optional.

Env: `TELEGRAM_BOT_TOKEN` (from @BotFather), `TELEGRAM_CHAT_ID` (default channel, e.g.
`@my_channel` or `-1001234567890`), `TZ` (e.g. `Africa/Cairo` — when `show_start` fires).
`show_start` posts are de-duplicated across the 2 gunicorn workers via an atomic file
claim under `agents/.broadcast_state/`.

## Auth
None client-side. AzuraCast bearer token is loaded from `AZURACAST_API_KEY` env var. Add HTTP auth in front (Coolify, Traefik, oauth2-proxy) when exposing publicly.
