"""Thin wrapper around the unofficial notebooklm-py client.

All NotebookLM access goes through here so that, if Google ever ships an
official query API, only this file has to change.
"""

import asyncio
import logging
import mimetypes
import re
import time
from collections import defaultdict
from pathlib import Path

import notebooklm
from notebooklm import AskResult, Notebook, NotebookLMClient, SharePermission, Source
from notebooklm.types import Artifact, ArtifactType, ReportFormat

log = logging.getLogger(__name__)

_CACHE_SECONDS = 60


# Studio item kind -> (label, emoji, download method, {format: extension}).
# The first format is the default. Files (things NotebookLM's chat saved to
# Studio, e.g. a .md summary) have no formats: they're posted as they are.
STUDIO_KINDS: dict[ArtifactType, tuple[str, str, str, dict[str, str]]] = {
    ArtifactType.AUDIO: ("Audio Overview", "🎧", "download_audio", {"m4a": ".m4a"}),
    ArtifactType.VIDEO: ("Video Overview", "🎬", "download_video", {"mp4": ".mp4"}),
    ArtifactType.SLIDE_DECK: ("Slide deck", "📊", "download_slide_deck", {"pdf": ".pdf", "pptx": ".pptx"}),
    ArtifactType.INFOGRAPHIC: ("Infographic", "🖼️", "download_infographic", {"png": ".png"}),
    ArtifactType.REPORT: ("Report", "📄", "download_report", {"markdown": ".md"}),
    ArtifactType.QUIZ: ("Quiz", "❓", "download_quiz", {"markdown": ".md", "html": ".html", "json": ".json"}),
    ArtifactType.FLASHCARDS: (
        "Flashcards", "🗂️", "download_flashcards", {"markdown": ".md", "html": ".html", "json": ".json"}
    ),
    ArtifactType.MIND_MAP: ("Mind map", "🧠", "download_mind_map", {"json": ".json"}),
    ArtifactType.DATA_TABLE: ("Data table", "📋", "download_data_table", {"csv": ".csv"}),
    ArtifactType.FILE: ("File", "📎", "", {}),
}
STUDIO_FORMATS = sorted({fmt for *_, formats in STUDIO_KINDS.values() for fmt in formats})

# What /studio create can make: choice -> (label, typical wait, how long to keep checking in seconds).
STUDIO_CREATE: dict[str, tuple[str, str, int]] = {
    "audio": ("Audio Overview", "5–10 minutes", 45 * 60),
    "video": ("Video Overview", "10–30 minutes", 60 * 60),
    "slide_deck": ("Slide deck", "5–10 minutes", 30 * 60),
    "infographic": ("Infographic", "a few minutes", 30 * 60),
    "briefing_doc": ("Briefing doc", "1–3 minutes", 20 * 60),
    "study_guide": ("Study guide", "1–3 minutes", 20 * 60),
    "blog_post": ("Blog post", "1–3 minutes", 20 * 60),
    "quiz": ("Quiz", "1–3 minutes", 20 * 60),
    "flashcards": ("Flashcards", "1–3 minutes", 20 * 60),
    "mind_map": ("Mind map", "under a minute", 0),
    "data_table": ("Data table", "1–3 minutes", 20 * 60),
}


def safe_filename(title: str, extension: str) -> str:
    stem = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:80] or "studio"
    return stem + extension


