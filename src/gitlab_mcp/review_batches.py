"""Short-lived, single-use storage for validated review notes. 🗃️."""

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastmcp.exceptions import ToolError


class ReviewBatchUnavailableError(ToolError):
    """The requested batch is not available in this server's cache."""

    def __init__(self) -> None:
        """Explain that the cache cannot identify why the batch is missing."""
        super().__init__("Review note batch is unavailable. Its prior use or expiration is unknown.")


class ReviewBatchMismatchError(ToolError):
    """A claimed batch does not match the supplied project or MR."""

    def __init__(self) -> None:
        """Explain that the mismatched batch was consumed without a GitLab request."""
        super().__init__(
            "Review note batch does not match the supplied project or merge request. "
            "The batch was consumed; no GitLab requests were made by this call."
        )


@dataclass(frozen=True)
class CachedReviewNote:
    """An immutable review note with its resolved body and requested coordinates."""

    file: str | None
    line: int | None
    old_line: int | None
    body: str | None


@dataclass(frozen=True)
class CachedReviewBatch:
    """A validated review batch and its cache accounting metadata."""

    project: str
    iid: int
    head_sha: str
    notes: tuple[CachedReviewNote, ...]
    expires_at: float
    size_bytes: int


class ReviewBatchCache:
    """Keep bounded, expiring review batches in memory for one server lifespan."""

    TTL_SECONDS = 1800
    MAX_BATCHES = 32
    MAX_CACHE_BYTES = 8 * 1024 * 1024
    NOTE_OVERHEAD_BYTES = 32
    BATCH_OVERHEAD_BYTES = 64

    def __init__(self) -> None:
        """Create an empty ordered cache."""
        self._batches: OrderedDict[str, CachedReviewBatch] = OrderedDict()
        self._cache_bytes = 0

    def put(self, project: str, iid: int, head_sha: str, notes: tuple[CachedReviewNote, ...]) -> str:
        """Cache notes and return a random, single-use identifier."""
        self._purge_expired()
        size_bytes = self._measure_size(project, head_sha, notes)
        if size_bytes > self.MAX_CACHE_BYTES:
            raise ToolError("Review note batch is too large to cache. Send the notes again in a smaller batch.")  # noqa: TRY003

        batch_id = secrets.token_urlsafe(18)
        batch = CachedReviewBatch(
            project=project,
            iid=iid,
            head_sha=head_sha,
            notes=notes,
            expires_at=time.monotonic() + self.TTL_SECONDS,
            size_bytes=size_bytes,
        )
        self._store(batch_id, batch)
        return batch_id

    def restore(self, batch_id: str, batch: CachedReviewBatch) -> int | None:
        """Put back a claimed batch after a call that sent no write request, keeping its id and expiry.

        `claim` pops the batch, so no other call can use the handle while this call holds it, even across awaits.
        Only GitLab reads may have happened, so nothing can post twice. ♻️

        Returns:
            The whole seconds left before the batch expires, or `None` when it expired first and was dropped.
        """
        self._purge_expired()
        remaining = batch.expires_at - time.monotonic()
        if remaining <= 0:
            return None
        self._store(batch_id, batch)
        return max(1, int(remaining))

    def _store(self, batch_id: str, batch: CachedReviewBatch) -> None:
        """Insert a batch, evicting the oldest ones first when count or byte limits would be exceeded."""
        while self._batches and (
            len(self._batches) >= self.MAX_BATCHES or self._cache_bytes + batch.size_bytes > self.MAX_CACHE_BYTES
        ):
            self._evict_oldest()
        self._batches[batch_id] = batch
        self._cache_bytes += batch.size_bytes

    def claim(self, batch_id: str, project: str, iid: int) -> CachedReviewBatch:
        """Consume a batch, even if its project or merge request does not match.

        The head SHA is not matched here. The caller compares it, so a head mismatch can put the batch back, and
        the live head check before posting still catches a push made after validation. 🔒
        """
        self._purge_expired()
        batch = self._batches.pop(batch_id, None)
        if batch is None:
            raise ReviewBatchUnavailableError
        self._cache_bytes -= batch.size_bytes

        if batch.project != project or batch.iid != iid:
            raise ReviewBatchMismatchError
        return batch

    def _purge_expired(self) -> None:
        """Remove expired batches and update the byte count."""
        now = time.monotonic()
        expired_ids = [batch_id for batch_id, batch in self._batches.items() if batch.expires_at <= now]
        for batch_id in expired_ids:
            batch = self._batches.pop(batch_id)
            self._cache_bytes -= batch.size_bytes

    def _evict_oldest(self) -> None:
        """Remove the oldest cached batch and update the byte count."""
        _, batch = self._batches.popitem(last=False)
        self._cache_bytes -= batch.size_bytes

    @classmethod
    def _measure_size(cls, project: str, head_sha: str, notes: tuple[CachedReviewNote, ...]) -> int:
        """Estimate retained memory using UTF-8 text sizes and fixed record overhead."""
        size_bytes = cls.BATCH_OVERHEAD_BYTES + len(project.encode("utf-8")) + len(head_sha.encode("utf-8"))
        for note in notes:
            size_bytes += cls.NOTE_OVERHEAD_BYTES + len((note.body or "").encode("utf-8"))
            if note.file is not None:
                size_bytes += len(note.file.encode("utf-8"))
        return size_bytes
