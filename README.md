# hackQueue

A Discord bot that tracks your community's progress across CTF/hacking platforms —
**Hack The Box**, **TryHackMe**, **Root-Me**, and **OffSec Proving Grounds** — and
runs server leaderboards that reward *this week's grind*, not account age.

> 🖼️ *screenshot: weekly composite leaderboard embed — placeholder*
> 🖼️ *GIF: /link → /profile → /leaderboard flow — placeholder*

## Features

- **Account linking** — `/link htb|thm|rootme <id-or-username>`, with optional
  ownership verification (`/verify`) via a token in your profile bio. One
  account per platform per Discord user; `/unlink` deletes everything the bot
  stored about that account.
- **Leaderboards** — `/leaderboard [board] [weekly|monthly|alltime]`:
  - per-platform boards (raw points, all-time),
  - **delta boards** (points gained this week/month — the default),
  - a **composite board** blending all platforms with configurable weights.
- **Manual claims** — `/solved pg <box> [proof screenshot]` for platforms with
  no API (Proving Grounds ships by default). Claims queue to a mod channel with
  Approve/Deny buttons and award configurable per-difficulty points. Adding
  VulnHub or PortSwigger labs is a few lines of TOML, zero code.
- **Box recommendations** — `/suggest [difficulty] [os] [tag]` recommends HTB
  boxes you haven't solved, and `/box <name>` shows an info card with the
  matching [IppSec](https://ippsec.rocks) walkthrough video when one exists.
- **Weekly recap** — optional Monday digest of the completed week: top
  gainers, new solves, first bloods, and a box of the week.
- **Profiles** — `/profile [@user]` shows all linked accounts, ranks, and
  recent solves.
- **Ops-friendly** — `/health` for admins, structured logging, per-platform
  rate limiting and backoff, and hard isolation: one platform's outage never
  breaks the others' boards (stale data is marked, not dropped).
- **Multi-guild** — one instance serves many servers; all moderation/recap/
  verification settings are per-guild (`/config`).

## Why Python?

Both discord.py and discord.js are healthy, mature options. hackQueue uses
**Python 3.11+ / discord.py 2.7** because its target contributor base — CTF
players — overwhelmingly lives in Python, and a community-maintained project
lives or dies by its contributors. SQLAlchemy 2 (async) gives SQLite by default
with a config-only path to Postgres.

## Self-hosting

### Tokens you need

| Variable | Required | Where to get it |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | [discord.com/developers/applications](https://discord.com/developers/applications) → your app → *Bot* → Reset Token. No privileged intents needed. |
| `HTB_APP_TOKEN` | for HTB | HTB → your profile → *Profile Settings* → **Create App Token**. ⚠ Tokens expire (≤ 1 year); `/health` shows when polling starts failing with auth errors. |
| `ROOTME_API_KEY` | for Root-Me | Log in at root-me.org → [Preferences](https://www.root-me.org/?page=preferences) → API key. |

TryHackMe needs no token. A platform whose credential is missing is simply
disabled — everything else keeps working.

When creating the Discord application, invite the bot with the
`bot` + `applications.commands` scopes (Send Messages + Embed Links permissions
are enough).

### Docker (recommended)

```bash
git clone https://github.com/b-3llum/hackQueue && cd hackQueue
cp .env.example .env   # fill in your tokens
docker compose up -d
```

The SQLite database lives in the `hackqueue-data` volume; migrations run
automatically at startup.

### Bare metal

```bash
git clone https://github.com/b-3llum/hackQueue && cd hackQueue
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in your tokens
hackqueue              # or: python -m hackqueue
```

Postgres instead of SQLite: `pip install -e '.[postgres]'` and set
`DATABASE_URL=postgresql+asyncpg://user:pass@host/hackqueue`.

### First-time server setup (Discord side)

1. `/config mod-channel #claims` and `/config mod-role @Mods` — enables `/solved` claims.
2. `/config recap-channel #general` — enables the Monday recap (optional).
3. `/config require-verified true` — hide unverified links from boards (optional).

## Configuration reference

Everything env-var-based is documented inline in [`.env.example`](.env.example)
(poll intervals, log level/format, database URL, catalog refresh cadence).

Scoring lives in [`scoring.toml`](scoring.toml):

```toml
[composite.weights]   # relative weight of each platform on the composite board
htb = 1.0
thm = 1.0
rootme = 1.0
claims = 1.0

[claims.pg]           # a manual-claim platform: key = what users type in /solved
name = "OffSec Proving Grounds"
[claims.pg.points]    # difficulty -> points awarded on approval
easy = 10
intermediate = 20
hard = 30
insane = 40
```

### How scoring works (the exact math)

1. The poller snapshots every linked profile on a per-platform interval
   (default 45–60 min, jittered).
2. A period **delta** = latest snapshot − the last snapshot taken at/before the
   period start (weekly = Monday 00:00 UTC, monthly = 1st 00:00 UTC), floored
   at 0. Members who link mid-period baseline at their first snapshot, so
   pre-existing points never count as gains.
3. The **composite** board normalizes each platform's deltas within the server
   to 0–100 (top gainer = 100), then takes the weighted average using the
   weights above. Approved manual claims participate as their own "claims"
   platform. A platform that was down all week contributes 0 for everyone —
   diluting scores rather than inflating whoever happened to lead elsewhere.

### TryHackMe reliability

THM has no official public API, and its unofficial endpoints sit behind
aggressive bot mitigation (Vercel Security Checkpoint) that intermittently
blocks non-browser clients entirely. hackQueue treats THM as **best-effort**:
when it's blocked, THM flips to *degraded* in `/health`, boards keep rendering
the last good data with a staleness marker, and polling backs off until the
challenge clears. If THM data matters a lot to your server, weight it
accordingly in `scoring.toml`.

### Verification per platform

| Platform | `/verify` support | Notes |
|---|---|---|
| Hack The Box | ✅ bio token | Put the issued token in your profile description. |
| TryHackMe | ❌ (planned) | Deferred until API access stabilizes. |
| Root-Me | ❌ not possible | The Root-Me API exposes no bio field and the profile page blocks non-browser clients, so there is nothing the bot can check. Root-Me links always show the ⚠ unverified marker. |

## Privacy

The bot stores:

- your Discord user ID and the platform IDs/usernames you link,
- point/rank snapshots and solve events for those accounts,
- any manual claims you submit via `/solved` (box name, difficulty, a link to
  the proof screenshot, and who reviewed it) — these are per-server records.

No message content, no member lists.

**Deleting your data:** `/unlink <platform>` immediately and permanently
deletes that link with all its snapshots and solve history. Manual claims are
server records reviewed by that server's moderators, so they're deleted by a
server admin: `/config purge-member` removes all your claims in that server
(and `/config unlink-member` covers links for departed members).

## Development

```bash
pip install -e '.[dev]'
pytest            # unit tests (scoring math, adapters against fixtures, …)
ruff check .      # lint
ruff format .     # format
```

Want to add a platform? See [CONTRIBUTING.md](CONTRIBUTING.md) — an adapter is
one file.

## License

[MIT](LICENSE)
