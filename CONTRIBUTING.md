# Contributing to hackQueue

Thanks for helping! PRs are welcome — bug fixes, new platform adapters, docs.

## Dev setup

```bash
git clone https://github.com/b-3llum/hackQueue && cd hackQueue
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest && ruff check . && ruff format --check .
```

CI runs exactly those three commands on every PR — green locally means green in CI.

## Ground rules

- **Never commit secrets.** Tokens live in `.env` (gitignored). If you add a
  new credential, document it in `.env.example` and the README token table.
- Slash commands only; no privileged intents.
- All user-visible strings live in cogs/`ui/embeds.py` (Discord) and
  `web/static/` (the web board); all math lives in `services/scoring.py`
  (pure functions — add tests there for any behavior change).
- The web board is plain aiohttp + vanilla JS/CSS with **no build step and no
  CDN**: self-hosters shouldn't need node, and the page must work offline.
- A platform being down must never break another platform's boards.

## How to add a platform adapter

An adapter is **one file** in `hackqueue/adapters/` plus one registry entry.

1. **Create `hackqueue/adapters/yourplatform.py`** implementing
   `PlatformAdapter` (see `base.py` for the contract, `rootme.py` for a
   compact example):

   ```python
   class YourAdapter(PlatformAdapter):
       platform = Platform.YOURPLATFORM       # add it to the Platform enum
       supports_verification = False          # True if a public bio is readable

       async def resolve_user(self, user_ref): ...   # /link-time validation
       async def get_profile(self, user): ...        # -> ProfileStats
       async def get_recent_solves(self, user): ...  # -> list[SolveEvent], best-effort
   ```

   Rules that keep adapters uniform:
   - **All URLs in a constants block** at the top of the file — unofficial APIs
     drift and get patched in one place.
   - **Normalize at the edge**: whatever weird shapes the API returns
     (strings-as-numbers, array wrappers), convert them in the adapter; the
     rest of the bot only sees `ProfileStats`/`SolveEvent`.
   - **Map every failure** to the `AdapterError` family (`ProfileNotFound`,
     `ProfilePrivate`, `AuthExpired`, `RateLimited`, `PlatformUnavailable`).
     The poller uses these to isolate failures and mark health.
   - Use the shared `HttpClient` (identifiable User-Agent, backoff, per-host
     spacing). If the API needs pacing, add its host to `HOST_MIN_INTERVALS`
     in `registry.py`.
   - If one request serves both profile and solves, override `poll()` to avoid
     a second round-trip (see `rootme.py`).

2. **Register it** in `adapters/registry.py` (gate on its credential if it
   needs one) and add the `Platform` enum member + label in `adapters/base.py`.

3. **Add tests** in `tests/test_yourplatform_adapter.py` using `aioresponses`
   fixtures captured from *real* responses — live-verify the endpoints before
   hardcoding shapes, and note the verification date in the module docstring.

4. **Docs**: extend the README token table and `.env.example` if a credential
   is needed; add a poll-interval env var in `config.py` if the default doesn't fit.

Platforms without any API (like Proving Grounds) don't need an adapter at
all — they're a `[claims.<key>]` section in `scoring.toml`.

## Commit / PR conventions

- Small, focused PRs with a clear description beat big ones.
- Add or update tests for anything in `services/scoring.py`, `services/boards.py`,
  or an adapter's parsing.
- Run `ruff format .` before pushing; CI enforces it.
