"""Embed builders. All user-visible rendering lives here so cogs stay thin."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import discord

from hackqueue.adapters.base import PLATFORM_LABELS, Platform
from hackqueue.db.models import AccountLink, CatalogBox, Claim, Snapshot, Solve
from hackqueue.services.scoring import Period

if TYPE_CHECKING:
    from hackqueue.services.boards import Board
    from hackqueue.services.health import HealthRegistry

COLOR_OK = 0x2ECC71
COLOR_INFO = 0x5865F2
COLOR_WARN = 0xE67E22
COLOR_ERROR = 0xE74C3C

MEDALS = ("🥇", "🥈", "🥉")
PAGE_SIZE = 10

PERIOD_LABELS = {
    Period.WEEKLY: "This week",
    Period.MONTHLY: "This month",
    Period.ALLTIME: "All-time",
}


def platform_label(platform: str) -> str:
    try:
        return PLATFORM_LABELS[Platform(platform)]
    except ValueError:
        return platform


def board_pages(board: Board, *, title: str, value_suffix: str = "") -> list[discord.Embed]:
    """Render a board into paginated embeds (one embed per PAGE_SIZE rows)."""
    if not board.rows:
        embed = discord.Embed(
            title=title,
            description="Nothing on the board yet — `/link` an account to get started!",
            color=COLOR_INFO,
        )
        _board_footer(embed, board, page=1, pages=1)
        return [embed]

    pages: list[discord.Embed] = []
    chunks = [board.rows[i : i + PAGE_SIZE] for i in range(0, len(board.rows), PAGE_SIZE)]
    for page_index, chunk in enumerate(chunks):
        lines = []
        for offset, row in enumerate(chunk):
            position = page_index * PAGE_SIZE + offset
            marker = MEDALS[position] if position < len(MEDALS) else f"`#{position + 1}`"
            value = f"{row.value:,.1f}" if isinstance(row.value, float) else f"{row.value:,}"
            if row.value == int(row.value):
                value = f"{int(row.value):,}"
            name = f" · {row.label}" if row.label else ""
            flag = "" if row.verified else " ⚠"
            lines.append(
                f"{marker} <@{row.discord_user_id}>{name}{flag} — **{value}{value_suffix}**"
            )
        embed = discord.Embed(title=title, description="\n".join(lines), color=COLOR_INFO)
        _board_footer(embed, board, page=page_index + 1, pages=len(chunks))
        pages.append(embed)
    return pages


def _board_footer(embed: discord.Embed, board: Board, *, page: int, pages: int) -> None:
    parts = [PERIOD_LABELS[board.period]]
    if pages > 1:
        parts.append(f"page {page}/{pages}")
    if any(not r.verified for r in board.rows):
        parts.append("⚠ = unverified link")
    if board.stale_platforms:
        labels = ", ".join(platform_label(p) for p in board.stale_platforms)
        parts.append(f"⚠ stale data: {labels} unreachable")
    embed.set_footer(text=" · ".join(parts))


def profile_embed(
    member: discord.abc.User,
    links: list[AccountLink],
    latest: dict[int, Snapshot | None],
    recent_solves: list[Solve],
    approved_claims: int,
    verifiable: set[str] | None = None,
) -> discord.Embed:
    """``verifiable``: platforms where ownership verification is possible at
    all. A link on any other platform is shown as 'no verification' — it is not
    the member's fault, and no ⚠ is implied."""
    embed = discord.Embed(title=f"{member.display_name}'s profile", color=COLOR_INFO)
    embed.set_thumbnail(url=member.display_avatar.url)
    if not links:
        embed.description = "No linked accounts. Use `/link` to add one."
        return embed
    verifiable = verifiable if verifiable is not None else set()
    for link in links:
        snap = latest.get(link.id)
        if link.verified:
            badge = "✅ verified"
        elif link.platform in verifiable:
            badge = "⚠ unverified"
        else:
            badge = "no verification on this platform"
        status = "" if link.status == "ok" else f" · ⚠ {link.status.replace('_', ' ')}"
        stats = "no data yet"
        if snap is not None:
            rank = f" · rank #{snap.rank:,}" if snap.rank else ""
            unit = "flags" if link.platform == Platform.HTB.value else "pts"
            stats = f"{snap.points:,} {unit}{rank}"
            if detail := _counter_detail(link.platform, snap.counters):
                stats += f"\n{detail}"
        embed.add_field(
            name=f"{platform_label(link.platform)} — {link.platform_username} ({badge})",
            value=f"{stats}{status}",
            inline=False,
        )
    if approved_claims:
        embed.add_field(name="Approved claims", value=str(approved_claims), inline=False)
    if recent_solves:
        lines = [
            f"• **{s.item_name}** ({platform_label(s.platform)} · {s.kind})"
            + (" 🩸" if s.first_blood else "")
            for s in recent_solves[:5]
        ]
        embed.add_field(name="Recent solves", value="\n".join(lines), inline=False)
    return embed


