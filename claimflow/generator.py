import csv
import hashlib
import json
import math
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import GenerationConfig
from .database import SCHEMA_PATH, connect, initialize
from .rules import CLAIM_CAUSES, process_claim, record_payment, verify_document_metadata


REGIONS = ("NORTH", "SOUTH", "EAST", "WEST")
PRODUCT_CLAIM_TYPES = {
    "AUTO": ("AUTO_COLLISION", "AUTO_COMPREHENSIVE"),
    "HOME": ("HOME_PROPERTY",),
    "HEALTH": ("HEALTH_MEDICAL",),
    "LIABILITY": ("THIRD_PARTY_LIABILITY",),
}
CLAIM_TYPE_DETAILS = {
    "AUTO_COLLISION": ("Collision damage", ("incident_report", "repair_estimate", "photos")),
    "AUTO_COMPREHENSIVE": (
        "Comprehensive loss",
        ("incident_report", "proof_of_loss", "photos"),
    ),
    "HOME_PROPERTY": (
        "Home property damage",
        ("incident_report", "repair_estimate", "proof_of_loss"),
    ),
    "HEALTH_MEDICAL": (
        "Medical expense",
        ("medical_invoice", "medical_report", "proof_of_identity"),
    ),
    "THIRD_PARTY_LIABILITY": (
        "Third-party liability",
        ("incident_report", "third_party_statement", "evidence"),
    ),
}
NAMES = (
    "Avery", "Jordan", "Taylor", "Morgan", "Riley", "Casey", "Quinn", "Parker",
    "Jamie", "Cameron", "Drew", "Skyler", "Reese", "Rowan", "Kendall", "Alex",
)
SURNAMES = (
    "Bennett", "Rivera", "Kim", "Patel", "Brooks", "Nguyen", "Carter", "Reed",
    "Foster", "Murphy", "Bell", "Ward", "Diaz", "Price", "Ross", "Perry",
)


def _iso_day(value: date) -> str:
    return value.isoformat()


def _iso_time(value: date | datetime) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def _date_between(rng: random.Random, start: date, end: date) -> date:
    span = max((end - start).days, 0)
    return start + timedelta(days=rng.randint(0, span))


def _coverage_limits(product_type: str, rng: random.Random) -> tuple[int, int]:
    bounds = {
        "AUTO": ((2_500_000, 10_000_000), (50_000, 250_000)),
        "HOME": ((5_000_000, 30_000_000), (100_000, 500_000)),
        "HEALTH": ((1_000_000, 10_000_000), (10_000, 100_000)),
        "LIABILITY": ((10_000_000, 50_000_000), (100_000, 1_000_000)),
    }
    limit_range, deductible_range = bounds[product_type]
    limit = rng.randint(*limit_range)
    deductible = rng.randint(*deductible_range)
    return limit, min(deductible, limit - 1)


def _scenario_for(index: int, claim_count: int) -> str:
    fraction = index / claim_count
    scenarios = (
        (0.70, "standard"),
        (0.75, "missing_documents"),
        (0.80, "outside_policy_period"),
        (0.825, "expired_policy"),
        (0.85, "cancelled_policy"),
        (0.89, "over_limit"),
        (0.91, "coverage_not_on_policy"),
        (0.95, "duplicate_claim"),
        (0.97, "high_exposure"),
        (0.98, "excluded_cause"),
        (0.99, "unverified_document"),
        (1.0, "duplicate_payment"),
    )
    for boundary, scenario in scenarios:
        if fraction < boundary:
            return scenario
    return "duplicate_payment"


