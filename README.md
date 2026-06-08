# radio-control

Brutalist operational panel that sits on top of AzuraCast at `radio.zad.tools`.

## What it does
- Multi-station overview (live listeners, on-air state)
- Media library browser with region filters (Egypt / Gulf / Levant / Maghreb / Global)
- Playlist creation and listing
- 7-day timeline visualization of scheduled blocks
- Live now-playing strip with public-player links
- Bilingual EN/AR with full RTL
- Dev console drawer (Ctrl+\) — request log, force refresh
- Server-side AzuraCast bearer token; the frontend never sees it

## Run locally
```bash
cp .env.example .env
# fill AZURACAST_API_KEY=<the bearer token from AzuraCast → Account → API Keys>
pip install -r requirements.txt
python app.py
```
Open http://localhost:4180

## Deploy (Coolify)
```bash
docker build -t radio-control .
docker run -d --name radio-control -p 4180:4180 \
  -e AZURACAST_BASE_URL=https://radio.zad.tools \
  -e AZURACAST_API_KEY=$AZ_KEY \
  radio-control
```
Add `radio-control.zad.tools` in Coolify Traefik labels.

## Stack
Flask · requests · Tailwind (CDN) · Alpine.js · HTMX · Lucide.
