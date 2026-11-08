"""Tests for the log parser module."""

import pytest

from parser.log_parser import parse_log


# ── Valid logs ───────────────────────────────────────────────────────

def test_basic_valid_log():
    text = (
        "Username: Smaxhy\n"
        "Lore name: Jessica Cruz\n"
        "EP: 2\n"
        "TP: 1\n"
        "Proof: [screenshot]"
    )
    result = parse_log(text)
    assert result is not None
    assert result.username == "Smaxhy"
    assert result.lore_name == "Jessica Cruz"
    assert result.ep == 2
    assert result.tp == 1
    assert result.mp == 0


def test_all_three_stats():
    text = "Username: Player2\nLore name: Bob Jones\nEP: 5\nTP: 2\nMP: 3"
    result = parse_log(text)
    assert result is not None
    assert result.ep == 5
    assert result.tp == 2
    assert result.mp == 3


def test_negative_values():
    text = "Username: Player3\nEP: -2\nTP: 1"
    result = parse_log(text)
    assert result is not None
    assert result.ep == -2
    assert result.tp == 1


def test_case_insensitive_keys():
    text = "USERNAME: Smaxhy\nLORE NAME: Jessica Cruz\nep: 3\ntp: 1"
    result = parse_log(text)
    assert result is not None
    assert result.username == "Smaxhy"
    assert result.ep == 3


def test_extra_spacing():
    text = "Username :  Smaxhy \n Lore name :  Jessica Cruz \n EP :  2 \n TP :  1"
    result = parse_log(text)
    assert result is not None
    assert result.username == "Smaxhy"
    assert result.ep == 2


def test_no_lore_name():
    text = "Username: Smaxhy\nEP: 2\nTP: 1"
    result = parse_log(text)
    assert result is not None
    assert result.lore_name is None
    assert result.ep == 2


def test_only_mp():
    text = "Username: Player4\nMP: 5"
    result = parse_log(text)
    assert result is not None
    assert result.mp == 5
    assert result.ep == 0
    assert result.tp == 0


# ── Invalid messages ─────────────────────────────────────────────────

def test_empty_string():
    assert parse_log("") is None


def test_normal_chat():
    assert parse_log("Hey guys, great session today!") is None


def test_username_but_no_stats():
    assert parse_log("Username: Smaxhy\nLore name: Jessica Cruz") is None


def test_stats_but_no_username():
    assert parse_log("EP: 2\nTP: 1") is None


def test_bot_message_like():
    assert parse_log("Scan complete. Processed 5 logs.") is None


# ── Edge cases ───────────────────────────────────────────────────────

def test_plus_prefix():
    text = "Username: Smaxhy\nEP: +3\nTP: +1"
    result = parse_log(text)
    assert result is not None
    assert result.ep == 3
    assert result.tp == 1


def test_proof_is_ignored():
    text = "Username: Smaxhy\nEP: 2\nTP: 1\nProof: https://imgur.com/abc123"
    result = parse_log(text)
    assert result is not None
    assert result.ep == 2


def test_lore_name_without_space_label():
    result = parse_log("Username: Smaxhy\nLorename: Jessica Cruz\nEP: 1")
    assert result.lore_name == "Jessica Cruz"


def test_username_trailing_colon_stripped():
    assert parse_log("Username: Smaxhy:\nEP: 1").username == "Smaxhy"


def test_large_values_are_not_clamped_by_parser():
    # The parser keeps the raw number; run_scan rejects anything over 5.
    result = parse_log("Username: Smaxhy\nEP: 12\nTP: -9")
    assert (result.ep, result.tp) == (12, -9)


# ── Stat formats ─────────────────────────────────────────────────────

@pytest.mark.parametrize("line,ep", [
    ("EP: 2", 2),
    ("EP 2", 2),
    ("EP:2", 2),
    ("2 EP", 2),
    ("2EP", 2),
    ("-1 ep", -1),
])
def test_stat_formats(line, ep):
    assert parse_log(f"Username: Smaxhy\n{line}").ep == ep


def test_stats_on_one_line():
    result = parse_log("Username: Smaxhy\n1 ep 2 tp 3 mp")
    assert (result.ep, result.tp, result.mp) == (1, 2, 3)


def test_first_occurrence_wins():
    assert parse_log("Username: Smaxhy\nEP: 2\nEP: 4").ep == 2


def test_stat_inside_word_is_not_matched():
    assert parse_log("Username: Smaxhy\nSTEP 3\nTP: 1").ep == 0


def test_stat_mentioned_in_free_text_is_counted():
    # Current behaviour: any "<n> TP"/"MP <n>" anywhere in the message counts.
    result = parse_log("Username: Smaxhy\nEP: 2\nNote: 1 tp for helping")
    assert result.tp == 1


# ── Format B: unlabelled ─────────────────────────────────────────────

def test_unlabelled_username_and_lore():
    result = parse_log("Smaxhy\nJessica Cruz\nEP: 2\nTP: 1")
    assert (result.username, result.lore_name, result.ep, result.tp) == ("Smaxhy", "Jessica Cruz", 2, 1)


def test_unlabelled_username_only():
    result = parse_log("Smaxhy\n2 EP")
    assert (result.username, result.lore_name) == ("Smaxhy", None)


def test_unlabelled_skips_keywords_urls_and_stat_lines():
    text = (
        "Rook\n"
        "Training\n"
        "https://imgur.com/x\n"
        "[screenshot]\n"
        "1 ep 1 tp\n"
        "Smaxhy\n"
        "Jessica Cruz\n"
        "Proof: attached"
    )
    result = parse_log(text)
    assert (result.username, result.lore_name, result.ep, result.tp) == ("Smaxhy", "Jessica Cruz", 1, 1)


def test_unlabelled_lore_label_without_username_label():
    result = parse_log("Smaxhy\nLore name: Jessica Cruz\nEP: 1")
    assert result.username == "Smaxhy"
    # Lore label is only read in the labelled format
    assert result.lore_name is None


def test_unlabelled_only_stats_has_no_username():
    assert parse_log("EP: 1\nTP: 1\nRook") is None


def test_sentence_as_username_is_rejected():
    assert parse_log("we had a really fun session today\n2 EP") is None


def test_four_word_username_is_allowed():
    assert parse_log("Username: a b c d\nEP: 1").username == "a b c d"


def test_very_short_message():
    assert parse_log("EP 1") is None
