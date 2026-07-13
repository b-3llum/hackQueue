# hackQueue

A Discord bot that tracks your community's progress across CTF/hacking platforms —
**Hack The Box**, **TryHackMe**, **Root-Me**, and **OffSec Proving Grounds** — and
runs server leaderboards that reward *this week's grind*, not account age.

![The web leaderboard](docs/img/web-leaderboard.png)

*The composite board. Each bar is stacked by platform, so you can see at a
glance where someone's points came from — n0ctis leads on Proving Grounds
claims (violet), ghostbyte on Hack The Box (amber).*

> 🖼️ *screenshot: Discord `/leaderboard` embed — placeholder*
> 🖼️ *GIF: `/link` → `/profile` → `/leaderboard` flow — placeholder*

## Features

- **Account linking** — `/link htb|thm|rootme <id-or-username>`, with optional
  ownership verification (`/verify` — see the table below for how each platform
  proves it). One account per platform per Discord user; `/unlink` deletes
  everything the bot stored about that account.
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
  matching [IppSec](https://ippsec.rocks) walkthrough video when one exists
  (418 of 544 machines have one at the time of writing). Note: HTB no longer
  exposes topic tags via its API, so `tag` filters on HTB's labels
  (`SEASONAL`, `NEW`, …) rather than techniques.
- **Weekly recap** — optional Monday digest of the completed week: top
  gainers, new solves, first bloods, and a box of the week.
- **Profiles** — `/profile [@user]` shows all linked accounts, ranks, and
  recent solves.
- **Web leaderboard** — `/config web on` publishes the server's board as a
  page anyone can open (opt-in, per server). Light/dark, mobile-friendly, and
  it shows platform contributions as a stacked bar.
- **One-command setup** — `/setup` creates the channels the bot needs
  (`#leaderboard` for recaps, a moderators-only `#claim-review`) and wires the
  config up in one go.
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
`bot` + `applications.commands` scopes. Permissions: **Send Messages**,
**Embed Links** and **Attach Files** (for claim proof screenshots), plus
**Manage Channels** if you want `/setup` to create the channels for you.

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

Run **`/setup`** — it creates a `hackQueue` category with `#leaderboard`
(recaps and announcements) and a moderators-only `#claim-review` (claim
approvals), and points the config at them. Pass a role (`/setup mod_role:@Mods`)
to say who may approve claims; otherwise anyone with **Manage Server** can.
The bot needs **Manage Channels** for this; if you'd rather make the channels
yourself, use `/config mod-channel` and `/config recap-channel` instead.

Optional extras:

- `/config web on` — publish the leaderboard as a web page (requires
  `WEB_ENABLED=true` on the instance; the bot replies with your link).
- `/config require-verified true` — hide unverified links from boards.
- `/config show` — review everything.

### The web leaderboard

Set `WEB_ENABLED=true` (and `WEB_BASE_URL` to whatever the outside world
sees — a domain, if you put a reverse proxy in front). The bot then serves
`/g/<server id>` on `WEB_PORT`. **Nothing is published until a moderator runs
`/config web on` in that server**, and `/config web off` takes it straight
back down.

| | |
|---|---|
| ![light](docs/img/web-leaderboard-light.png) | ![mobile](docs/img/web-leaderboard-mobile.png) |

#### Putting it on a public URL, for free

The bot serves the board itself, so it only needs a way out to the internet.
A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
gives you free HTTPS with **no port forwarding and no router config** — your
box makes an outbound connection, and Cloudflare hands you a hostname.

Quick tunnel (throwaway URL, good for trying it out):

```bash
cloudflared tunnel --url http://localhost:8080
# → https://<random-words>.trycloudflare.com
```

Then set `WEB_BASE_URL` to that URL and restart the bot, so `/config web`
hands members the right link. Quick-tunnel URLs change on every restart; for a
stable one, create a named tunnel in the Cloudflare dashboard (Zero Trust →
Networks → Tunnels), point it at `http://localhost:8080`, put its token in
`TUNNEL_TOKEN`, and run:

```bash
docker compose --profile tunnel up -d
```

Data stays in your database — nothing is copied to a third party, and
`/config web off` takes a board down immediately.

> **Why not GitHub Pages?** Pages only serves static files: it can't run the
> bot or hold the database, so it would need the bot to export a JSON bundle
> and push it on a schedule. That works, but every push writes members'
> Discord display names and avatars into public git history *permanently* —
> a bad trade for a board that's about this week's activity. A tunnel keeps
> the data live and deletable.

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

### What each platform's "points" mean

| Platform | Score | Why |
|---|---|---|
| Hack The Box | **flags captured** — machine user/root owns + solved challenges + Pro Lab and Fortress flags | HTB *removes* a machine's points when it retires, so its own `points` field reads 0 for anyone grinding retired boxes or Pro Labs (verified: an account with 8 owns and a completed Dante reports 0). Flags are what HTB actually counts, need no invented weights, and let Pro Lab progress show up at all. |
| TryHackMe | THM points | THM's own metric, as reported by its API. |
| Root-Me | Root-Me score | Root-Me's own metric. |
| Claims | configured per-difficulty points | From `scoring.toml`. |

Scales differ per platform, which is fine: the composite board normalizes each
platform within the server before weighting, so nobody's HTB flags are compared
to somebody's Root-Me score directly.

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
| Hack The Box | ✅ social-link token | HTB profiles have no bio field, so `/verify htb` asks you to paste the issued token into any **social link** (Twitter/X, GitHub, LinkedIn, CV) in HTB → Profile Settings — a URL containing it works, e.g. `https://x.com/hq-ab12cd34`. Restore your real link once verified. |
| TryHackMe | ❌ (planned) | Deferred until API access stabilizes. |
| Root-Me | ❌ not possible | The Root-Me API exposes no bio field and the profile page blocks non-browser clients, so there is nothing the bot can check. Root-Me links always show the ⚠ unverified marker. |

**Unverifiable is not the same as unverified.** On platforms where
verification is impossible (Root-Me, TryHackMe), links are shown plainly — no
⚠ marker, and `/config require-verified true` does not hide them, since no
link there could ever satisfy it. The ⚠ only appears where a member *could*
verify and hasn't (today: Hack The Box).

## Privacy

The bot stores:

- your Discord user ID and the platform IDs/usernames you link,
- point/rank snapshots and solve events for those accounts,
- any manual claims you submit via `/solved` (box name, difficulty, a link to
  the proof screenshot, and who reviewed it) — these are per-server records,
- your Discord display name and avatar URL, cached only so the web board can
  show who's who (the bot runs without privileged intents, so it can't read
  member lists — it only ever caches people who interact with it).

No message content, no member lists.

If a server turns the web board on, participating members' display names,
avatars and scores are visible to anyone with the link. It's off by default,
any moderator can take it down with `/config web off`, and `/unlink` removes
you from it entirely.

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
