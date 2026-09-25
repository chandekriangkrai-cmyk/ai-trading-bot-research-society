from pathlib import Path

from app.config import settings


def test_app_version_matches_version_file():
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    assert settings.app_version == version_file.read_text(encoding="utf-8").strip()
