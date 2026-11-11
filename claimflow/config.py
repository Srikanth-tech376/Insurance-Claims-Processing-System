from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class GenerationConfig:
    seed: int = 20261022
    policy_count: int = 2_000
    claim_count: int = 1_000
    adjuster_count: int = 20
    as_of_date: date = date(2026, 10, 22)
    output_dir: Path = Path("data/generated")

    def __post_init__(self) -> None:
        if self.policy_count < 1:
            raise ValueError("policy_count must be at least 1")
        if self.claim_count < 1:
            raise ValueError("claim_count must be at least 1")
        if self.adjuster_count < 1:
            raise ValueError("adjuster_count must be at least 1")