def _counter_detail(platform: str, counters: dict) -> str:
    """One line of what actually makes up a score — HTB's flags come from four
    different places, and 'Dante 27/27' is the interesting part."""
    if platform != Platform.HTB.value or not counters:
        return ""
    bits = []
    owns = int(counters.get("user_owns", 0)) + int(counters.get("system_owns", 0))
    if owns:
        bits.append(f"{owns} machine owns")
    if challenges := int(counters.get("challenges", 0)):
        bits.append(f"{challenges} challenges")
    if prolab := int(counters.get("prolab_flags", 0)):
        done = int(counters.get("prolabs_completed", 0))
        bits.append(f"{prolab} Pro Lab flags" + (f" ({done} completed)" if done else ""))
    if fortress := int(counters.get("fortress_flags", 0)):
        bits.append(f"{fortress} Fortress flags")
    bloods = int(counters.get("user_bloods", 0)) + int(counters.get("system_bloods", 0))
    if bloods:
        bits.append(f"🩸 {bloods}")
    return "-# " + " · ".join(bits) if bits else ""


def claim_embed(claim: Claim, cfg_name: str, image_ref: str | None = None) -> discord.Embed:
    """``image_ref`` is an ``attachment://…`` reference to the proof re-uploaded
    onto the mod message — interaction attachment URLs expire, so the message's
    own attachment is the durable copy."""
    color = {"pending": COLOR_WARN, "approved": COLOR_OK, "denied": COLOR_ERROR}[claim.status]
    embed = discord.Embed(title=f"Claim: {claim.item_name}", color=color)
    embed.add_field(name="Platform", value=cfg_name)
    embed.add_field(name="Difficulty", value=claim.difficulty.title())
    embed.add_field(name="Points", value=str(claim.points))
    embed.add_field(name="Claimed by", value=f"<@{claim.discord_user_id}>")
    embed.add_field(name="Status", value=claim.status.title())
    if claim.reviewed_by:
        embed.add_field(name="Reviewed by", value=f"<@{claim.reviewed_by}>")
    if image_ref:
        embed.set_image(url=image_ref)
    embed.set_footer(text=f"Claim #{claim.id}")
    return embed


def box_embed(box: CatalogBox) -> discord.Embed:
    embed = discord.Embed(title=box.name, url=box.url or None, color=COLOR_INFO)
    embed.add_field(name="Platform", value=platform_label(box.platform))
    if box.os:
        embed.add_field(name="OS", value=box.os)
    if box.difficulty:
        embed.add_field(name="Difficulty", value=box.difficulty.title())
    if box.stars is not None:
        embed.add_field(name="Rating", value=f"{box.stars:.1f} ★")
    embed.add_field(name="Status", value="Retired" if box.retired else "Active")
    if box.tags:
        embed.add_field(name="Tags", value=", ".join(box.tags[:8]), inline=False)
    if box.ippsec_url:
        embed.add_field(name="Walkthrough", value=f"[IppSec video]({box.ippsec_url})", inline=False)
    if box.release_date:
        embed.set_footer(text=f"Released {box.release_date.date().isoformat()}")
    return embed


