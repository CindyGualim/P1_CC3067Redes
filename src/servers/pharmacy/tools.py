"""Tool catalogue of the pharmacy MCP server.

Each entry pairs a JSON Schema (what the LLM is allowed to send) with a handler
(what actually runs). Two conventions are followed everywhere:

* ``description`` is written for the model, not for a developer: it says when to
  reach for the tool, because that text is all the LLM has to choose with.
* the handler returns a readable Spanish summary *and* the structured payload.
  The model quotes the summary, the TUI renders the structure.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from core.mcp.server import MCPServer, ToolError, ToolOutput
from servers.pharmacy.database import PharmacyDatabase, PharmacyError

SERVER_NAME = "pharmacy-mcp-server"
SERVER_VERSION = "1.0.0"

INSTRUCTIONS = (
    "Servidor de una cadena de farmacias. Permite buscar medicamentos por sintoma o "
    "por nombre, consultar existencias por sucursal, verificar la validez de una receta "
    "medica y generar ordenes de compra. Los medicamentos marcados con "
    "requires_prescription solo pueden venderse con un folio de receta vigente. "
    "Este servidor no diagnostica: siempre recomienda consultar a un profesional de la "
    "salud antes de iniciar cualquier tratamiento."
)


def _money(amount: float, currency: str = "GTQ") -> str:
    symbol = "Q" if currency == "GTQ" else ""
    return f"{symbol}{amount:,.2f}"


def _guard(handler: Callable[[Dict[str, Any]], ToolOutput]) -> Callable[[Dict[str, Any]], ToolOutput]:
    """Translate domain failures into MCP tool errors.

    This is the seam between the pharmacy rules and the protocol: the database
    knows nothing about MCP, and the server never sees a PharmacyError.
    """

    def wrapper(arguments: Dict[str, Any]) -> ToolOutput:
        try:
            return handler(arguments)
        except PharmacyError as exc:
            raise ToolError(str(exc)) from exc

    wrapper.__name__ = handler.__name__
    wrapper.__doc__ = handler.__doc__
    return wrapper


def build_server(db: PharmacyDatabase) -> MCPServer:
    """Register every pharmacy tool on a fresh MCP server instance."""
    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        title="Farmacia Vida - Servidor MCP",
        instructions=INSTRUCTIONS,
    )

    # ------------------------------------------------------------------ #
    # 1. list_branches
    # ------------------------------------------------------------------ #
    def list_branches(_: Dict[str, Any]) -> ToolOutput:
        branches = db.list_branches()
        lines = [f"- {b['id']}: {b['name']} | {b['address']} | Tel. {b['phone']}" for b in branches]
        return ToolOutput(
            text="Sucursales disponibles:\n" + "\n".join(lines),
            data={"branches": branches},
        )

    server.add_tool(
        name="list_branches",
        title="Listar sucursales",
        description=(
            "Devuelve las sucursales de la cadena con su direccion y telefono. "
            "Usalo cuando el usuario pregunte donde comprar o antes de generar una "
            "orden, para elegir el branch_id correcto."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_guard(list_branches),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )

    # ------------------------------------------------------------------ #
    # 2. search_medicines
    # ------------------------------------------------------------------ #
    def search_medicines(arguments: Dict[str, Any]) -> ToolOutput:
        query = arguments.get("query")
        symptom = arguments.get("symptom")
        if not query and not symptom:
            raise PharmacyError("Indique al menos 'query' o 'symptom' para buscar.")

        results = db.search_medicines(
            query=query,
            symptom=symptom,
            prescription_filter=arguments.get("prescription_filter", "any"),
            limit=int(arguments.get("limit", 5)),
        )
        if not results:
            criteria = symptom or query
            raise PharmacyError(
                f"No se encontraron medicamentos para '{criteria}'. "
                "Intente con otro sintoma o con el nombre del principio activo."
            )

        lines: List[str] = []
        for med in results:
            flag = "requiere receta" if med["requires_prescription"] else "venta libre"
            if med["controlled"]:
                flag = "medicamento controlado, requiere receta"
            lines.append(
                f"- [{med['sku']}] {med['name']} ({med['presentation']}) "
                f"- {_money(med['unit_price'], med['currency'])} - {flag} "
                f"- existencias totales: {med['total_stock']}"
            )
        header = f"Se encontraron {len(results)} medicamento(s):"
        return ToolOutput(
            text=header + "\n" + "\n".join(lines),
            data={"count": len(results), "results": results},
        )

    server.add_tool(
        name="search_medicines",
        title="Buscar medicamentos",
        description=(
            "Busca medicamentos por sintoma (por ejemplo 'dolor de cabeza', 'tos', "
            "'alergia') o por nombre / principio activo. Es el punto de partida cuando "
            "el usuario describe una molestia o menciona un medicamento. Devuelve SKU, "
            "precio, presentacion, si requiere receta y las existencias totales."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Nombre comercial, principio activo o categoria.",
                },
                "symptom": {
                    "type": "string",
                    "description": "Sintoma descrito por el paciente, en espaniol.",
                },
                "prescription_filter": {
                    "type": "string",
                    "enum": ["any", "otc_only", "prescription_only"],
                    "description": "Filtra por tipo de venta. Por defecto 'any'.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximo de resultados a devolver (por defecto 5).",
                },
            },
            "required": [],
        },
        handler=_guard(search_medicines),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )

    # ------------------------------------------------------------------ #
    # 3. get_medicine_details
    # ------------------------------------------------------------------ #
    def get_medicine_details(arguments: Dict[str, Any]) -> ToolOutput:
        medicine = db.get_medicine(str(arguments["sku"]).strip().upper())
        stock_lines = [
            f"  - {row['branch_name']}: {row['stock']} unidades "
            f"(lote {row['lot']}, vence {row['expiry_date']})"
            for row in medicine["availability"]
        ]
        text = (
            f"{medicine['name']} [{medicine['sku']}]\n"
            f"Principio activo : {medicine['active_ingredient']}\n"
            f"Presentacion     : {medicine['presentation']} ({medicine['form']})\n"
            f"Precio           : {_money(medicine['unit_price'], medicine['currency'])}\n"
            f"Categoria        : {medicine['category']}\n"
            f"Receta           : {'SI, requiere receta medica' if medicine['requires_prescription'] else 'venta libre'}"
            f"{' (medicamento controlado)' if medicine['controlled'] else ''}\n"
            f"Indicado para    : {', '.join(medicine['symptoms'])}\n"
            f"Advertencias     : {medicine['contraindications']}\n"
            f"Existencias:\n" + "\n".join(stock_lines)
        )
        return ToolOutput(text=text, data=medicine)

    server.add_tool(
        name="get_medicine_details",
        title="Detalle de un medicamento",
        description=(
            "Ficha completa de un medicamento por SKU: principio activo, presentacion, "
            "precio, sintomas que atiende, contraindicaciones y existencias por sucursal. "
            "Usalo despues de search_medicines cuando el usuario pida mas informacion o "
            "antes de recomendar un producto."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sku": {
                    "type": "string",
                    "description": "Codigo del medicamento, por ejemplo 'MED-001'.",
                }
            },
            "required": ["sku"],
        },
        handler=_guard(get_medicine_details),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )

    # ------------------------------------------------------------------ #
    # 4. check_inventory
    # ------------------------------------------------------------------ #
    def check_inventory(arguments: Dict[str, Any]) -> ToolOutput:
        sku = arguments.get("sku")
        rows = db.get_inventory(
            sku=str(sku).strip().upper() if sku else None,
            branch_id=arguments.get("branch_id"),
        )
        if not rows:
            raise PharmacyError("No hay registros de inventario para ese criterio.")

        lines = [
            f"- {row['name']} [{row['sku']}] en {row['branch_name']}: {row['stock']} unidades"
            + (" (AGOTADO)" if row["stock"] == 0 else "")
            for row in rows
        ]
        total = sum(row["stock"] for row in rows)
        return ToolOutput(
            text="\n".join(lines) + f"\nTotal: {total} unidades en {len(rows)} registro(s).",
            data={"total_stock": total, "rows": rows},
        )

    server.add_tool(
        name="check_inventory",
        title="Consultar inventario",
        description=(
            "Existencias de un medicamento por sucursal. Usalo para confirmar "
            "disponibilidad antes de generar una orden, o cuando el usuario pregunte si "
            "hay producto en una sucursal concreta. Sin argumentos devuelve todo el "
            "inventario."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Codigo del medicamento."},
                "branch_id": {
                    "type": "string",
                    "description": "Sucursal a consultar, por ejemplo 'SUC-01'.",
                },
            },
            "required": [],
        },
        handler=_guard(check_inventory),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )

    # ------------------------------------------------------------------ #
    # 5. verify_prescription
    # ------------------------------------------------------------------ #
    def verify_prescription(arguments: Dict[str, Any]) -> ToolOutput:
        prescription = db.validate_prescription(
            str(arguments["folio"]), arguments.get("patient_id")
        )
        verdict = "VALIDA" if prescription["valid"] else "NO VALIDA"
        item_lines = [
            f"  - {item['name']} [{item['sku']}]: recetadas {item['quantity_prescribed']}, "
            f"despachadas {item['quantity_dispensed']}, pendientes {item['quantity_remaining']}"
            for item in prescription["items"]
        ]
        text = (
            f"Receta {prescription['folio']}: {verdict}\n"
            f"Paciente   : {prescription['patient_name']} (DPI {prescription['patient_id']})\n"
            f"Medico     : {prescription['doctor_name']} ({prescription['doctor_license']})\n"
            f"Diagnostico: {prescription['diagnosis']}\n"
            f"Vigencia   : {prescription['issued_at']} a {prescription['expires_at']}\n"
            f"Medicamentos:\n" + "\n".join(item_lines)
        )
        if prescription["problems"]:
            text += "\nMotivos del rechazo: " + " ".join(prescription["problems"])
        return ToolOutput(text=text, data=prescription)

    server.add_tool(
        name="verify_prescription",
        title="Verificar receta medica",
        description=(
            "Valida una receta medica por folio: vigencia, estado, medico que la emitio "
            "y cantidades pendientes de despacho. Es obligatorio usarlo antes de vender "
            "cualquier medicamento con requires_prescription. Devuelve valid=false con "
            "el motivo cuando la receta esta vencida, anulada o ya fue despachada."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "folio": {
                    "type": "string",
                    "description": "Folio impreso en la receta, por ejemplo 'RX-2026-0001'.",
                },
                "patient_id": {
                    "type": "string",
                    "description": "DPI del paciente, para confirmar que la receta le pertenece.",
                },
            },
            "required": ["folio"],
        },
        handler=_guard(verify_prescription),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )

    # ------------------------------------------------------------------ #
    # 6. create_purchase_order
    # ------------------------------------------------------------------ #
    def create_purchase_order(arguments: Dict[str, Any]) -> ToolOutput:
        order = db.create_order(
            branch_id=str(arguments["branch_id"]).strip().upper(),
            customer_name=str(arguments["customer_name"]).strip(),
            items=arguments["items"],
            customer_id=arguments.get("customer_id"),
            prescription_folio=arguments.get("prescription_folio"),
        )
        return ToolOutput(text=_render_order(order), data=order)

    server.add_tool(
        name="create_purchase_order",
        title="Generar orden de compra",
        description=(
            "Genera una orden de compra, descuenta el inventario de la sucursal y "
            "registra el despacho contra la receta cuando aplica. Rechaza la operacion "
            "si falta stock, si un medicamento requiere receta y no se envio el folio, o "
            "si la receta no cubre las cantidades solicitadas. Confirme los productos y "
            "las cantidades con el usuario antes de llamarlo: la orden modifica datos."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "branch_id": {
                    "type": "string",
                    "description": "Sucursal que despacha, por ejemplo 'SUC-01'.",
                },
                "customer_name": {"type": "string", "description": "Nombre del cliente."},
                "customer_id": {
                    "type": "string",
                    "description": "DPI del cliente. Obligatorio si se usa una receta.",
                },
                "prescription_folio": {
                    "type": "string",
                    "description": "Folio de la receta que respalda los medicamentos controlados.",
                },
                "items": {
                    "type": "array",
                    "description": "Medicamentos a comprar.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string", "description": "Codigo del medicamento."},
                            "quantity": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "Unidades a comprar.",
                            },
                        },
                        "required": ["sku", "quantity"],
                    },
                },
            },
            "required": ["branch_id", "customer_name", "items"],
        },
        handler=_guard(create_purchase_order),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )

    # ------------------------------------------------------------------ #
    # 7. get_order
    # ------------------------------------------------------------------ #
    def get_order(arguments: Dict[str, Any]) -> ToolOutput:
        order = db.get_order(str(arguments["order_id"]))
        return ToolOutput(text=_render_order(order), data=order)

    server.add_tool(
        name="get_order",
        title="Consultar una orden",
        description=(
            "Recupera una orden de compra ya generada por su identificador, con el "
            "detalle de productos y totales. Usalo cuando el usuario pregunte por el "
            "estado de una compra."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Identificador de la orden, por ejemplo 'ORD-20260820-0001'.",
                }
            },
            "required": ["order_id"],
        },
        handler=_guard(get_order),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )

    return server


def _render_order(order: Dict[str, Any]) -> str:
    currency = order.get("currency", "GTQ")
    lines = [
        f"  - {item['name']} [{item['sku']}] x{item['quantity']} a "
        f"{_money(item['unit_price'], currency)} = {_money(item['line_total'], currency)}"
        for item in order["items"]
    ]
    receipt = (
        f"Orden {order['id']} ({order['status']})\n"
        f"Sucursal : {order['branch_name']} [{order['branch_id']}]\n"
        f"Cliente  : {order['customer_name']}\n"
        f"Fecha    : {order['created_at']}\n"
    )
    if order.get("prescription_folio"):
        receipt += f"Receta   : {order['prescription_folio']}\n"
    receipt += (
        "Detalle:\n" + "\n".join(lines) + "\n"
        f"Subtotal : {_money(order['subtotal'], currency)}\n"
        f"IVA {int(order.get('tax_rate', 0.12) * 100)}%   : {_money(order['tax'], currency)}\n"
        f"TOTAL    : {_money(order['total'], currency)}"
    )
    return receipt