class NotebookService:
    def __init__(self, profile: str | None) -> None:
        self._profile = profile
        self._ctx = None
        self.client: NotebookLMClient | None = None
        # One question at a time per conversation, so follow-ups stay in order.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._notebooks_cache: tuple[float, list[Notebook]] | None = None
        self._sources_cache: dict[str, tuple[float, list[Source]]] = {}
        self._studio_cache: dict[str, tuple[float, list[Artifact]]] = {}

    async def start(self) -> None:
        self._ctx = NotebookLMClient.from_storage(profile=self._profile)
        self.client = await self._ctx.__aenter__()

    async def close(self) -> None:
        if self._ctx is not None:
            await self._ctx.__aexit__(None, None, None)
            self._ctx = self.client = None

    async def account_email(self) -> str | None:
        assert self.client
        try:
            return await self.client.get_account_email()
        except notebooklm.NotebookLMError:
            return None

    def lock(self, place_id: int) -> asyncio.Lock:
        return self._locks[place_id]

    # Notebooks --------------------------------------------------------

    async def list_notebooks(self, *, cached: bool = False) -> list[Notebook]:
        assert self.client
        now = time.monotonic()
        if cached and self._notebooks_cache and now - self._notebooks_cache[0] < _CACHE_SECONDS:
            return self._notebooks_cache[1]
        notebooks = await self.client.notebooks.list()
        self._notebooks_cache = (now, notebooks)
        return notebooks

    async def get_notebook(self, notebook_id: str) -> Notebook:
        assert self.client
        return await self.client.notebooks.get(notebook_id)

    async def create_notebook(self, title: str) -> Notebook:
        assert self.client
        notebook = await self.client.notebooks.create(title)
        self._notebooks_cache = None
        return notebook

    async def share(self, notebook_id: str, email: str, editor: bool) -> None:
        assert self.client
        permission = SharePermission.EDITOR if editor else SharePermission.VIEWER
        await self.client.sharing.add_user(notebook_id, email, permission)

    # Sources ----------------------------------------------------------

    async def list_sources(self, notebook_id: str, *, cached: bool = False) -> list[Source]:
        assert self.client
        now = time.monotonic()
        hit = self._sources_cache.get(notebook_id)
        if cached and hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
        sources = await self.client.sources.list(notebook_id)
        self._sources_cache[notebook_id] = (now, sources)
        return sources

    async def add_url(self, notebook_id: str, url: str) -> Source:
        assert self.client
        self._sources_cache.pop(notebook_id, None)
        return await self.client.sources.add_url(notebook_id, url, wait=True, wait_timeout=300)

    async def add_text(self, notebook_id: str, title: str, content: str) -> Source:
        assert self.client
        self._sources_cache.pop(notebook_id, None)
        return await self.client.sources.add_text(notebook_id, title, content, wait=True, wait_timeout=300)

    async def add_file(self, notebook_id: str, path: Path, mime_type: str | None, title: str) -> Source:
        assert self.client
        self._sources_cache.pop(notebook_id, None)
        return await self.client.sources.add_file(
            notebook_id, path, mime_type, wait=True, wait_timeout=300, title=title
        )

    async def remove_source(self, notebook_id: str, source_id: str) -> None:
        assert self.client
        self._sources_cache.pop(notebook_id, None)
        await self.client.sources.delete(notebook_id, source_id)

    # Studio -----------------------------------------------------------

    async def list_studio(self, notebook_id: str, *, cached: bool = False) -> list[Artifact]:
        """Studio items the bot knows how to download, newest first."""
        assert self.client
        now = time.monotonic()
        hit = self._studio_cache.get(notebook_id)
        if cached and hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
        everything = await self.client.artifacts.list(notebook_id)
        artifacts = [a for a in everything if a.kind in STUDIO_KINDS]
        skipped = {str(a.kind) for a in everything if a.kind not in STUDIO_KINDS}
        if skipped:
            log.info("Notebook %s has studio items the bot can't download: %s", notebook_id, ", ".join(sorted(skipped)))
        artifacts.sort(key=lambda a: a.created_at.timestamp() if a.created_at else 0, reverse=True)
        self._studio_cache[notebook_id] = (now, artifacts)
        return artifacts

    def peek_studio(self, notebook_id: str) -> list[Artifact]:
        """The last studio listing for this notebook, however old, without calling NotebookLM."""
        hit = self._studio_cache.get(notebook_id)
        return hit[1] if hit else []

    async def download_studio(self, notebook_id: str, artifact: Artifact, fmt: str | None, folder: Path) -> Path:
        """Download a finished studio item into `folder` and return the file's path."""
        assert self.client
        if artifact.kind == ArtifactType.FILE:
            return await self._download_file_item(notebook_id, artifact, folder)
        _, _, method, formats = STUDIO_KINDS[artifact.kind]
        fmt = fmt if fmt in formats else next(iter(formats))
        path = folder / safe_filename(artifact.title or "studio", formats[fmt])
        kwargs = {"output_format": fmt} if len(formats) > 1 else {}
        await getattr(self.client.artifacts, method)(notebook_id, str(path), artifact.id, **kwargs)
        return path

    async def _download_file_item(self, notebook_id: str, artifact: Artifact, folder: Path) -> Path:
        """Download a file that NotebookLM saved to Studio.

        notebooklm-py 0.8.6 lists these but has no download method for them, so this
        reads the raw listing row: index 24 is [filename, mime type, viewer URL,
        download URL]. The download URL needs the Google login, which the library's
        own downloader sends. Replace this once notebooklm-py supports files."""
        assert self.client
        a = self.client.artifacts
        rows = await a._list_raw(notebook_id)
        row = next((r for r in rows if isinstance(r, list) and r and r[0] == artifact.id), None)
        if row is None:
            raise notebooklm.ArtifactNotFoundError(artifact.id, "file")
        info = row[24] if len(row) > 24 and isinstance(row[24], list) else []
        url = info[3] if len(info) > 3 and isinstance(info[3], str) and info[3].startswith("https://") else None
        if url is None:
            raise notebooklm.ArtifactDownloadError("file", "no download link in NotebookLM's reply", artifact.id)
        name = Path(info[0] if info and isinstance(info[0], str) else artifact.title or "file")
        mime = info[1] if len(info) > 1 and isinstance(info[1], str) else None
        suffix = re.sub(r"[^\w.]+", "", name.suffix)[:10] or (mimetypes.guess_extension(mime or "") or "")
        path = folder / safe_filename(name.stem, suffix)
        await a._download_url(url, str(path))
        return path

    async def create_studio(self, notebook_id: str, kind: str, instructions: str | None) -> str:
        """Start generating a studio item from all of the notebook's sources.
        Returns its id. Mind maps are made straight away; everything else is
        still generating when this returns (see `wait_studio`)."""
        assert self.client
        a = self.client.artifacts
        nb = notebook_id
        self._studio_cache.pop(notebook_id, None)
        if kind == "mind_map":
            result = await a.generate_mind_map(nb, instructions=instructions)
            if not result.note_id:
                raise notebooklm.ArtifactError("NotebookLM didn't return the new mind map")
            return result.note_id
        reports = {"briefing_doc": ReportFormat.BRIEFING_DOC, "blog_post": ReportFormat.BLOG_POST}
        if kind == "audio":
            status = await a.generate_audio(nb, instructions=instructions)
        elif kind == "video":
            status = await a.generate_video(nb, instructions=instructions)
        elif kind == "slide_deck":
            status = await a.generate_slide_deck(nb, instructions=instructions)
        elif kind == "infographic":
            status = await a.generate_infographic(nb, instructions=instructions)
        elif kind in reports:
            status = await a.generate_report(nb, reports[kind], extra_instructions=instructions)
        elif kind == "study_guide":
            status = await a.generate_study_guide(nb, extra_instructions=instructions)
        elif kind == "quiz":
            status = await a.generate_quiz(nb, instructions=instructions)
        elif kind == "flashcards":
            status = await a.generate_flashcards(nb, instructions=instructions)
        elif kind == "data_table":
            status = await a.generate_data_table(nb, instructions=instructions)
        else:
            raise ValueError(f"Unknown studio kind {kind!r}")
        if status.is_failed:
            raise notebooklm.ArtifactError(status.error or "generation failed")
        return status.task_id

    async def wait_studio(self, notebook_id: str, artifact_id: str, timeout: float) -> Artifact | None:
        """Wait for a studio item to finish (or fail) and return it.
        Raises TimeoutError if it's still going after `timeout` seconds."""
        assert self.client
        if timeout > 0:
            await self.client.artifacts.wait_for_completion(
                notebook_id, artifact_id, initial_interval=10, max_interval=30, timeout=timeout
            )
        artifacts = await self.list_studio(notebook_id)
        return next((a for a in artifacts if a.id == artifact_id), None)

    # Chat -------------------------------------------------------------

    async def ask(self, notebook_id: str, question: str, conversation_id: str | None) -> AskResult:
        assert self.client
        return await self.client.chat.ask(notebook_id, question, conversation_id=conversation_id)

    async def save_note(self, notebook_id: str, result: AskResult, title: str) -> None:
        assert self.client
        await self.client.chat.save_answer_as_note(notebook_id, result, title=title)