def suggest_embed(boxes: list[CatalogBox]) -> discord.Embed:
    embed = discord.Embed(title="Box suggestions", color=COLOR_INFO)
    if not boxes:
        embed.description = (
            "No matching boxes found (or you've solved them all — nice). Try loosening the filters."
        )
        return embed
    for box in boxes:
        bits = [b for b in (box.os, box.difficulty and box.difficulty.title()) if b]
        if box.retired:
            bits.append("retired")
        video = f" · [IppSec]({box.ippsec_url})" if box.ippsec_url else ""
        embed.add_field(
            name=box.name,
            value=f"[{' · '.join(bits) or 'view'}]({box.url}){video}",
            inline=False,
        )
    return embed


def recap_embed(
    board: Board,
    solve_counts: dict[str, int],
    bloods: list[Solve],
    box: CatalogBox | None,
    week_of: datetime | None = None,
) -> discord.Embed:
    title = "📊 Weekly recap"
    if week_of is not None:
        title += f" — week of {week_of.date().isoformat()}"
    embed = discord.Embed(title=title, color=COLOR_OK)
    if board.rows:
        lines = [
            f"{MEDALS[i] if i < 3 else f'`#{i + 1}`'} "
            f"<@{row.discord_user_id}> — **{row.value:.1f}**"
            for i, row in enumerate(board.rows[:5])
        ]
        embed.add_field(name="Top gainers (composite)", value="\n".join(lines), inline=False)
    else:
        embed.description = "A quiet week — no tracked activity."
    if solve_counts:
        counts = " · ".join(
            f"{platform_label(p)}: **{n}**" for p, n in sorted(solve_counts.items())
        )
        embed.add_field(name="New solves", value=counts, inline=False)
    if bloods:
        lines = [f"🩸 **{s.item_name}** ({platform_label(s.platform)})" for s in bloods[:5]]
        embed.add_field(name="First bloods", value="\n".join(lines), inline=False)
    if box is not None:
        video = f" — [IppSec walkthrough]({box.ippsec_url})" if box.ippsec_url else ""
        embed.add_field(
            name="📦 Box of the week",
            value=f"[{box.name}]({box.url}) ({box.difficulty or '?'}){video}",
            inline=False,
        )
    if board.stale_platforms:
        labels = ", ".join(platform_label(p) for p in board.stale_platforms)
        embed.set_footer(text=f"⚠ stale data: {labels} unreachable this week")
    return embed


def health_embed(
    health: HealthRegistry,
    enabled: list[Platform],
    counts: dict[str, int],
) -> discord.Embed:
    embed = discord.Embed(title="hackQueue health", color=COLOR_INFO)
    status_icons = {"ok": "🟢", "degraded": "🟡", "auth_error": "🔴", "unknown": "⚪"}
    for platform in Platform:
        if platform not in enabled:
            embed.add_field(
                name=platform_label(platform.value), value="⚫ disabled (no credential)"
            )
            continue
        entry = health.entry(platform)
        lines = [f"{status_icons[entry.status.value]} {entry.status.value}"]
        if entry.last_success:
            lines.append(f"last poll <t:{int(entry.last_success.timestamp())}:R>")
        if entry.last_error and entry.status.value != "ok":
            lines.append(entry.last_error[:120])
        embed.add_field(name=platform_label(platform.value), value="\n".join(lines))
    embed.add_field(
        name="Data",
        value=(
            f"links: **{counts.get('links', 0)}** · snapshots: **{counts.get('snapshots', 0):,}**"
            f" · solves: **{counts.get('solves', 0):,}** · boxes: **{counts.get('boxes', 0):,}**"
        ),
        inline=False,
    )
    return embed
