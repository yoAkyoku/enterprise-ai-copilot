"""CLI entrypoint for the synthetic Agent Runtime and local API."""

from __future__ import annotations

import argparse

from services.bootstrap import build_runtime, demo_identity


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic customer-service Agent demo")
    parser.add_argument("--order-id", default="SO-1001")
    parser.add_argument("--serve", action="store_true", help="start the local HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.serve:
        import uvicorn

        uvicorn.run("services.api.app:app", host=args.host, port=args.port, log_level="info")
        return 0

    runtime, audit = build_runtime()
    result = runtime.run(
        "Where is my order?",
        demo_identity(),
        order_id=args.order_id,
    )
    print(result.message)
    print(f"status={result.status} trace_id={result.trace_id} audit_events={len(audit.events)}")
    return 0 if result.status.value == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
