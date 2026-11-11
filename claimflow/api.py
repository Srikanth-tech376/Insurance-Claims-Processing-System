import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .database import connect
from .rules import CLAIM_CAUSES, process_claim, record_payment, verify_document_metadata


class CustomerCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(min_length=3, max_length=32)


class PolicyCoverageInput(BaseModel):
    claim_type: Literal[
        "AUTO_COLLISION",
        "AUTO_COMPREHENSIVE",
        "HOME_PROPERTY",
        "HEALTH_MEDICAL",
        "THIRD_PARTY_LIABILITY",
    ]
    limit_cents: int = Field(gt=0)
    deductible_cents: int = Field(ge=0)
    exclusions: list[str] | None = None


class PolicyCreate(BaseModel):
    customer_id: str
    product_type: Literal["AUTO", "HOME", "HEALTH", "LIABILITY"]
    effective_from: date
    effective_to: date
    lifecycle_state: Literal["ACTIVE", "EXPIRED", "CANCELLED"] = "ACTIVE"
    coverages: list[PolicyCoverageInput] = Field(min_length=1)


class ClaimItemInput(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0)
    unit_amount_cents: int = Field(ge=0)


class DocumentInput(BaseModel):
    document_type: str = Field(min_length=1, max_length=80)
    file_name: str = Field(min_length=1, max_length=255)
    mime_type: str
    size_bytes: int = Field(ge=0)
    content_sha256: str


class ClaimCreate(BaseModel):
    customer_id: str
    policy_id: str
    occurred_on: date
    claim_type: Literal[
        "AUTO_COLLISION",
        "AUTO_COMPREHENSIVE",
        "HOME_PROPERTY",
        "HEALTH_MEDICAL",
        "THIRD_PARTY_LIABILITY",
    ]
    cause_code: str = Field(min_length=1, max_length=80)
    region: Literal["NORTH", "SOUTH", "EAST", "WEST"]
    description: str = Field(min_length=1, max_length=500)
    items: list[ClaimItemInput] = Field(min_length=1)
    documents: list[DocumentInput] = Field(default_factory=list)


class PaymentRequest(BaseModel):
    payment_reference: str = Field(min_length=1, max_length=100)


def _next_numeric_id(connection: sqlite3.Connection, table: str, column: str, prefix: str) -> int:
    rows = connection.execute(f'SELECT "{column}" FROM "{table}"').fetchall()
    values = [
        int(row[column].rsplit("-", 1)[-1])
        for row in rows
        if row[column].startswith(prefix)
    ]
    return max(values, default=0) + 1


