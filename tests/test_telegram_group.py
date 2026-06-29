"""Test: in gruppo il bot risponde solo se menzionato/in reply."""

from channels.telegram import (
    should_respond_in_group, strip_bot_mention, TelegramChannel,
)


def test_private_always_responds():
    assert should_respond_in_group("private", "ciao", "openvurpbot", False) is True


def test_group_ignores_unaddressed():
    assert should_respond_in_group("group", "ciao a tutti", "openvurpbot", False) is False
    assert should_respond_in_group("supergroup", "che si fa stasera", "openvurpbot", False) is False


def test_group_responds_when_mentioned():
    assert should_respond_in_group("group", "ehi @openvurpbot come va", "openvurpbot", False) is True
    # case-insensitive
    assert should_respond_in_group("group", "@OpenvurpBot ?", "openvurpbot", False) is True


def test_group_responds_when_reply_to_bot():
    assert should_respond_in_group("supergroup", "e questo?", "openvurpbot", True) is True


def test_group_without_bot_username_only_reply():
    assert should_respond_in_group("group", "@qualcuno", "", False) is False
    assert should_respond_in_group("group", "qualsiasi", "", True) is True


def test_strip_bot_mention():
    assert strip_bot_mention("@openvurpbot dimmi l'ora", "openvurpbot") == "dimmi l'ora"
    # menzione in mezzo: la toglie (resta il doppio spazio, non normalizzato)
    assert strip_bot_mention("ehi @OpenvurpBot ciao", "openvurpbot") == "ehi  ciao"
    # senza username non tocca nulla
    assert strip_bot_mention("@openvurpbot ciao", "") == "@openvurpbot ciao"


def test_split_text_short_stays_one_message():
    # Sotto il limite: una sola parte, intatta. Niente troncamento.
    assert TelegramChannel._split_text("ciao", limit=4000) == ["ciao"]
    assert TelegramChannel._split_text("", limit=4000) == []


def test_split_text_long_is_split_not_truncated():
    # Messaggio lungo: spezzato in più parti, NIENTE perso (era il bug dei
    # 'messaggi tagliati': prima si faceva text[:4096]).
    text = "\n".join(f"riga numero {i} con un po' di testo" for i in range(400))
    parts = TelegramChannel._split_text(text, limit=500)
    assert len(parts) > 1
    assert all(len(p) <= 500 for p in parts)
    # Ricomponendo si recupera tutto il contenuto (a meno degli spazi di giunzione).
    joined = " ".join(parts).replace("\n", " ")
    original = text.replace("\n", " ")
    assert "".join(joined.split()) == "".join(original.split())


def test_split_text_prefers_line_boundary():
    # Taglia preferendo il newline, non a metà parola.
    text = "primo blocco di testo\n" + "x" * 30 + "\nultimo blocco"
    parts = TelegramChannel._split_text(text, limit=40)
    assert parts[0] == "primo blocco di testo"
