"""MLLP listener service — Phase 8 (app.mllp_server).

The live HL7 v2 front door: a real asyncio TCP server speaking MLLP
framing (VT ... FS CR). Each complete frame is handed to
app.interop.process_inbound (ORU -> lab draft ingest, ADT -> recorded
external event with conflict refusal, everything else -> AE), and the
HL7 ACK comes back framed. It is a standalone service in the same
codebase — run it next to the API:

    python -m app.mllp_server [--host 0.0.0.0] [--port 2575]

It builds the same FastAPI app's session factory (same database, same
seeded Studio config) without serving HTTP. (The roadmap's Go tier
remains the long-term home for this listener; no Go toolchain or Go
skeleton exists in this repo today, so the listener is Python/asyncio
— the wire behaviour is the deliverable, and process_inbound is
transport-agnostic if the listener is ever re-homed.)
"""
import argparse
import asyncio

from . import hl7
from .interop import process_inbound

MAX_FRAME_BYTES = 4 * 1024 * 1024


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  session_factory) -> None:
    peer = writer.get_extra_info("peername")
    peer_label = f"{peer[0]}:{peer[1]}" if peer else ""
    buffer = b""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > MAX_FRAME_BYTES:
                writer.write(hl7.wrap_mllp(hl7.build_ack(
                    "", "AR", "Frame exceeds maximum size")))
                await writer.drain()
                break
            while True:
                message, buffer = hl7.extract_frame(buffer)
                if message is None:
                    break
                with session_factory() as session:
                    ack = process_inbound(session, message, peer=peer_label)
                    session.commit()
                writer.write(hl7.wrap_mllp(ack))
                await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve(session_factory, host: str, port: int) -> asyncio.Server:
    return await asyncio.start_server(
        lambda r, w: _handle(r, w, session_factory), host, port)


def _session_factory():
    """Build the listener's own engine + session factory WITHOUT
    importing app.main (whose module level builds the HTTP app). The
    schema is owned by the API process: create_all here is the
    fresh-database convenience, and a concurrent API boot winning the
    create race is fine — what matters is that the tables exist."""
    from sqlalchemy import inspect

    from . import (access, billing, boards, documentation, inpatient,
                   interop, lab, orders, pharmacy, pophealth, revenue,
                   studio_config)
    from .db import Base, make_engine, make_session_factory
    from .security import seed_users

    engine = make_engine()
    try:
        Base.metadata.create_all(engine)
    except Exception:
        if not inspect(engine).has_table("users"):
            raise
    factory = make_session_factory(engine)
    with factory() as session:
        # The same seed chain app.main.create_app runs (idempotent):
        # the listener needs the published Studio configs (lab catalog
        # + workflows for ingest, endpoints config for ACK identity).
        seed_users(session)
        studio_config.ensure_default_configs(session)
        lab.ensure_lab_defaults(session)
        pophealth.ensure_pophealth_defaults(session)
        billing.ensure_billing_defaults(session)
        pharmacy.ensure_pharmacy_defaults(session)
        boards.ensure_boards_defaults(session)
        orders.ensure_orders_defaults(session)
        documentation.ensure_documentation_defaults(session)
        inpatient.ensure_inpatient_defaults(session)
        access.ensure_access_defaults(session)
        revenue.ensure_revenue_defaults(session)
        interop.ensure_interop_defaults(session)
    return factory


async def _run(host: str, port: int) -> None:
    server = await serve(_session_factory(), host, port)
    print(f"OpenCare MLLP listener on {host}:{port}")
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenCare HL7 MLLP listener")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=2575)
    args = parser.parse_args()
    asyncio.run(_run(args.host, args.port))


if __name__ == "__main__":
    main()
