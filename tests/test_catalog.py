from __future__ import annotations

import pytest

from hackqueue.adapters.registry import AdapterRegistry
from hackqueue.config import Settings
from hackqueue.db.models import AccountLink, CatalogBox, Solve
from hackqueue.services.catalog import (
    CatalogService,
    extract_htb_machine,
    ippsec_video_map,
    normalize_box_name,
)

# ── name normalization: the dirty-data cases live-observed in the dataset ────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HackTheBox - Lame", "Lame"),
        ("HackThebox - Sniper", "Sniper"),
        ("HacktheBox - Bashed", "Bashed"),
        ("HackTheBox -  Aragog", "Aragog"),  # double space after dash
        ("HackTheBox   RegistryTwo", "RegistryTwo"),  # no dash at all
        ("hackthebox - lowercase", "lowercase"),
    ],
)
def test_extract_htb_machine_variants(raw, expected):
    assert extract_htb_machine(raw) == expected


@pytest.mark.parametrize(
    "raw", ["VulnHub - Mr Robot", "UHC - Nunchucks", "Academy: Learning Process", "AV Evasion"]
)
def test_extract_htb_machine_rejects_non_htb(raw):
    assert extract_htb_machine(raw) is None


def test_normalize_box_name():
    assert normalize_box_name("Registry Two") == normalize_box_name("RegistryTwo")
    assert normalize_box_name("  Lame ") == "lame"
    assert normalize_box_name("Ap0calypse!") == "ap0calypse"


def test_ippsec_video_map_dedupes_and_skips_academy():
    entries = [
        {
            "machine": "HackTheBox - Lame",
            "videoId": "abc123",
            "timestamp": {"minutes": 0, "seconds": 10},
        },
        {
            "machine": "HackTheBox - Lame",
            "videoId": "abc123",
            "timestamp": {"minutes": 5, "seconds": 0},
        },
        {"machine": "Academy: Learning Process", "academy": "9", "line": "x"},  # no videoId
        {
            "machine": "VulnHub - Mr Robot",
            "videoId": "vvv",
            "timestamp": {"minutes": 1, "seconds": 2},
        },
        {
            "machine": "HackTheBox   Zipping",
            "videoId": "zzz",
            "timestamp": {"minutes": 0, "seconds": 0},
        },
    ]
    videos = ippsec_video_map(entries)
    assert videos == {"lame": "abc123", "zipping": "zzz"}


# ── catalog queries ──────────────────────────────────────────────────────────


@pytest.fixture
def catalog(db, http):
    settings = Settings(discord_token="x", _env_file=None)
    return CatalogService(db, http, AdapterRegistry(), settings)


async def seed_boxes(db):
    async with db.session() as session, session.begin():
        session.add_all(
            [
                CatalogBox(
                    platform="htb",
                    platform_ref="1",
                    name="Lame",
                    name_normalized="lame",
                    os="Linux",
                    difficulty="easy",
                    tags=["samba"],
                    retired=True,
                    url="https://app.hackthebox.com/machines/1",
                    ippsec_video_id="abc",
                ),
                CatalogBox(
                    platform="htb",
                    platform_ref="2",
                    name="RegistryTwo",
                    name_normalized="registrytwo",
                    os="Linux",
                    difficulty="hard",
                    tags=[],
                    retired=False,
                    url="https://app.hackthebox.com/machines/2",
                ),
                CatalogBox(
                    platform="htb",
                    platform_ref="3",
                    name="Sniper",
                    name_normalized="sniper",
                    os="Windows",
                    difficulty="medium",
                    tags=["Active Directory"],
                    retired=True,
                    url="https://app.hackthebox.com/machines/3",
                ),
            ]
        )


async def test_suggest_filters(db, catalog):
    await seed_boxes(db)
    linux = await catalog.suggest(os="Linux")
    assert {b.name for b in linux} == {"Lame", "RegistryTwo"}
    active = await catalog.suggest(include_retired=False)
    assert {b.name for b in active} == {"RegistryTwo"}
    tagged = await catalog.suggest(tag="active directory")
    assert {b.name for b in tagged} == {"Sniper"}


async def test_suggest_excludes_owned(db, catalog):
    await seed_boxes(db)
    boxes = await catalog.suggest(exclude_refs={"1", "3"})
    assert {b.name for b in boxes} == {"RegistryTwo"}


async def test_owned_refs_from_solves(db, catalog):
    await seed_boxes(db)
    async with db.session() as session, session.begin():
        link = AccountLink(
            discord_user_id=7, platform="htb", platform_user_id="70", platform_username="x"
        )
        session.add(link)
        await session.flush()
        session.add(
            Solve(link_id=link.id, platform="htb", item_ref="1", item_name="Lame", kind="root")
        )
    assert await catalog.owned_refs(7, "htb") == {"1"}


async def test_find_box_exact_and_fuzzy(db, catalog):
    await seed_boxes(db)
    assert (await catalog.find_box("lame")).name == "Lame"
    assert (await catalog.find_box("Registry Two")).name == "RegistryTwo"
    assert (await catalog.find_box("regis")).name == "RegistryTwo"
    assert await catalog.find_box("nonexistent") is None


async def test_ippsec_url_property(db, catalog):
    await seed_boxes(db)
    box = await catalog.find_box("lame")
    assert box.ippsec_url == "https://youtube.com/watch?v=abc"
    assert (await catalog.find_box("sniper")).ippsec_url is None
