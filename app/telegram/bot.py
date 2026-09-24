"""Telegram bot: the user-facing surface.

- Only TELEGRAM_ALLOWED_USER_ID can use the bot. Everyone else is ignored.
- Normal usage: paste an affiliate link -> smart auto queue.
- /instant <link> -> publish as soon as analysis+content is done.
- Commands: /start /help /status /queue /stats /last /auto /pause /settings
- Notifications: short human summaries only (technical detail stays in logs).
"""
from __future__ import annotations

import html
import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import Settings, get_settings
from app.pipeline import Pipeline
from app.product.service import ProductService
from app.security.url_validation import URLValidationError, is_allowed_url
from app.utils.timeutil import my_now

log = logging.getLogger("telegram")

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


class TelegramBot:
    def __init__(self, pipeline: Pipeline, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.pipeline = pipeline
        self.products = ProductService(self.s)
        self._app: Application | None = None

    # ---------- auth ----------

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        return user.id == self.s.telegram_allowed_user_id

    # ---------- notifier (used by pipeline) ----------

    async def notify(self, level: str, text: str) -> None:
        """Send a notification if verbosity allows."""
        if not self.s.telegram_allowed_user_id or self._app is None:
            return
        if self.s.notify_level == "minimal" and level == "info":
            return
        bot = self._app.bot
        try:
            await bot.send_message(
                chat_id=self.s.telegram_allowed_user_id,
                text=text[:4000],
            )
        except Exception as e:
            log.warning("telegram notify failed: %s", e)

    # ---------- command handlers ----------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "👋 TikTok → Threads engine online.\n\n"
            "Just paste a TikTok affiliate link and I'll handle the rest.\n"
            "/help for commands."
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "How to use:\n"
            "• Paste an affiliate link → Smart Auto queue (waits for the right moment)\n"
            "• /instant <link> → publish as soon as content is ready\n\n"
            "Commands:\n"
            "/status — system health\n"
            "/queue — products in the pipeline\n"
            "/stats — 7-day performance\n"
            "/last — last published post\n"
            "/auto — toggle auto-publish\n"
            "/pause — pause/resume processing\n"
            "/settings — current configuration\n"
        )

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        from app.learning.memory import StrategyMemoryService

        cfg = self.s.is_configured()
        lines = ["📡 System status", ""]
        lines.append(f"LLM: {'✅ ' + self.s.llm_model if cfg['llm'] else '❌ not configured'}")
        lines.append(f"Threads: {'✅ connected' if cfg['threads'] else '❌ not connected'}")
        lines.append(f"TikTok API: {'✅' if cfg['tiktok_api'] else '⬜ not connected (ok)'}")
        lines.append(f"Mode: {'DRY RUN' if self.s.dry_run else ('AUTO PUBLISH' if self.s.auto_publish else 'HOLD (no auto publish)')}")
        lines.append(f"Time (MYT): {my_now().strftime('%a %d %b %H:%M')}")
        with self.products.session_factory() as db:
            from sqlalchemy import func, select
            from app.models import Product

            counts = dict(
                db.execute(
                    select(Product.state, func.count(Product.id)).group_by(Product.state)
                ).all()
            )
        if counts:
            lines.append("")
            lines.append("Queue: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        await update.message.reply_text("\n".join(lines))

    async def cmd_queue(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        with self.products.session_factory() as db:
            from sqlalchemy import select
            from app.models import Product

            rows = db.scalars(
                select(Product).where(Product.state.notin_(["COMPLETED"])).order_by(Product.submitted_at.desc()).limit(15)
            ).all()
            if not rows:
                await update.message.reply_text("Queue is empty. Send me an affiliate link!")
                return
            lines = ["📋 Pipeline:"]
            for p in rows:
                title = (p.title or p.original_url[:40])[:45]
                lines.append(f"• {title}\n  {p.state} — {p.state_reason[:60]}")
            await update.message.reply_text("\n".join(lines)[:4000])

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        from app.analytics.conversion_provider import ConversionService
        from app.learning.memory import StrategyMemoryService

        stats = StrategyMemoryService(self.s).stats_summary(days=7)
        conv = ConversionService(self.s)
        lines = [f"📊 Last {stats['days']} days", ""]
        lines.append(f"Products submitted: {stats['products_submitted']}")
        lines.append(f"Posts published: {stats['posts_published']}")
        lines.append(f"Total views: {stats['total_views']:,}")
        lines.append(f"Average engagement: {stats['avg_engagement']:.1%}")
        if stats["best_category"]:
            lines.append(f"Best category: {stats['best_category'][0]} ({stats['best_category'][1]:.1%})")
        if stats["best_angle"]:
            lines.append(f"Best strategy: {stats['best_angle'][0]} ({stats['best_angle'][1]:.1%})")
        if stats["best_window"]:
            lines.append(f"Best posting window: {stats['best_window'][0]}")
        lines.append("")
        lines.append(f"Affiliate orders: {conv.status_summary()}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_last(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        with self.products.session_factory() as db:
            from sqlalchemy import select
            from app.models import PublishedPost

            post = db.scalar(
                select(PublishedPost).order_by(PublishedPost.id.desc()).limit(1)
            )
            if post is None:
                await update.message.reply_text("Nothing published yet.")
                return
            lines = [
                "🕘 Last post",
                "",
                post.content[:800],
                "",
                f"Status: {post.status}" + (" (dry run)" if post.dry_run else ""),
            ]
            if post.threads_permalink:
                lines.append(post.threads_permalink)
            await update.message.reply_text("\n".join(lines))

    async def cmd_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.s.auto_publish = not self.s.auto_publish
        await update.message.reply_text(
            f"AUTO_PUBLISH is now {'ON' if self.s.auto_publish else 'OFF'}.\n"
            f"(DRY_RUN={'ON' if self.s.dry_run else 'OFF'})\n"
            "Note: this is in-memory for this run; set it in .env to persist."
        )

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        # Toggle a runtime pause flag on the pipeline (simpler than editing .env)
        self.pipeline.paused = not getattr(self.pipeline, "paused", False)
        state = "PAUSED" if self.pipeline.paused else "RESUMED"
        await update.message.reply_text(f"⏸️ Processing {state}.")

    async def cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        s = self.s
        await update.message.reply_text(
            "⚙️ Settings\n"
            f"• dry_run: {s.dry_run}\n"
            f"• auto_publish: {s.auto_publish}\n"
            f"• opportunity_threshold: {s.opportunity_threshold}\n"
            f"• smart_max_wait_hours: {s.smart_max_wait_hours}\n"
            f"• max_posts_per_day: {s.max_posts_per_day}\n"
            f"• min_hours_between_posts: {s.min_hours_between_posts}\n"
            f"• quiet_hours: {s.quiet_hours}\n"
            f"• trend_refresh: {s.trend_refresh_minutes}m\n"
            f"• analytics_checkpoints: {s.analytics_checkpoints}h\n"
            f"• disclosure: {s.affiliate_disclosure_enabled} ({s.affiliate_disclosure_text})\n"
            f"• language: {s.content_language}\n"
            f"• llm: {s.llm_provider}/{s.llm_model}\n"
            f"• notify_level: {s.notify_level}"
        )

    # ---------- link handler (the main UX) ----------

    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        msg = update.message
        if msg is None or msg.text is None:
            return
        text = msg.text.strip()

        # /instant <url>
        if text.startswith("/instant"):
            rest = text[len("/instant"):].strip()
            urls = URL_RE.findall(rest)
            if not urls:
                await msg.reply_text("Usage: /instant <affiliate link>")
                return
            await self._submit(urls[0], mode="instant", msg=msg)
            return

        # Plain link -> smart auto
        urls = URL_RE.findall(text)
        if urls:
            await self._submit(urls[0], mode="smart", msg=msg)
            return

        if text.startswith("/"):
            return  # unknown command; PTB already replied unknown
        # Non-link, non-command: gentle nudge
        await msg.reply_text("Send me a TikTok affiliate link (or /help).")

    async def _submit(self, url: str, mode: str, msg) -> None:
        try:
            product, is_new = self.products.submit(url, mode=mode, chat_id=self.s.telegram_allowed_user_id)
        except URLValidationError as e:
            await msg.reply_text(f"⚠️ {e}")
            return

        if not is_new:
            await msg.reply_text(
                f"ℹ️ Already in the pipeline (state: {product.state}).\n{product.state_reason}"
            )
            return

        await msg.reply_text(
            f"✅ Product detected\n\nLink queued ({mode} mode).\n"
            "I'll update you as it moves through the pipeline."
        )
        # Kick off intake now (don't wait for the 30s tick).
        import asyncio

        asyncio.create_task(self.pipeline.intake(product.id))

    # ---------- application wiring ----------

    def build_application(self) -> Application:
        token = self.s.telegram_bot_token
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("queue", self.cmd_queue))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("last", self.cmd_last))
        app.add_handler(CommandHandler("auto", self.cmd_auto))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CommandHandler("instant", self.on_message))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        self._app = app
        return app

    async def run_polling(self) -> None:
        app = self.build_application()
        await app.initialize()
        await app.start()
        log.info("telegram bot polling started")
        await app.updater.start_polling(drop_pending_updates=True)
        # Keep the app alive
        import asyncio

        try:
            await asyncio.Event().wait()
        finally:
            await app.stop()
            await app.shutdown()
