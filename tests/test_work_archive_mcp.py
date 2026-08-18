from __future__ import annotations

from wechat_decrypt_tool.mcp.tools import MCP_REGISTRY


def test_archive_mcp_tools_and_annotations():
    tools = {item["name"]: item for item in MCP_REGISTRY.list_tools()["tools"]}
    expected_read = {
        "wechat.archive.list_profiles",
        "wechat.archive.get_status",
        "wechat.archive.list_pending",
        "wechat.archive.preview_sync",
        "wechat.archive.verify",
    }
    expected_write = {
        "wechat.archive.set_conversation_status",
        "wechat.archive.run_sync",
        "wechat.archive.cancel_sync",
    }
    assert expected_read | expected_write <= set(tools)
    for name in expected_read:
        assert tools[name]["annotations"]["readOnlyHint"] is True
    for name in expected_write:
        assert tools[name]["annotations"]["readOnlyHint"] is False
        assert "archiveRoot" not in tools[name]["inputSchema"]["properties"]
    assert tools["wechat.archive.run_sync"]["annotations"]["destructiveHint"] is True
    assert tools["wechat.archive.set_conversation_status"]["annotations"]["destructiveHint"] is True
