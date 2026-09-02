import asyncio
import json
import os

from mcp import Client


EXPECTED_TOOLS = {
    "kit_lab_status",
    "kit_runtime_info",
    "kit_stage_summary",
    "kit_prim_inspect",
    "kit_extensions_list",
    "kit_setting_get",
    "kit_viewport_info",
    "kit_execute_python",
    "kit_reset_python_session",
}


async def main() -> None:
    endpoint = os.environ.get(
        "KIT_LAB_MCP_ENDPOINT",
        "http://127.0.0.1:9910/mcp",
    )
    print("Endpoint:", endpoint)

    async with Client(endpoint) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        missing = sorted(EXPECTED_TOOLS - names)
        if missing:
            raise SystemExit(f"Missing tools: {missing}")
        print("tool count:", len(names))
        print("expected tools: PASS")

        status = await client.call_tool("kit_lab_status", {})
        if status.is_error:
            raise SystemExit(f"kit_lab_status failed: {status.content}")
        print("kit_lab_status:")
        print(json.dumps(status.structured_content, indent=2, ensure_ascii=False))

        stage = await client.call_tool("kit_stage_summary", {})
        if stage.is_error:
            raise SystemExit(f"kit_stage_summary failed: {stage.content}")
        print("kit_stage_summary: PASS")

        setting = await client.call_tool(
            "kit_setting_get",
            {"path": "/renderer/multiGpu/enable"},
        )
        if setting.is_error:
            raise SystemExit(f"kit_setting_get failed: {setting.content}")
        print("kit_setting_get: PASS")

    print("KIT_LAB_RUNTIME_MCP_OK")


if __name__ == "__main__":
    asyncio.run(main())
