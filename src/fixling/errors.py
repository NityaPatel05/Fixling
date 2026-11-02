"""Exception types shared across Fixling."""

from __future__ import annotations


class FixlingError(Exception):
    """Base class for all Fixling errors."""


class LLMUnavailable(FixlingError):
    """LM Studio is unreachable or the configured models are not loaded."""


class PromptTooLarge(FixlingError):
    """A node prompt exceeds ``max_prompt_tokens``. The caller must shrink its context."""


class LLMFormatError(FixlingError):
    """The model returned output that does not validate against the requested schema."""


class BudgetExceeded(FixlingError):
    """A run budget (wall clock, tokens, cancel flag) was exhausted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SandboxError(FixlingError):
    """A sandbox could not be created, used or cleaned up."""


class WorkspaceError(FixlingError):
    """The repo workspace could not be built (unsafe symlink, missing tree, ...)."""


class GitHubError(FixlingError):
    """A GitHub API or git operation failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