def friendly_error(exc: BaseException) -> str:
    """Turn a notebooklm-py exception into something to show in Discord."""
    if isinstance(exc, notebooklm.AuthError):
        return (
            "The bot's Google login has expired. An admin needs to run "
            "`notebooklm login` on the bot's computer and restart the bot."
        )
    if isinstance(exc, notebooklm.RateLimitError):
        return "NotebookLM is rate-limiting the bot. Wait a minute and try again."
    if isinstance(exc, notebooklm.NotebookNotFoundError):
        return (
            "That notebook doesn't exist any more, or it isn't shared with the bot's "
            "Google account. Re-link this channel with `/notebook link`."
        )
    if isinstance(exc, notebooklm.SourceNotFoundError):
        return "That source no longer exists."
    if isinstance(exc, notebooklm.ArtifactNotFoundError):
        return "That studio item no longer exists. Run `/studio list` to see what's there now."
    if isinstance(exc, notebooklm.ArtifactNotReadyError):
        return "That studio item is still being generated. Try again once it's finished."
    if isinstance(exc, notebooklm.ArtifactDownloadError):
        return "NotebookLM wouldn't hand over that file. Try again, or open it in NotebookLM."
    if isinstance(exc, notebooklm.NotebookLimitError):
        return "The bot's Google account has hit NotebookLM's notebook limit."
    if isinstance(exc, (TimeoutError, notebooklm.WaitTimeoutError)):
        return "NotebookLM took too long to respond. Try again in a moment."
    if isinstance(exc, notebooklm.NetworkError):
        return "Couldn't reach NotebookLM. Check the bot's internet connection."
    if isinstance(exc, (notebooklm.SourceAddError, notebooklm.SourceProcessingError)):
        return f"NotebookLM couldn't add that source: {exc}"
    if isinstance(exc, notebooklm.NotebookLMError):
        return f"NotebookLM returned an error: {exc}"
    return "Something went wrong. Check the bot's log for details."
