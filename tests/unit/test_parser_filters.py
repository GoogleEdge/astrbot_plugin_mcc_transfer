from filter_chain import FilterChain
from models import MessageKind
from parser import MessageParser


def test_parser_handles_spec_event():
    parsed = MessageParser().parse(
        {
            "type": "event",
            "event": "PlayerMessage",
            "player": "brightmoon",
            "message": "大家好",
        }
    )
    assert parsed is not None
    assert parsed.sender == "brightmoon"
    assert parsed.message == "大家好"
    assert parsed.kind is MessageKind.CHAT
    assert parsed.fingerprint


def test_parser_handles_nested_mapping():
    parsed = MessageParser(
        {
            "field_paths": {
                "sender": "payload.name",
                "message": "payload.text",
                "kind": "payload.kind",
            }
        }
    ).parse(
        {
            "type": "event",
            "event": "PlayerMessage",
            "payload": {"name": "Alex", "text": "hello", "kind": "chat"},
        }
    )
    assert parsed is not None
    assert (parsed.sender, parsed.message, parsed.kind) == ("Alex", "hello", MessageKind.CHAT)


def test_blacklist_precedes_whitelist():
    chain = FilterChain(
        {
            "enabled": True,
            "ignore_empty_messages": False,
            "enable_player_blacklist": True,
            "blacklist_players": ["Alex"],
            "enable_player_whitelist": True,
            "whitelist_players": ["Alex"],
        }
    )
    result = chain.evaluate({"sender": "Alex", "message": "hello", "kind": "chat"})
    assert not result.accepted
    assert result.reason == "blacklisted_player"


def test_filter_order_rejects_command_before_name_rules():
    chain = FilterChain(
        {
            "ignore_command_messages": True,
            "command_prefixes": ["/"],
            "enable_player_blacklist": True,
            "blacklist_players": ["Alex"],
        }
    )
    result = chain.evaluate({"sender": "Alex", "message": "/say hi", "kind": "chat"})
    assert result.reason == "command_message"
