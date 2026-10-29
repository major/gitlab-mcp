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
    """A claimed batch does not match the supplied project, MR, or head SHA."""

    def __init__(self) -> None:
        """Explain that the mismatched batch was consumed without a GitLab request."""
        super().__init__(
            "Review note batch does not match the supplied project, merge request, or head SHA. "
            "The batch was consumed; no GitLab requests were made by this call."
        )


@dataclass(frozen=True)
class CachedReviewNote:
    """An immutable review note with its resolved body and requested coordinates."""

    file: str | None
    line: int | None
    old_line: int | None
    body: str


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
        while self._batches and (
            len(self._batches) >= self.MAX_BATCHES or self._cache_bytes + size_bytes > self.MAX_CACHE_BYTES
        ):
            self._evict_oldest()
        self._batches[batch_id] = batch
        self._cache_bytes += size_bytes
        return batch_id

    def claim(self, batch_id: str, project: str, iid: int, head_sha: str | None) -> CachedReviewBatch:
        """Consume a batch, even if its project, merge request, or head SHA does not match.

        A `None` head SHA skips the SHA match and keeps the SHA stored at validation. The live head check
        before posting still catches a push made after validation. 🔒
        """
        self._purge_expired()
        batch = self._batches.pop(batch_id, None)
        if batch is None:
            raise ReviewBatchUnavailableError
        self._cache_bytes -= batch.size_bytes

        head_mismatch = head_sha is not None and batch.head_sha.lower() != head_sha.lower()
        if batch.project != project or batch.iid != iid or head_mismatch:
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
            size_bytes += cls.NOTE_OVERHEAD_BYTES + len(note.body.encode("utf-8"))
            if note.file is not None:
                size_bytes += len(note.file.encode("utf-8"))
        return size_bytes
