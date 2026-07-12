"""SQLAlchemy models. Links are global per Discord user; board membership is
per guild; snapshots/solves hang off links so /unlink purges them via cascade
(the privacy guarantee in the README). Manual claims are guild submissions —
not tied to a link — and are purged per-guild via /config purge-member."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class TZDateTime(TypeDecorator):
    """Stores naive UTC, returns aware UTC — consistent across SQLite and
    Postgres (SQLite silently drops tzinfo on timezone=True columns)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to TZDateTime; use aware UTC datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {datetime: TZDateTime, dict[str, Any]: JSON, list[str]: JSON}  # noqa: RUF012


class Guild(Base):
    """Per-server configuration."""

    __tablename__ = "guilds"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    mod_role_id: Mapped[int | None] = mapped_column(BigInteger)
    mod_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    announce_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    recap_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    recap_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    require_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class GuildMember(Base):
    """Opt-in participation of a Discord user in a guild's boards."""

    __tablename__ = "guild_members"

    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True
    )
    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = mapped_column(default=utcnow)


class AccountLink(Base):
    """A Discord user's account on one platform. Global (not per guild)."""

    __tablename__ = "account_links"
    __table_args__ = (
        UniqueConstraint("discord_user_id", "platform", name="uq_link_user_platform"),
        UniqueConstraint("platform", "platform_user_id", name="uq_link_platform_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    platform: Mapped[str] = mapped_column(String(16))
    platform_user_id: Mapped[str] = mapped_column(String(64))
    platform_username: Mapped[str] = mapped_column(String(128))
    extra_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_token: Mapped[str | None] = mapped_column(String(32))
    verify_expires_at: Mapped[datetime | None]
    #: ok | private | not_found | auth_error — last poll outcome for this link
    status: Mapped[str] = mapped_column(String(16), default="ok")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Snapshot(Base):
    """Point-in-time stats for a link. All leaderboard math derives from these."""

    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snapshots_link_taken", "link_id", "taken_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(
        ForeignKey("account_links.id", ondelete="CASCADE"), index=True
    )
    taken_at: Mapped[datetime] = mapped_column(default=utcnow)
    points: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Solve(Base):
    """A normalized solve event (machine own, challenge, room)."""

    __tablename__ = "solves"
    __table_args__ = (
        UniqueConstraint("link_id", "item_ref", "kind", name="uq_solve_link_item_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(
        ForeignKey("account_links.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(16))
    item_ref: Mapped[str] = mapped_column(String(128))
    item_name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(16))  # user | root | challenge | room
    points: Mapped[int] = mapped_column(Integer, default=0)
    solved_at: Mapped[datetime | None]
    first_blood: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    #: True for solves imported on a link's FIRST poll (pre-link history) —
    #: excluded from "new this week" counts in recaps.
    backfilled: Mapped[bool] = mapped_column(Boolean, default=False)


class Claim(Base):
    """A manual solve claim (Proving Grounds etc.), per guild, mod-approved."""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), index=True
    )
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    platform_key: Mapped[str] = mapped_column(String(32))
    item_name: Mapped[str] = mapped_column(String(256))
    difficulty: Mapped[str] = mapped_column(String(32))
    points: Mapped[int] = mapped_column(Integer)
    proof_url: Mapped[str | None] = mapped_column(Text)
    #: pending | approved | denied
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    mod_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    reviewed_at: Mapped[datetime | None]


class CatalogBox(Base):
    """A box/room in the local catalog (HTB machines enriched with IppSec links)."""

    __tablename__ = "catalog_boxes"
    __table_args__ = (UniqueConstraint("platform", "platform_ref", name="uq_box_platform_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    platform_ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    name_normalized: Mapped[str] = mapped_column(String(128), index=True)
    os: Mapped[str | None] = mapped_column(String(32))
    difficulty: Mapped[str | None] = mapped_column(String(32))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    stars: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str] = mapped_column(Text)
    ippsec_video_id: Mapped[str | None] = mapped_column(String(16))
    release_date: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    @property
    def ippsec_url(self) -> str | None:
        return (
            f"https://youtube.com/watch?v={self.ippsec_video_id}" if self.ippsec_video_id else None
        )


class KV(Base):
    """Small key-value store: ETags, sync cursors, recap markers."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
