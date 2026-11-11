# Business Rules and Explainability

All monetary operations use integer cents. Rule reasons and anomaly codes are
stored in the assessment rationale and exported separately as ground truth;
the rule engine computes them from database facts.

| Rule | Evaluation | Decision / trace |
|---|---|---|
| Claimant consistency | Claim customer must match the policyholder and incident owner | Inconsistent claims are rejected; validator also reports mismatched joins |
| Policy term | Incident date must fall within the policy's inclusive effective dates | Outside dates are rejected; expired and cancelled policies receive specific labels |
| Report order | The submitted/report date cannot precede the incident date | Invalid ordering is rejected and labeled `reported_before_incident` |
| Coverage | The policy must carry the selected claim type and its coverage term must include the incident date | Missing policy coverage or out-of-term coverage is rejected |
| Exclusions | Incident `cause_code` is compared with the applicable policy-coverage exclusions | Excluded causes are rejected and labeled `excluded_cause` |
| Claim total | Sum `quantity * unit_amount_cents` over claim items | Reconciled to `claims.claimed_amount_cents`; no independently generated total |
| Deductible and limit | For each policy-covered coverage, payable is `min(max(item total - deductible, 0), limit)` | Settlement recommendation is never greater than the applicable coverage cap |
| Required evidence | Each coverage's required document types must have a verified document | Missing or rejected evidence results in `NEEDS_INFO`; reasons list each document and labels include `missing_mandatory_document` |
| Document metadata | Require a filename, supported PDF/JPEG/PNG MIME type, 1–25,000,000 bytes, and a 64-character hexadecimal SHA-256 | Invalid metadata is persisted as `REJECTED`; it does not count as evidence |
| Duplicate claim | Same policy, incident, claim type, and item-derived amount as an earlier reported claim | Later claim is rejected and references `duplicate_of` |
| High exposure / fraud indicator | Gross claim amount is greater than twice the coverage limit | `UNDER_REVIEW`, no settlement recommendation, `exposure_more_than_twice_limit`; this is a review signal, not a fraud conclusion |
| Adjuster assignment | Select an active adjuster in the incident region with the lowest active workload; if none match, use the lowest-workload active adjuster | Assignment ID and adjuster are persisted; ties break by adjuster ID |
| Reserve | `assessed settlement * 80 // 100` | Persisted method and integer-cent result |
| Settlement | Recommended eligible amount after deductible, capped by coverage limit | Amount, status, and reasons are stored in the settlement record |
| Duplicate payment | First payment attempt changes the settlement/claim to paid; any later attempt is recorded but rejected | Unique payment references, `REJECTED_DUPLICATE`, no second paid amount |
| Lifecycle | Claim state changes must follow the transition table below | Every transition records from/to state, actor, time, and reason; processing is idempotent |

The high-exposure rule is intentionally conservative and explainable. It raises a
manual-review indicator based on the submitted amount relative to the policy
limit; it does not assert that a claimant committed fraud.

## Claim lifecycle transitions

| From | Allowed next state(s) |
|---|---|
| `SUBMITTED` | `TRIAGE` |
| `TRIAGE` | `NEEDS_INFO`, `UNDER_REVIEW`, `REJECTED`, `SETTLEMENT_RECOMMENDED` |
| `NEEDS_INFO` | `UNDER_REVIEW`, `REJECTED` |
| `UNDER_REVIEW` | `NEEDS_INFO`, `REJECTED`, `APPROVED`, `SETTLEMENT_RECOMMENDED` |
| `APPROVED` | `SETTLEMENT_RECOMMENDED` |
| `SETTLEMENT_RECOMMENDED` | `PAID` |
| `REJECTED`, `PAID` | Terminal |

The initial `SUBMITTED` event is recorded by intake. Processing appends triage
and decision events; it does not rewrite prior events. A claim in `NEEDS_INFO`
can receive more document metadata and be reassessed; each assessment, reserve,
and settlement recommendation receives a new per-claim sequence, and the prior
versions remain queryable. Repeating processing without new evidence returns
the persisted current result.

## Synthetic exception cases

The default 1,000-claim dataset has deterministic scenario bands after a valid
baseline: incomplete mandatory documents, incidents outside the policy period,
expired/cancelled policies, amounts above coverage limits, unsupported policy
coverage, duplicate incidents/claims, high exposure, rejected document
metadata, and duplicate payment attempts. The banding is scale-relative and
reproducible; individual labels are **not** assigned by a random label generator.
Ground-truth reasons and expected decisions are derived from the same
source-record rules run against the persisted dataset.

Invalid cases preserve referential integrity: negative conditions are modeled
as claim/evidence/payment facts with valid foreign keys, not broken IDs or
unrelated records silently corrupted to manufacture an anomaly.
