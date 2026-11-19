import asyncio
import logging
import re
import tempfile
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
import notebooklm
from notebooklm import AskResult

from . import config
from .nlm import STUDIO_CREATE, STUDIO_KINDS, NotebookService, friendly_error
from .store import Link, Store
from .ui import (
    AddTextModal,
    AnswerView,
    StudioReadyView,
    StudioView,
    answer_embeds,
    send_studio_file,
    shorten,
    studio_embed,
    studio_label,
    studio_status,
)

log = logging.getLogger(__name__)

MAX_FILE_MB = 100


class NotLinked(app_commands.CheckFailure):
    pass


class NotManager(app_commands.CheckFailure):
    pass


def is_manager(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    return config.MANAGER_ROLE_ID is not None and any(r.id == config.MANAGER_ROLE_ID for r in member.roles)


def manager_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_manager(interaction.user):
            raise NotManager()
        return True

    return app_commands.check(predicate)


def link_key(channel: discord.abc.GuildChannel | discord.Thread | None) -> int | None:
    """Threads use their parent channel's notebook."""
    if channel is None:
        return None
    if isinstance(channel, discord.Thread):
        return channel.parent_id
    return channel.id


class NotebookCog(commands.Cog):
    notebook = app_commands.Group(name="notebook", description="Manage this channel's notebook", guild_only=True)
    source = app_commands.Group(name="source", description="Manage the notebook's sources", guild_only=True)
    studio = app_commands.Group(
        name="studio", description="Get Audio Overviews, slide decks and other studio items", guild_only=True
    )

    def __init__(self, bot: commands.Bot, service: NotebookService, store: Store) -> None:
        self.bot = bot
        self.service = service
        self.store = store
        self._last_ask: dict[int, float] = {}
        self._creations: set[asyncio.Task] = set()  # keeps /studio create waits alive

    # Helpers ----------------------------------------------------------

    def find_link(self, channel) -> Link | None:
        if channel is None:
            return None
        # A thread can have its own link; otherwise it inherits the parent's.
        return self.store.get_link(channel.id) or self.store.get_link(link_key(channel) or 0)

    def require_link(self, interaction: discord.Interaction) -> Link:
        link = self.find_link(interaction.channel)
        if link is None:
            raise NotLinked()
        return link

    def cooldown_left(self, user_id: int) -> float:
        last = self._last_ask.get(user_id, 0.0)
        return max(0.0, config.ASK_COOLDOWN - (time.monotonic() - last))

    async def run_question(self, place_id: int, link: Link, question: str, fresh: bool) -> tuple[AskResult, str | None]:
        """Ask in this channel's/thread's conversation. Returns the result and an optional note."""
        async with self.service.lock(place_id):
            conversation_id = None if fresh else self.store.get_conversation(place_id, link.notebook_id)
            note = "new conversation" if fresh else None
            try:
                result = await self.service.ask(link.notebook_id, question, conversation_id)
            except Exception:
                if conversation_id is None:
                    raise
                # The stored conversation may have been deleted in NotebookLM; start over once.
                log.warning("Ask failed in conversation %s; retrying in a new one", conversation_id, exc_info=True)
                result = await self.service.ask(link.notebook_id, question, None)
                note = "the previous conversation was gone, so this started a new one"
            self.store.set_conversation(place_id, link.notebook_id, result.conversation_id)
            return result, note

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, NotLinked):
            message = "This channel isn't linked to a notebook yet. A manager can run `/notebook link`."
        elif isinstance(error, NotManager):
            message = "Only server managers (or people with the bot manager role) can do that."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You can't use that here."
        else:
            original = getattr(error, "original", error)
            log.error("Command failed", exc_info=original)
            message = friendly_error(original)
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # Asking -----------------------------------------------------------

    @app_commands.command(description="Ask this channel's notebook a question")
    @app_commands.describe(
        question="What do you want to know?",
        new_conversation="Start fresh instead of following on from earlier questions here",
    )
    @app_commands.guild_only()
    async def ask(self, interaction: discord.Interaction, question: str, new_conversation: bool = False) -> None:
        link = self.require_link(interaction)
        wait = self.cooldown_left(interaction.user.id)
        if wait:
            await interaction.response.send_message(f"Slow down a little: try again in {wait:.0f}s.", ephemeral=True)
            return
        self._last_ask[interaction.user.id] = time.monotonic()
        await interaction.response.defer(thinking=True)

        assert interaction.channel
        result, note = await self.run_question(interaction.channel.id, link, question, new_conversation)
        embeds = answer_embeds(question, result, interaction.user, link, note)
        view = AnswerView(self.service, link, question, result)
        for i, embed in enumerate(embeds):
            last = i == len(embeds) - 1
            message = await interaction.followup.send(embed=embed, view=view if last else discord.utils.MISSING, wait=True)
            if last:
                view.message = message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """`@Bot question` works like /ask in linked channels."""
        if message.author.bot or not message.guild or not self.bot.user:
            return
        mention = re.compile(rf"<@!?{self.bot.user.id}>")
        if not mention.search(message.content):
            return  # ignore reply pings without an explicit mention
        question = mention.sub("", message.content).strip()
        if not question:
            await message.reply("Ask me something, e.g. `@me what are the key points?`", mention_author=False)
            return
        link = self.find_link(message.channel)
        if link is None:
            await message.reply(
                "This channel isn't linked to a notebook yet. A manager can run `/notebook link`.",
                mention_author=False,
            )
            return
        wait = self.cooldown_left(message.author.id)
        if wait:
            await message.add_reaction("⏳")
            return
        self._last_ask[message.author.id] = time.monotonic()

        try:
            async with message.channel.typing():
                result, note = await self.run_question(message.channel.id, link, question, fresh=False)
        except Exception as exc:
            log.exception("Mention question failed")
            await message.reply(friendly_error(exc), mention_author=False)
            return

        embeds = answer_embeds(question, result, message.author, link, note)
        view = AnswerView(self.service, link, question, result)
        reply_to = message
        for i, embed in enumerate(embeds):
            last = i == len(embeds) - 1
            sent = await reply_to.reply(embed=embed, view=view if last else None, mention_author=False)
            reply_to = sent
            if last:
                view.message = sent

    @app_commands.command(description="Start a new conversation in this channel or thread")
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction) -> None:
        link = self.require_link(interaction)
        assert interaction.channel
        self.store.set_conversation(interaction.channel.id, link.notebook_id, None)
        await interaction.response.send_message(
            f"🔄 {interaction.user.mention} started a new conversation. "
            "The next question won't follow on from earlier ones.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # /notebook --------------------------------------------------------

    async def notebook_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            notebooks = await self.service.list_notebooks(cached=True)
        except Exception:
            log.exception("Autocomplete couldn't list notebooks")
            return []
        current = current.lower()
        return [
            app_commands.Choice(name=shorten(nb.title or "Untitled", 100), value=nb.id)
            for nb in notebooks
            if current in (nb.title or "").lower()
        ][:25]

    @notebook.command(name="list", description="List the notebooks the bot can see")
    async def notebook_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        notebooks = await self.service.list_notebooks()
        if not notebooks:
            await interaction.followup.send(
                "The bot's Google account has no notebooks. Share one with it, or use `/notebook create`.",
                ephemeral=True,
            )
            return
        lines = [
            f"• [{shorten(nb.title or 'Untitled', 80)}]({config.NOTEBOOK_URL.format(nb.id)}) · "
            f"{nb.sources_count} sources{'' if nb.is_owner else ' · shared with bot'}"
            for nb in notebooks
        ]
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await interaction.followup.send(
            embed=discord.Embed(title="Notebooks", description=text, color=config.EMBED_COLOR), ephemeral=True
        )

    @notebook.command(name="link", description="Link this channel to a notebook")
    @app_commands.describe(notebook="Start typing the notebook's name")
    @app_commands.autocomplete(notebook=notebook_autocomplete)
    @manager_only()
    async def notebook_link(self, interaction: discord.Interaction, notebook: str) -> None:
        await interaction.response.defer(thinking=True)
        nb = await self.service.get_notebook(notebook)
        assert interaction.channel
        self.store.set_link(interaction.channel.id, Link(notebook_id=nb.id, title=nb.title or "Untitled"))
        self.store.set_conversation(interaction.channel.id, nb.id, None)
        await interaction.followup.send(
            f"🔗 This channel is now linked to **{nb.title}**. Ask away with `/ask` or by mentioning me.\n"
            f"Open it in NotebookLM: <{config.NOTEBOOK_URL.format(nb.id)}>"
        )

    @notebook.command(name="create", description="Create a new notebook and link this channel to it")
    @app_commands.describe(title="Name for the new notebook")
    @manager_only()
    async def notebook_create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 100]) -> None:
        await interaction.response.defer(thinking=True)
        nb = await self.service.create_notebook(title)
        assert interaction.channel
        self.store.set_link(interaction.channel.id, Link(notebook_id=nb.id, title=nb.title or title))
        await interaction.followup.send(
            f"📓 Created **{title}** and linked it to this channel. Add sources with `/source add-url`, "
            f"`/source add-file` or `/source add-text`.\n<{config.NOTEBOOK_URL.format(nb.id)}>"
        )

    @notebook.command(name="unlink", description="Unlink this channel from its notebook")
    @manager_only()
    async def notebook_unlink(self, interaction: discord.Interaction) -> None:
        assert interaction.channel
        link = self.store.remove_link(interaction.channel.id)
        if link is None:
            await interaction.response.send_message(
                "This channel has no notebook of its own (a thread may be using its parent channel's).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Unlinked **{link.title}**. The notebook itself wasn't changed."
        )

    @notebook.command(name="info", description="Show which notebook this channel uses")
    async def notebook_info(self, interaction: discord.Interaction) -> None:
        link = self.require_link(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        sources = await self.service.list_sources(link.notebook_id)
        assert interaction.channel
        in_conversation = self.store.get_conversation(interaction.channel.id, link.notebook_id) is not None
        embed = discord.Embed(
            title=link.title, url=config.NOTEBOOK_URL.format(link.notebook_id), color=config.EMBED_COLOR
        )
        embed.add_field(name="Sources", value=str(len(sources)))
        embed.add_field(name="Conversation", value="ongoing" if in_conversation else "fresh")
        email = await self.service.account_email()
        if email:
            embed.set_footer(text=f"Bot's Google account: {email}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @notebook.command(name="share", description="Give someone access to this notebook in NotebookLM")
    @app_commands.describe(email="Their Google account email", role="What they can do (default: editor)")
    @app_commands.choices(
        role=[app_commands.Choice(name="editor", value="editor"), app_commands.Choice(name="viewer", value="viewer")]
    )
    @manager_only()
    async def notebook_share(
        self, interaction: discord.Interaction, email: str, role: app_commands.Choice[str] | None = None
    ) -> None:
        link = self.require_link(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        editor = role is None or role.value == "editor"
        await self.service.share(link.notebook_id, email.strip(), editor)
        await interaction.followup.send(
            f"Shared **{link.title}** with `{email}` as {'editor' if editor else 'viewer'}.", ephemeral=True
        )

    # /source ----------------------------------------------------------

    async def source_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        link = self.find_link(interaction.channel)
        if link is None:
            return []
        try:
            sources = await self.service.list_sources(link.notebook_id, cached=True)
        except Exception:
            log.exception("Autocomplete couldn't list sources")
            return []
        current = current.lower()
        return [
            app_commands.Choice(name=shorten(s.title or "Untitled", 100), value=s.id)
            for s in sources
            if current in (s.title or "").lower()
        ][:25]

    @source.command(name="list", description="List the notebook's sources")
    async def source_list(self, interaction: discord.Interaction) -> None:
        link = self.require_link(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        sources = await self.service.list_sources(link.notebook_id)
        if not sources:
            await interaction.followup.send(f"**{link.title}** has no sources yet.", ephemeral=True)
            return

        def describe(s) -> str:
            title = shorten(s.title or "Untitled", 80)
            status = " ⏳ processing" if s.is_processing else " ⚠️ failed" if s.is_error else ""
            return f"• [{title}]({s.url}){status}" if s.url else f"• {title}{status}"

        text = "\n".join(describe(s) for s in sources)
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await interaction.followup.send(
            embed=discord.Embed(title=f"Sources in {link.title}", description=text, color=config.EMBED_COLOR),
            ephemeral=True,
        )

    @source.command(name="add-url", description="Add a web page or YouTube video as a source")
    @app_commands.describe(url="Link to the page or video")
    @manager_only()
    async def source_add_url(self, interaction: discord.Interaction, url: str) -> None:
        link = self.require_link(interaction)
        if not re.match(r"https?://", url):
            await interaction.response.send_message("That doesn't look like a link (it should start with https://).", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        src = await self.service.add_url(link.notebook_id, url)
        await interaction.followup.send(
            f"🌐 {interaction.user.mention} added **{src.title or url}** to **{link.title}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @source.command(name="add-file", description="Add a PDF, document or text file as a source")
    @app_commands.describe(file="The file to add")
    @manager_only()
    async def source_add_file(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        link = self.require_link(interaction)
        if file.size > MAX_FILE_MB * 1024 * 1024:
            await interaction.response.send_message(f"Files must be under {MAX_FILE_MB} MB.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / Path(file.filename).name
            await file.save(path)
            mime = file.content_type.split(";")[0] if file.content_type else None
            src = await self.service.add_file(link.notebook_id, path, mime, file.filename)
        await interaction.followup.send(
            f"📎 {interaction.user.mention} added **{src.title or file.filename}** to **{link.title}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @source.command(name="add-text", description="Paste text in as a source")
    @app_commands.describe(title="Name for the source")
    @manager_only()
    async def source_add_text(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 100]) -> None:
        link = self.require_link(interaction)
        await interaction.response.send_modal(AddTextModal(self.service, link, title))

    @source.command(name="remove", description="Remove a source from the notebook")
    @app_commands.describe(source="Start typing the source's name")
    @app_commands.autocomplete(source=source_autocomplete)
    @manager_only()
    async def source_remove(self, interaction: discord.Interaction, source: str) -> None:
        link = self.require_link(interaction)
        await interaction.response.defer(thinking=True)
        sources = await self.service.list_sources(link.notebook_id)
        title = next((s.title for s in sources if s.id == source), None)
        if title is None:
            await interaction.followup.send("Couldn't find that source. Pick one from the list as you type.")
            return
        await self.service.remove_source(link.notebook_id, source)
        await interaction.followup.send(
            f"🗑️ {interaction.user.mention} removed **{title}** from **{link.title}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # /studio ----------------------------------------------------------

    async def studio_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        link = self.find_link(interaction.channel)
        if link is None:
            return []
        try:
            artifacts = await self.service.list_studio(link.notebook_id, cached=True)
        except Exception:
            log.exception("Autocomplete couldn't list studio items")
            return []
        current = current.lower()
        return [
            app_commands.Choice(name=shorten(studio_label(a) + studio_status(a), 100), value=a.id)
            for a in artifacts
            if current in studio_label(a).lower()
        ][:25]

    @studio.command(name="list", description="Show the notebook's studio items and pick one to download")
    async def studio_list(self, interaction: discord.Interaction) -> None:
        link = self.require_link(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        artifacts = await self.service.list_studio(link.notebook_id)
        if not artifacts:
            await interaction.followup.send(
                f"**{link.title}** has no studio items yet. Create them in NotebookLM's Studio panel: "
                f"<{config.NOTEBOOK_URL.format(link.notebook_id)}>",
                ephemeral=True,
            )
            return
        view = StudioView(self.service, link, artifacts)
        view.message = await interaction.followup.send(
            embed=studio_embed(link, artifacts), view=view, ephemeral=True, wait=True
        )

    @studio.command(name="download", description="Post a studio item (audio, slides, report…) as a file")
    @app_commands.describe(
        item="Start typing the item's name",
        format="Slide decks: pdf or pptx. Quizzes and flashcards: markdown, html or json.",
        private="Only you see the file (default: post it for everyone)",
    )
    @app_commands.autocomplete(item=studio_autocomplete)
    @app_commands.choices(
        format=[app_commands.Choice(name=f, value=f) for f in ("pdf", "pptx", "markdown", "html", "json")]
    )
    async def studio_download(
        self,
        interaction: discord.Interaction,
        item: str,
        format: app_commands.Choice[str] | None = None,
        private: bool = False,
    ) -> None:
        link = self.require_link(interaction)
        artifact = next((a for a in self.service.peek_studio(link.notebook_id) if a.id == item), None)
        if artifact is None or not artifact.is_completed:
            # Not known yet or possibly out of date: ask NotebookLM, which can take
            # longer than Discord's 3-second reply window, so defer first.
            await interaction.response.defer(ephemeral=private, thinking=True)
            artifacts = await self.service.list_studio(link.notebook_id)
            artifact = next((a for a in artifacts if a.id == item), None)
            if artifact is None:
                await interaction.followup.send(
                    "Couldn't find that studio item. Pick one from the list as you type.", ephemeral=True
                )
                return
        await send_studio_file(interaction, self.service, link, artifact, format.value if format else None, private)

    @studio.command(name="create", description="Have NotebookLM make a new studio item and post it here")
    @app_commands.describe(
        kind="What to make",
        instructions="Optional: what to focus on, who it's for, tone…",
    )
    @app_commands.choices(kind=[app_commands.Choice(name=v[0], value=k) for k, v in STUDIO_CREATE.items()])
    @manager_only()
    async def studio_create(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        instructions: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        link = self.require_link(interaction)
        label, eta, timeout = STUDIO_CREATE[kind.value]
        await interaction.response.defer(thinking=True)
        if not await self.service.list_sources(link.notebook_id, cached=True):
            await interaction.followup.send(f"**{link.title}** has no sources yet, so there's nothing to make a {label} from.")
            return
        started = time.monotonic()
        try:
            artifact_id = await self.service.create_studio(link.notebook_id, kind.value, instructions)
        except notebooklm.RateLimitError:
            await interaction.followup.send(
                f"NotebookLM refused to make another {label}. The bot's Google account has probably hit "
                "its daily limit for this kind of item. Try again tomorrow, or try a different kind."
            )
            return
        about = f" about *{shorten(instructions, 200)}*" if instructions else ""
        await interaction.followup.send(
            f"🛠️ {interaction.user.mention} asked NotebookLM for a **{label}** of **{link.title}**{about}. "
            f"This usually takes {eta}; I'll post it here when it's ready.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        task = asyncio.create_task(self.finish_creation(interaction, link, label, artifact_id, timeout, started))
        self._creations.add(task)
        task.add_done_callback(self._creations.discard)

    async def finish_creation(
        self,
        interaction: discord.Interaction,
        link: Link,
        label: str,
        artifact_id: str,
        timeout: int,
        started: float,
    ) -> None:
        """Wait for a /studio create item, then post it. Runs in the background."""
        user = interaction.user
        # A slash command's reply token lasts 15 minutes. Inside that window the file
        # can be posted as a reply (no Attach Files permission needed); after it, the
        # bot posts a normal message with a button that posts the file.
        def token_alive() -> bool:
            return started > 0 and time.monotonic() - started < 14 * 60

        async def say(text: str, view: discord.ui.View | None = None) -> discord.Message | None:
            mentions = discord.AllowedMentions(users=[user])
            try:
                if token_alive():
                    return await interaction.followup.send(
                        text, view=view or discord.utils.MISSING, allowed_mentions=mentions, wait=True
                    )
                if isinstance(interaction.channel, discord.abc.Messageable):
                    return await interaction.channel.send(text, view=view, allowed_mentions=mentions)
            except discord.HTTPException:
                log.exception("Couldn't post the result of /studio create")
            return None

        try:
            artifact = await self.service.wait_studio(link.notebook_id, artifact_id, timeout)
        except TimeoutError:
            await say(
                f"⏳ {user.mention} your **{label}** is still being generated after {timeout // 60} minutes. "
                "It'll show up in `/studio list` when NotebookLM finishes it."
            )
            return
        except Exception as exc:
            log.exception("Waiting for studio item %s failed", artifact_id)
            await say(f"⚠️ {user.mention} I lost track of your **{label}**: {friendly_error(exc)}")
            return

        if artifact is None:
            await say(
                f"⚠️ {user.mention} NotebookLM says your **{label}** is done, but I can't find it. "
                "Check `/studio list`."
            )
        elif artifact.is_failed:
            await say(
                f"⚠️ {user.mention} NotebookLM couldn't make your **{label}**. Try again, or try it in NotebookLM: "
                f"<{config.NOTEBOOK_URL.format(link.notebook_id)}>"
            )
        elif not artifact.is_completed:
            await say(f"⏳ {user.mention} your **{label}** isn't finished yet. It'll show up in `/studio list`.")
        else:
            # Leave a few minutes for the download and upload before the token expires.
            if time.monotonic() - started < 12 * 60:
                try:
                    await send_studio_file(interaction, self.service, link, artifact, cooldown=False)
                    return
                except discord.HTTPException:
                    log.warning("Couldn't post studio item %s as a reply; posting a button instead", artifact_id)
                    started = 0.0  # treat the reply token as expired from here on
            _, emoji, *_ = STUDIO_KINDS[artifact.kind]
            view = StudioReadyView(self.service, link, artifact)
            view.message = await say(
                f"{emoji} {user.mention} your **{label}** of **{link.title}** is ready: **{artifact.title}**",
                view=view,
            )
