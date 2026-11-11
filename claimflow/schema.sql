PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverages (
    coverage_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    product_type TEXT NOT NULL,
    claim_type TEXT NOT NULL UNIQUE,
    required_documents_json TEXT NOT NULL,
    CHECK (product_type IN ('AUTO', 'HOME', 'HEALTH', 'LIABILITY')),
    CHECK (claim_type IN (
        'AUTO_COLLISION', 'AUTO_COMPREHENSIVE', 'HOME_PROPERTY',
        'HEALTH_MEDICAL', 'THIRD_PARTY_LIABILITY'
    ))
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    product_type TEXT NOT NULL CHECK (
        product_type IN ('AUTO', 'HOME', 'HEALTH', 'LIABILITY')
    ),
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL CHECK (
        lifecycle_state IN ('ACTIVE', 'EXPIRED', 'CANCELLED')
    ),
    created_at TEXT NOT NULL,
    CHECK (effective_from < effective_to)
);

CREATE TABLE IF NOT EXISTS policy_coverages (
    policy_id TEXT NOT NULL REFERENCES policies(policy_id),
    coverage_id TEXT NOT NULL REFERENCES coverages(coverage_id),
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    limit_cents INTEGER NOT NULL CHECK (limit_cents > 0),
    deductible_cents INTEGER NOT NULL CHECK (deductible_cents >= 0),
    exclusions_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (policy_id, coverage_id),
    CHECK (effective_from < effective_to),
    CHECK (deductible_cents < limit_cents)
);

CREATE TABLE IF NOT EXISTS policy_events (
    policy_event_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES policies(policy_id),
    from_state TEXT,
    to_state TEXT NOT NULL CHECK (
        to_state IN ('ISSUED', 'ACTIVE', 'EXPIRED', 'CANCELLED')
    ),
    event_at TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    occurred_on TEXT NOT NULL,
    incident_type TEXT NOT NULL,
    cause_code TEXT NOT NULL,
    region TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    policy_id TEXT NOT NULL REFERENCES policies(policy_id),
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    claim_type TEXT NOT NULL REFERENCES coverages(claim_type),
    reported_at TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    claimed_amount_cents INTEGER NOT NULL DEFAULT 0 CHECK (
        claimed_amount_cents >= 0
    ),
    lifecycle_state TEXT NOT NULL CHECK (
        lifecycle_state IN (
            'SUBMITTED', 'TRIAGE', 'NEEDS_INFO', 'UNDER_REVIEW',
            'REJECTED', 'APPROVED', 'SETTLEMENT_RECOMMENDED', 'PAID'
        )
    ),
    UNIQUE (claim_id, policy_id)
);

CREATE TABLE IF NOT EXISTS claim_items (
    claim_item_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    coverage_id TEXT NOT NULL REFERENCES coverages(coverage_id),
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_amount_cents INTEGER NOT NULL CHECK (unit_amount_cents >= 0),
    line_amount_cents INTEGER NOT NULL CHECK (
        line_amount_cents = quantity * unit_amount_cents
    )
);

CREATE TABLE IF NOT EXISTS claim_coverages (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    policy_id TEXT NOT NULL,
    coverage_id TEXT NOT NULL REFERENCES coverages(coverage_id),
    PRIMARY KEY (claim_id, coverage_id),
    FOREIGN KEY (claim_id, policy_id) REFERENCES claims(claim_id, policy_id),
    FOREIGN KEY (policy_id, coverage_id)
        REFERENCES policy_coverages(policy_id, coverage_id)
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    document_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_verifications (
    verification_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    status TEXT NOT NULL CHECK (status IN ('VERIFIED', 'REJECTED')),
    verified_at TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjusters (
    adjuster_id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    region TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS adjuster_assignments (
    assignment_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    adjuster_id TEXT NOT NULL REFERENCES adjusters(adjuster_id),
    assigned_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETED', 'REASSIGNED')),
    UNIQUE (claim_id, adjuster_id),
    CHECK (ended_at IS NULL OR assigned_at <= ended_at)
);

CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    assessment_sequence INTEGER NOT NULL CHECK (assessment_sequence > 0),
    assessed_at TEXT NOT NULL,
    assessed_amount_cents INTEGER NOT NULL CHECK (assessed_amount_cents >= 0),
    eligibility TEXT NOT NULL CHECK (
        eligibility IN ('ELIGIBLE', 'INELIGIBLE', 'PENDING')
    ),
    rationale_json TEXT NOT NULL,
    UNIQUE (claim_id, assessment_sequence)
);

CREATE TABLE IF NOT EXISTS reserves (
    reserve_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    reserve_sequence INTEGER NOT NULL CHECK (reserve_sequence > 0),
    calculated_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    method TEXT NOT NULL,
    UNIQUE (claim_id, reserve_sequence)
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    settlement_sequence INTEGER NOT NULL CHECK (settlement_sequence > 0),
    recommended_at TEXT NOT NULL,
    recommended_amount_cents INTEGER NOT NULL CHECK (
        recommended_amount_cents >= 0
    ),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RECOMMENDED', 'APPROVED', 'PAID', 'DECLINED')
    ),
    rationale_json TEXT NOT NULL,
    payment_reference TEXT UNIQUE,
    UNIQUE (claim_id, settlement_sequence)
);

CREATE TABLE IF NOT EXISTS settlement_events (
    settlement_event_id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN ('RECOMMENDED', 'APPROVED', 'PAYMENT_ATTEMPT', 'PAID')
    ),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('RECORDED', 'SUCCEEDED', 'REJECTED_DUPLICATE')
    ),
    event_at TEXT NOT NULL,
    payment_reference TEXT UNIQUE,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_events (
    claim_event_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    from_state TEXT,
    to_state TEXT NOT NULL CHECK (
        to_state IN (
            'SUBMITTED', 'TRIAGE', 'NEEDS_INFO', 'UNDER_REVIEW',
            'REJECTED', 'APPROVED', 'SETTLEMENT_RECOMMENDED', 'PAID'
        )
    ),
    event_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policies_customer ON policies(customer_id);
CREATE INDEX IF NOT EXISTS idx_claims_policy ON claims(policy_id);
CREATE INDEX IF NOT EXISTS idx_claims_incident ON claims(incident_id);
CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(claim_type);
CREATE INDEX IF NOT EXISTS idx_claim_items_claim ON claim_items(claim_id);
CREATE INDEX IF NOT EXISTS idx_documents_claim ON documents(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_events_claim ON claim_events(claim_id);
