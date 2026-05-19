# ChessWager API

A multiplayer chess platform where players can create accounts, play real-time chess matches, and invest small amounts (up to **₹100**) per match. Every move is validated server-side — cheating is actively detected and punished.

---

## Features

| Feature | Details |
|---|---|
| Account system | Register / Login with JWT auth |
| Wallet | Deposit & withdraw up to ₹100 total at any time |
| Micro-investments | Bet any amount (₹1–₹100) on a chess match |
| Real-time play | WebSocket endpoint for live move broadcasting |
| REST fallback | Full REST API for moves (no WebSocket required) |
| Anti-cheat | Server-side validation, timing analysis, illegal-move banning |
| Video & payouts | PvP: both players need usable recordings before admin can release winnings; vs CPU: human only |

---

## Anti-Cheat System

All of these protections run **on the server**; clients cannot bypass them.

1. **Server-side move validation** — every move is checked with `python-chess`. Illegal moves are rejected before being stored.
2. **Move timing analysis** — moves submitted in under 500 ms are flagged. Five consecutive ultra-fast moves trigger an automatic ban.
3. **Illegal-move-attempt tracking** — repeated illegal move submissions in one game can trigger an account ban (threshold configurable via `MAX_ILLEGAL_MOVE_ATTEMPTS_PER_GAME`, default 12). Legitimate lag or mis-taps should be far less likely to hit this than under the old fixed threshold.
4. **One session per player per game** — if a second WebSocket connection opens, the first is forcibly disconnected (prevents multi-tab engine use).
5. **Minimum move time** — configurable floor (default 500 ms); any move below it is logged as a suspicious event.

---

## Quick Start

```bash
# 1. Clone / enter the folder
cd D:\chess-api

# 2. Copy environment config
copy .env.example .env   # edit SECRET_KEY before production!

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server
python -m uvicorn main:app --reload --port 8000
```

Interactive API docs: **http://localhost:8000/docs**

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ENV` | `development` | Set to `production` on live servers — enables strict secret validation. |
| `SECRET_KEY` | random (dev) | JWT signing secret. **Must be ≥ 32 chars in production**, otherwise app refuses to boot. |
| `ALGORITHM` | `HS256` | JWT algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Token lifetime |
| `DATABASE_URL` | SQLite local | SQLAlchemy async DB URL. Accepts `postgres://`, `postgresql://`, or `postgresql+asyncpg://` — auto-normalised. |
| `DATABASE_SSL` | unset | Set to `require` to force SSL on managed Postgres providers. |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS allow-list. Set explicitly in production. |
| `ADMIN_SECRET` | dev value | Admin dashboard secret. **Must be ≥16 chars and non-default in production**. |
| `MAX_WALLET_BALANCE` | `100` | Hard cap on wallet balance |
| `MIN_BET` / `MAX_BET` | `10` / `100` | Stake bounds |
| `MIN_MOVE_TIME_MS` | `500` | Minimum milliseconds between moves |
| `UPI_ID` / `UPI_NAME` / `UPI_NOTE` | dev defaults | Your real UPI payee details — used for deposit QR generation. |

---

## Production deployment

The repo ships a `Procfile` and `render.yaml` so you can deploy on Render (or any Heroku-style platform) without further changes.

### Render (recommended)

1. Push the repo to GitHub.
2. In Render → New + Blueprint → point at the repo. Render reads `render.yaml`:
   - Creates a managed Postgres database (`chesswager-db`).
   - Wires `DATABASE_URL` into the web service automatically.
   - Auto-generates `SECRET_KEY`.
3. In the dashboard set the *manual* env vars: `ADMIN_SECRET`, `ALLOWED_ORIGINS`, `UPI_ID`, `UPI_NAME`.
4. First deploy will run `gunicorn -k uvicorn.workers.UvicornWorker main:app` with two workers.
5. Open `/health` → expect `{"status":"ok","env":"production"}`.

### Any other host

Required env vars: `ENV=production`, `SECRET_KEY` (≥32 chars), `ADMIN_SECRET` (≥16 chars, non-default), `DATABASE_URL` (Postgres recommended), `ALLOWED_ORIGINS`.

Start command:

```bash
gunicorn -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60 --keep-alive 30
```

### Database — Postgres

The async engine uses **asyncpg**. Any of these URL forms is accepted:

