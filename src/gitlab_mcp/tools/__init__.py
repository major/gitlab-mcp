"""GitLab tool modules. Importing this package registers every tool it contains 🧩."""

from gitlab_mcp.tools import ci, labels, mr_create, mr_land, mr_read, mr_update, users, work_items

__all__ = ["ci", "labels", "mr_create", "mr_land", "mr_read", "mr_update", "users", "work_items"]
