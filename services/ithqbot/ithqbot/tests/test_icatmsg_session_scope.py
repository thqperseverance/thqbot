from ithqbot.channels.icatmsg import ChannelAdapter


def test_channel_adapter_build_session_key_isolated_by_bot() -> None:
    adapter = ChannelAdapter("icatmsg", processing_receipt=True)

    assert adapter.build_session_key("channel", "user123", "chat456", "tenant-a", "bot-a") == "icatmsg:tenant-a:user123:chat456:bot-a"
    assert adapter.build_session_key("account_chat", "user123", "chat456", "tenant-b", "bot-b") == "shared:tenant-b:user123:chat456:bot-b"
    assert adapter.build_session_key("channel", "user123", "chat456", None, None) == "icatmsg:default:user123:chat456"
