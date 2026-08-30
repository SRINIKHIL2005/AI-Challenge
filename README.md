# magicpin AI Challenge Bot

Deterministic, zero-dependency HTTP bot for the magicpin AI Challenge. The service exposes the judge-facing endpoints required by the challenge and is packaged for simple Docker-based deployment.

## What This Bot Does

The bot accepts four kinds of context from the judge and stores them in memory:

- `category` context for the business vertical
- `merchant` context for the specific merchant
- `trigger` context for the current event
- `customer` context for customer-facing outreach

When the judge calls `POST /v1/tick`, the bot selects the highest-priority active trigger, builds a category-aware message, and returns an action for the judge to consume. When the judge calls `POST /v1/reply`, the bot reads the simulated merchant or customer response and decides whether to `send`, `wait`, or `end` the conversation.

The current implementation is rule-based and deterministic, which makes it easy to test and deploy reliably.

## Features

- Required judge endpoints implemented
- In-memory context storage
- Deterministic trigger prioritization
- Category-aware copy for dentists, salons, restaurants, gyms, and pharmacies
- Docker support for easy hosting on free tiers

## API Surface

The bot exposes these endpoints:

- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`

## How It Works

1. The judge pushes context with `POST /v1/context`.
2. The bot stores the latest version of each context object in memory.
3. The judge wakes the bot with `POST /v1/tick` and passes active trigger IDs.
4. The bot picks the strongest trigger, composes a message, and returns an action.
5. The judge sends a simulated merchant/customer response to `POST /v1/reply`.
6. The bot decides whether to continue, wait, or end the conversation.

## Local Run

Run directly with Python:

```bash
python bot.py
```

The server listens on `0.0.0.0:8000` by default.
        
## Docker Run

Build and run the container locally:

```bash
docker build -t vera-bot .
docker run -p 8080:8080 -e PORT=8080 vera-bot
```

## Quick Smoke Test

Check liveness:

```bash
curl http://127.0.0.1:8000/v1/healthz
```

Check metadata:

```bash
curl http://127.0.0.1:8000/v1/metadata
```

Minimal context push:

```bash
curl -X POST http://127.0.0.1:8000/v1/context \
  -H "Content-Type: application/json" \
  -d '{"scope":"category","context_id":"dentists","version":1,"payload":{"slug":"dentists"},"delivered_at":"2026-08-27T00:00:00Z"}'
```

Trigger a tick:

```bash
curl -X POST http://127.0.0.1:8000/v1/tick \
  -H "Content-Type: application/json" \
  -d '{"now":"2026-08-27T00:00:00Z","available_triggers":["trg_demo"]}'
```

## Free Deployment Guide

Use a free Docker-friendly host such as Render or Railway.

### Render

1. Push this repository to GitHub.
2. Create a new Web Service on Render.
3. Connect the GitHub repo.
4. Choose `Docker` as the environment.
5. Keep the container port at `8080`.
6. Deploy.

Render will give you a public HTTPS base URL, for example:

```text
https://your-service.onrender.com
```

That base URL is the submission URL for the challenge.

### Railway

1. Push the repo to GitHub.
2. Create a new Railway project from the repo.
3. Deploy using the Dockerfile.
4. Set the service port to `8080` if prompted.
5. Copy the generated HTTPS URL and submit it.

## Environment Variables

The bot works without extra configuration, but these optional variables are supported:

- `PORT` - listening port, defaults to `8080`
- `HOST` - bind address, defaults to `0.0.0.0`
- `TEAM_NAME`
- `TEAM_MEMBERS`
- `MODEL_NAME`
- `APPROACH`
- `CONTACT_EMAIL`
- `APP_VERSION`
- `SUBMITTED_AT`

## Repository Layout

- [bot.py](bot.py) - HTTP server and composition logic
- [Dockerfile](Dockerfile) - container build for deployment
- [.dockerignore](.dockerignore) - excludes local and generated files from Docker build

## Notes

- No `.bat` or `.cmd` files are needed for this deployment.
- Context is stored in memory, so the service should remain running during the judge session.
- For production-like hosting, do not restart the container during evaluation.

## Suggested Next Step

If you want, the next useful step is to deploy this repo on Render and use the generated HTTPS URL as the submission URL.