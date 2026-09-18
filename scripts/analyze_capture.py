"""Classify the JSON-RPC traffic inside a Wireshark capture (items 7 and 9).

    python scripts/analyze_capture.py [captures/mcp-remote-session.pcapng]

Reads the ``.pcapng`` produced by ``scripts/capture_mcp_session.py`` through
``tshark`` and prints three tables:

* **Application layer** -- every JSON-RPC message found in an HTTP body,
  labelled as *sincronizacion* (the ``initialize`` handshake and its
  ``notifications/initialized``), *solicitud*, *respuesta* or *error*, which is
  exactly what item 7 of the statement asks for.
* **Transport layer** -- the TCP handshake, the segments that carry each
  message and the connection teardown.
* **Link and network layers** -- encapsulation, addresses and MTU-relevant
  frame sizes.

The classification follows JSON-RPC 2.0 (https://www.jsonrpc.org/specification)
and the MCP lifecycle (https://modelcontextprotocol.io/specification):

    method + id  -> request        (solicitud, expects a response)
    method only  -> notification   (no response by definition)
    result       -> response       (respuesta exitosa)
    error        -> error response
"""

from __future__ import annotations

import argparse
import binascii
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TSHARK = Path(r"C:\Program Files\Wireshark\tshark.exe")
DEFAULT_CAPTURE = PROJECT_ROOT / "captures" / "mcp-remote-session.pcapng"

# MCP methods that belong to the session-synchronisation phase rather than to
# ordinary tool traffic.
SYNC_METHODS = {"initialize", "notifications/initialized"}

TCP_FLAG_NAMES = [
    (0x02, "SYN"),
    (0x10, "ACK"),
    (0x08, "PSH"),
    (0x01, "FIN"),
    (0x04, "RST"),
]

FIELDS = [
    "frame.number",
    "frame.time_relative",
    "frame.len",
    "frame.encap_type",
    "eth.src",
    "eth.dst",
    "ip.src",
    "ip.dst",
    "ip.ttl",
    "ip.proto",
    "ipv6.src",
    "ipv6.dst",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.len",
    "tcp.flags",
    "tcp.seq",
    "tcp.analysis.retransmission",
    "http.request.method",
    "http.request.uri",
    "http.response.code",
    "http.file_data",
    "tls.record.content_type",
    "tls.handshake.type",
]


def find_tshark(explicit: Optional[str]) -> str:
    if explicit:
        return explicit

    from shutil import which

    found = which("tshark")
    if found:
        return found
    if DEFAULT_TSHARK.exists():
        return str(DEFAULT_TSHARK)
    raise SystemExit("tshark not found. Install Wireshark or pass --tshark <path>.")


def read_packets(tshark: str, capture: Path) -> List[Dict[str, str]]:
    """Run tshark once and return one flat dict per frame."""
    command = [tshark, "-r", str(capture), "-T", "fields", "-E", "separator=\t"]
    for field in FIELDS:
        command += ["-e", field]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise SystemExit(f"tshark failed to read {capture}:\n{detail}")

    packets: List[Dict[str, str]] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        values = line.split("\t")
        values += [""] * (len(FIELDS) - len(values))
        packets.append(dict(zip(FIELDS, values)))
    return packets


def decode_bodies(raw: str) -> List[Any]:
    """Turn tshark's http.file_data column into parsed JSON objects.

    The column is hex when the frame carries bytes, and a single frame can hold
    more than one body when HTTP responses are pipelined on the same segment.
    """
    bodies: List[Any] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            text = binascii.unhexlify(chunk.replace(":", "")).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            text = chunk
        try:
            bodies.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return bodies


def classify(message: Dict[str, Any]) -> tuple[str, str]:
    """Return (category, detail) for one JSON-RPC object."""
    method = message.get("method")
    has_id = "id" in message and message["id"] is not None

    if method:
        if method in SYNC_METHODS:
            kind = "solicitud" if has_id else "notificacion"
            return "SINCRONIZACION", f"{method} ({kind})"
        # tools/call is far more readable with the tool name attached.
        tool = (message.get("params") or {}).get("name")
        label = f"{method} -> {tool}" if tool else method
        if has_id:
            return "SOLICITUD", f"id={message['id']} {label}"
        return "NOTIFICACION", label
    if "error" in message:
        error = message["error"] or {}
        return "ERROR", f"id={message.get('id')} code={error.get('code')}"
    if "result" in message:
        result = message["result"] or {}
        if "protocolVersion" in result:
            return "SINCRONIZACION", f"id={message.get('id')} InitializeResult"
        # A tools/call that failed at the domain level answers with a normal
        # result carrying isError=true, not with a JSON-RPC error.
        if result.get("isError"):
            return "RESPUESTA", f"id={message.get('id')} result (isError=true)"
        return "RESPUESTA", f"id={message.get('id')} result"
    return "DESCONOCIDO", json.dumps(message)[:60]


def direction(packet: Dict[str, str]) -> str:
    src = packet.get("ip.src") or packet.get("ipv6.src") or "?"
    dst = packet.get("ip.dst") or packet.get("ipv6.dst") or "?"
    return f"{src}:{packet.get('tcp.srcport', '?')} -> {dst}:{packet.get('tcp.dstport', '?')}"


def tcp_flags(raw: str) -> str:
    try:
        value = int(raw, 16) if raw.startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return "-"
    names = [name for mask, name in TCP_FLAG_NAMES if value & mask]
    return ",".join(names) or "-"


