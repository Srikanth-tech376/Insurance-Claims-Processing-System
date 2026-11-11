import argparse
import json
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import uvicorn

from .api import create_app
from .config import GenerationConfig
from .database import connect
from .generator import generate_dataset
from .rules import process_claim, record_payment
from .validation import validate_database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claims",
        description="Generate and validate the YottaFlex synthetic claims system.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Generate the synthetic dataset")
    generate.add_argument("--seed", type=int, default=20261022)
    generate.add_argument("--policies", type=int, default=2_000)
    generate.add_argument("--claims", type=int, default=1_000)
    generate.add_argument("--adjusters", type=int, default=20)
    generate.add_argument("--as-of-date", type=date.fromisoformat, default=date(2026, 10, 22))
    generate.add_argument("--output-dir", type=Path, default=Path("data/generated"))

    validate = commands.add_parser("validate", help="Run integrity and reproducibility checks")
    validate.add_argument("--database", type=Path, default=Path("data/generated/claims.sqlite"))
    validate.add_argument("--ground-truth", type=Path, default=Path("data/generated/ground_truth.json"))
    validate.add_argument("--output-dir", type=Path, default=Path("data/generated"))

    process = commands.add_parser("process", help="Process an existing claim")
    process.add_argument("claim_id")
    process.add_argument("--database", type=Path, default=Path("data/generated/claims.sqlite"))

    pay = commands.add_parser("pay", help="Record a payment attempt for a claim")
    pay.add_argument("claim_id")
    pay.add_argument("--reference", required=True)
    pay.add_argument("--database", type=Path, default=Path("data/generated/claims.sqlite"))

    serve = commands.add_parser("serve", help="Run the local REST API")
    serve.add_argument("--database", type=Path, default=Path("data/generated/claims.sqlite"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        manifest = generate_dataset(
            GenerationConfig(
                seed=args.seed,
                policy_count=args.policies,
                claim_count=args.claims,
                adjuster_count=args.adjusters,
                as_of_date=args.as_of_date,
                output_dir=args.output_dir,
            )
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        result = validate_database(
            args.database,
            ground_truth_path=args.ground_truth,
            output_dir=args.output_dir,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["valid"] else 1
    if args.command == "process":
        with closing(connect(args.database)) as connection:
            result = process_claim(connection, args.claim_id)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "pay":
        with closing(connect(args.database)) as connection:
            result = record_payment(
                connection,
                args.claim_id,
                args.reference,
                datetime.now(timezone.utc),
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "serve":
        uvicorn.run(create_app(args.database), host=args.host, port=args.port)
        return 0
    raise AssertionError(f"Unknown command: {args.command}")
