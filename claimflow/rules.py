import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any


CLAIM_TRANSITIONS: dict[str, set[str]] = {
    "SUBMITTED": {"TRIAGE"},
    "TRIAGE": {
        "NEEDS_INFO", "UNDER_REVIEW", "REJECTED", "SETTLEMENT_RECOMMENDED"
    },
    "NEEDS_INFO": {"UNDER_REVIEW", "REJECTED"},
    "UNDER_REVIEW": {
        "NEEDS_INFO", "REJECTED", "APPROVED", "SETTLEMENT_RECOMMENDED"
    },
    "APPROVED": {"SETTLEMENT_RECOMMENDED"},
    "SETTLEMENT_RECOMMENDED": {"PAID"},
    "REJECTED": set(),
    "PAID": set(),
}
ALLOWED_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
}
CLAIM_CAUSES = {
    "AUTO_COLLISION": {
        "normal": ("collision", "weather_event"),
        "excluded": ("wear_and_tear", "intentional_damage"),
    },
    "AUTO_COMPREHENSIVE": {
        "normal": ("theft", "hail"),
        "excluded": ("intentional_loss",),
    },
    "HOME_PROPERTY": {
        "normal": ("fire", "water_damage", "storm"),
        "excluded": ("wear_and_tear", "maintenance_neglect"),
    },
    "HEALTH_MEDICAL": {
        "normal": ("illness", "accidental_injury"),
        "excluded": ("cosmetic_procedure", "pre_existing_condition"),
    },
    "THIRD_PARTY_LIABILITY": {
        "normal": ("accidental_injury", "property_damage"),
        "excluded": ("intentional_injury",),
    },
}


def verify_document_metadata(
    file_name: str,
    mime_type: str,
    size_bytes: int,
    content_sha256: str,
) -> tuple[str, str]:
    if not file_name.strip():
        return "REJECTED", "File name is required"
    if mime_type not in ALLOWED_DOCUMENT_MIME_TYPES:
        return "REJECTED", f"Unsupported MIME type: {mime_type}"
    if size_bytes <= 0 or size_bytes > 25_000_000:
        return "REJECTED", "Document size must be between 1 and 25000000 bytes"
    if not re.fullmatch(r"[a-fA-F0-9]{64}", content_sha256):
        return "REJECTED", "Document content SHA-256 must contain 64 hexadecimal characters"
    return "VERIFIED", "Required metadata present and document constraints passed"


