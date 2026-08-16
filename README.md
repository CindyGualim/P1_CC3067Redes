# Pharmacy MCP Chatbot

Course project for **CC3067 Redes** (Universidad del Valle de Guatemala): a
terminal chatbot that acts as an **MCP host**, talking to several Model Context
Protocol servers over JSON-RPC 2.0.

The protocol is implemented **by hand**. No MCP SDK (FastMCP or otherwise) is
used anywhere in this repository: every `initialize`, `tools/list` and
`tools/call` frame is built, framed and parsed by the code under `src/core/`.

## Use case

A pharmacy chain assistant. Through its own MCP server the chatbot can look up
medicines by symptom or name, check stock across branches, validate a medical
prescription and turn it into a purchase order.

## Stack

| Piece | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11 | asyncio makes concurrent MCP sessions straightforward |
| LLM | Google Gemini (`google-genai`) | native function calling, generous free tier |
| Local transport | stdio (one JSON object per line) | what the official MCP servers speak |
| Remote transport | Streamable HTTP on Cloud Run *(delivery 2)* | keeps the same Python server behind a different transport |
| UI | Textual TUI | required to run in a terminal |
| Storage | SQLite | a real transactional inventory, no extra dependency |

## Project layout

```
src/core/
  jsonrpc.py            JSON-RPC 2.0 messages, builders, parser, error codes
  config.py             settings loaded from .env
  logging_setup.py      diagnostic logging (file + console)
  transport/
    base.py             Transport interface (stdio now, HTTP in delivery 2)
    stdio.py            runs an MCP server as a child process
  mcp/
    types.py            MCP data model and method names
    client.py           session: handshake, id correlation, tools
    protocol_log.py     audit log of every MCP message (requirement #3)
tests/                  unit tests + end-to-end session against a mock server
scripts/demo_protocol.py  prints a full annotated session
```

## Install

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
copy .env.example .env        # then paste your GEMINI_API_KEY
```

## Verify the protocol core

```bash
python -m pytest -q
```

```bash
python scripts/demo_protocol.py
```

The demo opens a real MCP session against `tests/fixtures/mock_mcp_server.py`
and prints every message exchanged, labelled as synchronization, request,
response or error. The same trace is appended to `logs/mcp_protocol_*.jsonl`.

## Roadmap

- [x] JSON-RPC 2.0 core, MCP session lifecycle, protocol log
- [ ] Pharmacy MCP server (local, stdio)
- [ ] Gemini host with conversation context and tool-calling loop
- [ ] Official Filesystem and Git MCP servers
- [ ] Textual TUI and written report
- [ ] Remote server on Cloud Run + Wireshark analysis *(delivery 2)*
