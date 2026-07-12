from __future__ import annotations

from pathlib import Path

from hackqueue.config import ScoringConfig


def test_missing_file_falls_back_to_defaults(tmp_path):
    cfg = ScoringConfig.load(tmp_path / "nope.toml")
    assert cfg.weights["htb"] == 1.0
    assert cfg.claims["pg"].points["insane"] == 40


def test_parse_custom_file(tmp_path):
    path = tmp_path / "scoring.toml"
    path.write_text(
        """
[composite.weights]
htb = 2.0
claims = 0.5

[claims.vulnhub]
name = "VulnHub"
[claims.vulnhub.points]
Easy = 5
HARD = 15
"""
    )
    cfg = ScoringConfig.load(path)
    assert cfg.weights == {"htb": 2.0, "claims": 0.5}
    assert set(cfg.claims) == {"vulnhub"}
    assert cfg.claims["vulnhub"].points == {"easy": 5, "hard": 15}  # keys lowercased


def test_repo_scoring_toml_parses():
    cfg = ScoringConfig.load(Path(__file__).parent.parent / "scoring.toml")
    assert cfg.claims["pg"].name == "OffSec Proving Grounds"
    assert cfg.weights["rootme"] == 1.0