def application_layer(packets: List[Dict[str, str]]) -> Dict[str, int]:
    print("\n== Capa de aplicacion: mensajes JSON-RPC ==\n")
    header = f"{'#':>6}  {'t(s)':>8}  {'HTTP':<22}  {'CATEGORIA':<14}  DETALLE"
    print(header)
    print("-" * len(header))

    counts: Dict[str, int] = {}
    for packet in packets:
        bodies = decode_bodies(packet.get("http.file_data", ""))
        if not bodies:
            continue
        if packet.get("http.request.method"):
            http = f"{packet['http.request.method']} {packet.get('http.request.uri', '')}"
        elif packet.get("http.response.code"):
            http = f"HTTP {packet['http.response.code']}"
        else:
            http = "-"
        for body in bodies:
            if not isinstance(body, dict):
                continue
            category, detail = classify(body)
            counts[category] = counts.get(category, 0) + 1
            print(
                f"{packet['frame.number']:>6}  "
                f"{float(packet['frame.time_relative']):>8.3f}  "
                f"{http[:22]:<22}  {category:<14}  {detail}"
            )

    if not counts:
        print("  (sin cuerpos JSON legibles: la captura es HTTPS, ver la seccion TLS)")
    return counts


def transport_layer(packets: List[Dict[str, str]]) -> None:
    print("\n== Capa de transporte: TCP ==\n")
    header = f"{'#':>6}  {'t(s)':>8}  {'FLAGS':<14}  {'BYTES':>6}  SENTIDO"
    print(header)
    print("-" * len(header))

    handshake = teardown = payload = retransmissions = 0
    payload_bytes = 0
    for packet in packets:
        if not packet.get("tcp.flags"):
            continue
        flags = tcp_flags(packet["tcp.flags"])
        length = int(packet.get("tcp.len") or 0)
        if "SYN" in flags:
            handshake += 1
        if "FIN" in flags or "RST" in flags:
            teardown += 1
        if length:
            payload += 1
            payload_bytes += length
        if packet.get("tcp.analysis.retransmission"):
            retransmissions += 1
        # Only the interesting frames: control flags and segments with payload.
        if length or {"SYN", "FIN", "RST"} & set(flags.split(",")):
            print(
                f"{packet['frame.number']:>6}  "
                f"{float(packet['frame.time_relative']):>8.3f}  "
                f"{flags:<14}  {length:>6}  {direction(packet)}"
            )

    print(
        f"\n  Segmentos SYN: {handshake} | FIN/RST: {teardown} | "
        f"con datos: {payload} ({payload_bytes} bytes) | retransmisiones: {retransmissions}"
    )


def tls_layer(packets: List[Dict[str, str]]) -> None:
    tls = [p for p in packets if p.get("tls.record.content_type")]
    if not tls:
        return
    print("\n== Capa de aplicacion: TLS (captura contra Cloud Run) ==\n")
    handshake_types = {
        "1": "ClientHello",
        "2": "ServerHello",
        "11": "Certificate",
        "16": "ClientKeyExchange",
        "20": "Finished",
    }
    for packet in tls:
        kinds = [handshake_types.get(t, f"handshake({t})") for t in packet["tls.handshake.type"].split(",") if t]
        label = ", ".join(kinds) or f"record type {packet['tls.record.content_type']}"
        print(f"{packet['frame.number']:>6}  {float(packet['frame.time_relative']):>8.3f}  {label}")
    print(
        "\n  Los cuerpos JSON-RPC viajan cifrados dentro de los registros "
        "application_data: por eso el analisis de mensajes usa la captura en claro."
    )


def link_and_network_layer(packets: List[Dict[str, str]]) -> None:
    print("\n== Capas de enlace y de red ==\n")
    encaps = {p.get("frame.encap_type", "?") for p in packets}
    # 1 = Ethernet, 15 = raw IP / Npcap loopback (no link-layer addresses).
    encap_names = {"1": "Ethernet", "15": "Raw IP (adaptador de loopback Npcap)"}
    for encap in sorted(encaps):
        print(f"  Encapsulado : {encap_names.get(encap, encap)}")

    macs = {p["eth.src"] for p in packets if p.get("eth.src")}
    if macs:
        print(f"  MAC origen  : {', '.join(sorted(macs))}")
    else:
        print("  MAC         : ninguna (el trafico de loopback no atraviesa una NIC)")

    hosts = {p["ip.src"] for p in packets if p.get("ip.src")}
    hosts |= {p["ipv6.src"] for p in packets if p.get("ipv6.src")}
    print(f"  Direcciones : {', '.join(sorted(hosts)) or '-'}")

    ttls = {p["ip.ttl"] for p in packets if p.get("ip.ttl")}
    if ttls:
        print(f"  TTL         : {', '.join(sorted(ttls))}")

    sizes = [int(p["frame.len"]) for p in packets if p.get("frame.len")]
    if sizes:
        print(
            f"  Trama       : min {min(sizes)} B | max {max(sizes)} B | "
            f"total {len(sizes)} tramas, {sum(sizes)} bytes"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "capture", nargs="?", type=Path, default=DEFAULT_CAPTURE, help="File to analyse"
    )
    parser.add_argument("--tshark", help="Path to tshark.exe")
    args = parser.parse_args()

    if not args.capture.exists():
        raise SystemExit(
            f"{args.capture} does not exist. Run scripts/capture_mcp_session.py first."
        )

    packets = read_packets(find_tshark(args.tshark), args.capture)
    print(f"Captura: {args.capture}  ({len(packets)} tramas)")

    counts = application_layer(packets)
    transport_layer(packets)
    tls_layer(packets)
    link_and_network_layer(packets)

    if counts:
        print("\n== Conteo por categoria JSON-RPC ==\n")
        for category, total in sorted(counts.items()):
            print(f"  {category:<15} {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
