"""Embed policy model — controls manifest redaction for embedding."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from genblaze_core.models.enums import PromptVisibility


class EmbedPolicy(BaseModel):
    prompt_visibility: PromptVisibility = PromptVisibility.PUBLIC
    embed_mode: Literal["full", "pointer", "none"] = "full"
    include_params: bool = True
    include_seed: bool = True
