"""GitLab tool modules. Importing this package registers every tool it contains 🧩."""

from gitlab_mcp.tools import (
    api,
    ci,
    ci_write,
    labels,
    mr_create,
    mr_diff,
    mr_land,
    mr_read,
    mr_review,
    mr_threads,
    mr_update,
    users,
    work_items,
)

__all__ = [
    "api",
    "ci",
    "ci_write",
    "labels",
    "mr_create",
    "mr_diff",
    "mr_land",
    "mr_read",
    "mr_review",
    "mr_threads",
    "mr_update",
    "users",
    "work_items",
]
