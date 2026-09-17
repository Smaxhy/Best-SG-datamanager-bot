"""Tests for the log parser module."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