def _utc_datetime(value: str | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(f"{value}T00:00:00+00:00")


def _event_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _insert_claim_event(
    connection: sqlite3.Connection,
    claim_id: str,
    from_state: str | None,
    to_state: str,
    event_at: datetime,
    reason: str,
) -> None:
    if from_state and to_state not in CLAIM_TRANSITIONS[from_state]:
        raise ValueError(f"Invalid claim lifecycle transition: {from_state} -> {to_state}")
    event_number = connection.execute(
        "SELECT COUNT(*) FROM claim_events WHERE claim_id = ?", (claim_id,)
    ).fetchone()[0] + 1
    connection.execute(
        """
        INSERT INTO claim_events
            (claim_event_id, claim_id, from_state, to_state, event_at, actor, reason)
        VALUES (?, ?, ?, ?, ?, 'rules-engine', ?)
        """,
        (
            f"EVT-{claim_id}-{event_number:03d}",
            claim_id,
            from_state,
            to_state,
            _event_time(event_at),
            reason,
        ),
    )


def _matching_duplicate(
    connection: sqlite3.Connection, claim: sqlite3.Row, amount_cents: int
) -> str | None:
    row = connection.execute(
        """
        SELECT claim_id
        FROM claims
        WHERE claim_id != ?
          AND policy_id = ?
          AND incident_id = ?
          AND claim_type = ?
          AND claimed_amount_cents = ?
          AND (reported_at < ? OR (reported_at = ? AND claim_id < ?))
        ORDER BY reported_at, claim_id
        LIMIT 1
        """,
        (
            claim["claim_id"],
            claim["policy_id"],
            claim["incident_id"],
            claim["claim_type"],
            amount_cents,
            claim["reported_at"],
            claim["reported_at"],
            claim["claim_id"],
        ),
    ).fetchone()
    return row["claim_id"] if row else None


def _existing_result(connection: sqlite3.Connection, claim_id: str) -> dict[str, Any]:
    assessment = connection.execute(
        """
        SELECT * FROM assessments
        WHERE claim_id = ? ORDER BY assessment_sequence DESC LIMIT 1
        """,
        (claim_id,),
    ).fetchone()
    reserve = connection.execute(
        "SELECT * FROM reserves WHERE claim_id = ? ORDER BY reserve_sequence DESC LIMIT 1",
        (claim_id,),
    ).fetchone()
    settlement = connection.execute(
        """
        SELECT * FROM settlements
        WHERE claim_id = ? ORDER BY settlement_sequence DESC LIMIT 1
        """,
        (claim_id,),
    ).fetchone()
    return {
        "claim_id": claim_id,
        "decision": assessment["eligibility"],
        "lifecycle_state": connection.execute(
            "SELECT lifecycle_state FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()["lifecycle_state"],
        "assessed_amount_cents": assessment["assessed_amount_cents"],
        "reserve_cents": reserve["amount_cents"],
        "settlement_cents": settlement["recommended_amount_cents"],
        "settlement_status": settlement["status"],
        "reasons": json.loads(assessment["rationale_json"])["reasons"],
        "anomaly_codes": json.loads(assessment["rationale_json"])["anomaly_codes"],
        "duplicate_of": json.loads(assessment["rationale_json"]).get("duplicate_of"),
    }


def process_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    processed_at: datetime | None = None,
    reassess: bool = False,
) -> dict[str, Any]:
    """Assess one claim once; persisted assessments are returned without mutation."""
    claim = connection.execute(
        """
        SELECT cl.*, p.product_type, p.effective_from, p.effective_to,
               p.customer_id AS policy_customer_id,
               p.lifecycle_state AS policy_state, i.occurred_on,
               i.region AS incident_region, i.customer_id AS incident_customer_id,
               i.cause_code
        FROM claims cl
        JOIN policies p ON p.policy_id = cl.policy_id
        JOIN incidents i ON i.incident_id = cl.incident_id
        WHERE cl.claim_id = ?
        """,
        (claim_id,),
    ).fetchone()
    if claim is None:
        raise LookupError(f"Claim not found: {claim_id}")
    has_assessment = connection.execute(
        "SELECT 1 FROM assessments WHERE claim_id = ? LIMIT 1", (claim_id,)
    ).fetchone()
    if has_assessment and not reassess:
        return _existing_result(connection, claim_id)
    if reassess and claim["lifecycle_state"] != "NEEDS_INFO":
        raise ValueError("Only claims in NEEDS_INFO can be reassessed with new evidence")

    processed_at = processed_at or _utc_datetime(claim["reported_at"])
    incident_date = date.fromisoformat(claim["occurred_on"])
    policy_start = date.fromisoformat(claim["effective_from"])
    policy_end = date.fromisoformat(claim["effective_to"])
    reasons: list[str] = []
    anomaly_codes: list[str] = []

    coverage = connection.execute(
        """
        SELECT c.coverage_id, c.product_type, c.required_documents_json,
               pc.limit_cents, pc.deductible_cents,
               pc.effective_from AS coverage_start, pc.effective_to AS coverage_end,
               pc.exclusions_json
        FROM coverages c
        LEFT JOIN policy_coverages pc
            ON pc.coverage_id = c.coverage_id AND pc.policy_id = ?
        WHERE c.claim_type = ?
        """,
        (claim["policy_id"], claim["claim_type"]),
    ).fetchone()
    if coverage is None:
        raise ValueError(f"Claim references an unknown claim type: {claim['claim_type']}")

    amount_cents = connection.execute(
        "SELECT COALESCE(SUM(line_amount_cents), 0) FROM claim_items WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()[0]
    if amount_cents != claim["claimed_amount_cents"]:
        raise ValueError(f"Claim amount does not reconcile with items: {claim_id}")
    if amount_cents == 0:
        reasons.append("zero_claim_amount")
        anomaly_codes.append("invalid_claim_amount")
    if (
        claim["customer_id"] != claim["policy_customer_id"]
        or claim["customer_id"] != claim["incident_customer_id"]
    ):
        reasons.append("claimant_does_not_match_policyholder")
        anomaly_codes.append("claimant_policy_mismatch")
    valid_causes = (
        set(CLAIM_CAUSES[claim["claim_type"]]["normal"])
        | set(CLAIM_CAUSES[claim["claim_type"]]["excluded"])
    )
    if claim["cause_code"] not in valid_causes:
        reasons.append(f"unknown_incident_cause:{claim['cause_code']}")
        anomaly_codes.append("invalid_incident_cause")
    if date.fromisoformat(claim["reported_at"][:10]) < incident_date:
        reasons.append("claim_reported_before_incident")
        anomaly_codes.append("reported_before_incident")
    if incident_date < policy_start or incident_date > policy_end:
        reasons.append("incident_outside_policy_period")
        anomaly_codes.append("incident_outside_policy_period")
        if incident_date > policy_end and claim["policy_state"] == "EXPIRED":
            reasons.append("policy_expired_before_incident")
            anomaly_codes.append("policy_expired_before_incident")
        elif incident_date > policy_end and claim["policy_state"] == "CANCELLED":
            reasons.append("policy_cancelled_before_incident")
            anomaly_codes.append("policy_cancelled_before_incident")
    if coverage["limit_cents"] is None:
        reasons.append("policy_does_not_include_claim_coverage")
        anomaly_codes.append("coverage_not_on_policy")
    elif (
        incident_date < date.fromisoformat(coverage["coverage_start"])
        or incident_date > date.fromisoformat(coverage["coverage_end"])
    ):
        reasons.append("incident_outside_coverage_effective_period")
        anomaly_codes.append("incident_outside_coverage_period")
    if (
        coverage["exclusions_json"] is not None
        and claim["cause_code"] in json.loads(coverage["exclusions_json"])
    ):
        reasons.append(f"incident_cause_excluded:{claim['cause_code']}")
        anomaly_codes.append("excluded_cause")

    duplicate_of = _matching_duplicate(connection, claim, amount_cents)
    if duplicate_of:
        reasons.append(f"duplicate_of:{duplicate_of}")
        anomaly_codes.append("duplicate_claim")

    required_documents = json.loads(coverage["required_documents_json"])
    verified_document_types = {
        row["document_type"]
        for row in connection.execute(
            """
            SELECT DISTINCT d.document_type
            FROM documents d
            WHERE d.claim_id = ?
              AND (
                  SELECT v.status
                  FROM document_verifications v
                  WHERE v.document_id = d.document_id
                  ORDER BY v.verified_at DESC, v.verification_id DESC
                  LIMIT 1
              ) = 'VERIFIED'
            """,
            (claim_id,),
        )
    }
    missing_documents = sorted(set(required_documents) - verified_document_types)
    if missing_documents:
        reasons.extend(f"missing_or_unverified_document:{doc}" for doc in missing_documents)
        anomaly_codes.append("incomplete_evidence")
        anomaly_codes.append("missing_mandatory_document")

    item_totals = connection.execute(
        """
        SELECT coverage_id, SUM(line_amount_cents) AS amount_cents
        FROM claim_items
        WHERE claim_id = ?
        GROUP BY coverage_id
        """,
        (claim_id,),
    ).fetchall()
    payable_cents = 0
    eligible_exposure_cents = 0
    per_coverage_limit_exceeded = False
    if coverage["limit_cents"] is not None:
        for item_total in item_totals:
            terms = connection.execute(
                """
                SELECT limit_cents, deductible_cents
                FROM policy_coverages
                WHERE policy_id = ? AND coverage_id = ?
                """,
                (claim["policy_id"], item_total["coverage_id"]),
            ).fetchone()
            if terms is None:
                continue
            gross = item_total["amount_cents"]
            eligible_exposure_cents += gross
            if gross > terms["limit_cents"]:
                per_coverage_limit_exceeded = True
                anomaly_codes.append("claim_exceeds_coverage_limit")
                reasons.append(
                    f"coverage_limit_exceeded:{item_total['coverage_id']}"
                )
            payable_cents += min(
                max(gross - terms["deductible_cents"], 0), terms["limit_cents"]
            )
    payable_cents = min(payable_cents, eligible_exposure_cents)
    very_high_exposure = (
        coverage["limit_cents"] is not None
        and amount_cents > coverage["limit_cents"] * 2
    )
    if very_high_exposure:
        anomaly_codes.append("exposure_more_than_twice_limit")
        reasons.append("exposure_exceeds_twice_policy_limit_requires_manual_review")

    hard_rejection = any(
        code in anomaly_codes
        for code in (
            "zero_claim_amount",
            "claimant_policy_mismatch",
            "incident_outside_policy_period",
            "incident_outside_coverage_period",
            "coverage_not_on_policy",
            "duplicate_claim",
            "reported_before_incident",
            "excluded_cause",
            "invalid_incident_cause",
        )
    )
    if hard_rejection:
        lifecycle_state = "REJECTED"
        eligibility = "INELIGIBLE"
        settlement_status = "DECLINED"
        payable_cents = 0
        reasons.append("claim_rejected_by_documented_eligibility_rule")
    elif missing_documents:
        lifecycle_state = "NEEDS_INFO"
        eligibility = "PENDING"
        settlement_status = "PENDING"
        payable_cents = 0
        reasons.append("request_missing_or_unverified_mandatory_evidence")
    elif very_high_exposure:
        lifecycle_state = "UNDER_REVIEW"
        eligibility = "PENDING"
        settlement_status = "PENDING"
        payable_cents = 0
        reasons.append("manual_review_required_before_settlement")
    else:
        lifecycle_state = "SETTLEMENT_RECOMMENDED"
        eligibility = "ELIGIBLE"
        settlement_status = "RECOMMENDED"
        reasons.append("eligible_amount_after_deductible_and_limit")
        if per_coverage_limit_exceeded:
            reasons.append("settlement_capped_at_coverage_limit")

    if not anomaly_codes:
        reasons.append("no_rule_exceptions")
    rationale = {
        "reasons": reasons,
        "anomaly_codes": sorted(set(anomaly_codes)),
        "missing_documents": missing_documents,
        "claimed_amount_cents": amount_cents,
        "covered_exposure_cents": eligible_exposure_cents,
        "deductible_cents": coverage["deductible_cents"] or 0,
        "coverage_limit_cents": coverage["limit_cents"],
        "exclusions": json.loads(coverage["exclusions_json"])
        if coverage["exclusions_json"] is not None
        else [],
        "cause_code": claim["cause_code"],
        "duplicate_of": duplicate_of,
        "fraud_indicator": very_high_exposure or bool(duplicate_of),
        "settlement_formula": (
            "sum(min(max(coverage_item_total - deductible, 0), coverage_limit))"
        ),
    }

    with connection:
        connection.execute(
            "UPDATE claims SET claimed_amount_cents = ? WHERE claim_id = ?",
            (amount_cents, claim_id),
        )
        current_state = claim["lifecycle_state"]
        if current_state == "SUBMITTED":
            _insert_claim_event(
                connection,
                claim_id,
                "SUBMITTED",
                "TRIAGE",
                processed_at,
                "Automated intake and eligibility triage started",
            )
            decision_from_state = "TRIAGE"
        elif current_state == "NEEDS_INFO":
            _insert_claim_event(
                connection,
                claim_id,
                "NEEDS_INFO",
                "UNDER_REVIEW",
                processed_at,
                "Additional evidence received; reassessment started",
            )
            decision_from_state = "UNDER_REVIEW"
        else:
            raise ValueError(f"Claim cannot be assessed from state {current_state}")
        if lifecycle_state != decision_from_state:
            _insert_claim_event(
                connection,
                claim_id,
                decision_from_state,
                lifecycle_state,
                processed_at,
                "; ".join(reasons),
            )
        connection.execute(
            "UPDATE claims SET lifecycle_state = ? WHERE claim_id = ?",
            (lifecycle_state, claim_id),
        )
        assessment_sequence = connection.execute(
            """
            SELECT COALESCE(MAX(assessment_sequence), 0) + 1
            FROM assessments WHERE claim_id = ?
            """,
            (claim_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO assessments
                (assessment_id, claim_id, assessment_sequence, assessed_at,
                 assessed_amount_cents, eligibility, rationale_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"ASM-{claim_id}-{assessment_sequence:03d}",
                claim_id,
                assessment_sequence,
                _event_time(processed_at),
                payable_cents,
                eligibility,
                json.dumps(rationale, sort_keys=True),
            ),
        )
        reserve_cents = payable_cents * 80 // 100
        connection.execute(
            """
            INSERT INTO reserves
                (reserve_id, claim_id, reserve_sequence, calculated_at, amount_cents, method)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"RSV-{claim_id}-{assessment_sequence:03d}",
                claim_id,
                assessment_sequence,
                _event_time(processed_at),
                reserve_cents,
                "80% of assessed settlement recommendation; integer cents",
            ),
        )
        connection.execute(
            """
            INSERT INTO settlements
                (settlement_id, claim_id, settlement_sequence, recommended_at,
                 recommended_amount_cents, status, rationale_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"SET-{claim_id}-{assessment_sequence:03d}",
                claim_id,
                assessment_sequence,
                _event_time(processed_at),
                payable_cents,
                settlement_status,
                json.dumps(
                    {
                        "reasons": reasons,
                        "formula": rationale["settlement_formula"],
                    },
                    sort_keys=True,
                ),
            ),
        )
        _assign_adjuster(connection, claim, processed_at)

    return _existing_result(connection, claim_id)


def _assign_adjuster(
    connection: sqlite3.Connection, claim: sqlite3.Row, assigned_at: datetime
) -> None:
    if connection.execute(
        "SELECT 1 FROM adjuster_assignments WHERE claim_id = ? LIMIT 1",
        (claim["claim_id"],),
    ).fetchone():
        return
    adjusters = connection.execute(
        "SELECT adjuster_id, region FROM adjusters WHERE active = 1 ORDER BY adjuster_id"
    ).fetchall()
    if not adjusters:
        raise ValueError("No active adjusters are available")
    workload = {
        row["adjuster_id"]: row["assignment_count"]
        for row in connection.execute(
            """
            SELECT a.adjuster_id, COUNT(aa.assignment_id) AS assignment_count
            FROM adjusters a
            LEFT JOIN adjuster_assignments aa
                ON aa.adjuster_id = a.adjuster_id AND aa.status = 'ACTIVE'
            GROUP BY a.adjuster_id
            """
        )
    }
    region_matches = [a for a in adjusters if a["region"] == claim["incident_region"]]
    candidates = region_matches or adjusters
    chosen = min(candidates, key=lambda a: (workload[a["adjuster_id"]], a["adjuster_id"]))
    connection.execute(
        """
        INSERT INTO adjuster_assignments
            (assignment_id, claim_id, adjuster_id, assigned_at, status)
        VALUES (?, ?, ?, ?, 'ACTIVE')
        """,
        (
            f"ASN-{claim['claim_id']}-001",
            claim["claim_id"],
            chosen["adjuster_id"],
            _event_time(assigned_at),
        ),
    )


def record_payment(
    connection: sqlite3.Connection, claim_id: str, payment_reference: str, paid_at: datetime
) -> dict[str, Any]:
    """Record the first payment and reject later payment attempts without double-paying."""
    settlement = connection.execute(
        """
        SELECT * FROM settlements
        WHERE claim_id = ? ORDER BY settlement_sequence DESC LIMIT 1
        """,
        (claim_id,),
    ).fetchone()
    if settlement is None:
        raise LookupError(f"Settlement not found for claim: {claim_id}")
    if settlement["status"] in {"DECLINED", "PENDING"}:
        raise ValueError(f"Settlement is not payable: {settlement['status']}")
    if settlement["recommended_amount_cents"] <= 0:
        raise ValueError("Settlement amount must be positive before payment")
    if settlement["status"] == "PAID":
        outcome = "REJECTED_DUPLICATE"
        reason = "Settlement already has a successful payment; duplicate attempt rejected"
    else:
        outcome = "SUCCEEDED"
        reason = "First payment recorded"
    event_count = connection.execute(
        "SELECT COUNT(*) FROM settlement_events WHERE settlement_id = ?",
        (settlement["settlement_id"],),
    ).fetchone()[0] + 1
    with connection:
        connection.execute(
            """
            INSERT INTO settlement_events
                (settlement_event_id, settlement_id, event_type, outcome, event_at,
                 payment_reference, reason)
            VALUES (?, ?, 'PAYMENT_ATTEMPT', ?, ?, ?, ?)
            """,
            (
                f"PAYEVT-{settlement['settlement_id']}-{event_count:03d}",
                settlement["settlement_id"],
                outcome,
                _event_time(paid_at),
                payment_reference,
                reason,
            ),
        )
        if outcome == "SUCCEEDED":
            connection.execute(
                """
                UPDATE settlements
                SET status = 'PAID', payment_reference = ?
                WHERE settlement_id = ? AND status IN ('RECOMMENDED', 'APPROVED')
                """,
                (payment_reference, settlement["settlement_id"]),
            )
            current_state = connection.execute(
                "SELECT lifecycle_state FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()["lifecycle_state"]
            if current_state == "SETTLEMENT_RECOMMENDED":
                _insert_claim_event(
                    connection,
                    claim_id,
                    current_state,
                    "PAID",
                    paid_at,
                    "First payment recorded",
                )
                connection.execute(
                    "UPDATE claims SET lifecycle_state = 'PAID' WHERE claim_id = ?",
                    (claim_id,),
                )
    return {
        "claim_id": claim_id,
        "payment_reference": payment_reference,
        "outcome": outcome,
        "reason": reason,
    }