```
postgres://user:pass@host:5432/dbname
postgresql://user:pass@host:5432/dbname
postgresql+asyncpg://user:pass@host:5432/dbname
```

Tables are created on first boot via `create_all`. Subsequent column additions are applied by `_upgrade_schema_sync` (idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`). No Alembic migration is required for the current schema.

On Render's managed Postgres, set `DATABASE_SSL=require` (the bundled `render.yaml` already does this).

---

## API Reference

### Authentication

| Method | Path | Description |
|---|---|---|
| POST | `/auth/register` | Create account |
| POST | `/auth/login` | Login → JWT token |
| GET | `/auth/me` | My profile |

### Wallet

| Method | Path | Description |
|---|---|---|
| GET | `/wallet/balance` | Current balance & locked funds |
| POST | `/wallet/deposit` | Add funds (max ₹100 total) |
| POST | `/wallet/withdraw` | Withdraw available funds (JSON: `amount`, `destination_upi`) |
| GET | `/wallet/my-withdrawals` | Your UPI withdrawal queue |
| GET | `/wallet/transactions` | Transaction history |

### Games

| Method | Path | Description |
|---|---|---|
| GET | `/games` | List games (filter by `?status=waiting`) |
| POST | `/games` | Create game (as white), lock bet |
| GET | `/games/{id}` | Full game state + move history |
| POST | `/games/{id}/join` | Join as black, lock bet, game starts |
| POST | `/games/{id}/move` | Make a move (REST) |
| POST | `/games/{id}/resign` | Resign |
| WS | `/games/ws/{id}?token=JWT` | Real-time WebSocket |

### Users

| Method | Path | Description |
|---|---|---|
| GET | `/users/{id}` | Public player profile |
| GET | `/users/me/flags` | Your anti-cheat flags |

---

## WebSocket Protocol

Connect to `ws://localhost:8000/games/ws/{game_id}?token=<JWT>`

**Send:**
```json
{"type": "move",   "data": {"move": "e2e4", "client_timestamp": 1700000000000}}
{"type": "resign"}
{"type": "ping"}
```

**Receive:**
```json
{"type": "connected",    "data": {"game_id": 1, "fen": "...", "status": "active"}}
{"type": "move",         "data": {"move_san": "e4", "fen": "...", "game_over": false}}
{"type": "game_over",    "data": {"result": "white", "reason": "checkmate_or_draw"}}
{"type": "game_started", "data": {"black_player_id": 2}}
{"type": "error",        "data": {"message": "Illegal move: 'e2e5'."}}
{"type": "kicked",       "data": {"reason": "New session opened from another location."}}
{"type": "pong"}
```

---

## Wallet Rules

- Maximum wallet balance: **₹100**
- Bets are **locked** when a game is created/joined and released when it ends
- Winner receives **2× the bet amount**
- On a **draw**, both players are **refunded** their bet
- Funds at stake in active games cannot be withdrawn

---

## Payout & video verification

- Winnings may sit in **pending payout** until an admin reviews and approves release.
- **Multiplayer (human vs human):** the server requires **usable webcam chunks from both players** (each ≥ 1 KB on disk) before `/admin/payouts/{id}/approve` will succeed.
- **Vs CPU:** only the **human** player must have usable chunks.
- **UPI cash-out:** Players submit a **Google Pay / UPI ID** with each withdrawal. Ops see the destination in **Admin → Payouts → Wallet → UPI withdrawals** (`GET /admin/withdrawals`) and mark paid or reject (refund).

## Project Structure

```
D:\chess-api\
├── main.py              # FastAPI app + CORS + lifespan
├── config.py            # Environment config
├── database.py          # Async SQLAlchemy engine + session
├── models.py            # ORM models (User, Wallet, Game, Move, …)
├── schemas.py           # Pydantic request/response models
├── auth.py              # JWT creation, verification, dependencies
├── anticheat.py         # Anti-cheat detection engine
├── video_evidence.py    # Payout rules: both sides must have video (PvP)
├── websocket_manager.py # WebSocket connection manager
├── requirements.txt
├── .env.example
└── routers/
    ├── auth_router.py   # /auth/*
    ├── users_router.py  # /users/*
    ├── wallet_router.py # /wallet/*
    └── games_router.py  # /games/* + WebSocket
```
