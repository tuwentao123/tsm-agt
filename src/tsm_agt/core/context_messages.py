"""Identities for request projections; none of these are authored user input."""

GENERATED_CONTEXT_PREFIXES = (
    "project-instructions-context-", "task-spec-context-",
    "conversation-environment-context-", "project-onboarding-context-",
    "project-memory-context-", "session-context-", "document-references-context-",
    "working-memory-context-", "execution-context-",
)


def is_generated_context(message_id: str) -> bool:
    return message_id.startswith(GENERATED_CONTEXT_PREFIXES)


def is_execution_context(message_id: str) -> bool:
    return message_id.startswith("execution-context-")
