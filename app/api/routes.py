"""Lightweight FastAPI admin/debug surface (localhost only).

Not a pretty dashboard — functional visibility:
  GET /health            -> service + config status
  GET /queue             -> products by state
  GET /trends            -> active trends
  GET /opportunities     -> recent opportunities
  GET /posts             -> published posts
  GET /analytics         -> metric snapshots
  GET /events            -> recent system events
  GET /llm-usage         -> LLM cost/usage
  GET /stats             -> 7-day business stats
  GET /                  -> minimal HTML status page

Auth: if ADMIN_TOKEN is set, require `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.database import get_session
from app.learning.memory import StrategyMemoryService
from app.models import (
    LlmUsage,
    Opportunity,
    PostMetricSnapshot,
    Product,
    PublishedPost,
    SystemEvent,
    Trend,
)
from app.utils.timeutil import now_naive, now_utc

app = FastAPI(title="tiktok-threads-engine admin", version="0.1.0")


def _auth(x_admin_token: str | None = Header(default=None), s: Settings = Depends(get_settings)):
    if not s.admin_token:
        return  # no token configured -> open (localhost only anyway)
    if x_admin_token != s.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")


def _session():
    return get_session()


@app.get("/health")
def health(s: Settings = Depends(get_settings), _=Depends(_auth)):
    return {
        "ok": True,
        "time_utc": now_utc().isoformat(),
        "config": s.is_configured(),
        "mode": {
            "dry_run": s.dry_run,
            "auto_publish": s.auto_publish,
            "llm_provider": s.llm_provider,
            "llm_model": s.llm_model,
        },
    }


@app.get("/queue")
def queue(limit: int = 50, _=Depends(_auth)):
    with _session() as db:
        rows = db.scalars(select(Product).order_by(Product.submitted_at.desc()).limit(limit)).all()
        return [
            {
                "id": p.id,
                "title": p.title,
                "state": p.state,
                "mode": p.mode,
                "price": p.price,
                "confidence": p.confidence,
                "reason": p.state_reason,
                "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            }
            for p in rows
        ]


@app.get("/queue/counts")
def queue_counts(_=Depends(_auth)):
    with _session() as db:
        rows = db.execute(select(Product.state, func.count(Product.id)).group_by(Product.state)).all()
        return {state: count for state, count in rows}


@app.get("/trends")
def trends(limit: int = 25, _=Depends(_auth)):
    with _session() as db:
        rows = db.scalars(
            select(Trend).where(Trend.active.is_(True)).order_by(Trend.velocity.desc()).limit(limit)
        ).all()
        return [
            {
                "id": t.id,
                "topic": t.topic,
                "velocity": t.velocity,
                "volume": t.discussion_volume,
                "sources": t.sources,
                "sensitivity": t.sensitivity,
                "confidence": t.confidence,
                "keywords": t.keywords,
                "last_seen": t.last_seen.isoformat() if t.last_seen else None,
            }
            for t in rows
        ]


@app.get("/opportunities")
def opportunities(limit: int = 25, _=Depends(_auth)):
    with _session() as db:
        rows = db.scalars(select(Opportunity).order_by(Opportunity.id.desc()).limit(limit)).all()
        return [
            {
                "id": o.id,
                "product_id": o.product_id,
                "trend_id": o.trend_id,
                "kind": o.kind,
                "score": o.score,
                "status": o.status,
                "breakdown": o.breakdown,
            }
            for o in rows
        ]


@app.get("/posts")
def posts(limit: int = 25, _=Depends(_auth)):
    with _session() as db:
        rows = db.scalars(select(PublishedPost).order_by(PublishedPost.id.desc()).limit(limit)).all()
        return [
            {
                "id": p.id,
                "product_id": p.product_id,
                "threads_post_id": p.threads_post_id,
                "status": p.status,
                "dry_run": p.dry_run,
                "strategy": p.strategy,
                "publish_time": p.publish_time.isoformat() if p.publish_time else None,
                "content": p.content[:200],
                "permalink": p.threads_permalink,
            }
            for p in rows
        ]


@app.get("/analytics")
def analytics(limit: int = 50, _=Depends(_auth)):
    with _session() as db:
        rows = db.scalars(
            select(PostMetricSnapshot).order_by(PostMetricSnapshot.captured_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": s.id,
                "post_id": s.post_id,
                "checkpoint": s.checkpoint,
                "views": s.views,
                "likes": s.likes,
                "replies": s.replies,
                "reposts": s.reposts,
                "shares": s.shares,
                "external_actions": s.external_actions,
                "captured_at": s.captured_at.isoformat() if s.captured_at else None,
            }
            for s in rows
        ]


@app.get("/events")
def events(limit: int = 50, _=Depends(_auth)):
    with _session() as db:
        rows = db.scalars(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(limit)).all()
        return [
            {
                "id": e.id,
                "level": e.level,
                "category": e.category,
                "message": e.message,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]


@app.get("/llm-usage")
def llm_usage(days: int = 7, _=Depends(_auth)):
    cutoff = now_naive() - timedelta(days=days)
    with _session() as db:
        rows = db.scalars(select(LlmUsage).where(LlmUsage.created_at >= cutoff)).all()
        total_in = sum(r.input_tokens for r in rows)
        total_out = sum(r.output_tokens for r in rows)
        total_cost = sum(r.estimated_cost for r in rows)
        by_purpose: dict[str, int] = {}
        for r in rows:
            by_purpose[r.purpose] = by_purpose.get(r.purpose, 0) + 1
        return {
            "days": days,
            "calls": len(rows),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "estimated_cost_usd": round(total_cost, 4),
            "by_purpose": by_purpose,
        }


@app.get("/stats")
def stats(days: int = 7, _=Depends(_auth)):
    return StrategyMemoryService(get_settings()).stats_summary(days=days)


@app.get("/", response_class=HTMLResponse)
def index(_=Depends(_auth)):
    with _session() as db:
        counts = dict(db.execute(select(Product.state, func.count(Product.id)).group_by(Product.state)).all())
        n_trends = db.scalar(select(func.count(Trend.id)).where(Trend.active.is_(True))) or 0
        n_posts = db.scalar(select(func.count(PublishedPost.id))) or 0
    s = get_settings()
    mode = "DRY RUN" if s.dry_run else ("AUTO" if s.auto_publish else "HOLD")
    rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in sorted(counts.items())
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>engine status</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;padding:24px}}
h1{{font-size:20px}} table{{border-collapse:collapse;margin-top:12px}}
td,th{{border:1px solid #2a2f3a;padding:6px 12px;text-align:left}}
.bad{{color:#f0b429}} .ok{{color:#4ade80}}</style></head><body>
<h1>tiktok-threads-engine</h1>
<p>Mode: <span class='bad'>{mode}</span> · LLM: {html.escape(s.llm_model)} · Trends active: {n_trends} · Posts: {n_posts}</p>
<h3>Queue by state</h3>
<table><tr><th>state</th><th>count</th></tr>{rows or '<tr><td colspan=2>empty</td></tr>'}</table>
<p style='margin-top:16px;color:#8b93a7'>Endpoints: /queue /trends /opportunities /posts /analytics /events /llm-usage /stats /health</p>
</body></html>"""
