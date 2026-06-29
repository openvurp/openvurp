"""Test: l'agente sa DOVE si trova (CLI / privato / gruppo) e si comporta di conseguenza."""

from core.personality import describe_venue


def test_cli_is_owner_session():
    v = describe_venue("cli", "", "user", True)
    assert "terminale" in v.lower()
    assert "1-a-1" in v
    # in CLI può dilungarsi / formattare
    assert "ricca" in v.lower() or "markdown" in v.lower()


def test_empty_source_defaults_to_cli():
    assert "terminale" in describe_venue("", "", "user", True).lower()


def test_telegram_private_is_one_to_one():
    v = describe_venue("telegram", "private", "Mario", True)
    assert "privata" in v.lower()
    assert "Mario" in v
    assert "Telegram" in v
    # niente formattazione pesante sui canali messaggistica
    assert "brevi" in v.lower()


def test_telegram_group_addressed():
    v = describe_venue("telegram", "group", "Mario", True)
    assert "gruppo" in v.lower()
    assert "più persone" in v.lower()
    assert "È rivolto a te" in v


def test_telegram_group_not_addressed_is_cautious():
    v = describe_venue("telegram", "supergroup", "Mario", False)
    assert "gruppo" in v.lower()
    assert "NON ti è esplicitamente rivolto" in v
    assert "taci" in v.lower()


def test_internal_sources_have_no_venue():
    for s in ("heartbeat", "cron", "system", "subagent"):
        assert describe_venue(s, "", "system", True) == ""


def test_discord_group_label():
    v = describe_venue("discord", "group", "x", False)
    assert "Discord" in v