def _insert_reference_data(
    connection: sqlite3.Connection,
    config: GenerationConfig,
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    customer_count = max(1, min(config.policy_count, math.ceil(config.policy_count * 0.65)))
    for index in range(1, customer_count + 1):
        customer_id = f"CUS-{index:06d}"
        full_name = (
            f"{NAMES[(index - 1) % len(NAMES)]} "
            f"{SURNAMES[((index - 1) // len(NAMES)) % len(SURNAMES)]}"
        )
        connection.execute(
            """
            INSERT INTO customers (customer_id, full_name, email, phone, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                full_name,
                f"customer{index:06d}@example.test",
                f"+1-555-{index % 10_000:04d}",
                _iso_time(date(2024, 1, 1) + timedelta(days=index % 700)),
            ),
        )

    coverage_ids: dict[str, str] = {}
    coverage_rows = []
    for index, (claim_type, (name, required_docs)) in enumerate(
        CLAIM_TYPE_DETAILS.items(), start=1
    ):
        coverage_id = f"COV-{index:03d}"
        coverage_ids[claim_type] = coverage_id
        product = next(
            product for product, types in PRODUCT_CLAIM_TYPES.items()
            if claim_type in types
        )
        coverage_rows.append(
            {
                "coverage_id": coverage_id,
                "code": claim_type,
                "name": name,
                "product_type": product,
                "claim_type": claim_type,
                "required_documents_json": json.dumps(required_docs),
            }
        )
    connection.executemany(
        """
        INSERT INTO coverages
            (coverage_id, code, name, product_type, claim_type, required_documents_json)
        VALUES (:coverage_id, :code, :name, :product_type, :claim_type,
                :required_documents_json)
        """,
        coverage_rows,
    )

    product_types = tuple(PRODUCT_CLAIM_TYPES)
    policies: list[dict[str, Any]] = []
    for index in range(1, config.policy_count + 1):
        product = product_types[(index - 1) % len(product_types)]
        cycle_position = (index - 1) % 20
        if cycle_position < 14:
            state = "ACTIVE"
        elif cycle_position < 19:
            state = "EXPIRED"
        else:
            state = "CANCELLED"
        effective_from = date(2024, 1, 1) + timedelta(days=rng.randint(0, 800))
        if state == "ACTIVE":
            effective_to = max(
                config.as_of_date + timedelta(days=1),
                effective_from + timedelta(days=365),
            ) + timedelta(days=rng.randint(0, 365))
        elif state == "EXPIRED":
            effective_to = min(
                config.as_of_date - timedelta(days=1),
                effective_from + timedelta(days=rng.randint(180, 730)),
            )
            effective_from = min(effective_from, effective_to - timedelta(days=180))
        else:
            effective_to = min(
                config.as_of_date - timedelta(days=1),
                effective_from + timedelta(days=rng.randint(60, 500)),
            )
            effective_from = min(effective_from, effective_to - timedelta(days=60))
        policy_id = f"POL-{product}-2026-{index:06d}"
        customer_id = f"CUS-{((index - 1) % customer_count) + 1:06d}"
        policy = {
            "policy_id": policy_id,
            "customer_id": customer_id,
            "product_type": product,
            "effective_from": _iso_day(effective_from),
            "effective_to": _iso_day(effective_to),
            "lifecycle_state": state,
            "created_at": _iso_time(effective_from),
        }
        policies.append(policy)
        connection.execute(
            """
            INSERT INTO policies
                (policy_id, customer_id, product_type, effective_from, effective_to,
                 lifecycle_state, created_at)
            VALUES (:policy_id, :customer_id, :product_type, :effective_from,
                    :effective_to, :lifecycle_state, :created_at)
            """,
            policy,
        )
        connection.execute(
            """
            INSERT INTO policy_events
                (policy_event_id, policy_id, from_state, to_state, event_at, reason)
            VALUES (?, ?, 'ISSUED', 'ACTIVE', ?, ?)
            """,
            (
                f"PLE-{policy_id}-001",
                policy_id,
                _iso_time(effective_from),
                "Policy issued and coverage became active",
            ),
        )
        if state != "ACTIVE":
            connection.execute(
                """
                INSERT INTO policy_events
                    (policy_event_id, policy_id, from_state, to_state, event_at, reason)
                VALUES (?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    f"PLE-{policy_id}-002",
                    policy_id,
                    state,
                    _iso_time(effective_to),
                    f"Policy coverage ended; lifecycle state changed to {state.lower()}",
                ),
            )
        for claim_type in PRODUCT_CLAIM_TYPES[product]:
            limit, deductible = _coverage_limits(product, rng)
            connection.execute(
                """
                INSERT INTO policy_coverages
                    (policy_id, coverage_id, effective_from, effective_to,
                     limit_cents, deductible_cents, exclusions_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    coverage_ids[claim_type],
                    policy["effective_from"],
                    policy["effective_to"],
                    limit,
                    deductible,
                    json.dumps(CLAIM_CAUSES[claim_type]["excluded"]),
                ),
            )

    adjusters: list[dict[str, Any]] = []
    for index in range(1, config.adjuster_count + 1):
        adjuster = {
            "adjuster_id": f"ADJ-{index:05d}",
            "full_name": (
                f"{NAMES[(index + 3) % len(NAMES)]} "
                f"{SURNAMES[(index + 5) % len(SURNAMES)]}"
            ),
            "region": REGIONS[(index - 1) % len(REGIONS)],
            "active": 1,
        }
        adjusters.append(adjuster)
        connection.execute(
            """
            INSERT INTO adjusters (adjuster_id, full_name, region, active)
            VALUES (:adjuster_id, :full_name, :region, :active)
            """,
            adjuster,
        )
    return policies, adjusters, coverage_ids


def _select_policy(
    policies: list[dict[str, Any]],
    product_type: str | None = None,
    lifecycle_state: str | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    candidates = [
        policy for policy in policies
        if (product_type is None or policy["product_type"] == product_type)
        and (lifecycle_state is None or policy["lifecycle_state"] == lifecycle_state)
    ]
    if not candidates and lifecycle_state is not None:
        candidates = [
            policy for policy in policies
            if policy["lifecycle_state"] == lifecycle_state
        ]
    if not candidates:
        candidates = [
            policy for policy in policies
            if product_type is None or policy["product_type"] == product_type
        ]
    if not candidates:
        candidates = policies
    if not candidates:
        raise ValueError("At least one policy is required to generate claims")
    return (rng.choice(candidates) if rng else candidates[0])


def _build_claim(
    connection: sqlite3.Connection,
    index: int,
    scenario: str,
    policies: list[dict[str, Any]],
    coverage_ids: dict[str, str],
    rng: random.Random,
    config: GenerationConfig,
    source_claim: dict[str, Any] | None = None,
) -> dict[str, Any]:
    claim_id = f"CLM-2026-{index:06d}"
    if source_claim:
        policy = source_claim["policy"]
        customer_id = source_claim["customer_id"]
        claim_type = source_claim["claim_type"]
        incident_id = source_claim["incident_id"]
        incident_date = source_claim["incident_date"]
        cause_code = source_claim["cause_code"]
        region = source_claim["region"]
        item_specs = [dict(item) for item in source_claim["item_specs"]]
    else:
        requested_state = {
            "expired_policy": "EXPIRED",
            "cancelled_policy": "CANCELLED",
        }.get(scenario)
        product = rng.choice(tuple(PRODUCT_CLAIM_TYPES))
        policy = _select_policy(policies, product, requested_state, rng)
        customer_id = policy["customer_id"]
        claim_type = rng.choice(PRODUCT_CLAIM_TYPES[policy["product_type"]])
        start = date.fromisoformat(policy["effective_from"])
        end = date.fromisoformat(policy["effective_to"])
        if scenario == "outside_policy_period":
            incident_date = start - timedelta(days=rng.randint(1, 30))
        elif scenario in {"expired_policy", "cancelled_policy"}:
            incident_date = end + timedelta(days=rng.randint(1, 30))
        elif policy["lifecycle_state"] == "ACTIVE":
            incident_date = _date_between(rng, start, min(end, config.as_of_date))
        else:
            incident_date = _date_between(rng, start, end)
        if scenario == "coverage_not_on_policy":
            unsupported_types = [
                candidate for candidate in CLAIM_TYPE_DETAILS
                if candidate not in PRODUCT_CLAIM_TYPES[policy["product_type"]]
            ]
            claim_type = rng.choice(unsupported_types)
        if scenario == "excluded_cause":
            cause_code = rng.choice(CLAIM_CAUSES[claim_type]["excluded"])
        else:
            cause_code = rng.choice(CLAIM_CAUSES[claim_type]["normal"])
        incident_id = f"INC-{claim_id}"
        region = rng.choice(REGIONS)

        terms = connection.execute(
            """
            SELECT limit_cents, deductible_cents
            FROM policy_coverages
            WHERE policy_id = ? AND coverage_id = ?
            """,
            (policy["policy_id"], coverage_ids[claim_type]),
        ).fetchone()
        limit_cents = terms["limit_cents"] if terms else 5_000_000
        deductible_cents = terms["deductible_cents"] if terms else 0
        if scenario == "over_limit":
            target_amount = limit_cents + max(1, limit_cents // 4)
        elif scenario == "high_exposure":
            target_amount = limit_cents * 3
        else:
            target_amount = max(
                deductible_cents + 10_000,
                rng.randint(max(10_000, limit_cents // 20), max(10_001, limit_cents // 2)),
            )
        item_specs = [
            {
                "coverage_id": coverage_ids[claim_type],
                "description": f"{CLAIM_TYPE_DETAILS[claim_type][0]} item 1",
                "quantity": 1,
                "unit_amount_cents": target_amount,
            }
        ]

    reported_date = incident_date + timedelta(days=rng.randint(1, 14))
    if reported_date < date.fromisoformat(policy["effective_from"]):
        reported_date = date.fromisoformat(policy["effective_from"])
    reported_at = _iso_time(reported_date)
    connection.execute(
        """
        INSERT INTO incidents
            (incident_id, customer_id, occurred_on, incident_type, cause_code,
             region, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            incident_id,
            customer_id,
            _iso_day(incident_date),
            claim_type,
            cause_code,
            region,
            f"Synthetic {cause_code.replace('_', ' ')} incident",
        ),
    ) if not source_claim else None
    connection.execute(
        """
        INSERT INTO claims
            (claim_id, customer_id, policy_id, incident_id, claim_type, reported_at,
             currency, claimed_amount_cents, lifecycle_state)
        VALUES (?, ?, ?, ?, ?, ?, 'USD', 0, 'SUBMITTED')
        """,
        (
            claim_id,
            customer_id,
            policy["policy_id"],
            incident_id,
            claim_type,
            reported_at,
        ),
    )
    for item_index, item in enumerate(item_specs, start=1):
        line_amount = item["quantity"] * item["unit_amount_cents"]
        connection.execute(
            """
            INSERT INTO claim_items
                (claim_item_id, claim_id, coverage_id, description, quantity,
                 unit_amount_cents, line_amount_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"ITM-{claim_id}-{item_index:03d}",
                claim_id,
                item["coverage_id"],
                item["description"],
                item["quantity"],
                item["unit_amount_cents"],
                line_amount,
            ),
        )
    amount_cents = sum(
        item["quantity"] * item["unit_amount_cents"] for item in item_specs
    )
    connection.execute(
        "UPDATE claims SET claimed_amount_cents = ? WHERE claim_id = ?",
        (amount_cents, claim_id),
    )
    if connection.execute(
        """
        SELECT 1 FROM policy_coverages
        WHERE policy_id = ? AND coverage_id = ?
        """,
        (policy["policy_id"], coverage_ids[claim_type]),
    ).fetchone():
        connection.execute(
            """
            INSERT INTO claim_coverages (claim_id, policy_id, coverage_id)
            VALUES (?, ?, ?)
            """,
            (claim_id, policy["policy_id"], coverage_ids[claim_type]),
        )
    connection.execute(
        """
        INSERT INTO claim_events
            (claim_event_id, claim_id, from_state, to_state, event_at, actor, reason)
        VALUES (?, ?, NULL, 'SUBMITTED', ?, 'synthetic-intake', 'Claim submitted')
        """,
        (f"EVT-{claim_id}-001", claim_id, reported_at),
    )

    required_documents = CLAIM_TYPE_DETAILS[claim_type][1]
    docs_to_create = list(required_documents)
    if scenario == "missing_documents":
        docs_to_create.remove(required_documents[index % len(required_documents)])
    if scenario == "unverified_document":
        unverified_type = required_documents[index % len(required_documents)]
    else:
        unverified_type = None
    for doc_index, document_type in enumerate(docs_to_create, start=1):
        document_id = f"DOC-{claim_id}-{doc_index:03d}"
        invalid_metadata = document_type == unverified_type
        mime_type = "application/octet-stream" if invalid_metadata else "application/pdf"
        size_bytes = 0 if invalid_metadata else 1024 + (index * 31 + doc_index * 17) % 100_000
        digest = hashlib.sha256(
            f"{config.seed}:{document_id}:{document_type}".encode("utf-8")
        ).hexdigest()
        metadata = {
            "source": "synthetic",
            "file_name": f"{document_type}-{claim_id}.pdf",
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "content_sha256": digest,
        }
        connection.execute(
            """
            INSERT INTO documents
                (document_id, claim_id, document_type, received_at, file_name,
                 mime_type, size_bytes, content_sha256, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                claim_id,
                document_type,
                reported_at,
                metadata["file_name"],
                mime_type,
                size_bytes,
                digest,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        verification_status, reason = verify_document_metadata(
            metadata["file_name"],
            mime_type,
            size_bytes,
            digest,
        )
        connection.execute(
            """
            INSERT INTO document_verifications
                (verification_id, document_id, status, verified_at, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"VER-{document_id}-001",
                document_id,
                verification_status,
                reported_at,
                reason,
            ),
        )
    return {
        "claim_id": claim_id,
        "scenario": scenario,
        "policy": policy,
        "customer_id": customer_id,
        "claim_type": claim_type,
        "incident_id": incident_id,
        "incident_date": incident_date,
        "cause_code": cause_code,
        "region": region,
        "item_specs": item_specs,
        "reported_at": reported_at,
        "amount_cents": amount_cents,
    }


def _generate_claims(
    connection: sqlite3.Connection,
    policies: list[dict[str, Any]],
    coverage_ids: dict[str, str],
    rng: random.Random,
    config: GenerationConfig,
) -> list[dict[str, Any]]:
    generated: list[dict[str, Any]] = []
    standard_claims: list[dict[str, Any]] = []
    for index in range(1, config.claim_count + 1):
        scenario = _scenario_for(index, config.claim_count)
        source_claim = None
        if scenario == "duplicate_claim" and standard_claims:
            source_claim = standard_claims[(index - 1) % len(standard_claims)]
        claim = _build_claim(
            connection,
            index,
            scenario,
            policies,
            coverage_ids,
            rng,
            config,
            source_claim,
        )
        generated.append(claim)
        if scenario == "standard":
            standard_claims.append(claim)
    return generated


def _process_generated_claims(
    connection: sqlite3.Connection,
    generated: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    ordered = sorted(generated, key=lambda row: (row["reported_at"], row["claim_id"]))
    for claim in ordered:
        result = process_claim(
            connection,
            claim["claim_id"],
            datetime.fromisoformat(claim["reported_at"]),
        )
        if claim["scenario"] == "duplicate_payment" and result["settlement_status"] == "RECOMMENDED":
            record_payment(
                connection,
                claim["claim_id"],
                f"PAY-{claim['claim_id']}-001",
                datetime.fromisoformat(claim["reported_at"]) + timedelta(days=14),
            )
            duplicate_attempt = record_payment(
                connection,
                claim["claim_id"],
                f"PAY-{claim['claim_id']}-002",
                datetime.fromisoformat(claim["reported_at"]) + timedelta(days=15),
            )
            result = process_claim(
                connection,
                claim["claim_id"],
                datetime.fromisoformat(claim["reported_at"]),
            )
            result["duplicate_payment_attempt"] = duplicate_attempt
        else:
            result["duplicate_payment_attempt"] = None
        results[claim["claim_id"]] = result
    return results


def _export_tables(connection: sqlite3.Connection, output_dir: Path) -> dict[str, int]:
    row_counts: dict[str, int] = {}
    table_names = [
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    for table in table_names:
        columns = connection.execute(f"PRAGMA table_info('{table}')").fetchall()
        primary_key = [row for row in columns if row["pk"]]
        order_clause = ", ".join(f'"{row["name"]}"' for row in primary_key)
        rows = connection.execute(
            f'SELECT * FROM "{table}" ORDER BY {order_clause}'
        ).fetchall()
        row_counts[table] = len(rows)
        path = output_dir / f"{table}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow([row["name"] for row in columns])
            writer.writerows([tuple(row) for row in rows])
    return row_counts


def _export_ground_truth(
    output_dir: Path,
    generated: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    config: GenerationConfig,
) -> None:
    records: list[dict[str, Any]] = []
    for claim in sorted(generated, key=lambda row: row["claim_id"]):
        result = results[claim["claim_id"]]
        labels = list(result["anomaly_codes"])
        reasons = list(result["reasons"])
        if result.get("duplicate_payment_attempt"):
            labels.append("duplicate_payment_attempt")
            reasons.append(
                "A second payment attempt was rejected; only the first payment was recorded"
            )
        records.append(
            {
                "claim_id": claim["claim_id"],
                "anomaly_labels": sorted(set(labels)),
                "expected_decision": result["lifecycle_state"],
                "expected_eligibility": result["decision"],
                "expected_settlement_status": result["settlement_status"],
                "expected_settlement_cents": result["settlement_cents"],
                "reasons": reasons,
                "duplicate_of": result["duplicate_of"],
            }
        )
    document = {
        "schema_version": "1.0",
        "seed": config.seed,
        "as_of_date": config.as_of_date.isoformat(),
        "label_policy": (
            "Labels are generated from source-record rule outcomes and are stored "
            "outside the input tables and CSVs."
        ),
        "claims": records,
    }
    (output_dir / "ground_truth.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "ground_truth.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "claim_id",
                "anomaly_labels",
                "expected_decision",
                "expected_eligibility",
                "expected_settlement_status",
                "expected_settlement_cents",
                "reasons",
                "duplicate_of",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    **record,
                    "anomaly_labels": json.dumps(record["anomaly_labels"]),
                    "reasons": json.dumps(record["reasons"]),
                }
            )


def _write_seed_sql(connection: sqlite3.Connection, path: Path) -> None:
    table_order = (
        "customers",
        "coverages",
        "adjusters",
        "policies",
        "policy_coverages",
        "policy_events",
        "incidents",
        "claims",
        "claim_items",
        "claim_coverages",
        "documents",
        "document_verifications",
        "adjuster_assignments",
        "assessments",
        "reserves",
        "settlements",
        "settlement_events",
        "claim_events",
    )

    def sql_literal(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        raise TypeError(f"Unsupported SQL seed value: {type(value).__name__}")

    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(SCHEMA_PATH.read_text(encoding="utf-8"))
        stream.write("\nBEGIN TRANSACTION;\n")
        for table in table_order:
            columns = connection.execute(f"PRAGMA table_info('{table}')").fetchall()
            primary_key = [column for column in columns if column["pk"]]
            order_by = ", ".join(f'"{column["name"]}"' for column in primary_key)
            column_names = ", ".join(f'"{column["name"]}"' for column in columns)
            for row in connection.execute(
                f'SELECT * FROM "{table}" ORDER BY {order_by}'
            ):
                values = ", ".join(sql_literal(value) for value in row)
                stream.write(
                    f'INSERT INTO "{table}" ({column_names}) VALUES ({values});\n'
                )
        stream.write("COMMIT;\n")


def _write_manifest(
    output_dir: Path,
    config: GenerationConfig,
    row_counts: dict[str, int],
) -> dict[str, Any]:
    checksums = {}
    for path in sorted(output_dir.iterdir(), key=lambda entry: entry.name):
        if path.is_file() and path.name != "manifest.json":
            checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "seed": config.seed,
        "as_of_date": config.as_of_date.isoformat(),
        "policy_count": config.policy_count,
        "claim_count": config.claim_count,
        "adjuster_count": config.adjuster_count,
        "row_counts": row_counts,
        "sha256": checksums,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def generate_dataset(config: GenerationConfig) -> dict[str, Any]:
    output_dir = config.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "claims.sqlite"
    if database_path.exists():
        database_path.unlink()
    rng = random.Random(config.seed)
    connection = connect(database_path)
    try:
        initialize(connection)
        with connection:
            policies, _, coverage_ids = _insert_reference_data(
                connection, config, rng
            )
            generated = _generate_claims(
                connection, policies, coverage_ids, rng, config
            )
            results = _process_generated_claims(connection, generated)
        connection.execute("VACUUM")
        _export_ground_truth(output_dir, generated, results, config)
        row_counts = _export_tables(connection, output_dir)
        _write_seed_sql(connection, output_dir / "seed.sql")
        return _write_manifest(output_dir, config, row_counts)
    finally:
        connection.close()
