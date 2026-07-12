"""Entry point: `python -m hackqueue` or the `hackqueue` console script."""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from hackqueue.config import Settings
from hackqueue.log import configure_logging, get_logger


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(e["loc"][0]).upper() for e in exc.errors())
        print(
            f"Configuration error — check these variables: {missing}\n"
            "Copy .env.example to .env and fill in at least DISCORD_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    configure_logging(settings.log_level, settings.log_format)
    log = get_logger("hackqueue")

    # Imported after logging is configured so import-time warnings are formatted.
    from hackqueue.bot import HackQueueBot

    bot = HackQueueBot(settings)

    async def runner() -> None:
        async with bot:
            await bot.start(settings.discord_token)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
