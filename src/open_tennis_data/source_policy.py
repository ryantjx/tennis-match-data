"""Versioned source-policy registry for public research releases."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourcePolicy:
    source: str
    policy_state: str
    terms_url: str
    allowed_uses: tuple[str, ...]
    allowed_fields: tuple[str, ...]
    attribution: str
    rate_limit: str
    parser_version: str
    reviewed_at: str
    policy_revision: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourcePolicy:
        return cls(
            source=str(value["source"]),
            policy_state=str(value["policy_state"]),
            terms_url=str(value["terms_url"]),
            allowed_uses=tuple(map(str, value["allowed_uses"])),
            allowed_fields=tuple(map(str, value["allowed_fields"])),
            attribution=str(value["attribution"]),
            rate_limit=str(value["rate_limit"]),
            parser_version=str(value["parser_version"]),
            reviewed_at=str(value["reviewed_at"]),
            policy_revision=str(value["policy_revision"]),
        )


class SourcePolicyRegistry:
    SOURCE_ALIASES = {
        "70s": "sackmann",
        "80s": "sackmann",
        "90s": "sackmann",
        "00s": "sackmann",
        "10s": "sackmann",
        "20s": "sackmann",
        "current": "sackmann",
        "futures": "sackmann",
        "players": "sackmann",
        "qual_chall": "sackmann",
        "qual_itf": "sackmann",
        "tour": "sackmann",
    }

    def __init__(self, policies: dict[str, SourcePolicy]) -> None:
        self.policies = policies

    @classmethod
    def load(cls, path: Path | None = None) -> SourcePolicyRegistry:
        target = path or Path(__file__).with_name("sources.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        policies = {
            item["source"]: SourcePolicy.from_dict(item)
            for item in payload["sources"]
        }
        return cls(policies)

    def require_publishable(self, sources: set[str]) -> None:
        resolved = {
            source: self.SOURCE_ALIASES.get(source, source) for source in sources
        }
        missing = {
            source for source, policy_source in resolved.items()
            if policy_source not in self.policies
        }
        if missing:
            raise ValueError(f"sources lack policy entries: {sorted(missing)}")
        blocked = sorted(
            source
            for source, policy_source in resolved.items()
            if self.policies[policy_source].policy_state
            not in {"public_research", "approved"}
            or "public_research_release"
            not in self.policies[policy_source].allowed_uses
        )
        if blocked:
            raise ValueError(f"sources are not publishable in the research release: {blocked}")

    def policy_rows(self) -> list[tuple[Any, ...]]:
        """Return canonical and legacy-label rows suitable for a DuckDB join."""
        labels = {source: source for source in self.policies}
        labels.update(self.SOURCE_ALIASES)
        rows: list[tuple[Any, ...]] = []
        for source_label, policy_source in sorted(labels.items()):
            policy = self.policies[policy_source]
            rows.append(
                (
                    source_label,
                    policy.source,
                    policy.policy_state,
                    policy.terms_url,
                    list(policy.allowed_uses),
                    list(policy.allowed_fields),
                    policy.attribution,
                    policy.rate_limit,
                    policy.parser_version,
                    policy.reviewed_at,
                    policy.policy_revision,
                )
            )
        return rows

    @property
    def revisions(self) -> list[str]:
        return sorted({policy.policy_revision for policy in self.policies.values()})
