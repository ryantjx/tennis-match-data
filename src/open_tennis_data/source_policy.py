"""Versioned source-policy registry for public research releases."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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
    grant_id: str | None = None
    grant_sha256: str | None = None
    grant_expires_at: str | None = None
    network_collection_enabled: bool = False
    allowed_domains: tuple[str, ...] = ()
    allowed_routes: tuple[str, ...] = ()
    retention_mode: str = "metadata_only"
    redistribution_mode: str = "normalized_facts"
    minimum_request_interval: float = 0.0
    maximum_requests_per_day: int = 0

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
            grant_id=str(value["grant_id"]) if value.get("grant_id") else None,
            grant_sha256=(
                str(value["grant_sha256"]) if value.get("grant_sha256") else None
            ),
            grant_expires_at=(
                str(value["grant_expires_at"])
                if value.get("grant_expires_at")
                else None
            ),
            network_collection_enabled=bool(
                value.get("network_collection_enabled", False)
            ),
            allowed_domains=tuple(map(str, value.get("allowed_domains", ()))),
            allowed_routes=tuple(map(str, value.get("allowed_routes", ()))),
            retention_mode=str(value.get("retention_mode", "metadata_only")),
            redistribution_mode=str(
                value.get("redistribution_mode", "normalized_facts")
            ),
            minimum_request_interval=float(
                value.get("minimum_request_interval", 0)
            ),
            maximum_requests_per_day=int(value.get("maximum_requests_per_day", 0)),
        )


@dataclass(frozen=True)
class PolicyDecision:
    source: str
    allowed: bool
    reason: str
    policy_revision: str
    public_projection_allowed: bool


class SourcePolicyRegistry:
    SOURCE_ALIASES = {
        "players": "sackmann",
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

    def evaluate_network_request(
        self,
        source: str,
        url: str,
        *,
        route: str,
        fields: set[str] | None = None,
        use: str = "private_evaluation",
        at: datetime | None = None,
    ) -> PolicyDecision:
        """Evaluate a network request before a browser or HTTP client is created."""
        policy = self.policies.get(source)
        if policy is None:
            return PolicyDecision(source, False, "unknown_source", "unknown", False)
        revision = policy.policy_revision
        if policy.policy_state not in {"approved", "public_research", "private_evaluation"}:
            return PolicyDecision(source, False, "policy_blocked", revision, False)
        if not policy.network_collection_enabled:
            return PolicyDecision(source, False, "network_collection_disabled", revision, False)
        if not policy.grant_id or not policy.grant_sha256:
            return PolicyDecision(source, False, "missing_grant", revision, False)
        if not re.fullmatch(r"[0-9a-f]{64}", policy.grant_sha256):
            return PolicyDecision(source, False, "invalid_grant_digest", revision, False)
        instant = (at or datetime.now(UTC)).astimezone(UTC)
        if policy.grant_expires_at:
            try:
                expiry = date.fromisoformat(policy.grant_expires_at)
            except ValueError:
                return PolicyDecision(source, False, "invalid_grant_expiry", revision, False)
            if instant.date() > expiry:
                return PolicyDecision(source, False, "expired_grant", revision, False)
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        allowed_hosts = {item.casefold() for item in policy.allowed_domains}
        if not host or host not in allowed_hosts:
            return PolicyDecision(source, False, "domain_not_allowlisted", revision, False)
        if route not in policy.allowed_routes:
            return PolicyDecision(source, False, "route_not_allowlisted", revision, False)
        requested_fields = fields or set()
        if not requested_fields.issubset(set(policy.allowed_fields)):
            return PolicyDecision(source, False, "fields_not_allowlisted", revision, False)
        if use not in policy.allowed_uses:
            return PolicyDecision(source, False, "use_not_allowlisted", revision, False)
        if policy.maximum_requests_per_day < 1:
            return PolicyDecision(source, False, "request_limit_disabled", revision, False)
        public = (
            "public_research_release" in policy.allowed_uses
            and policy.redistribution_mode in {"normalized_facts", "public"}
        )
        return PolicyDecision(source, True, "allowed", revision, public)

    def require_network_request(
        self,
        source: str,
        url: str,
        *,
        route: str,
        fields: set[str] | None = None,
        use: str = "private_evaluation",
        at: datetime | None = None,
    ) -> PolicyDecision:
        decision = self.evaluate_network_request(
            source, url, route=route, fields=fields, use=use, at=at
        )
        if not decision.allowed:
            raise ValueError(
                f"{source} network request rejected by policy: {decision.reason}"
            )
        return decision

    def policy_rows(self) -> list[tuple[Any, ...]]:
        """Return canonical and ingestion-label rows suitable for a DuckDB join."""
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
