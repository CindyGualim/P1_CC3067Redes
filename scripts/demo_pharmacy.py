"""Walk through the pharmacy scenario end to end, printing the MCP traffic.

    python scripts/demo_pharmacy.py

It spawns the real server as a child process and drives it with the real host
client, so what you see here is exactly what the chatbot will exchange once the
LLM is wired in.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.logging_setup import setup_logging  # noqa: E402
from core.mcp.client import MCPClient  # noqa: E402
from core.mcp.protocol_log import ProtocolLogger  # noqa: E402
from core.transport.stdio import StdioTransport, configure_event_loop  # noqa: E402

SERVER = PROJECT_ROOT / "src" / "servers" / "pharmacy" / "__main__.py"
RULE = "=" * 78


def step(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


async def main() -> None:
    setup_logging(PROJECT_ROOT / "logs", level="WARNING", console=True)
    protocol_logger = ProtocolLogger(PROJECT_ROOT / "logs")

    transport = StdioTransport(
        [sys.executable, str(SERVER)],
        name="pharmacy",
        env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )

    async with MCPClient(transport, protocol_logger=protocol_logger) as client:
        step("1. Handshake")
        print(f"Servidor : {client.server_info.name} v{client.server_info.version}")
        print(f"Protocolo: {client.protocol_version}")

        step("2. Catalogo de herramientas (tools/list)")
        for tool in await client.list_tools():
            required = ", ".join(tool.inputSchema.get("required", [])) or "-"
            print(f"- {tool.name:<22} requeridos: {required}")

        step("3. El paciente describe un sintoma")
        print((await client.call_tool("search_medicines", {"symptom": "tos con flema"})).as_text())

        step("4. Detalle del medicamento elegido")
        print((await client.call_tool("get_medicine_details", {"sku": "MED-010"})).as_text())

        step("5. Intento de comprar un antibiotico sin receta")
        refused = await client.call_tool(
            "create_purchase_order",
            {
                "branch_id": "SUC-01",
                "customer_name": "Ana Lucia Morales",
                "items": [{"sku": "MED-005", "quantity": 1}],
            },
        )
        print(f"isError = {refused.isError}\n{refused.as_text()}")

        step("6. Verificacion de la receta")
        print((await client.call_tool("verify_prescription", {"folio": "RX-2026-0001"})).as_text())

        step("7. Orden de compra con receta valida")
        order = await client.call_tool(
            "create_purchase_order",
            {
                "branch_id": "SUC-01",
                "customer_name": "Ana Lucia Morales",
                "customer_id": "2547891230101",
                "prescription_folio": "RX-2026-0001",
                "items": [{"sku": "MED-005", "quantity": 1}, {"sku": "MED-010", "quantity": 1}],
            },
        )
        print(f"isError = {order.isError}\n{order.as_text()}")

    step("Log del protocolo MCP")
    for entry in protocol_logger.snapshot():
        print(entry.summary())
        print("    " + json.dumps(entry.payload, ensure_ascii=False)[:150])
    print(f"\nConteos: {protocol_logger.stats()}")
    print(f"Archivo: {protocol_logger.path}")


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
