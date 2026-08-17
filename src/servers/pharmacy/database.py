"""SQLite data layer for the pharmacy MCP server.

The database is built from ``data/pharmacy_seed.json`` the first time the server
runs, so the repository stays readable (the seed is plain JSON a reviewer can
open) while the server gets real transactional behaviour: dispensing a
prescription decrements stock and marks the prescription in the same
transaction, and a failure half-way rolls everything back.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class PharmacyError(Exception):
    """A business rule was violated (no stock, invalid prescription, ...).

    Kept independent of the protocol layer: ``tools.py`` is what turns it into
    an MCP ``isError`` result.
    """


def normalize(text: str) -> str:
    """Lowercase and strip accents, so 'Congestión' matches 'congestion'."""
    decomposed = unicodedata.normalize("NFD", text.lower().strip())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT NOT NULL,
    phone       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medicines (
    sku                   TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    search_name           TEXT NOT NULL,   -- normalized, for accent-free search
    active_ingredient     TEXT NOT NULL,
    presentation          TEXT NOT NULL,
    form                  TEXT NOT NULL,
    unit_price            REAL NOT NULL,
    requires_prescription INTEGER NOT NULL,
    controlled            INTEGER NOT NULL,
    category              TEXT NOT NULL,
    contraindications     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medicine_symptoms (
    sku             TEXT NOT NULL REFERENCES medicines(sku),
    symptom         TEXT NOT NULL,
    search_symptom  TEXT NOT NULL,
    PRIMARY KEY (sku, symptom)
);

CREATE TABLE IF NOT EXISTS inventory (
    sku         TEXT NOT NULL REFERENCES medicines(sku),
    branch_id   TEXT NOT NULL REFERENCES branches(id),
    stock       INTEGER NOT NULL CHECK (stock >= 0),
    lot         TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    PRIMARY KEY (sku, branch_id)
);

CREATE TABLE IF NOT EXISTS prescriptions (
    folio          TEXT PRIMARY KEY,
    patient_name   TEXT NOT NULL,
    patient_id     TEXT NOT NULL,
    doctor_name    TEXT NOT NULL,
    doctor_license TEXT NOT NULL,
    diagnosis      TEXT NOT NULL,
    issued_at      TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    status         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prescription_items (
    folio               TEXT NOT NULL REFERENCES prescriptions(folio),
    sku                 TEXT NOT NULL REFERENCES medicines(sku),
    quantity_prescribed INTEGER NOT NULL,
    quantity_dispensed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folio, sku)
);

CREATE TABLE IF NOT EXISTS orders (
    id                 TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    branch_id          TEXT NOT NULL REFERENCES branches(id),
    customer_name      TEXT NOT NULL,
    customer_id        TEXT,
    prescription_folio TEXT REFERENCES prescriptions(folio),
    subtotal           REAL NOT NULL,
    tax                REAL NOT NULL,
    total              REAL NOT NULL,
    status             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    order_id   TEXT NOT NULL REFERENCES orders(id),
    sku        TEXT NOT NULL REFERENCES medicines(sku),
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    line_total REAL NOT NULL,
    PRIMARY KEY (order_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_symptom ON medicine_symptoms(search_symptom);
CREATE INDEX IF NOT EXISTS idx_inventory_branch ON inventory(branch_id);
"""


