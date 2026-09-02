# SAF Adapter Contract Matrix

| Resource | Identity | Role | V0 transport | V2 transport |
|---|---|---|---|---|
| Pi CLI | agent://pi | coding runtime | subprocess | gRPC worker |
| Cursor CLI | agent://cursor-cli | coding runtime | subprocess | gRPC worker |
| Codex CLI | agent://codex-cli | coding runtime | subprocess | gRPC worker |
| OpenCode | agent://opencode | coding runtime | subprocess | gRPC worker |
| Aider | agent://aider | coding runtime | subprocess | gRPC worker |
| Kimi K3 | model://kimi-k3 | model | provider/local | remote inference |
| OpenRouter | model-provider://openrouter | model aggregation | HTTPS | service |
| Abacus.AI | provider://abacus-ai | model/agent/workflow | HTTPS/MCP | service |
| MCP | tool-protocol://mcp | tool interoperability | stdio/HTTP | remote MCP |
| VIA-X | interaction://via-x | browser/OS interaction | local | gateway |

No adapter may leak provider-specific objects into `saf/core`.
