| | Envoy AI Gateway | agentgateway |
|---|---|---|
| initialize | http 200 | http 200 |
| advertised server name | `envoy-ai-gateway` | `agentgateway` |
| protocol version | `2025-06-18` | `2025-06-18` |
| capabilities advertised | `tools` | `prompts`, `resources`, `tools` |
| session id issued | yes | yes |
| tools after multiplexing | 5 | 5 |
| tool naming | `mcp-clock__get_time`<br>`mcp-clock__ping_host`<br>`mcp-math__add`<br>`mcp-math__multiply`<br>`mcp-math__get_time` | `clock_get_time`<br>`clock_ping_host`<br>`math_add`<br>`math_multiply`<br>`math_get_time` |
| tools/call #1 | `mcp-math__add` → [math] 2 + 40 = 42 | `math_add` → [math] 2 + 40 = 42 |
| tools/call #2 | `mcp-clock__ping_host` → [clock] example.com responded in 12ms | `clock_ping_host` → [clock] example.com responded in 12ms |
