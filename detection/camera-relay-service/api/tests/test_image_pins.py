"""Alpine drops old package revisions, so an exact pin would break a fresh build in the field. The
bundle's two apk packages float within a minor release, and nothing sets a revision from outside."""

from pathlib import Path

RELAY = Path(__file__).resolve().parents[2]


def test_chrony_floats_within_its_minor_release():
    dockerfile = (RELAY / "chrony" / "Dockerfile").read_text()
    assert "apk add --no-cache 'chrony~4.8'" in dockerfile
    assert "RELAY_CHRONY_VERSION" not in dockerfile


def test_nftables_floats_within_its_minor_release():
    assert "apk add --no-cache 'nftables~1.1.5'" in (RELAY / "netguard" / "Dockerfile").read_text()


def test_no_setting_is_left_for_an_exact_chrony_revision():
    for name in ("compose.yaml", "README.md", ".env.example"):
        assert "RELAY_CHRONY_VERSION" not in (RELAY / name).read_text(), name
