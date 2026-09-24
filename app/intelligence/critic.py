"""Critic + safety guard: LLM call 4. Scores candidates, picks winner or rejects all.

Two layers:
1. Deterministic scan (app.security.prompt_security) runs FIRST and can veto
   any candidate instantly (banned claims, injection attempts).
2. LLM critic scores the survivors, picks a winner, may polish it.
"""
from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.intelligence.prompts import critic_system, critic_user
from app.intelligence.strategist import product_brief
from app.llm.client import LLMClient, get_llm
from app.models import Product
from app.schemas import CriticReport, ProductIntelligence
from app.security.prompt_security import scan_content

log = logging.getLogger("intelligence.critic")


class CriticResult:
    def __init__(self, report: CriticReport, vetoed: list[int], winner_content: str | None, winner_index: int | None):
        self.report = report
        self.vetoed = vetoed  # indices vetoed by deterministic scan
        self.winner_content = winner_content
        self.winner_index = winner_index

    @property
    def publish(self) -> bool:
        return self.report.publish and self.winner_content is not None


async def critique(
    product: Product,
    intelligence: ProductIntelligence,
    candidates: list[dict],
    trend: dict | None,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> CriticResult:
    s = settings or get_settings()
    llm = llm or get_llm()

    # Layer 1: deterministic veto.
    survivors: list[tuple[int, dict]] = []
    vetoed: list[int] = []
    for i, c in enumerate(candidates):
        violations = scan_content(c.get("content", ""))
        if violations:
            log.warning("candidate %d vetoed by deterministic guard: %s", i, violations)
            vetoed.append(i)
        else:
            survivors.append((i, c))

    if not survivors:
        return CriticResult(
            CriticReport(publish=False, notes="all candidates vetoed by deterministic guard"),
            vetoed, None, None,
        )

    # Layer 2: LLM critic over survivors (re-indexed, mapped back later).
    survivor_list = [c for _, c in survivors]
    report: CriticReport = llm.generate_structured(
        CriticReport,
        critic_system(),
        critic_user(
            product_brief(product, intelligence),
            survivor_list,
            trend,
            s.affiliate_disclosure_enabled,
        ),
        purpose="critic",
        temperature=0.2,
    )

    # Map winner back to original index; pull improved content if provided.
    winner_content = None
    winner_index = None
    if report.publish and report.winner_index is not None:
        if 0 <= report.winner_index < len(survivors):
            orig_idx, cand = survivors[report.winner_index]
            winner_index = orig_idx
            verdict = next(
                (v for v in report.verdicts if v.candidate_index == report.winner_index),
                None,
            )
            if verdict is not None:
                if not verdict.safe:
                    return CriticResult(report, vetoed, None, None)
                if verdict.misinformation_risk > 0.5 or verdict.policy_risk > 0.5:
                    return CriticResult(report, vetoed, None, None)
            winner_content = (verdict.improved_content if verdict and verdict.improved_content else cand["content"])
            # Final deterministic pass on the (possibly improved) content.
            if scan_content(winner_content):
                log.warning("improved content failed deterministic guard; using original")
                winner_content = cand["content"]
            if scan_content(winner_content):
                winner_content = None

    return CriticResult(report, vetoed, winner_content, winner_index)
