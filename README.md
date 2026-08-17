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
    stdio.py            client side: runs an MCP server as a child process
    stdio_server.py     server side: stdin/stdout loop
  mcp/
    types.py            MCP data model and method names
    client.py           session: handshake, id correlation, tools
    server.py           tool registry, dispatch, schema validation
    protocol_log.py     audit log of every MCP message (requirement #3)
src/servers/pharmacy/
  __main__.py           entry point of the pharmacy server
  tools.py              the seven tools and their JSON Schemas
  database.py           SQLite layer and the business rules
data/pharmacy_seed.json catalogue, inventory and prescriptions
docs/                   server specification
tests/                  unit, dispatch and end-to-end tests
scripts/                runnable demos
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

## The pharmacy MCP server

Seven tools: `list_branches`, `search_medicines`, `get_medicine_details`,
`check_inventory`, `verify_prescription`, `create_purchase_order` and
`get_order`. Full specification, arguments, error codes and wire examples in
[docs/pharmacy-mcp-server.md](docs/pharmacy-mcp-server.md).

Run the whole scenario — symptom, medicine details, an antibiotic refused for
lack of a prescription, prescription check, and the resulting order:

```bash
python scripts/demo_pharmacy.py
```

The server is a normal MCP server: it also works from Claude Desktop or any
other host, see the configuration snippet in the specification.

## Roadmap

- [x] JSON-RPC 2.0 core, MCP session lifecycle, protocol log
- [x] Pharmacy MCP server (local, stdio)
- [ ] Gemini host with conversation context and tool-calling loop
- [ ] Official Filesystem and Git MCP servers
- [ ] Textual TUI and written report
- [ ] Remote server on Cloud Run + Wireshark analysis *(delivery 2)*