class PharmacyDatabase:
    def __init__(self, db_path: Path, seed_path: Path) -> None:
        self.db_path = Path(db_path)
        self.seed_path = Path(seed_path)
        self.currency = "GTQ"
        self.tax_rate = 0.12
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False keeps the door open for the threaded HTTP
        # server of the second delivery; access stays serialized by the loop.
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._seed()

    # ------------------------------------------------------------------ #
    # Seeding
    # ------------------------------------------------------------------ #
    def _seed(self) -> None:
        seed = json.loads(self.seed_path.read_text(encoding="utf-8"))
        self.currency = seed.get("currency", "GTQ")
        self.tax_rate = float(seed.get("tax_rate", 0.12))

        already = self.conn.execute("SELECT COUNT(*) AS n FROM medicines").fetchone()["n"]
        if already:
            logger.debug("Database already seeded with %d medicines", already)
            return

        with self.conn:
            self.conn.executemany(
                "INSERT INTO branches (id, name, address, phone) VALUES (?,?,?,?)",
                [(b["id"], b["name"], b["address"], b["phone"]) for b in seed["branches"]],
            )
            for med in seed["medicines"]:
                self.conn.execute(
                    """INSERT INTO medicines (sku, name, search_name, active_ingredient,
                           presentation, form, unit_price, requires_prescription,
                           controlled, category, contraindications)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        med["sku"], med["name"],
                        normalize(med["name"] + " " + med["active_ingredient"] + " " + med["category"]),
                        med["active_ingredient"], med["presentation"], med["form"],
                        med["unit_price"], int(med["requires_prescription"]),
                        int(med["controlled"]), med["category"], med["contraindications"],
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO medicine_symptoms (sku, symptom, search_symptom) VALUES (?,?,?)",
                    [(med["sku"], s, normalize(s)) for s in med["symptoms"]],
                )
            self.conn.executemany(
                "INSERT INTO inventory (sku, branch_id, stock, lot, expiry_date) VALUES (?,?,?,?,?)",
                [
                    (i["sku"], i["branch_id"], i["stock"], i["lot"], i["expiry_date"])
                    for i in seed["inventory"]
                ],
            )
            for rx in seed["prescriptions"]:
                self.conn.execute(
                    """INSERT INTO prescriptions (folio, patient_name, patient_id, doctor_name,
                           doctor_license, diagnosis, issued_at, expires_at, status)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        rx["folio"], rx["patient_name"], rx["patient_id"], rx["doctor_name"],
                        rx["doctor_license"], rx["diagnosis"], rx["issued_at"],
                        rx["expires_at"], rx["status"],
                    ),
                )
                self.conn.executemany(
                    """INSERT INTO prescription_items (folio, sku, quantity_prescribed,
                           quantity_dispensed) VALUES (?,?,?,?)""",
                    [
                        (rx["folio"], it["sku"], it["quantity_prescribed"], it["quantity_dispensed"])
                        for it in rx["items"]
                    ],
                )
        logger.info("Seeded database from %s", self.seed_path.name)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Catalogue
    # ------------------------------------------------------------------ #
    def list_branches(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM branches ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def search_medicines(
        self,
        *,
        query: Optional[str] = None,
        symptom: Optional[str] = None,
        prescription_filter: str = "any",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find medicines by free text and/or symptom.

        Both criteria are matched against the normalized columns with LIKE, so
        partial words and missing accents still hit ('conges' -> 'congestion').
        """
        clauses: List[str] = []
        params: List[Any] = []

        if query:
            clauses.append("m.search_name LIKE ?")
            params.append(f"%{normalize(query)}%")
        if symptom:
            clauses.append(
                "EXISTS (SELECT 1 FROM medicine_symptoms s "
                "WHERE s.sku = m.sku AND s.search_symptom LIKE ?)"
            )
            params.append(f"%{normalize(symptom)}%")
        if prescription_filter == "otc_only":
            clauses.append("m.requires_prescription = 0")
        elif prescription_filter == "prescription_only":
            clauses.append("m.requires_prescription = 1")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""SELECT m.*, COALESCE(SUM(i.stock), 0) AS total_stock
                FROM medicines m LEFT JOIN inventory i ON i.sku = m.sku
                {where}
                GROUP BY m.sku
                ORDER BY total_stock DESC, m.name
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [self._medicine_row(row) for row in rows]

    def get_medicine(self, sku: str) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM medicines WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise PharmacyError(f"No existe el medicamento con SKU '{sku}'.")
        medicine = self._medicine_row(row)
        medicine["symptoms"] = self._symptoms(sku)
        medicine["contraindications"] = row["contraindications"]
        medicine["availability"] = self.get_inventory(sku=sku)
        return medicine

    def _medicine_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        keys = row.keys()
        medicine = {
            "sku": row["sku"],
            "name": row["name"],
            "active_ingredient": row["active_ingredient"],
            "presentation": row["presentation"],
            "form": row["form"],
            "unit_price": round(row["unit_price"], 2),
            "currency": self.currency,
            "requires_prescription": bool(row["requires_prescription"]),
            "controlled": bool(row["controlled"]),
            "category": row["category"],
        }
        if "total_stock" in keys:
            medicine["total_stock"] = row["total_stock"]
            medicine["symptoms"] = self._symptoms(row["sku"])
        return medicine

    def _symptoms(self, sku: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT symptom FROM medicine_symptoms WHERE sku = ? ORDER BY symptom", (sku,)
        ).fetchall()
        return [row["symptom"] for row in rows]

    # ------------------------------------------------------------------ #
    # Inventory
    # ------------------------------------------------------------------ #
    def get_inventory(
        self, *, sku: Optional[str] = None, branch_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if sku:
            self._assert_medicine(sku)
            clauses.append("i.sku = ?")
            params.append(sku)
        if branch_id:
            self._assert_branch(branch_id)
            clauses.append("i.branch_id = ?")
            params.append(branch_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""SELECT i.sku, m.name, i.branch_id, b.name AS branch_name,
                       i.stock, i.lot, i.expiry_date
                FROM inventory i
                JOIN medicines m ON m.sku = i.sku
                JOIN branches b ON b.id = i.branch_id
                {where}
                ORDER BY i.sku, i.branch_id""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Prescriptions
    # ------------------------------------------------------------------ #
    def get_prescription(self, folio: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM prescriptions WHERE folio = ?", (folio.strip().upper(),)
        ).fetchone()
        if row is None:
            raise PharmacyError(f"No se encontro ninguna receta con folio '{folio}'.")

        items = self.conn.execute(
            """SELECT p.sku, m.name, m.requires_prescription, m.controlled,
                      p.quantity_prescribed, p.quantity_dispensed,
                      (p.quantity_prescribed - p.quantity_dispensed) AS quantity_remaining
               FROM prescription_items p JOIN medicines m ON m.sku = p.sku
               WHERE p.folio = ?""",
            (row["folio"],),
        ).fetchall()

        prescription = dict(row)
        prescription["items"] = [
            {
                **dict(item),
                "requires_prescription": bool(item["requires_prescription"]),
                "controlled": bool(item["controlled"]),
            }
            for item in items
        ]
        return prescription

    def validate_prescription(
        self, folio: str, patient_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return the prescription plus a verdict the LLM can quote verbatim."""
        prescription = self.get_prescription(folio)
        today = date.today().isoformat()

        problems: List[str] = []
        if prescription["status"] == "cancelled":
            problems.append("La receta fue anulada.")
        if prescription["expires_at"] < today:
            problems.append(f"La receta vencio el {prescription['expires_at']}.")
        if all(item["quantity_remaining"] <= 0 for item in prescription["items"]):
            problems.append("Todos los medicamentos de la receta ya fueron despachados.")
        if patient_id and patient_id.strip() != prescription["patient_id"]:
            problems.append(
                "El documento de identificacion no coincide con el paciente de la receta."
            )

        prescription["valid"] = not problems
        prescription["problems"] = problems
        prescription["checked_at"] = today
        return prescription

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    def create_order(
        self,
        *,
        branch_id: str,
        customer_name: str,
        items: Sequence[Dict[str, Any]],
        customer_id: Optional[str] = None,
        prescription_folio: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate, price and register a purchase order atomically.

        Every rule is checked *before* anything is written, so a rejected order
        leaves no trace and the customer gets one complete explanation instead
        of a partially applied change.
        """
        self._assert_branch(branch_id)
        if not items:
            raise PharmacyError("La orden debe incluir al menos un medicamento.")

        # 1. Merge duplicated lines and validate quantities.
        wanted: Dict[str, int] = {}
        for item in items:
            sku = str(item.get("sku", "")).strip().upper()
            quantity = int(item.get("quantity", 0))
            if quantity <= 0:
                raise PharmacyError(f"La cantidad para '{sku}' debe ser mayor a cero.")
            wanted[sku] = wanted.get(sku, 0) + quantity

        # 2. Stock and catalogue.
        lines: List[Dict[str, Any]] = []
        needs_prescription: List[str] = []
        for sku, quantity in wanted.items():
            medicine = self.conn.execute(
                "SELECT * FROM medicines WHERE sku = ?", (sku,)
            ).fetchone()
            if medicine is None:
                raise PharmacyError(f"No existe el medicamento con SKU '{sku}'.")

            stock_row = self.conn.execute(
                "SELECT stock FROM inventory WHERE sku = ? AND branch_id = ?",
                (sku, branch_id),
            ).fetchone()
            available = stock_row["stock"] if stock_row else 0
            if available < quantity:
                elsewhere = [
                    f"{row['branch_name']} ({row['stock']})"
                    for row in self.get_inventory(sku=sku)
                    if row["branch_id"] != branch_id and row["stock"] >= quantity
                ]
                hint = f" Disponible en: {', '.join(elsewhere)}." if elsewhere else ""
                raise PharmacyError(
                    f"Stock insuficiente de {medicine['name']} en {branch_id}: "
                    f"se piden {quantity} y hay {available}.{hint}"
                )

            if medicine["requires_prescription"]:
                needs_prescription.append(sku)

            unit_price = round(medicine["unit_price"], 2)
            lines.append(
                {
                    "sku": sku,
                    "name": medicine["name"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "line_total": round(unit_price * quantity, 2),
                    "requires_prescription": bool(medicine["requires_prescription"]),
                }
            )

        # 3. Prescription rules for the items that need one.
        prescription = None
        if needs_prescription:
            if not prescription_folio:
                names = ", ".join(line["name"] for line in lines if line["requires_prescription"])
                raise PharmacyError(
                    f"Los siguientes medicamentos requieren receta medica: {names}. "
                    "Proporcione el folio de la receta para continuar."
                )
            prescription = self.validate_prescription(prescription_folio, customer_id)
            if not prescription["valid"]:
                raise PharmacyError(
                    "La receta " + prescription["folio"] + " no es valida: "
                    + " ".join(prescription["problems"])
                )
            covered = {item["sku"]: item for item in prescription["items"]}
            for sku in needs_prescription:
                item = covered.get(sku)
                if item is None:
                    raise PharmacyError(
                        f"La receta {prescription['folio']} no incluye el medicamento {sku}."
                    )
                if item["quantity_remaining"] < wanted[sku]:
                    raise PharmacyError(
                        f"La receta solo tiene {item['quantity_remaining']} unidad(es) "
                        f"pendientes de {item['name']}, se solicitaron {wanted[sku]}."
                    )

            # Anything else in the order that the prescription also lists counts
            # as dispensed, capped at what is pending. An over-the-counter item
            # is not limited by the prescription, it is only marked off it.
            to_dispense = {
                sku: min(quantity, covered[sku]["quantity_remaining"])
                for sku, quantity in wanted.items()
                if sku in covered and covered[sku]["quantity_remaining"] > 0
            }

        # 4. Totals. IVA of Guatemala, rounded to cents at each step.
        subtotal = round(sum(line["line_total"] for line in lines), 2)
        tax = round(subtotal * self.tax_rate, 2)
        total = round(subtotal + tax, 2)
        order_id = self._next_order_id()
        created_at = datetime.now().isoformat(timespec="seconds")

        # 5. Write everything in a single transaction.
        with self.conn:
            self.conn.execute(
                """INSERT INTO orders (id, created_at, branch_id, customer_name, customer_id,
                       prescription_folio, subtotal, tax, total, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_id, created_at, branch_id, customer_name, customer_id,
                    prescription["folio"] if prescription else None,
                    subtotal, tax, total, "confirmed",
                ),
            )
            self.conn.executemany(
                """INSERT INTO order_items (order_id, sku, quantity, unit_price, line_total)
                   VALUES (?,?,?,?,?)""",
                [
                    (order_id, line["sku"], line["quantity"], line["unit_price"], line["line_total"])
                    for line in lines
                ],
            )
            for sku, quantity in wanted.items():
                self.conn.execute(
                    "UPDATE inventory SET stock = stock - ? WHERE sku = ? AND branch_id = ?",
                    (quantity, sku, branch_id),
                )
            if prescription:
                for sku, quantity in to_dispense.items():
                    self.conn.execute(
                        """UPDATE prescription_items
                           SET quantity_dispensed = quantity_dispensed + ?
                           WHERE folio = ? AND sku = ?""",
                        (quantity, prescription["folio"], sku),
                    )
                self._close_prescription_if_complete(prescription["folio"])

        return self.get_order(order_id)

    def _close_prescription_if_complete(self, folio: str) -> None:
        pending = self.conn.execute(
            """SELECT COUNT(*) AS n FROM prescription_items
               WHERE folio = ? AND quantity_dispensed < quantity_prescribed""",
            (folio,),
        ).fetchone()["n"]
        if not pending:
            self.conn.execute(
                "UPDATE prescriptions SET status = 'dispensed' WHERE folio = ?", (folio,)
            )

    def _next_order_id(self) -> str:
        stamp = date.today().strftime("%Y%m%d")
        used = self.conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE id LIKE ?", (f"ORD-{stamp}-%",)
        ).fetchone()["n"]
        return f"ORD-{stamp}-{used + 1:04d}"

    def get_order(self, order_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT o.*, b.name AS branch_name FROM orders o JOIN branches b ON b.id = o.branch_id "
            "WHERE o.id = ?",
            (order_id.strip().upper(),),
        ).fetchone()
        if row is None:
            raise PharmacyError(f"No existe la orden '{order_id}'.")

        items = self.conn.execute(
            """SELECT i.sku, m.name, i.quantity, i.unit_price, i.line_total
               FROM order_items i JOIN medicines m ON m.sku = i.sku
               WHERE i.order_id = ?""",
            (row["id"],),
        ).fetchall()

        order = dict(row)
        order["currency"] = self.currency
        order["tax_rate"] = self.tax_rate
        order["items"] = [dict(item) for item in items]
        return order

    # ------------------------------------------------------------------ #
    # Guards
    # ------------------------------------------------------------------ #
    def _assert_branch(self, branch_id: str) -> None:
        found = self.conn.execute(
            "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
        ).fetchone()
        if not found:
            valid = ", ".join(branch["id"] for branch in self.list_branches())
            raise PharmacyError(f"La sucursal '{branch_id}' no existe. Validas: {valid}.")

    def _assert_medicine(self, sku: str) -> None:
        found = self.conn.execute("SELECT 1 FROM medicines WHERE sku = ?", (sku,)).fetchone()
        if not found:
            raise PharmacyError(f"No existe el medicamento con SKU '{sku}'.")
