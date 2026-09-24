"""Telegram authorization + command surface (bot logic without network)."""
import pytest

from app.telegram.bot import TelegramBot


class FakeUser:
    def __init__(self, id):
        self.id = id


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return self


class FakeUpdate:
    def __init__(self, user_id, text="hello"):
        self.effective_user = FakeUser(user_id)
        self.message = FakeMessage(text)


def make_bot(settings):
    from app.pipeline import Pipeline

    return TelegramBot(Pipeline(settings), settings)


class TestAuthorization:
    def test_authorized_user_passes(self, settings):
        bot = make_bot(settings)
        assert bot._authorized(FakeUpdate(12345))

    def test_other_user_blocked(self, settings):
        bot = make_bot(settings)
        assert not bot._authorized(FakeUpdate(99999))

    @pytest.mark.asyncio
    async def test_stranger_link_ignored(self, settings):
        bot = make_bot(settings)
        update = FakeUpdate(99999, text="https://vt.tiktok.com/STEAL/")
        await bot.on_message(update, None)
        assert update.message.replies == []  # no response at all

    @pytest.mark.asyncio
    async def test_authorized_link_accepted(self, settings):
        bot = make_bot(settings)
        update = FakeUpdate(12345, text="https://vt.tiktok.com/OK123/")
        await bot.on_message(update, None)
        assert any("Product detected" in r for r in update.message.replies)

    @pytest.mark.asyncio
    async def test_disallowed_link_rejected(self, settings):
        bot = make_bot(settings)
        update = FakeUpdate(12345, text="https://evil.example.com/product")
        await bot.on_message(update, None)
        assert any("not a supported shop link" in r.lower() for r in update.message.replies)

    @pytest.mark.asyncio
    async def test_duplicate_link_noted(self, settings):
        bot = make_bot(settings)
        u1 = FakeUpdate(12345, text="https://vt.tiktok.com/DUP9/")
        await bot.on_message(u1, None)
        u2 = FakeUpdate(12345, text="https://vt.tiktok.com/DUP9/")
        await bot.on_message(u2, None)
        assert any("Already in the pipeline" in r for r in u2.message.replies)

    @pytest.mark.asyncio
    async def test_instant_requires_url(self, settings):
        bot = make_bot(settings)
        update = FakeUpdate(12345, text="/instant")
        await bot.on_message(update, None)
        assert any("Usage" in r for r in update.message.replies)

    @pytest.mark.asyncio
    async def test_help_lists_commands(self, settings):
        bot = make_bot(settings)
        update = FakeUpdate(12345)
        await bot.cmd_help(update, None)
        text = update.message.replies[0]
        for cmd in ["/status", "/queue", "/stats", "/last", "/auto", "/pause", "/settings", "/instant"]:
            assert cmd in text

    @pytest.mark.asyncio
    async def test_status_reports_config(self, settings):
        bot = make_bot(settings)
        update = FakeUpdate(12345)
        await bot.cmd_status(update, None)
        text = update.message.replies[0]
        assert "DRY RUN" in text
        assert "LLM:" in text
