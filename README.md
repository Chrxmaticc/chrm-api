# chrm-api

FastAPI + Postgres (Neon) service that sits between the dashboard and the engine.
It stores one Discord token per user plus the settings blob the engine polls for.

## Environment

| variable | required | purpose |
|---|---|---|
| `DATABASE_URL` | yes | Postgres DSN; the `users` table is created on boot |
| `ENGINE_SECRET` | yes | engine calls `GET /api/engine/bots` with this in `x-engine-secret` |
| `DASHBOARD_SECRET` | no | key used to sign dashboard keys (defaults to `ENGINE_SECRET`) |
| `DASHBOARD_KEY_TTL` | no | dashboard key lifetime in seconds (default 30 days) |
| `IMAGE_WEBHOOK_URL` | no | Discord webhook that RPC images are uploaded to |
| `PORT` | no | listen port, default `8080` |

## Endpoints

| method | path | auth |
|---|---|---|
| `POST` | `/api/auth/token` | none — this is the login, it mints the key |
| `GET` | `/api/user/{discord_id}` | `X-Dashboard-Key` or `x-engine-secret` |
| `PATCH` | `/api/user/{discord_id}` | `X-Dashboard-Key` or `x-engine-secret` |
| `POST` | `/api/rpc/{discord_id}` | `X-Dashboard-Key` or `x-engine-secret` |
| `DELETE` | `/api/auth/logout/{discord_id}` | `X-Dashboard-Key` or `x-engine-secret` |
| `GET` | `/api/engine/bots` | `x-engine-secret` |
| `GET`/`HEAD` | `/` | none (health check) |

### Login and dashboard keys

`POST /api/auth/token` sends the token straight to Discord (`GET /users/@me`) and
takes the account id from Discord's answer, never from the request body. A body
`discord_id` is only used as a cross-check and mismatches are rejected with 400.

The response carries `dashboard_key`: an HMAC-signed, expiring value bound to that
id, which the browser stores and replays in `X-Dashboard-Key`. Without it any
visitor could read, toggle or overwrite another user's row. The engine authenticates
the same endpoints with its shared `ENGINE_SECRET` instead, so cog-side saves
(`core/api.patch_user`) keep working.

### Settings blob

`users.settings` is a plain JSON blob that the engine polls and merges into its own
config. The contract is defined by `core/config.py::Config.apply_blob` in the bot
repo: keys named `settings` / `features` / `emojis` are merged into that namespace,
the feature keys (`afk`, `autorespond`, `autoreact`, `custom_status`) into `features`,
and everything else (`prefixes`, `delays_enabled`, `delay_min`, `delay_max`, `rpc`)
into `settings`. PATCH shallow-merges one level, so send only the keys you own.

## Run

```bash
pip install -r requirements.txt
DATABASE_URL=postgres://... ENGINE_SECRET=... python main.py
```
