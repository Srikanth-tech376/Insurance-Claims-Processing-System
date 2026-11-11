from datetime import date, timedelta
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from claimflow.api import create_app
from claimflow.config import GenerationConfig
from claimflow.generator import generate_dataset


def test_claim_read_process_and_missing_claim_routes(tmp_path: Path):
    output_dir = tmp_path / "generated"
    generate_dataset(
        GenerationConfig(
            policy_count=80,
            claim_count=40,
            adjuster_count=4,
            output_dir=output_dir,
        )
    )
    client = TestClient(create_app(output_dir / "claims.sqlite"))

    page = client.get("/claims?limit=5")
    assert page.status_code == 200
    assert page.json()["total"] == 40
    assert len(page.json()["claims"]) == 5

    claim_id = page.json()["claims"][0]["claim_id"]
    detail = client.get(f"/claims/{claim_id}")
    assert detail.status_code == 200
    assert detail.json()["claim"]["claim_id"] == claim_id
    assert detail.json()["assessment"] is not None
    assert detail.json()["lifecycle_events"]

    processed = client.post(f"/claims/{claim_id}/process")
    assert processed.status_code == 200
    assert processed.json()["claim_id"] == claim_id

    missing = client.get("/claims/CLM-DOES-NOT-EXIST")
    assert missing.status_code == 404


def test_customer_policy_claim_intake_and_duplicate_payment_protection(tmp_path: Path):
    output_dir = tmp_path / "generated"
    generate_dataset(
        GenerationConfig(
            policy_count=80,
            claim_count=40,
            adjuster_count=4,
            output_dir=output_dir,
        )
    )
    client = TestClient(create_app(output_dir / "claims.sqlite"))
    customer_response = client.post(
        "/customers",
        json={
            "full_name": "Test Customer",
            "email": "new-customer@example.test",
            "phone": "+1-555-0000",
        },
    )
    assert customer_response.status_code == 201
    customer_id = customer_response.json()["customer_id"]

    today = date.today()
    policy_response = client.post(
        "/policies",
        json={
            "customer_id": customer_id,
            "product_type": "AUTO",
            "effective_from": (today - timedelta(days=30)).isoformat(),
            "effective_to": (today + timedelta(days=365)).isoformat(),
            "lifecycle_state": "ACTIVE",
            "coverages": [
                {
                    "claim_type": "AUTO_COLLISION",
                    "limit_cents": 2_000_000,
                    "deductible_cents": 50_000,
                }
            ],
        },
    )
    assert policy_response.status_code == 201
    policy_id = policy_response.json()["policy_id"]

    digest = hashlib.sha256(b"synthetic test document").hexdigest()
    claim_response = client.post(
        "/claims",
        json={
            "customer_id": customer_id,
            "policy_id": policy_id,
            "occurred_on": today.isoformat(),
            "claim_type": "AUTO_COLLISION",
            "cause_code": "collision",
            "region": "NORTH",
            "description": "Test collision",
            "items": [
                {
                    "description": "Repair estimate",
                    "quantity": 1,
                    "unit_amount_cents": 200_000,
                }
            ],
            "documents": [],
        },
    )
    assert claim_response.status_code == 201, claim_response.text
    result = claim_response.json()
    assert result["claimed_amount_cents"] == 200_000
    assert result["lifecycle_state"] == "NEEDS_INFO"

    evidence_route = f"/claims/{result['claim_id']}/documents"
    evidence_results = []
    for document_type in ("incident_report", "repair_estimate", "photos"):
        evidence = client.post(
            evidence_route,
            json={
                "document_type": document_type,
                "file_name": f"{document_type}.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 1024,
                "content_sha256": digest,
            },
        )
        assert evidence.status_code == 201, evidence.text
        evidence_results.append(evidence.json()["decision"])
    assert [row["lifecycle_state"] for row in evidence_results] == [
        "NEEDS_INFO",
        "NEEDS_INFO",
        "SETTLEMENT_RECOMMENDED",
    ]
    result["claim_id"] = claim_response.json()["claim_id"]
    result["lifecycle_state"] = evidence_results[-1]["lifecycle_state"]
    result["settlement_cents"] = evidence_results[-1]["settlement_cents"]
    assert result["lifecycle_state"] == "SETTLEMENT_RECOMMENDED"
    assert result["settlement_cents"] == 150_000

    payment_route = f"/claims/{result['claim_id']}/payments"
    first_payment = client.post(
        payment_route,
        json={"payment_reference": "API-TEST-PAYMENT-1"},
    )
    second_payment = client.post(
        payment_route,
        json={"payment_reference": "API-TEST-PAYMENT-2"},
    )
    assert first_payment.status_code == 200
    assert first_payment.json()["outcome"] == "SUCCEEDED"
    assert second_payment.status_code == 200
    assert second_payment.json()["outcome"] == "REJECTED_DUPLICATE"

    detail = client.get(f"/claims/{result['claim_id']}").json()
    assert detail["claim"]["lifecycle_state"] == "PAID"
    assert detail["settlement"]["status"] == "PAID"
    assert len(detail["assessment_history"]) == 4
    assert len(detail["reserve_history"]) == 4
    assert len(detail["settlement_history"]) == 4
