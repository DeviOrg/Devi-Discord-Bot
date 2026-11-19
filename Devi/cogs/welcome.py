import asyncio
import logging
from typing import Optional

import disnake
from disnake.ext import commands

import i18n
from paths import env_var

logger = logging.getLogger(__name__)

SITE_URL = env_var("SITE_URL").strip().rstrip("/")

AUDIT_LOG_ATTEMPTS = 3
AUDIT_LOG_DELAY = 1.5  # seconds


def thanks_url(guild_id: int) -> str:
    return f"{SITE_URL}/thanks?guild_id={guild_id}"

def pick_locale(guild: disnake.Guild) -> disnake.Locale | str:
    locale = guild.preferred_locale
    return locale if i18n.is_valid_locale(locale) else i18n.DEFAULT_LOCALE


def pick_channel(guild: disnake.Guild) -> Optional[disnake.TextChannel]:
    me = guild.me
    if not me:
        return None

    def can_write(ch: disnake.TextChannel) -> bool:
        perms = ch.permissions_for(me)
        return perms.view_channel and perms.send_messages

    channels = sorted(guild.text_channels, key=lambda item: item.position)
    system = guild.system_channel

    if not system and can_write(system):
        return system

    everyone = guild.default_role
    for channel in channels:
        if can_write(channel) and channel.permissions_for(everyone).view_channel:
            return channel

    return next((channel for channel in channels if can_write(channel)), None)


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: disnake.Guild):
        if not SITE_URL:
            return

        try:
            await self._welcome(guild)
        except Exception:
            logger.exception("Welcome messages for guild %s failed.", guild.id)

    async def _welcome(self, guild: disnake.Guild):
        locale = pick_locale(guild)
        guild_name = disnake.utils.escape_markdown(disnake.utils.escape_mentions(guild.name))
        button = disnake.ui.Button(
            style=disnake.ButtonStyle.link,
            label=i18n.t("welcome_cog.open_dashboard", locale=locale),
            url=thanks_url(guild.id),
        )
        no_mentions = disnake.AllowedMentions.none()

        channel = pick_channel(guild)
        if channel:
            try:
                await channel.send(
                    i18n.t("welcome_cog.channel", locale=locale, guild_name=guild_name),
                    components=[button],
                    allowed_mentions=no_mentions,
                )
            except (disnake.Forbidden, disnake.HTTPException) as error:
                logger.info("Cannot send the welcome message to guild %s: %s", guild.id, error)

        inviter = await self._find_inviter(guild)
        if not inviter:
            return

        try:
            await inviter.send(
                i18n.t("welcome_cog.dm", locale=locale, guild_name=guild_name),
                components=[button],
                allowed_mentions=no_mentions,
            )
        except (disnake.Forbidden, disnake.HTTPException) as error:
            logger.info("Cannot DM %s about guild %s: %s", inviter.id, guild.id, error)

    async def _find_inviter(self, guild: disnake.Guild) -> Optional[disnake.abc.User]:
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return None

        for attempt in range(AUDIT_LOG_ATTEMPTS):
            try:
                async for entry in guild.audit_logs(limit=10, action=disnake.AuditLogAction.bot_add):
                    target_id = getattr(entry.target, "id", None)
                    if target_id == self.bot.user.id and entry.user and not entry.user.bot:
                        return entry.user
            except (disnake.Forbidden, disnake.HTTPException):
                return None

            if attempt < AUDIT_LOG_ATTEMPTS - 1:
                await asyncio.sleep(AUDIT_LOG_DELAY)

        return None


def setup(bot: commands.Bot):
    if not SITE_URL:
        logger.warning("SITE_URL is not set: the bot will not greet servers it is added to.")
    bot.add_cog(WelcomeCog(bot))