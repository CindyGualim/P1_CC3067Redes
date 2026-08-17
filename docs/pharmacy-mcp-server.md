# Pharmacy MCP Server — Specification

Custom MCP server for the CC3067 Redes project (requirement 5). It models a
pharmacy chain: symptom-driven catalogue search, stock per branch, medical
prescription validation and purchase orders.

The protocol is implemented by hand. No MCP SDK is used: every frame is built
and parsed by `src/core/jsonrpc.py` and `src/core/mcp/server.py`.

---

## 1. Identity and transport

| Field | Value |
| --- | --- |
| `serverInfo.name` | `pharmacy-mcp-server` |
| `serverInfo.version` | `1.0.0` |
| `serverInfo.title` | `Farmacia Vida - Servidor MCP` |
| Protocol | JSON-RPC 2.0 over MCP |
| Preferred protocol version | `2025-11-25` (accepts `2025-06-18`, `2025-03-26`, `2024-11-05`) |
| Transport (delivery 1) | **stdio** — one JSON object per line, UTF-8, `\n` delimited |
| Transport (delivery 2) | Streamable HTTP on Google Cloud Run |
| Declared capabilities | `{"tools": {"listChanged": false}}` |

There is no HTTP endpoint in this delivery: the host launches the server as a
child process and writes to its stdin. **stdout carries protocol frames only**;
all diagnostics go to stderr, which the host drains into `logs/app.log`.

## 2. Supported methods

| Method | Type | Purpose |
| --- | --- | --- |
| `initialize` | request | Version negotiation, capability exchange |
| `notifications/initialized` | notification | Client confirms the handshake; no reply |
| `ping` | request | Liveness check, answered at any point |
| `tools/list` | request | Returns the seven tools with their JSON Schemas |
| `tools/call` | request | Executes one tool |

Anything else is answered with `-32601 Method not found`. `tools/list` and
`tools/call` before `initialize` are answered with `-32002`.

## 3. Tools

| Tool | Required arguments | Optional | Mutates state |
| --- | --- | --- | --- |
| `list_branches` | — | — | no |
| `search_medicines` | — (one of `query` / `symptom`) | `query`, `symptom`, `prescription_filter`, `limit` | no |
| `get_medicine_details` | `sku` | — | no |
| `check_inventory` | — | `sku`, `branch_id` | no |
| `verify_prescription` | `folio` | `patient_id` | no |
| `create_purchase_order` | `branch_id`, `customer_name`, `items` | `customer_id`, `prescription_folio` | **yes** |
| `get_order` | `order_id` | — | no |

### 3.1 `search_medicines`

Finds medicines by symptom and/or free text. Both criteria are matched against
accent-normalized columns, so `congestión` and `congestion` behave the same.

| Argument | Type | Notes |
| --- | --- | --- |
| `query` | string | Commercial name, active ingredient or category |
| `symptom` | string | Symptom in Spanish, e.g. `dolor de cabeza` |
| `prescription_filter` | enum | `any` (default), `otc_only`, `prescription_only` |
| `limit` | integer ≥ 1 | Default 5 |

Returns `{"count": n, "results": [...]}`, each result carrying `sku`, `name`,
`presentation`, `unit_price`, `currency`, `requires_prescription`,
`controlled`, `category`, `symptoms` and `total_stock` across all branches.

### 3.2 `get_medicine_details`

Full record for one `sku`, including `contraindications` and an `availability`
array with stock, lot and expiry date per branch.

### 3.3 `check_inventory`

Stock rows filtered by `sku` and/or `branch_id`. With no arguments it returns
the whole inventory. Response includes `total_stock` and the matching `rows`.

### 3.4 `verify_prescription`

Validates a prescription by `folio`. Returns the prescription plus:

- `valid`: boolean verdict,
- `problems`: list of reasons when `valid` is false,
- `items[]`: each with `quantity_prescribed`, `quantity_dispensed` and
  `quantity_remaining`.

A prescription is rejected when it is cancelled, expired, fully dispensed, or
when the supplied `patient_id` does not match the patient on record.

### 3.5 `create_purchase_order`

The only tool that writes. `items` is an array of `{sku, quantity}` with
`quantity` an integer ≥ 1.

Rules enforced **before anything is written**, so a rejected order leaves the
database untouched:

1. The branch exists and every SKU exists.
2. The branch has enough stock. If another branch does, the error names it.
3. Every item with `requires_prescription: true` needs a `prescription_folio`.
4. That prescription must be valid and must list the item with enough
   `quantity_remaining`.
5. Totals are computed with Guatemala's 12 % VAT, rounded to cents.

On success, in a single SQLite transaction: the order and its lines are
inserted, branch stock is decremented, the dispensed quantities of the
prescription are increased, and the prescription is closed (`status`
`dispensed`) once nothing is pending.

### 3.6 `get_order`

Retrieves a stored order by id (`ORD-YYYYMMDD-NNNN`) with its lines and totals.

## 4. Error model