def create_app(database_path: str | Path = Path("data/generated/claims.sqlite")) -> FastAPI:
    database_path = Path(database_path)
    app = FastAPI(
        title="YottaFlex Claims API",
        version="1.0.0",
        description="Read and process the deterministic synthetic claims dataset.",
    )

    def get_connection():
        if not database_path.is_file():
            raise HTTPException(
                status_code=503,
                detail=f"Database is unavailable: {database_path}",
            )
        connection = connect(database_path)
        try:
            yield connection
        finally:
            connection.close()

    Connection = Annotated[sqlite3.Connection, Depends(get_connection)]

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok" if database_path.is_file() else "not_ready",
            "database": str(database_path),
        }

    @app.post("/customers", status_code=201)
    def create_customer(payload: CustomerCreate, connection: Connection) -> dict[str, Any]:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM customers WHERE email = ?", (payload.email,)
        ).fetchone():
            raise HTTPException(status_code=409, detail="A customer with this email already exists")
        customer_id = f"CUS-{_next_numeric_id(connection, 'customers', 'customer_id', 'CUS-'):06d}"
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO customers (customer_id, full_name, email, phone, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (customer_id, payload.full_name, payload.email, payload.phone, created_at),
        )
        connection.commit()
        return {
            "customer_id": customer_id,
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
            "created_at": created_at,
        }

    @app.post("/policies", status_code=201)
    def create_policy(payload: PolicyCreate, connection: Connection) -> dict[str, Any]:
        if payload.effective_to <= payload.effective_from:
            raise HTTPException(status_code=422, detail="effective_to must be after effective_from")
        if any(term.deductible_cents >= term.limit_cents for term in payload.coverages):
            raise HTTPException(status_code=422, detail="Deductible must be below coverage limit")
        if len({term.claim_type for term in payload.coverages}) != len(payload.coverages):
            raise HTTPException(status_code=422, detail="Coverage claim types must be unique")

        connection.execute("BEGIN IMMEDIATE")
        customer = connection.execute(
            "SELECT 1 FROM customers WHERE customer_id = ?", (payload.customer_id,)
        ).fetchone()
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        coverage_rows: list[tuple[PolicyCoverageInput, str, list[str]]] = []
        for term in payload.coverages:
            coverage = connection.execute(
                """
                SELECT coverage_id, product_type FROM coverages WHERE claim_type = ?
                """,
                (term.claim_type,),
            ).fetchone()
            if coverage is None or coverage["product_type"] != payload.product_type:
                raise HTTPException(
                    status_code=422,
                    detail=f"{term.claim_type} is not valid for {payload.product_type}",
                )
            allowed_exclusions = set(CLAIM_CAUSES[term.claim_type]["excluded"])
            requested_exclusions = (
                set(term.exclusions)
                if term.exclusions is not None
                else allowed_exclusions
            )
            if not requested_exclusions.issubset(allowed_exclusions):
                raise HTTPException(
                    status_code=422,
                    detail=f"Unsupported exclusion for {term.claim_type}",
                )
            coverage_rows.append(
                (term, coverage["coverage_id"], sorted(requested_exclusions))
            )

        prefix = f"POL-{payload.product_type}-2026-"
        policy_id = (
            f"{prefix}{_next_numeric_id(connection, 'policies', 'policy_id', prefix):06d}"
        )
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO policies
                (policy_id, customer_id, product_type, effective_from, effective_to,
                 lifecycle_state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                payload.customer_id,
                payload.product_type,
                payload.effective_from.isoformat(),
                payload.effective_to.isoformat(),
                payload.lifecycle_state,
                created_at,
            ),
        )
        for term, coverage_id, exclusions in coverage_rows:
            connection.execute(
                """
                INSERT INTO policy_coverages
                    (policy_id, coverage_id, effective_from, effective_to,
                     limit_cents, deductible_cents, exclusions_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    coverage_id,
                    payload.effective_from.isoformat(),
                    payload.effective_to.isoformat(),
                    term.limit_cents,
                    term.deductible_cents,
                    json.dumps(exclusions),
                ),
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
                f"{payload.effective_from.isoformat()}T00:00:00+00:00",
                "Policy issued and coverage became active",
            ),
        )
        if payload.lifecycle_state != "ACTIVE":
            connection.execute(
                """
                INSERT INTO policy_events
                    (policy_event_id, policy_id, from_state, to_state, event_at, reason)
                VALUES (?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    f"PLE-{policy_id}-002",
                    policy_id,
                    payload.lifecycle_state,
                    f"{payload.effective_to.isoformat()}T00:00:00+00:00",
                    f"Policy lifecycle state changed to {payload.lifecycle_state.lower()}",
                ),
            )
        connection.commit()
        return {
            "policy_id": policy_id,
            "customer_id": payload.customer_id,
            "product_type": payload.product_type,
            "effective_from": payload.effective_from.isoformat(),
            "effective_to": payload.effective_to.isoformat(),
            "lifecycle_state": payload.lifecycle_state,
            "coverages": [
                {
                    "claim_type": term.claim_type,
                    "limit_cents": term.limit_cents,
                    "deductible_cents": term.deductible_cents,
                    "exclusions": exclusions,
                }
                for term, _, exclusions in coverage_rows
            ],
        }

    @app.post("/claims", status_code=201)
    def submit_claim(payload: ClaimCreate, connection: Connection) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if payload.occurred_on > now.date():
            raise HTTPException(status_code=422, detail="Incident date cannot be in the future")
        connection.execute("BEGIN IMMEDIATE")
        policy = connection.execute(
            """
            SELECT customer_id FROM policies WHERE policy_id = ?
            """,
            (payload.policy_id,),
        ).fetchone()
        if policy is None:
            raise HTTPException(status_code=404, detail="Policy not found")
        if policy["customer_id"] != payload.customer_id:
            raise HTTPException(
                status_code=422,
                detail="Claim customer must match the policyholder",
            )
        coverage = connection.execute(
            """
            SELECT coverage_id FROM coverages WHERE claim_type = ?
            """,
            (payload.claim_type,),
        ).fetchone()
        if coverage is None:
            raise HTTPException(status_code=422, detail="Unknown claim type")

        claim_id = (
            f"CLM-2026-{_next_numeric_id(connection, 'claims', 'claim_id', 'CLM-2026-'):06d}"
        )
        incident_id = f"INC-{claim_id}"
        reported_at = now.isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO incidents
                (incident_id, customer_id, occurred_on, incident_type, cause_code,
                 region, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                payload.customer_id,
                payload.occurred_on.isoformat(),
                payload.claim_type,
                payload.cause_code,
                payload.region,
                payload.description,
            ),
        )
        connection.execute(
            """
            INSERT INTO claims
                (claim_id, customer_id, policy_id, incident_id, claim_type, reported_at,
                 currency, claimed_amount_cents, lifecycle_state)
            VALUES (?, ?, ?, ?, ?, ?, 'USD', 0, 'SUBMITTED')
            """,
            (
                claim_id,
                payload.customer_id,
                payload.policy_id,
                incident_id,
                payload.claim_type,
                reported_at,
            ),
        )
        amount_cents = 0
        for item_index, item in enumerate(payload.items, start=1):
            line_amount_cents = item.quantity * item.unit_amount_cents
            amount_cents += line_amount_cents
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
                    coverage["coverage_id"],
                    item.description,
                    item.quantity,
                    item.unit_amount_cents,
                    line_amount_cents,
                ),
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
            (payload.policy_id, coverage["coverage_id"]),
        ).fetchone():
            connection.execute(
                """
                INSERT INTO claim_coverages (claim_id, policy_id, coverage_id)
                VALUES (?, ?, ?)
                """,
                (claim_id, payload.policy_id, coverage["coverage_id"]),
            )
        connection.execute(
            """
            INSERT INTO claim_events
                (claim_event_id, claim_id, from_state, to_state, event_at, actor, reason)
            VALUES (?, ?, NULL, 'SUBMITTED', ?, 'api', 'Claim submitted')
            """,
            (f"EVT-{claim_id}-001", claim_id, reported_at),
        )
        for document_index, document in enumerate(payload.documents, start=1):
            document_id = f"DOC-{claim_id}-{document_index:03d}"
            status, reason = verify_document_metadata(
                document.file_name,
                document.mime_type,
                document.size_bytes,
                document.content_sha256,
            )
            metadata = document.model_dump()
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
                    document.document_type,
                    reported_at,
                    document.file_name,
                    document.mime_type,
                    document.size_bytes,
                    document.content_sha256,
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            connection.execute(
                """
                INSERT INTO document_verifications
                    (verification_id, document_id, status, verified_at, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"VER-{document_id}-001", document_id, status, reported_at, reason),
            )
        connection.commit()
        try:
            decision = process_claim(connection, claim_id, now)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "claim_id": claim_id,
            "incident_id": incident_id,
            "claimed_amount_cents": amount_cents,
            **decision,
        }

    @app.get("/claims")
    def list_claims(
        connection: Connection,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        total = connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        rows = connection.execute(
            """
            SELECT c.claim_id, c.policy_id, c.incident_id, c.claim_type,
                   c.reported_at, c.claimed_amount_cents, c.lifecycle_state,
                   a.eligibility, s.status AS settlement_status,
                   s.recommended_amount_cents
            FROM claims c
            LEFT JOIN assessments a
              ON a.claim_id = c.claim_id
             AND a.assessment_sequence = (
                 SELECT MAX(a2.assessment_sequence)
                 FROM assessments a2 WHERE a2.claim_id = c.claim_id
             )
            LEFT JOIN settlements s
              ON s.claim_id = c.claim_id
             AND s.settlement_sequence = (
                 SELECT MAX(s2.settlement_sequence)
                 FROM settlements s2 WHERE s2.claim_id = c.claim_id
             )
            ORDER BY c.claim_id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return {"total": total, "limit": limit, "offset": offset, "claims": [dict(row) for row in rows]}

    @app.get("/claims/{claim_id}")
    def get_claim(claim_id: str, connection: Connection) -> dict[str, Any]:
        claim = connection.execute(
            """
            SELECT c.*, i.occurred_on, i.incident_type, i.region, i.description,
                   i.cause_code,
                   p.product_type, p.effective_from, p.effective_to, p.lifecycle_state
                       AS policy_state
            FROM claims c
            JOIN incidents i ON i.incident_id = c.incident_id
            JOIN policies p ON p.policy_id = c.policy_id
            WHERE c.claim_id = ?
            """,
            (claim_id,),
        ).fetchone()
        if claim is None:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")
        items = connection.execute(
            "SELECT * FROM claim_items WHERE claim_id = ? ORDER BY claim_item_id",
            (claim_id,),
        ).fetchall()
        documents = connection.execute(
            """
            SELECT d.*, v.status AS verification_status, v.reason AS verification_reason
            FROM documents d
            LEFT JOIN document_verifications v ON v.document_id = d.document_id
            WHERE d.claim_id = ?
            ORDER BY d.document_id
            """,
            (claim_id,),
        ).fetchall()
        assessment = connection.execute(
            """
            SELECT * FROM assessments WHERE claim_id = ?
            ORDER BY assessment_sequence DESC LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        reserve = connection.execute(
            """
            SELECT * FROM reserves WHERE claim_id = ?
            ORDER BY reserve_sequence DESC LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT * FROM settlements WHERE claim_id = ?
            ORDER BY settlement_sequence DESC LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        assessment_history = connection.execute(
            """
            SELECT * FROM assessments WHERE claim_id = ?
            ORDER BY assessment_sequence
            """,
            (claim_id,),
        ).fetchall()
        reserve_history = connection.execute(
            "SELECT * FROM reserves WHERE claim_id = ? ORDER BY reserve_sequence",
            (claim_id,),
        ).fetchall()
        settlement_history = connection.execute(
            "SELECT * FROM settlements WHERE claim_id = ? ORDER BY settlement_sequence",
            (claim_id,),
        ).fetchall()
        events = connection.execute(
            """
            SELECT * FROM claim_events WHERE claim_id = ?
            ORDER BY event_at, claim_event_id
            """,
            (claim_id,),
        ).fetchall()
        assignments = connection.execute(
            """
            SELECT aa.*, a.full_name AS adjuster_name, a.region
            FROM adjuster_assignments aa
            JOIN adjusters a ON a.adjuster_id = aa.adjuster_id
            WHERE aa.claim_id = ?
            ORDER BY aa.assigned_at, aa.assignment_id
            """,
            (claim_id,),
        ).fetchall()
        return {
            "claim": dict(claim),
            "items": [dict(row) for row in items],
            "documents": [dict(row) for row in documents],
            "assessment": (
                {
                    **dict(assessment),
                    "rationale": json.loads(assessment["rationale_json"]),
                }
                if assessment
                else None
            ),
            "assessment_history": [
                {
                    **dict(row),
                    "rationale": json.loads(row["rationale_json"]),
                }
                for row in assessment_history
            ],
            "reserve": dict(reserve) if reserve else None,
            "reserve_history": [dict(row) for row in reserve_history],
            "settlement": (
                {
                    **dict(settlement),
                    "rationale": json.loads(settlement["rationale_json"]),
                }
                if settlement
                else None
            ),
            "settlement_history": [
                {
                    **dict(row),
                    "rationale": json.loads(row["rationale_json"]),
                }
                for row in settlement_history
            ],
            "adjuster_assignments": [dict(row) for row in assignments],
            "lifecycle_events": [dict(row) for row in events],
        }

    @app.post("/claims/{claim_id}/process")
    def assess_claim(claim_id: str, connection: Connection) -> dict[str, Any]:
        try:
            return process_claim(connection, claim_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/claims/{claim_id}/documents", status_code=201)
    def add_claim_document(
        claim_id: str,
        payload: DocumentInput,
        connection: Connection,
    ) -> dict[str, Any]:
        claim = connection.execute(
            "SELECT lifecycle_state FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if claim is None:
            raise HTTPException(status_code=404, detail=f"Claim not found: {claim_id}")
        if claim["lifecycle_state"] != "NEEDS_INFO":
            raise HTTPException(
                status_code=409,
                detail="Additional evidence is accepted only for claims in NEEDS_INFO",
            )
        document_number = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE claim_id = ?", (claim_id,)
        ).fetchone()[0] + 1
        document_id = f"DOC-{claim_id}-{document_number:03d}"
        received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        status, reason = verify_document_metadata(
            payload.file_name,
            payload.mime_type,
            payload.size_bytes,
            payload.content_sha256,
        )
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
                payload.document_type,
                received_at,
                payload.file_name,
                payload.mime_type,
                payload.size_bytes,
                payload.content_sha256,
                json.dumps(payload.model_dump(), sort_keys=True),
            ),
        )
        connection.execute(
            """
            INSERT INTO document_verifications
                (verification_id, document_id, status, verified_at, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (f"VER-{document_id}-001", document_id, status, received_at, reason),
        )
        connection.commit()
        try:
            decision = process_claim(
                connection,
                claim_id,
                datetime.now(timezone.utc),
                reassess=True,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "document_id": document_id,
            "verification_status": status,
            "decision": decision,
        }

    @app.post("/claims/{claim_id}/payments")
    def make_payment(
        claim_id: str,
        payload: PaymentRequest,
        connection: Connection,
    ) -> dict[str, Any]:
        try:
            result = record_payment(
                connection,
                claim_id,
                payload.payment_reference,
                datetime.now(timezone.utc),
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return result

    return app


app = create_app()
