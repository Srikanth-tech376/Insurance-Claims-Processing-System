import csv
import json
import sqlite3
from pathlib import Path

from claimflow.config import GenerationConfig
from claimflow.database import connect
from claimflow.generator import generate_dataset
from claimflow.validation import validate_database


def _generate(path: Path):
    return generate_dataset(
        GenerationConfig(
            seed=20261022,
            policy_count=160,
            claim_count=100,
            adjuster_count=8,
            output_dir=path,
        )
    )


def test_generated_dataset_meets_integrity_and_edge_case_checks(tmp_path: Path):
    output_dir = tmp_path / "generated"
    manifest = _generate(output_dir)
    result = validate_database(
        output_dir / "claims.sqlite",
        output_dir / "ground_truth.json",
        output_dir,
    )

    assert result["valid"], result["errors"]
    assert result["policy_count"] == 160
    assert result["claim_count"] == 100
    assert result["ground_truth_claim_count"] == 100
    assert manifest["row_counts"]["customers"] > 0
    assert manifest["row_counts"]["claim_items"] >= 100
    assert manifest["row_counts"]["claim_events"] >= 300

    labels = {
        label
        for claim in json.loads(
            (output_dir / "ground_truth.json").read_text(encoding="utf-8")
        )["claims"]
        for label in claim["anomaly_labels"]
    }
    assert {
        "incomplete_evidence",
        "missing_mandatory_document",
        "incident_outside_policy_period",
        "policy_expired_before_incident",
        "policy_cancelled_before_incident",
        "excluded_cause",
        "claim_exceeds_coverage_limit",
        "coverage_not_on_policy",
        "duplicate_claim",
        "exposure_more_than_twice_limit",
        "duplicate_payment_attempt",
    } <= labels

    with (output_dir / "claims.csv").open(
        "r", encoding="utf-8", newline=""
    ) as stream:
        claim_columns = next(csv.reader(stream))
    assert "ground_truth" not in claim_columns
    assert "anomaly_labels" not in claim_columns


def test_same_seed_and_scale_produce_identical_files(tmp_path: Path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _generate(left)
    _generate(right)

    left_files = {path.name: path.read_bytes() for path in left.iterdir()}
    right_files = {path.name: path.read_bytes() for path in right.iterdir()}
    assert left_files == right_files


def test_sql_seed_imports_with_foreign_keys_enabled(tmp_path: Path):
    output_dir = tmp_path / "generated"
    _generate(output_dir)
    restored = sqlite3.connect(":memory:")
    try:
        restored.execute("PRAGMA foreign_keys = ON")
        restored.executescript(
            (output_dir / "seed.sql").read_text(encoding="utf-8")
        )
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
        assert restored.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 160
        assert restored.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 100
    finally:
        restored.close()


def test_assessment_is_idempotent_and_relations_are_enforced(tmp_path: Path):
    output_dir = tmp_path / "generated"
    _generate(output_dir)
    connection = connect(output_dir / "claims.sqlite")
    try:
        claim_id = "CLM-2026-000001"
        event_count = connection.execute(
            "SELECT COUNT(*) FROM claim_events WHERE claim_id = ?", (claim_id,)
        ).fetchone()[0]
        result = connection.execute(
            "SELECT lifecycle_state FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        from claimflow.rules import process_claim

        processed = process_claim(connection, claim_id)
        assert processed["lifecycle_state"] == result["lifecycle_state"]
        assert connection.execute(
            "SELECT COUNT(*) FROM claim_events WHERE claim_id = ?", (claim_id,)
        ).fetchone()[0] == event_count
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()