Two different failure channels, and the distinction is deliberate:

**Protocol errors** — a JSON-RPC `error` object. The call was malformed.

| Code | Meaning |
| --- | --- |
| `-32700` | Parse error: the line was not valid JSON |
| `-32600` | Invalid request: not a JSON-RPC 2.0 message |
| `-32601` | Method not found |
| `-32602` | Invalid params: unknown tool, missing/typed-wrong argument |
| `-32603` | Internal error |
| `-32002` | Called before `initialize` (application-defined) |

**Tool errors** — a *successful* response whose result has `isError: true` and a
human-readable Spanish message. These are business outcomes ("prescription
expired", "not enough stock") that the LLM is meant to read and act on. Turning
them into protocol errors would break the conversation instead of guiding it.

Arguments are validated against each tool's `inputSchema` (required keys, types,
enums, minimums, nested array items) before the handler runs.

## 5. Data model

SQLite, built on first run from `data/pharmacy_seed.json`:

```
branches(id, name, address, phone)
medicines(sku, name, search_name, active_ingredient, presentation, form,
          unit_price, requires_prescription, controlled, category,
          contraindications)
medicine_symptoms(sku, symptom, search_symptom)
inventory(sku, branch_id, stock, lot, expiry_date)
prescriptions(folio, patient_name, patient_id, doctor_name, doctor_license,
              diagnosis, issued_at, expires_at, status)
prescription_items(folio, sku, quantity_prescribed, quantity_dispensed)
orders(id, created_at, branch_id, customer_name, customer_id,
       prescription_folio, subtotal, tax, total, status)
order_items(order_id, sku, quantity, unit_price, line_total)
```

Seed contents: 3 branches, 16 medicines (6 prescription-only, 2 of them
controlled), 48 inventory rows and 5 prescriptions covering the interesting
cases — valid, expired, already dispensed, partially dispensed, and controlled.
Some stock values are zero on purpose, to exercise the out-of-stock path.

Currency is `GTQ`, VAT rate `0.12`.

## 6. Running it

Standalone (it will wait for JSON-RPC frames on stdin):

```bash
.venv\Scripts\python.exe src/servers/pharmacy/__main__.py
```

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PHARMACY_DB` | `data/pharmacy.db` | SQLite file to use |
| `PHARMACY_SEED` | `data/pharmacy_seed.json` | Seed dataset |
| `PHARMACY_LOG_LEVEL` | `INFO` | Verbosity on stderr |

Scripted demo of the whole scenario, with the protocol log printed at the end:

```bash
.venv\Scripts\python.exe scripts/demo_pharmacy.py
```

To try it from Claude Desktop instead, add to its `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pharmacy": {
      "command": "C:\\ruta\\al\\proyecto\\.venv\\Scripts\\python.exe",
      "args": ["C:\\ruta\\al\\proyecto\\src\\servers\\pharmacy\\__main__.py"]
    }
  }
}
```

## 7. Wire examples

Handshake:

```json
--> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"pharmacy-mcp-host","version":"0.1.0"}}}
<-- {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"pharmacy-mcp-server","version":"1.0.0"},"instructions":"Servidor de una cadena de farmacias..."}}
--> {"jsonrpc":"2.0","method":"notifications/initialized"}
```

Prescription check:

```json
--> {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"verify_prescription","arguments":{"folio":"RX-2026-0001"}}}
<-- {"jsonrpc":"2.0","id":6,"result":{"content":[{"type":"text","text":"Receta RX-2026-0001: VALIDA..."}],"isError":false,"structuredContent":{"folio":"RX-2026-0001","valid":true,"problems":[],"items":[{"sku":"MED-005","quantity_remaining":1}]}}}
```

Business rejection — note it is a **successful** response:

```json
--> {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"create_purchase_order","arguments":{"branch_id":"SUC-01","customer_name":"Ana Lucia Morales","items":[{"sku":"MED-005","quantity":1}]}}}
<-- {"jsonrpc":"2.0","id":5,"result":{"content":[{"type":"text","text":"Los siguientes medicamentos requieren receta medica: Amoxicilina 500 mg. Proporcione el folio de la receta para continuar."}],"isError":true}}
```

Schema violation — this one *is* a protocol error:

```json
--> {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"get_medicine_details","arguments":{}}}
<-- {"jsonrpc":"2.0","id":7,"error":{"code":-32602,"message":"Missing required argument: 'sku'"}}
```

## 8. Limitations

- The catalogue is fixed at startup, hence `listChanged: false`.
- Search is substring matching over normalized text, not semantic search; the
  LLM is what maps "me duele la cabeza" to the symptom `dolor de cabeza`.
- No authentication: any client that can spawn the process is trusted. Access
  control becomes relevant with the remote transport of delivery 2.
- Prescriptions are seeded, not issued: there is no tool to create one, because
  the use case is dispensing, not prescribing.
- The server does not diagnose. Its `instructions` tell the model to recommend
  consulting a health professional.
