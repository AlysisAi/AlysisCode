"""Dedicated configured provider ownership for TUI structured reviews."""

from __future__ import annotations

from pathlib import Path

from ...code_review import ChatReviewerClient, CodeReviewEngine, InvalidReviewRequest
from ...config import (
    AppConfig,
    clone_cfg,
    resolve_llm_timeout_s,
    resolve_model_access_api_key,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
)
from ...llm.factory import make_llm_client
from ...model_metadata_policy import ActiveModelRef, evaluate_active_model_metadata_policy
from ...model_registry import ModelRegistry
from ...model_router import ROLE_REVIEW, resolve_model_for_role
from ...profiles import get_active_profile
from ...review_service import ReviewCancellationToken
from ...workspace_context import resolve_workspace_context


def _close_client(client) -> None:
    # Current HTTP clients own request-scoped transports. Accommodate clients
    # with explicit resources without reaching into a shared session/transport.
    close = getattr(client, "close", None)
    if callable(close):
        close()


class DedicatedReviewEngine(CodeReviewEngine):
    def __init__(self, workspace, client, token):
        super().__init__(workspace, ChatReviewerClient(client, cancellation_token=token))
        self.owned_client = client

    def close(self) -> None:
        _close_client(self.owned_client)


def create_review_engine(
    workspace: Path,
    cfg: AppConfig,
    token: ReviewCancellationToken,
    *,
    api_key_override: str | None = None,
) -> DedicatedReviewEngine:
    """Called only by the serialized review worker after explicit Start."""
    token.throw_if_cancelled()
    repository = resolve_workspace_context(workspace).git_root
    if repository is None:
        raise InvalidReviewRequest("Structured review requires a Git repository.")
    snapshot = clone_cfg(cfg)
    profile = get_active_profile(snapshot)
    key = resolve_model_access_api_key(snapshot, override=api_key_override)
    model = resolve_model_for_role(cfg=snapshot, role=ROLE_REVIEW)
    evaluate_active_model_metadata_policy(
        cfg=snapshot,
        registry=ModelRegistry(cfg=snapshot, api_key=key),
        active_models=[ActiveModelRef(role=ROLE_REVIEW, model_name=model)],
    )
    token.throw_if_cancelled()
    client = make_llm_client(
        cfg=snapshot,
        api_key=key,
        model=model,
        profile=profile,
        timeout_s=resolve_llm_timeout_s(snapshot),
        temperature=0.0,
        prompt_cache_key=resolve_prompt_cache_key(snapshot),
        prompt_cache_retention=resolve_prompt_cache_retention(snapshot),
    )
    try:
        token.throw_if_cancelled()
        return DedicatedReviewEngine(repository, client, token)
    except BaseException:
        _close_client(client)
        raise


def dispose_review_engine(engine: CodeReviewEngine) -> None:
    close = getattr(engine, "close", None)
    if callable(close):
        close()
