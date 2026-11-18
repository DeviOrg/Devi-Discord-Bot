import disnake
from disnake.ext import commands, tasks

import i18n
from db import db_cursor
from discord_i18n import localized
from logs import LogColor, send_log
from paths import env_int
from permissions import Permission, require_permissions

# Saved roles older than this are deleted. 0 or less keeps them until the member returns.
RETENTION_DAYS = env_int("STICKY_ROLES_RETENTION_DAYS", 180)

MAX_LISTED_ROLES = 250

class StickyRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._enabled: set[int] = set()
        self._roles: dict[int, set[int]] = {}
        self._load_settings()

        self.cleanup_loop.start()

    def cog_unload(self):
        self.cleanup_loop.cancel()

    # ------------------------------------------------------------------ settings storage

    def _load_settings(self) -> None:
        with db_cursor() as cur:
            cur.execute("SELECT guild_id FROM sticky_roles_config WHERE enabled")
            self._enabled = {row["guild_id"] for row in cur.fetchall()}

            cur.execute("SELECT guild_id, role_id FROM sticky_roles_list")
            rows = cur.fetchall()

        self._roles = {}
        for row in rows:
            self._roles.setdefault(row["guild_id"], set()).add(row["role_id"])

    def is_enabled(self, guild_id: int) -> bool:
        return guild_id in self._enabled

    def get_roles(self, guild_id: int) -> list[int]:
        return sorted(self._roles.get(guild_id, ()))

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """INSERT INTO sticky_roles_config (guild_id, enabled) VALUES (%s, %s)
                   ON CONFLICT (guild_id) DO UPDATE SET enabled = EXCLUDED.enabled""",
                (guild_id, enabled),
            )
            if not enabled:
                cur.execute("DELETE FROM sticky_roles_saved WHERE guild_id = %s", (guild_id,))

        if enabled:
            self._enabled.add(guild_id)
        else:
            self._enabled.discard(guild_id)

    def add_role(self, guild_id: int, role_id: int) -> bool:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """INSERT INTO sticky_roles_list (guild_id, role_id) VALUES (%s, %s)
                   ON CONFLICT DO NOTHING""",
                (guild_id, role_id),
            )
            added = cur.rowcount > 0

        if added:
            self._roles.setdefault(guild_id, set()).add(role_id)
        return added

    def remove_role(self, guild_id: int, role_id: int) -> bool:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM sticky_roles_list WHERE guild_id = %s AND role_id = %s",
                (guild_id, role_id),
            )
            removed = cur.rowcount > 0

        if removed:
            roles = self._roles.get(guild_id)
            if roles:
                roles.discard(role_id)
                if not roles:
                    del self._roles[guild_id]
        return removed

    def configure(self, guild_id: int, enabled: bool, role_ids: list[int]) -> None:
        role_ids = sorted(set(role_ids))

        with db_cursor(commit=True) as cur:
            cur.execute(
                """INSERT INTO sticky_roles_config (guild_id, enabled) VALUES (%s, %s)
                   ON CONFLICT (guild_id) DO UPDATE SET enabled = EXCLUDED.enabled""",
                (guild_id, enabled),
            )
            cur.execute("DELETE FROM sticky_roles_list WHERE guild_id = %s", (guild_id,))
            if role_ids:
                cur.executemany(
                    "INSERT INTO sticky_roles_list (guild_id, role_id) VALUES (%s, %s)",
                    [(guild_id, role_id) for role_id in role_ids],
                )
            if not enabled:
                cur.execute("DELETE FROM sticky_roles_saved WHERE guild_id = %s", (guild_id,))

        if enabled:
            self._enabled.add(guild_id)
        else:
            self._enabled.discard(guild_id)

        if role_ids:
            self._roles[guild_id] = set(role_ids)
        else:
            self._roles.pop(guild_id, None)

    @staticmethod
    def _temp_role_ids(guild_id: int, user_id: int) -> set[int]:
        with db_cursor() as cur:
            cur.execute(
                "SELECT role_id FROM temp_roles WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            return {row["role_id"] for row in cur.fetchall()}

    @staticmethod
    def _save_member(guild_id: int, user_id: int, role_ids: list[int]) -> None:
        with db_cursor(commit=True) as cur:
            if not role_ids:
                cur.execute(
                    "DELETE FROM sticky_roles_saved WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                return

            cur.execute(
                """INSERT INTO sticky_roles_saved (guild_id, user_id, role_ids, saved_at)
                   VALUES (%s, %s, %s::bigint[], now())
                   ON CONFLICT (guild_id, user_id) DO UPDATE SET
                       role_ids = EXCLUDED.role_ids,
                       saved_at = EXCLUDED.saved_at""",
                (guild_id, user_id, role_ids),
            )

    @staticmethod
    def _take_member(guild_id: int, user_id: int) -> list[int]:
        """Returns the saved roles of a member and deletes the record in the same statement."""
        with db_cursor(commit=True) as cur:
            cur.execute(
                """DELETE FROM sticky_roles_saved WHERE guild_id = %s AND user_id = %s
                   RETURNING role_ids""",
                (guild_id, user_id),
            )
            row = cur.fetchone()
        return list(row["role_ids"]) if row else []

    @staticmethod
    def _can_give(guild: disnake.Guild, role: disnake.Role) -> bool:
        me = guild.me
        return bool(
            me
            and me.guild_permissions.manage_roles
            and not role.is_default()
            and not role.managed
            and role < me.top_role
        )

    def _is_listed(self, guild_id: int, role_id: int) -> bool:
        listed = self._roles.get(guild_id)
        return not listed or role_id in listed

    # ------------------------------------------------------------------ events

    @commands.Cog.listener()
    async def on_member_remove(self, member: disnake.Member):
        guild = member.guild

        if member.bot or not self.is_enabled(guild.id):
            return

        temp_role_ids = self._temp_role_ids(guild.id, member.id)

        role_ids = [role.id for role in member.roles
            if not role.is_default()
            and not role.managed
            and role.id not in temp_role_ids
            and self._is_listed(guild.id, role.id)
        ]

        self._save_member(guild.id, member.id, role_ids)

    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member):
        guild = member.guild
        gid = guild.id

        if member.bot or not self.is_enabled(gid):
            return None

        me = guild.me
        if not me or not me.guild_permissions.manage_roles:
            return await send_log(
                gid,
                i18n.t("sticky_roles_cog.logs.failed_title", locale=gid),
                color=LogColor.Member,
                fields=[
                    (i18n.t("logs_cog.fields.member", locale=gid), f"{member.mention} ({member.id})"),
                    (i18n.t("logs_cog.fields.reason", locale=gid), i18n.t("sticky_roles_cog.logs.no_manage_roles", locale=gid)),
                ],
            )

        saved_ids = self._take_member(gid, member.id)

        if not saved_ids:
            return None

        to_give: list[disnake.Role] = []

        for role_id in saved_ids:
            role = guild.get_role(role_id)

            if not role or role in member.roles or not self._is_listed(gid, role_id) or not self._can_give(guild, role):
                continue

            to_give.append(role)

        if not to_give:
            return None

        try:
            await member.add_roles(
                *to_give,
                reason=i18n.t("sticky_roles_cog.audit_reason", locale=gid, custom_emojis=False),
                atomic=False,
            )
        except (disnake.Forbidden, disnake.HTTPException) as error:
            return await send_log(
                gid,
                i18n.t("sticky_roles_cog.logs.failed_title", locale=gid),
                color=LogColor.Member,
                fields=[
                    (i18n.t("logs_cog.fields.member", locale=gid), f"{member.mention} ({member.id})"),
                    (i18n.t("logs_cog.fields.reason", locale=gid), str(error)[:1000]),
                ],
            )

        return await send_log(
            gid,
            i18n.t("sticky_roles_cog.logs.restored_title", locale=gid),
            color=LogColor.Member,
            fields=[
                (i18n.t("logs_cog.fields.member", locale=gid), f"{member.mention} ({member.id})"),
                (i18n.t("logs_cog.fields.role", locale=gid), " ".join(role.mention for role in to_give)),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: disnake.Role):
        if role.id in self._roles.get(role.guild.id, ()):
            self.remove_role(role.guild.id, role.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: disnake.Guild):
        with db_cursor(commit=True) as cur:
            cur.execute("DELETE FROM sticky_roles_saved WHERE guild_id = %s", (guild.id,))

    @tasks.loop(hours=24)
    async def cleanup_loop(self):
        if RETENTION_DAYS <= 0:
            return

        with db_cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM sticky_roles_saved WHERE saved_at < now() - make_interval(days => %s)",
                (RETENTION_DAYS,),
            )

    @cleanup_loop.before_loop
    async def before_cleanup_loop(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ commands

    @commands.slash_command(
        name="sticky_roles",
        description=localized("commands.sticky_roles.description"),
        contexts=disnake.InteractionContextTypes(guild=True),
    )
    async def sticky_roles_command(self, inter: disnake.ApplicationCommandInteraction):
        # Command group
        pass

    @sticky_roles_command.sub_command(
        name="enable",
        description=localized("commands.sticky_roles_enable.description"),
    )
    @require_permissions([{Permission.ManageStickyRoles: True}, {disnake.Permissions(administrator=True): True}])
    async def sticky_roles_enable(
        self,
        inter: disnake.ApplicationCommandInteraction,
        enable: bool = commands.Param(
            default=False,
            name=localized("commands.sticky_roles_enable.param_enable_name"),
            description=localized("commands.sticky_roles_enable.param_enable"),
        ),
    ):
        gid = inter.guild_id
        self.set_enabled(gid, enable)

        key = "enabled" if enable else "disabled"
        message = i18n.t(f"sticky_roles_cog.{key}", locale=gid)

        if enable and not inter.guild.me.guild_permissions.manage_roles:
            message += "\n" + i18n.t("sticky_roles_cog.bot_missing_manage_roles", locale=gid)

        await inter.response.send_message(message, ephemeral=True)

    @sticky_roles_command.sub_command(
        name="add",
        description=localized("commands.sticky_roles_add.description"),
    )
    @require_permissions([{Permission.ManageStickyRoles: True}, {disnake.Permissions(administrator=True): True}])
    async def sticky_roles_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        role: disnake.Role = commands.Param(
            name=localized("commands.sticky_roles_add.param_role_name"),
            description=localized("commands.sticky_roles_add.param_role"),
        ),
    ):
        gid = inter.guild_id

        if role.is_default() or role.managed:
            return await inter.response.send_message(i18n.t("sticky_roles_cog.role_not_allowed", locale=gid), ephemeral=True)

        if role >= inter.guild.me.top_role:
            return await inter.response.send_message(i18n.t("sticky_roles_cog.role_too_high_for_bot", locale=gid, role=role.mention), ephemeral=True)

        if not self.add_role(gid, role.id):
            return await inter.response.send_message(i18n.t("sticky_roles_cog.already_in_list", locale=gid, role=role.mention), ephemeral=True)

        return await inter.response.send_message(i18n.t("sticky_roles_cog.role_added", locale=gid, role=role.mention), ephemeral=True)

    @sticky_roles_command.sub_command(
        name="remove",
        description=localized("commands.sticky_roles_remove.description"),
    )
    @require_permissions([{Permission.ManageStickyRoles: True}, {disnake.Permissions(administrator=True): True}])
    async def sticky_roles_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        role: disnake.Role = commands.Param(
            name=localized("commands.sticky_roles_remove.param_role_name"),
            description=localized("commands.sticky_roles_remove.param_role"),
        ),
    ):
        gid = inter.guild_id

        if not self.remove_role(gid, role.id):
            return await inter.response.send_message(i18n.t("sticky_roles_cog.not_in_list", locale=gid, role=role.mention), ephemeral=True)

        message = i18n.t("sticky_roles_cog.role_removed", locale=gid, role=role.mention)

        if not self.get_roles(gid):
            message += "\n" + i18n.t("sticky_roles_cog.list_now_empty", locale=gid)

        return await inter.response.send_message(message, ephemeral=True)

    @sticky_roles_command.sub_command(
        name="list",
        description=localized("commands.sticky_roles_list.description"),
    )
    @require_permissions([{Permission.ManageStickyRoles: True}, {disnake.Permissions(administrator=True): True}])
    async def sticky_roles_list(self, inter: disnake.ApplicationCommandInteraction):
        gid = inter.guild_id
        guild = inter.guild

        enabled = self.is_enabled(gid)
        listed = self.get_roles(gid)

        embed = disnake.Embed(
            title=i18n.t("sticky_roles_cog.list_title", locale=gid),
            color=LogColor.Member,
        )
        embed.add_field(
            name=i18n.t("sticky_roles_cog.field_status", locale=gid),
            value=i18n.t("sticky_roles_cog.status_" + ("on" if enabled else "off"), locale=gid),
            inline=False,
        )

        if not listed:
            embed.add_field(
                name=i18n.t("sticky_roles_cog.field_roles", locale=gid),
                value=i18n.t("sticky_roles_cog.mode_all", locale=gid),
                inline=False,
            )
        else:
            lines = []
            for role_id in listed:
                role = guild.get_role(role_id)
                if not role:
                    lines.append(f"`{role_id}` ({i18n.t('sticky_roles_cog.unknown_role', locale=gid)})")
                elif not self._can_give(guild, role):
                    lines.append(f"{role.mention} ⚠️")
                else:
                    lines.append(role.mention)

            text = "\n".join(lines)
            if len(text) > 1000:  # field value
                text = text[:1000].rsplit("\n", 1)[0] + "\n…"

            embed.add_field(
                name=i18n.t("sticky_roles_cog.field_roles", locale=gid),
                value=text,
                inline=False,
            )
            embed.set_footer(text=i18n.t("sticky_roles_cog.footer_unassignable", locale=gid, custom_emojis=False))

        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(StickyRolesCog(bot))
