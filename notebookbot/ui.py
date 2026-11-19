import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from notebooklm import AskResult
from notebooklm.types import Artifact

from . import config
from .nlm import STUDIO_KINDS, NotebookService, friendly_error

if TYPE_CHECKING:
    from .store import Link

log = logging.getLogger(__name__)

EMBED_LIMIT = 4000  # Discord allows 4096 characters in an embed description
DOWNLOAD_COOLDOWN = 15  # seconds each person waits between studio downloads
_last_download: dict[int, float] = {}


def split_text(text: str, limit: int = EMBED_LIMIT) -> list[str]:
    """Split text into chunks under `limit`, preferring paragraph and line breaks."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks or ["(NotebookLM returned an empty answer.)"]


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def answer_embeds(
    question: str, result: AskResult, asker: discord.abc.User, link: "Link", note: str | None = None
) -> list[discord.Embed]:
    """One embed per chunk of the answer. Each goes in its own message
    (Discord caps a message at 6000 characters across all its embeds)."""
    chunks = split_text(result.answer or "")
    embeds = []
    for i, chunk in enumerate(chunks):
        embed = discord.Embed(description=chunk, color=config.EMBED_COLOR)
        if i == 0:
            embed.set_author(name=f"{asker.display_name} asked", icon_url=asker.display_avatar.url)
            embed.title = shorten(question, 256)
        embeds.append(embed)

    cited = len({r.source_id for r in result.references})
    footer = f"📓 {link.title}"
    if cited:
        footer += f" · cites {cited} source{'s' if cited != 1 else ''}"
    if result.is_follow_up:
        footer += " · follow-up (/reset for a fresh conversation)"
    if note:
        footer += f" · {note}"
    embeds[-1].set_footer(text=shorten(footer, 2048))
    embeds[0].url = config.NOTEBOOK_URL.format(link.notebook_id)
    return embeds


class AnswerView(discord.ui.View):
    """Buttons under an answer. They work for an hour, then are removed."""

    def __init__(self, service: NotebookService, link: "Link", question: str, result: AskResult) -> None:
        super().__init__(timeout=3600)
        self.service = service
        self.link = link
        self.question = question
        self.result = result
        self.message: discord.Message | None = None
        if not result.references:
            self.remove_item(self.show_sources)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Sources", emoji="📚", style=discord.ButtonStyle.secondary)
    async def show_sources(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sources = await self.service.list_sources(self.link.notebook_id, cached=True)
            titles = {s.id: s.title or "Untitled" for s in sources}
        except Exception:
            log.exception("Couldn't list sources")
            titles = {}

        lines = []
        seen: set[int] = set()
        for ref in sorted(self.result.references, key=lambda r: r.citation_number or 0):
            if ref.citation_number in seen:
                continue
            seen.add(ref.citation_number)
            title = titles.get(ref.source_id, "Unknown source")
            line = f"**[{ref.citation_number}] {title}**"
            if ref.cited_text:
                line += f"\n> {shorten(ref.cited_text, 300)}"
            lines.append(line)

        text = "\n\n".join(lines) or "No citations."
        if len(text) > EMBED_LIMIT:
            text = text[: EMBED_LIMIT - 1] + "…"
        embed = discord.Embed(title="Sources cited", description=text, color=config.EMBED_COLOR)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Save as note", emoji="📝", style=discord.ButtonStyle.secondary)
    async def save_note(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.save_note(self.link.notebook_id, self.result, shorten(self.question, 100))
        except Exception as exc:
            log.exception("Couldn't save note")
            await interaction.followup.send(friendly_error(exc), ephemeral=True)
            return
        button.disabled = True
        button.label = "Saved as note"
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            f"Saved to the notes in **{self.link.title}** (visible in NotebookLM).", ephemeral=True
        )
        if interaction.channel and isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.channel.send(
                f"📝 {interaction.user.mention} saved this answer as a note in **{self.link.title}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )


class AddTextModal(discord.ui.Modal, title="Add text as a source"):
    content = discord.ui.TextInput(
        label="Text", style=discord.TextStyle.paragraph, max_length=4000, placeholder="Paste notes here…"
    )

    def __init__(self, service: NotebookService, link: "Link", source_title: str) -> None:
        super().__init__()
        self.service = service
        self.link = link
        self.source_title = source_title

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            await self.service.add_text(self.link.notebook_id, self.source_title, self.content.value)
        except Exception as exc:
            log.exception("Couldn't add text source")
            await interaction.followup.send(friendly_error(exc))
            return
        await interaction.followup.send(
            f"📄 {interaction.user.mention} added **{self.source_title}** to **{self.link.title}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


# Studio -----------------------------------------------------------------


def studio_label(artifact: Artifact) -> str:
    label, emoji, *_ = STUDIO_KINDS[artifact.kind]
    return f"{emoji} {artifact.title or 'Untitled'} · {label}"


def studio_status(artifact: Artifact) -> str:
    if artifact.is_completed:
        return ""
    if artifact.is_failed:
        return " ⚠️ failed"
    return " ⏳ generating"


def studio_embed(link: "Link", artifacts: list[Artifact]) -> discord.Embed:
    lines = []
    for a in artifacts:
        line = f"• {shorten(studio_label(a), 90)}{studio_status(a)}"
        if a.created_at:
            line += f" · {discord.utils.format_dt(a.created_at, 'R')}"
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > EMBED_LIMIT:
        text = text[: EMBED_LIMIT - 1] + "…"
    embed = discord.Embed(
        title=f"Studio in {link.title}",
        url=config.NOTEBOOK_URL.format(link.notebook_id),
        description=text,
        color=config.EMBED_COLOR,
    )
    embed.set_footer(text="Pick one below to post it here, or use /studio download.")
    return embed


async def send_studio_file(
    interaction: discord.Interaction,
    service: NotebookService,
    link: "Link",
    artifact: Artifact,
    fmt: str | None = None,
    private: bool = False,
    cooldown: bool = True,
) -> None:
    """Download a studio item from NotebookLM and post it as an attachment.
    Works whether or not the interaction has already been deferred."""

    async def refuse(text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    if not artifact.is_completed:
        state = "failed to generate" if artifact.is_failed else "is still being generated"
        await refuse(f"**{artifact.title}** {state} in NotebookLM, so there's nothing to download.")
        return
    left = DOWNLOAD_COOLDOWN - (time.monotonic() - _last_download.get(interaction.user.id, 0.0))
    if cooldown and left > 0:
        await refuse(f"Slow down a little: try again in {left:.0f}s.")
        return
    _last_download[interaction.user.id] = time.monotonic()

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=private, thinking=True)
    label, emoji, _, formats = STUDIO_KINDS[artifact.kind]
    limit = interaction.guild.filesize_limit if interaction.guild else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
    notebook_url = config.NOTEBOOK_URL.format(link.notebook_id)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await service.download_studio(link.notebook_id, artifact, fmt, Path(tmp))
        except Exception as exc:
            log.exception("Couldn't download studio item %s", artifact.id)
            await interaction.followup.send(friendly_error(exc), ephemeral=True)
            return
        size = path.stat().st_size
        if size > limit:
            await interaction.followup.send(
                f"{emoji} **{artifact.title}** is {size / 1048576:.1f} MB, more than this server's "
                f"{limit / 1048576:.0f} MB upload limit. Open it in NotebookLM instead: <{notebook_url}>",
                ephemeral=private,
            )
            return
        if fmt is None or fmt in formats:
            note = ""
        elif formats:
            note = f" (only available as {', '.join(formats)})"
        else:
            note = " (files are posted as they were saved)"
        await interaction.followup.send(
            f"{emoji} **{artifact.title}** · {label} from **{link.title}**{note}\n"
            f"Requested by {interaction.user.mention}",
            file=discord.File(path, filename=path.name),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=private,
        )


class StudioView(discord.ui.View):
    """Dropdown under /studio list. Picking an item posts the file in the channel."""

    def __init__(self, service: NotebookService, link: "Link", artifacts: list[Artifact]) -> None:
        super().__init__(timeout=900)
        self.service = service
        self.link = link
        self.message: discord.Message | None = None
        ready = [a for a in artifacts if a.is_completed][:25]  # Discord allows 25 options
        self.artifacts = {a.id: a for a in ready}
        if not ready:
            self.remove_item(self.pick)
            return
        for a in ready:
            label, emoji, *_ = STUDIO_KINDS[a.kind]
            created = f" · {a.created_at:%d %b %Y}" if a.created_at else ""
            self.pick.add_option(
                label=shorten(a.title or "Untitled", 100),
                value=a.id,
                emoji=emoji,
                description=shorten(f"{label}{created}", 100),
            )

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.select(placeholder="Post a studio item in this channel…")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        artifact = self.artifacts.get(select.values[0])
        if artifact is None:
            await interaction.response.send_message("That item is gone. Run `/studio list` again.", ephemeral=True)
            return
        await send_studio_file(interaction, self.service, self.link, artifact)


class StudioReadyView(discord.ui.View):
    """Button on a "your item is ready" message, for items that took too long to
    post directly (a slash command's reply expires after 15 minutes)."""

    def __init__(self, service: NotebookService, link: "Link", artifact: Artifact) -> None:
        super().__init__(timeout=86400)
        self.service = service
        self.link = link
        self.artifact = artifact
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Post file", emoji="📥", style=discord.ButtonStyle.primary)
    async def post(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await send_studio_file(interaction, self.service, self.link, self.artifact)
