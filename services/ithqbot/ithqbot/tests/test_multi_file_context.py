"""Test that multi-file upload across separate messages preserves attachment metadata in session history."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from ithqbot.agent.context import ContextBuilder


def test_attachments_preserved_across_history_turns():
    """When two files are uploaded in separate messages, both should appear in the
    LLM context when a third message asks to compare them."""

    ctx = ContextBuilder(
        workspace=Path("/tmp/test_workspace"),
        enabled_skills=[],
    )

    # Simulate session history with two file upload turns
    history = [
        # Turn 1: File 1 upload
        {
            "role": "user",
            "content": "[file: 1.docx]",
            "metadata": {
                "attachments": [
                    {
                        "name": "1.docx",
                        "path": "feishu_attachments/msg1/1.docx",
                        "bucket": "ithqbot-storage",
                        "storage": {"path": "feishu_attachments/msg1/1.docx"},
                    }
                ]
            },
        },
        {
            "role": "assistant",
            "content": "已收到您的文件 1.docx。",
        },
        # Turn 2: File 2 upload
        {
            "role": "user",
            "content": "[file: 2.docx]",
            "metadata": {
                "attachments": [
                    {
                        "name": "2.docx",
                        "path": "feishu_attachments/msg2/2.docx",
                        "bucket": "ithqbot-storage",
                        "storage": {"path": "feishu_attachments/msg2/2.docx"},
                    }
                ]
            },
        },
        {
            "role": "assistant",
            "content": "已收到您的文件 2.docx。",
        },
    ]

    # Now build messages for the third turn: "比较两个文档"
    messages = ctx.build_messages(
        history=history,
        current_message="比较两个文档",
        channel="feishu",
        chat_id="test_chat",
        metadata={"account_id": "test_user", "tenant_id": "winlmp"},
    )

    # Collect all text from all messages
    all_text = "\n".join(
        m.get("content", "") if isinstance(m.get("content"), str)
        else "\n".join(p.get("text", "") for p in m.get("content", []) if isinstance(p, dict))
        for m in messages
    )

    print("=== ALL TEXT IN LLM CONTEXT ===")
    print(all_text)
    print("=== END ===")

    # Both file paths must appear in the LLM context
    assert "1.docx" in all_text, "1.docx should be visible in the LLM context"
    assert "2.docx" in all_text, "2.docx should be visible in the LLM context"
    assert "minio://ithqbot-storage/feishu_attachments/msg1/1.docx" in all_text, \
        "Full minio path for 1.docx should appear"
    assert "minio://ithqbot-storage/feishu_attachments/msg2/2.docx" in all_text, \
        "Full minio path for 2.docx should appear"
        
    # NEW: Check for the summary section
    assert "[Session Attachments Overview]" in all_text, "Summary section should appear"
    assert "[Doc Compare Auto Selection]" in all_text
    summary_part = all_text.split("[Session Attachments Overview]")[1].split("[Runtime Context")[0]
    assert "1.docx" in summary_part
    assert "2.docx" in summary_part
