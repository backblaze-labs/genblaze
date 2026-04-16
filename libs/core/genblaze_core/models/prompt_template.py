"""PromptTemplate — reusable prompt with {variable} placeholders."""

from __future__ import annotations

import string
from typing import Any

from pydantic import BaseModel


class PromptTemplate(BaseModel):
    """A prompt template with {variable} placeholders for batch workflows.

    Uses Python str.format_map() syntax — no external dependencies.

    Example::

        tpl = PromptTemplate(template="A {animal} in {style} style")
        tpl.render(animal="cat", style="watercolor")  # "A cat in watercolor style"
        tpl.variables  # {"animal", "style"}
    """

    template: str

    @property
    def variables(self) -> set[str]:
        """Return placeholder variable names found in the template."""
        formatter = string.Formatter()
        return {name for _, name, _, _ in formatter.parse(self.template) if name is not None}

    def render(self, **kwargs: Any) -> str:
        """Render the template with the given variables.

        Raises ValueError if required variables are missing.
        Extra variables are silently ignored (useful when one dict
        serves multiple steps with different templates).
        """
        required = self.variables
        missing = required - set(kwargs.keys())
        if missing:
            raise ValueError(f"Missing template variables: {', '.join(sorted(missing))}")
        return self.template.format_map(kwargs)
