# Data Dictionary

SQLite stores monetary values as integer USD cents; exported timestamps are ISO
8601 UTC strings and dates are ISO `YYYY-MM-DD`. Primary keys are immutable
deterministic identifiers. `NOT NULL`, `CHECK`, `UNIQUE`, and foreign-key
constraints are declared in `claimflow/schema.sql`.

## Core entities

| Entity / table | Primary key | Important fields and meaning |
|---|---|---|
| Customer / `customers` | `customer_id` | `full_name`, unique `email`, `phone`, `created_at` |
| Policy / `policies` | `policy_id` | `customer_id` FK; product enum `AUTO`, `HOME`, `HEALTH`, `LIABILITY`; inclusive `effective_from`/`effective_to`; lifecycle `ACTIVE`, `EXPIRED`, `CANCELLED` |
| Coverage / `coverages` | `coverage_id` | Unique `code` and `claim_type`; `product_type`; `required_documents_json` |
| Claim / `claims` | `claim_id` | Customer, policy, incident, and claim-type FKs; `reported_at`, `currency`, item-derived `claimed_amount_cents`, lifecycle state |
| ClaimItem / `claim_items` | `claim_item_id` | `claim_id`, `coverage_id`, `quantity`, `unit_amount_cents`, reconciled `line_amount_cents` |
| Document / `documents` | `document_id` | `claim_id`, document type, received time, file name, MIME type, size, SHA-256 metadata; document content is not stored |
| Incident / `incidents` | `incident_id` | Customer, `occurred_on`, incident/claim type, type-validated `cause_code`, region, description |
| Adjuster / `adjusters` | `adjuster_id` | Name, region, active flag |
| Assessment / `assessments` | `assessment_id` | Claim FK and per-claim `assessment_sequence`; eligibility `ELIGIBLE`, `INELIGIBLE`, `PENDING`; assessed payable amount and JSON rule rationale |
| Reserve / `reserves` | `reserve_id` | Claim FK and per-claim `reserve_sequence`; amount, calculation timestamp, documented method |
| Settlement / `settlements` | `settlement_id` | Claim FK and per-claim `settlement_sequence`; recommended amount, status, rationale, optional unique payment reference |

## Bridge, evidence, and history tables

| Table | Keys / relationship | Purpose |
|---|---|---|
| `policy_coverages` | `(policy_id, coverage_id)` | Policy-to-coverage M:N bridge with policy-specific effective dates, limit, deductible, and exclusions; exclusions are validated against the coverage's cause rules |
| `claim_coverages` | `(claim_id, coverage_id)` | Claim-to-coverage M:N bridge; composite FKs ensure the selected coverage belongs to the claim's policy |
| `adjuster_assignments` | `assignment_id`; claim and adjuster FKs | Claim-to-adjuster M:N history with assignment status and dates |
| `document_verifications` | `verification_id`; document FK | Appendable `VERIFIED`/`REJECTED` checks with time and reason |
| `policy_events` | `policy_event_id`; policy FK | Policy issuance and lifecycle-state history |
| `claim_events` | `claim_event_id`; claim FK | Append-only claim transition history with actor and reason |
| `settlement_events` | `settlement_event_id`; settlement FK | Recommendation/payment events; unique references and rejected duplicate attempts |

## Enumerations and lifecycle

- Claim types: `AUTO_COLLISION`, `AUTO_COMPREHENSIVE`, `HOME_PROPERTY`,
  `HEALTH_MEDICAL`, `THIRD_PARTY_LIABILITY`.
- Claim states: `SUBMITTED`, `TRIAGE`, `NEEDS_INFO`, `UNDER_REVIEW`,
  `REJECTED`, `APPROVED`, `SETTLEMENT_RECOMMENDED`, `PAID`.
- Assessment eligibility: `ELIGIBLE`, `INELIGIBLE`, `PENDING`.
- Settlement states: `PENDING`, `RECOMMENDED`, `APPROVED`, `PAID`, `DECLINED`.
- Lifecycle transitions are checked by the rules engine and independently
  validated against `claim_events`; see [rules.md](rules.md).

## ID format

- Customers: `CUS-{sequence:06d}`
- Policies: `POL-{product}-2026-{sequence:06d}`
- Claims: `CLM-2026-{sequence:06d}`
- Incidents: `INC-{claim_id}`
- Claim items: `ITM-{claim_id}-{sequence:03d}`
- Documents: `DOC-{claim_id}-{sequence:03d}`
- Adjusters: `ADJ-{sequence:05d}`
- Assignments: `ASN-{claim_id}-001`
- Assessments / reserves / settlements: `ASM-{claim_id}-{sequence:03d}`,
  `RSV-{claim_id}-{sequence:03d}`, `SET-{claim_id}-{sequence:03d}`

## Cardinality and consistency

- Customer `1:N` Policy, Claim, and Incident.
- Policy `1:N` Claim; Policy `M:N` Coverage through `policy_coverages`.
- Claim `N:1` Incident; Claim `1:N` ClaimItem and Document.
- Claim `M:N` Coverage through `claim_coverages`; the bridge is populated only
  when the policy actually carries the claimed coverage.
- Claim `M:N` Adjuster through `adjuster_assignments`.
- Claim `1:N` Assessment, Reserve, and Settlement versions; the latest version
  is the current operational result and earlier versions remain auditable.
- Document `1:N` verification records; Policy and Claim `1:N` lifecycle events;
  Settlement `1:N` settlement/payment events.

The validator also checks that a claim's customer matches both its policyholder
and incident owner, that reported dates follow incident dates, and that bridge
joins agree with claim type and policy terms.
