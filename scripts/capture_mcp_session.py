"""Capture a full remote MCP session with Wireshark (tshark), for item 7.

    python scripts/capture_mcp_session.py

By default it starts the remote Pharmacy MCP server on loopback over plain
HTTP, records the traffic on the Npcap loopback adapter and drives a complete
session with the real host client: handshake, ``tools/list`` and several
``tools/call``.  The result is a ``.pcapng`` that can be opened in the
Wireshark GUI, plus the protocol log of the same session for cross-checking.

Plain HTTP on loopback is used on purpose: Cloud Run only accepts HTTPS, so a
capture against the deployed service shows the TLS handshake but not the
JSON-RPC bodies.  Pass ``--url`` to capture that case too -- the report uses
both, one for the application layer and one for the transport layer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.mcp.client import MCPClient  # noqa: E402
from core.mcp.protocol_log import ProtocolLogger  # noqa: E402
from core.transport.http import HttpTransport  # noqa: E402

DEFAULT_TSHARK = Path(r"C:\Program Files\Wireshark\tshark.exe")
LOOPBACK_INTERFACE = r"\Device\NPF_Loopback"
DEMO_TOKEN = "capture-demo-token"
RULE = "=" * 78


def find_tshark(explicit: str | None) -> str:
    """Locate tshark: the flag wins, then PATH, then the default install dir."""
    if explicit:
        return explicit

    from shutil import which

    found = which("tshark")
    if found:
        return found
    if DEFAULT_TSHARK.exists():
        return str(DEFAULT_TSHARK)
    raise SystemExit(
        "tshark not found. Install Wireshark (with Npcap) or pass --tshark <path>."
    )


def start_server(port: int) -> subprocess.Popen:
    """Run src/servers/pharmacy/remote.py against a throwaway database."""
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "PYTHONUTF8": "1",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "MCP_AUTH_TOKEN": DEMO_TOKEN,
        "PHARMACY_DB": str(PROJECT_ROOT / "logs" / "capture_pharmacy.db"),
        "PHARMACY_SEED": str(PROJECT_ROOT / "data" / "pharmacy_seed.json"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "servers.pharmacy.remote"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("The remote server exited before becoming ready.")
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (URLError, OSError):
            time.sleep(0.3)
    process.terminate()
    raise SystemExit("The remote server did not answer /health in time.")


def start_tshark(
    tshark: str, interface: str, capture_filter: str, output: Path
) -> subprocess.Popen:
    output.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [tshark, "-i", interface, "-f", capture_filter, "-w", str(output), "-q"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # tshark needs a moment to open the adapter; packets sent earlier are lost.
    time.sleep(3)
    if process.poll() is not None:
        detail = (process.stderr.read() or b"").decode(errors="replace")
        raise SystemExit(f"tshark could not start capturing:\n{detail}")
    return process


def stop_tshark(process: subprocess.Popen) -> None:
    # Give the stack time to flush the last ACK/FIN before closing the file.
    time.sleep(2)
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


async def drive_session(url: str, token: str, logger: ProtocolLogger) -> None:
    """One complete MCP session, so the capture holds every message type."""
    transport = HttpTransport(
        url, name="pharmacy_remote", headers={"Authorization": f"Bearer {token}"}
    )
    async with MCPClient(transport, protocol_logger=logger) as client:
        print(f"\n{RULE}\n1. Handshake\n{RULE}")
        print(f"Servidor : {client.server_info.name} v{client.server_info.version}")
        print(f"Protocolo: {client.protocol_version}")

        print(f"\n{RULE}\n2. tools/list\n{RULE}")
        for tool in await client.list_tools():
            print(f"- {tool.name}")

        print(f"\n{RULE}\n3. tools/call search_medicines\n{RULE}")
        found = await client.call_tool("search_medicines", {"symptom": "tos con flema"})
        print(found.as_text()[:400])

        print(f"\n{RULE}\n4. tools/call verify_prescription\n{RULE}")
        checked = await client.call_tool("verify_prescription", {"folio": "RX-2026-0001"})
        print(checked.as_text()[:400])

        print(f"\n{RULE}\n5. tools/call rechazado (antibiotico sin receta)\n{RULE}")
        refused = await client.call_tool(
            "create_purchase_order",
            {
                "branch_id": "SUC-01",
                "customer_name": "Ana Lucia Morales",
                "items": [{"sku": "MED-005", "quantity": 1}],
            },
        )
        print(f"isError = {refused.isError}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080, help="Local port for the server")
    parser.add_argument(
        "--url",
        help="Capture against an already deployed server (e.g. the Cloud Run URL) "
        "instead of starting a local one",
    )
    parser.add_argument("--token", default=DEMO_TOKEN, help="Bearer token used with --url")
    parser.add_argument(
        "--interface", help="Capture interface (default: loopback for local runs)"
    )
    parser.add_argument("--tshark", help="Path to tshark.exe")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "captures" / "mcp-remote-session.pcapng",
        help="Destination .pcapng file",
    )
    args = parser.parse_args()

    tshark = find_tshark(args.tshark)
    server = None

    if args.url:
        url, token = args.url, args.token
        interface = args.interface
        if not interface:
            raise SystemExit("--interface is required when capturing against --url.")
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        capture_filter = f"host {parsed.hostname} and tcp port {port}"
    else:
        url, token = f"http://127.0.0.1:{args.port}/mcp", DEMO_TOKEN
        interface = args.interface or LOOPBACK_INTERFACE
        capture_filter = f"tcp port {args.port}"
        print(f"Iniciando el servidor remoto en 127.0.0.1:{args.port} ...")
        server = start_server(args.port)

    print(f"Capturando en {interface} con filtro '{capture_filter}' ...")
    capture = start_tshark(tshark, interface, capture_filter, args.output)
    logger = ProtocolLogger(PROJECT_ROOT / "logs")

    try:
        asyncio.run(drive_session(url, token, logger))
    finally:
        stop_tshark(capture)
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    print(f"\n{RULE}\nResumen\n{RULE}")
    print(f"Captura     : {args.output}")
    print(f"Log MCP     : {logger.path}")
    print(f"Conteos MCP : {json.dumps(logger.stats(), ensure_ascii=False)}")
    print("\nSiguiente paso: python scripts/analyze_capture.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
