import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .database import connect
from .rules import CLAIM_CAUSES, CLAIM_TRANSITIONS


def _count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def validate_database(
    database_path: str | Path,
    ground_truth_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    connection = connect(database_path)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            errors.append(f"SQLite integrity check failed: {quick_check}")
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_violations:
            errors.append(f"Foreign key violations found: {len(foreign_key_violations)}")

        tables = [
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        row_counts = {table: _count(connection, table) for table in tables}
        required_tables = {
            "customers", "policies", "coverages", "policy_coverages", "claims",
            "claim_items", "claim_coverages", "documents", "incidents", "adjusters",
            "adjuster_assignments", "assessments", "reserves", "settlements",
            "claim_events", "policy_events", "document_verifications",
            "settlement_events",
        }
        missing_tables = sorted(required_tables - set(tables))
        if missing_tables:
            errors.append(f"Required tables missing: {', '.join(missing_tables)}")
        else:
            _validate_primary_keys_and_enums(connection, tables, errors)
            _validate_relations(connection, errors)
            _validate_incident_causes(connection, errors)
            _validate_arithmetic(connection, errors)
            _validate_lifecycle(connection, errors)
            _validate_duplicate_detection(connection, errors)

        ground_truth_records = None
        if ground_truth_path:
            ground_truth_records = _load_ground_truth(Path(ground_truth_path), errors)
            if ground_truth_records is not None:
                _validate_ground_truth(connection, ground_truth_records, errors)

        if output_dir:
            _validate_exports(Path(output_dir), row_counts, errors)

        return {
            "valid": not errors,
            "errors": errors,
            "row_counts": row_counts,
            "policy_count": row_counts.get("policies", 0),
            "claim_count": row_counts.get("claims", 0),
            "ground_truth_claim_count": (
                len(ground_truth_records) if ground_truth_records is not None else None
            ),
        }
    finally:
        connection.close()


def _validate_primary_keys_and_enums(
    connection: sqlite3.Connection, tables: list[str], errors: list[str]
) -> None:
    for table in tables:
        primary_key = [
            row["name"]
            for row in connection.execute(f"PRAGMA table_info('{table}')")
            if row["pk"]
        ]
        if not primary_key:
            errors.append(f"Table has no primary key: {table}")
            continue
        columns = ", ".join(f'"{column}"' for column in primary_key)
        duplicates = connection.execute(
            f'SELECT COUNT(*) FROM (SELECT {columns} FROM "{table}" '
            f'GROUP BY {columns} HAVING COUNT(*) > 1)'
        ).fetchone()[0]
        if duplicates:
            errors.append(f"Primary key is not unique in {table}: {duplicates} group(s)")

    enum_queries = {
        "invalid policy product or lifecycle enum": """
            SELECT COUNT(*) FROM policies
            WHERE product_type NOT IN ('AUTO', 'HOME', 'HEALTH', 'LIABILITY')
               OR lifecycle_state NOT IN ('ACTIVE', 'EXPIRED', 'CANCELLED')
        """,
        "invalid coverage product or claim type enum": """
            SELECT COUNT(*) FROM coverages
            WHERE product_type NOT IN ('AUTO', 'HOME', 'HEALTH', 'LIABILITY')
               OR claim_type NOT IN (
                   'AUTO_COLLISION', 'AUTO_COMPREHENSIVE', 'HOME_PROPERTY',
                   'HEALTH_MEDICAL', 'THIRD_PARTY_LIABILITY'
               )
        """,
        "invalid claim lifecycle or currency enum": """
            SELECT COUNT(*) FROM claims
            WHERE lifecycle_state NOT IN (
                'SUBMITTED', 'TRIAGE', 'NEEDS_INFO', 'UNDER_REVIEW',
                'REJECTED', 'APPROVED', 'SETTLEMENT_RECOMMENDED', 'PAID'
            ) OR currency != 'USD'
               OR claim_type NOT IN (
                   'AUTO_COLLISION', 'AUTO_COMPREHENSIVE', 'HOME_PROPERTY',
                   'HEALTH_MEDICAL', 'THIRD_PARTY_LIABILITY'
               )
        """,
        "invalid document verification enum": """
            SELECT COUNT(*) FROM document_verifications
            WHERE status NOT IN ('VERIFIED', 'REJECTED')
        """,
        "invalid adjuster assignment enum": """
            SELECT COUNT(*) FROM adjuster_assignments
            WHERE status NOT IN ('ACTIVE', 'COMPLETED', 'REASSIGNED')
        """,
        "invalid assessment eligibility enum": """
            SELECT COUNT(*) FROM assessments
            WHERE eligibility NOT IN ('ELIGIBLE', 'INELIGIBLE', 'PENDING')
        """,
        "invalid settlement status enum": """
            SELECT COUNT(*) FROM settlements
            WHERE status NOT IN ('PENDING', 'RECOMMENDED', 'APPROVED', 'PAID', 'DECLINED')
        """,
        "invalid settlement event enum": """
            SELECT COUNT(*) FROM settlement_events
            WHERE event_type NOT IN ('RECOMMENDED', 'APPROVED', 'PAYMENT_ATTEMPT', 'PAID')
               OR outcome NOT IN ('RECORDED', 'SUCCEEDED', 'REJECTED_DUPLICATE')
        """,
    }
    for label, query in enum_queries.items():
        count = connection.execute(query).fetchone()[0]
        if count:
            errors.append(f"{label}: {count} row(s)")


def _validate_relations(connection: sqlite3.Connection, errors: list[str]) -> None:
    checks = {
        "claim customer differs from policyholder": """
            SELECT COUNT(*) FROM claims c JOIN policies p ON p.policy_id = c.policy_id
            WHERE c.customer_id != p.customer_id
        """,
        "claim customer differs from incident owner": """
            SELECT COUNT(*) FROM claims c JOIN incidents i ON i.incident_id = c.incident_id
            WHERE c.customer_id != i.customer_id
        """,
        "claim reported before its incident": """
            SELECT COUNT(*) FROM claims c JOIN incidents i ON i.incident_id = c.incident_id
            WHERE substr(c.reported_at, 1, 10) < i.occurred_on
        """,
        "policy effective dates are unordered": """
            SELECT COUNT(*) FROM policies WHERE effective_from >= effective_to
        """,
        "policy coverage effective period outside policy": """
            SELECT COUNT(*) FROM policy_coverages pc JOIN policies p ON p.policy_id = pc.policy_id
            WHERE pc.effective_from < p.effective_from OR pc.effective_to > p.effective_to
        """,
        "policy coverage product differs from policy": """
            SELECT COUNT(*) FROM policy_coverages pc
            JOIN policies p ON p.policy_id = pc.policy_id
            JOIN coverages c ON c.coverage_id = pc.coverage_id
            WHERE p.product_type != c.product_type
        """,
        "policy coverage dates or terms are invalid": """
            SELECT COUNT(*) FROM policy_coverages
            WHERE effective_from >= effective_to OR deductible_cents >= limit_cents
        """,
        "document verification predates receipt": """
            SELECT COUNT(*) FROM documents d
            JOIN document_verifications v ON v.document_id = d.document_id
            WHERE v.verified_at < d.received_at
        """,
        "assignment end predates assignment": """
            SELECT COUNT(*) FROM adjuster_assignments
            WHERE ended_at IS NOT NULL AND ended_at < assigned_at
        """,
        "claim coverage bridge does not match claim type": """
            SELECT COUNT(*) FROM claim_coverages cc
            JOIN claims c ON c.claim_id = cc.claim_id
            JOIN coverages v ON v.coverage_id = cc.coverage_id
            WHERE c.claim_type != v.claim_type
        """,
        "missing claim coverage bridge for a policy-covered type": """
            SELECT COUNT(*) FROM claims c
            JOIN coverages v ON v.claim_type = c.claim_type
            JOIN policy_coverages pc
              ON pc.policy_id = c.policy_id AND pc.coverage_id = v.coverage_id
            LEFT JOIN claim_coverages cc
              ON cc.claim_id = c.claim_id AND cc.coverage_id = v.coverage_id
            WHERE cc.claim_id IS NULL
        """,
    }
    for label, query in checks.items():
        count = connection.execute(query).fetchone()[0]
        if count:
            errors.append(f"{label}: {count} row(s)")


def _validate_incident_causes(
    connection: sqlite3.Connection, errors: list[str]
) -> None:
    policy_coverages = connection.execute(
        """
        SELECT c.claim_type, pc.exclusions_json
        FROM policy_coverages pc
        JOIN coverages c ON c.coverage_id = pc.coverage_id
        """
    ).fetchall()
    for policy_coverage in policy_coverages:
        exclusions = set(json.loads(policy_coverage["exclusions_json"]))
        allowed_exclusions = set(CLAIM_CAUSES[policy_coverage["claim_type"]]["excluded"])
        if not exclusions.issubset(allowed_exclusions):
            errors.append(
                f"Policy coverage contains invalid exclusions: "
                f"{policy_coverage['claim_type']}"
            )

    rows = connection.execute(
        """
        SELECT c.claim_id, c.claim_type, i.cause_code, pc.exclusions_json,
               a.eligibility, a.rationale_json
        FROM claims c
        JOIN incidents i ON i.incident_id = c.incident_id
        LEFT JOIN coverages v ON v.claim_type = c.claim_type
        LEFT JOIN policy_coverages pc
          ON pc.policy_id = c.policy_id AND pc.coverage_id = v.coverage_id
        LEFT JOIN assessments a ON a.claim_id = c.claim_id
        """
    ).fetchall()
    for row in rows:
        cause_rules = CLAIM_CAUSES.get(row["claim_type"])
        if cause_rules is None:
            errors.append(f"Unknown claim type for incident cause: {row['claim_id']}")
            continue
        valid_causes = set(cause_rules["normal"]) | set(cause_rules["excluded"])
        if row["cause_code"] not in valid_causes:
            errors.append(f"Unknown incident cause for {row['claim_id']}")
            continue
        excluded_causes = (
            set(json.loads(row["exclusions_json"]))
            if row["exclusions_json"] is not None
            else set()
        )
        if row["cause_code"] in excluded_causes:
            if row["eligibility"] != "INELIGIBLE":
                errors.append(f"Excluded incident cause was not rejected: {row['claim_id']}")
            elif not row["rationale_json"] or "excluded_cause" not in json.loads(
                row["rationale_json"]
            )["anomaly_codes"]:
                errors.append(f"Excluded incident cause lacks a traceable reason: {row['claim_id']}")


def _validate_arithmetic(connection: sqlite3.Connection, errors: list[str]) -> None:
    checks = {
        "claim item line arithmetic": """
            SELECT COUNT(*) FROM claim_items
            WHERE line_amount_cents != quantity * unit_amount_cents
        """,
        "claim amount reconciliation": """
            SELECT COUNT(*) FROM claims c
            WHERE c.claimed_amount_cents != (
                SELECT COALESCE(SUM(i.line_amount_cents), 0)
                FROM claim_items i WHERE i.claim_id = c.claim_id
            )
        """,
        "assessment and settlement reconciliation": """
            SELECT COUNT(*) FROM assessments a
            JOIN settlements s
              ON s.claim_id = a.claim_id
             AND s.settlement_sequence = a.assessment_sequence
            WHERE a.assessed_amount_cents != s.recommended_amount_cents
        """,
        "reserve calculation": """
            SELECT COUNT(*) FROM assessments a
            JOIN reserves r
              ON r.claim_id = a.claim_id
             AND r.reserve_sequence = a.assessment_sequence
            WHERE r.amount_cents != a.assessed_amount_cents * 80 / 100
        """,
        "claim missing assessment, reserve, or settlement": """
            SELECT COUNT(*) FROM claims c
            WHERE NOT EXISTS (SELECT 1 FROM assessments a WHERE a.claim_id = c.claim_id)
               OR NOT EXISTS (SELECT 1 FROM reserves r WHERE r.claim_id = c.claim_id)
               OR NOT EXISTS (SELECT 1 FROM settlements s WHERE s.claim_id = c.claim_id)
        """,
        "assessment version missing matching reserve or settlement": """
            SELECT COUNT(*) FROM assessments a
            WHERE NOT EXISTS (
                SELECT 1 FROM reserves r
                WHERE r.claim_id = a.claim_id
                  AND r.reserve_sequence = a.assessment_sequence
            ) OR NOT EXISTS (
                SELECT 1 FROM settlements s
                WHERE s.claim_id = a.claim_id
                  AND s.settlement_sequence = a.assessment_sequence
            )
        """,
        "duplicate payment accepted": """
            SELECT COUNT(*) FROM settlement_events
            WHERE outcome = 'REJECTED_DUPLICATE'
              AND event_type != 'PAYMENT_ATTEMPT'
        """,
    }
    for label, query in checks.items():
        count = connection.execute(query).fetchone()[0]
        if count:
            errors.append(f"{label}: {count} row(s)")

    over_limit_settlements = connection.execute(
        """
        SELECT COUNT(*)
        FROM settlements s
        JOIN claims c ON c.claim_id = s.claim_id
        JOIN assessments a
          ON a.claim_id = c.claim_id
         AND a.assessment_sequence = s.settlement_sequence
        WHERE s.recommended_amount_cents > a.assessed_amount_cents
        """
    ).fetchone()[0]
    if over_limit_settlements:
        errors.append(f"Settlement exceeds assessment: {over_limit_settlements} row(s)")

    claims = connection.execute(
        """
        SELECT c.claim_id, a.eligibility, a.assessed_amount_cents
        FROM claims c
        JOIN assessments a ON a.claim_id = c.claim_id
        ORDER BY c.claim_id
        """
    ).fetchall()
    for claim in claims:
        items = connection.execute(
            """
            SELECT coverage_id, SUM(line_amount_cents) AS gross
            FROM claim_items WHERE claim_id = ? GROUP BY coverage_id
            """,
            (claim["claim_id"],),
        ).fetchall()
        maximum_payable = 0
        for item in items:
            terms = connection.execute(
                """
                SELECT pc.limit_cents, pc.deductible_cents
                FROM claim_coverages cc
                JOIN policy_coverages pc
                  ON pc.policy_id = cc.policy_id AND pc.coverage_id = cc.coverage_id
                WHERE cc.claim_id = ? AND cc.coverage_id = ?
                """,
                (claim["claim_id"], item["coverage_id"]),
            ).fetchone()
            if terms:
                maximum_payable += min(
                    max(item["gross"] - terms["deductible_cents"], 0),
                    terms["limit_cents"],
                )
        if claim["assessed_amount_cents"] > maximum_payable:
            errors.append(
                f"Assessment exceeds deductible/limit calculation: {claim['claim_id']}"
            )
        if (
            claim["eligibility"] == "ELIGIBLE"
            and claim["assessed_amount_cents"] != maximum_payable
        ):
            errors.append(f"Eligible assessment does not reconcile: {claim['claim_id']}")

    payment_violations = connection.execute(
        """
        SELECT COUNT(*) FROM settlements s
        WHERE (s.status = 'PAID' AND (
            (SELECT COUNT(*) FROM settlement_events e
             WHERE e.settlement_id = s.settlement_id AND e.outcome = 'SUCCEEDED') != 1
            OR s.payment_reference IS NULL
            OR s.payment_reference != (
                SELECT e.payment_reference FROM settlement_events e
                WHERE e.settlement_id = s.settlement_id AND e.outcome = 'SUCCEEDED'
                LIMIT 1
            )
        )) OR (
            SELECT COUNT(*) FROM settlement_events e
            WHERE e.settlement_id = s.settlement_id AND e.outcome = 'SUCCEEDED'
        ) > 1
        """
    ).fetchone()[0]
    if payment_violations:
        errors.append(f"Payment idempotency violations: {payment_violations} settlement(s)")


def _validate_lifecycle(connection: sqlite3.Connection, errors: list[str]) -> None:
    event_rows = connection.execute(
        """
        SELECT claim_id, from_state, to_state, event_at, claim_event_id
        FROM claim_events
        ORDER BY claim_id, event_at, claim_event_id
        """
    ).fetchall()
    events_by_claim: dict[str, list[sqlite3.Row]] = {}
    for event in event_rows:
        events_by_claim.setdefault(event["claim_id"], []).append(event)
    for claim_id, events in events_by_claim.items():
        previous_state = None
        for event in events:
            if event["from_state"] != previous_state:
                errors.append(f"Lifecycle history is discontinuous for {claim_id}")
                break
            if previous_state and event["to_state"] not in CLAIM_TRANSITIONS[previous_state]:
                errors.append(
                    f"Invalid lifecycle transition for {claim_id}: "
                    f"{previous_state} -> {event['to_state']}"
                )
                break
            previous_state = event["to_state"]
        current_state = connection.execute(
            "SELECT lifecycle_state FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if current_state and current_state["lifecycle_state"] != previous_state:
            errors.append(f"Current claim state does not match event history: {claim_id}")

    claims_without_events = connection.execute(
        """
        SELECT COUNT(*) FROM claims c
        LEFT JOIN claim_events e ON e.claim_id = c.claim_id
        WHERE e.claim_id IS NULL
        """
    ).fetchone()[0]
    if claims_without_events:
        errors.append(f"Claims without lifecycle events: {claims_without_events}")

    policy_rows = connection.execute(
        """
        SELECT p.policy_id, p.lifecycle_state, e.from_state, e.to_state,
               e.event_at, e.policy_event_id
        FROM policies p
        LEFT JOIN policy_events e ON e.policy_id = p.policy_id
        ORDER BY p.policy_id, e.event_at, e.policy_event_id
        """
    ).fetchall()
    policy_events: dict[str, list[sqlite3.Row]] = {}
    policy_states: dict[str, str] = {}
    for row in policy_rows:
        policy_states[row["policy_id"]] = row["lifecycle_state"]
        if row["policy_event_id"] is not None:
            policy_events.setdefault(row["policy_id"], []).append(row)
    for policy_id, current_state in policy_states.items():
        events = policy_events.get(policy_id, [])
        if not events:
            errors.append(f"Policy without lifecycle events: {policy_id}")
            continue
        previous_state = "ISSUED"
        for event in events:
            if event["from_state"] != previous_state:
                errors.append(f"Policy history is discontinuous: {policy_id}")
                break
            allowed_policy_states = {
                "ISSUED": {"ACTIVE"},
                "ACTIVE": {"EXPIRED", "CANCELLED"},
                "EXPIRED": set(),
                "CANCELLED": set(),
            }
            if event["to_state"] not in allowed_policy_states[previous_state]:
                errors.append(f"Invalid policy lifecycle transition: {policy_id}")
                break
            previous_state = event["to_state"]
        if current_state != previous_state:
            errors.append(f"Current policy state does not match event history: {policy_id}")


def _validate_duplicate_detection(
    connection: sqlite3.Connection, errors: list[str]
) -> None:
    duplicates = connection.execute(
        """
        SELECT claim_id, policy_id, incident_id, claim_type, claimed_amount_cents,
               reported_at
        FROM claims
        ORDER BY policy_id, incident_id, claim_type, claimed_amount_cents,
                 reported_at, claim_id
        """
    ).fetchall()
    seen: dict[tuple[Any, ...], str] = {}
    for claim in duplicates:
        fingerprint = (
            claim["policy_id"],
            claim["incident_id"],
            claim["claim_type"],
            claim["claimed_amount_cents"],
        )
        original = seen.get(fingerprint)
        if original is None:
            seen[fingerprint] = claim["claim_id"]
            continue
        assessment = connection.execute(
            """
            SELECT rationale_json FROM assessments WHERE claim_id = ?
            ORDER BY assessment_sequence DESC LIMIT 1
            """,
            (claim["claim_id"],),
        ).fetchone()
        if not assessment:
            errors.append(f"Duplicate claim has no assessment: {claim['claim_id']}")
            continue
        rationale = json.loads(assessment["rationale_json"])
        if "duplicate_claim" not in rationale["anomaly_codes"]:
            errors.append(f"Duplicate claim not detected: {claim['claim_id']}")
        if rationale.get("duplicate_of") != original:
            errors.append(f"Duplicate reference is incorrect: {claim['claim_id']}")


def _load_ground_truth(path: Path, errors: list[str]) -> dict[str, dict[str, Any]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"Cannot read ground truth {path}: {error}")
        return None
    records = data.get("claims")
    if not isinstance(records, list):
        errors.append("Ground truth must contain a claims list")
        return None
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        claim_id = record.get("claim_id")
        if not claim_id or claim_id in indexed:
            errors.append(f"Ground-truth claim ID is empty or duplicated: {claim_id}")
            continue
        indexed[claim_id] = record
    return indexed


def _validate_ground_truth(
    connection: sqlite3.Connection,
    records: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    claim_ids = {
        row["claim_id"] for row in connection.execute("SELECT claim_id FROM claims")
    }
    if set(records) != claim_ids:
        errors.append(
            "Ground truth claim IDs do not exactly match input claims "
            f"(missing={len(claim_ids - set(records))}, extra={len(set(records) - claim_ids)})"
        )
    for claim_id in claim_ids & set(records):
        record = records[claim_id]
        claim = connection.execute(
            "SELECT lifecycle_state FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        assessment = connection.execute(
            """
            SELECT eligibility, rationale_json
            FROM assessments WHERE claim_id = ?
            ORDER BY assessment_sequence DESC LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT settlement_id, status, recommended_amount_cents
            FROM settlements WHERE claim_id = ?
            ORDER BY settlement_sequence DESC LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        if not assessment or not settlement:
            errors.append(f"Ground-truth claim lacks processing outputs: {claim_id}")
            continue
        rationale = json.loads(assessment["rationale_json"])
        labels = set(rationale["anomaly_codes"])
        has_duplicate_payment = connection.execute(
            """
            SELECT 1 FROM settlement_events
            WHERE settlement_id = ? AND outcome = 'REJECTED_DUPLICATE'
            LIMIT 1
            """,
            (settlement["settlement_id"],),
        ).fetchone()
        if has_duplicate_payment:
            labels.add("duplicate_payment_attempt")
        comparisons = (
            ("expected_decision", claim["lifecycle_state"]),
            ("expected_eligibility", assessment["eligibility"]),
            ("expected_settlement_status", settlement["status"]),
            ("expected_settlement_cents", settlement["recommended_amount_cents"]),
        )
        for field, expected in comparisons:
            if record.get(field) != expected:
                errors.append(f"Ground-truth {field} mismatch for {claim_id}")
        if set(record.get("anomaly_labels", [])) != labels:
            errors.append(f"Ground-truth anomaly labels mismatch for {claim_id}")
        if record.get("duplicate_of") != rationale.get("duplicate_of"):
            errors.append(f"Ground-truth duplicate reference mismatch for {claim_id}")


def _validate_exports(
    output_dir: Path, row_counts: dict[str, int], errors: list[str]
) -> None:
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"Cannot read manifest {manifest_path}: {error}")
        return
    if manifest.get("row_counts") != row_counts:
        errors.append("Manifest row counts do not match the database")
    for file_name, expected_hash in manifest.get("sha256", {}).items():
        path = output_dir / file_name
        if not path.is_file():
            errors.append(f"Manifest file is missing: {file_name}")
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            errors.append(f"Checksum mismatch: {file_name}")
    for table, expected_count in row_counts.items():
        csv_path = output_dir / f"{table}.csv"
        if not csv_path.is_file():
            errors.append(f"CSV export missing for table: {table}")
            continue
        with csv_path.open("r", encoding="utf-8", newline="") as stream:
            actual_count = sum(1 for _ in csv.reader(stream)) - 1
        if actual_count != expected_count:
            errors.append(
                f"CSV row count mismatch for {table}: "
                f"expected {expected_count}, got {actual_count}"
            )
    claims_csv = output_dir / "claims.csv"
    if claims_csv.is_file():
        with claims_csv.open("r", encoding="utf-8", newline="") as stream:
            input_fields = next(csv.reader(stream), [])
        if "anomaly_labels" in input_fields or "ground_truth" in input_fields:
            errors.append("Ground-truth labels leaked into the claims input table")
