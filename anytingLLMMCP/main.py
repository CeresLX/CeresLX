import os
from mcp.server import MCPServer
from mcp.server.caching import CacheHint

from internal.tools import register_tools

server = MCPServer(
    name="pet-hospital",
    version="1.0.0",
    description="宠物医院管理系统 MCP 服务",
    cache_hints={
        "tools/list": CacheHint(ttl_ms=60000, scope="private"),
    },
)

register_tools(server)


def main():
    if os.getenv("MCP_TRANSPORT") == "http":
        server.run("streamable-http", host="127.0.0.1", port=8080)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
