"""PromptTemplate — reusable prompt with {variable} placeholders."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

_VARIABLE_PATTERN = re.compile(r"(?<!\{)\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def _unescape_literal_braces(text: str) -> str:
    """Collapse doubled braces in literal template segments."""
    return text.replace("{{", "{").replace("}}", "}")


class PromptTemplate(BaseModel):
    """A prompt template with {variable} placeholders for batch workflows.

    Substitutes only ``{identifier}`` placeholders. Other braces are treated as
    literal prompt text so JSON, code snippets, and dict/set examples render as
    written.

    Example::

        tpl = PromptTemplate(template="A {animal} in {style} style")
        tpl.render(animal="cat", style="watercolor")  # "A cat in watercolor style"
        tpl.variables  # {"animal", "style"}
    """

    template: str

    @property
    def variables(self) -> set[str]:
        """Return placeholder variable names found in the template."""
        return {match.group("name") for match in _VARIABLE_PATTERN.finditer(self.template)}

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

        parts: list[str] = []
        cursor = 0
        for match in _VARIABLE_PATTERN.finditer(self.template):
            parts.append(_unescape_literal_braces(self.template[cursor : match.start()]))
            parts.append(str(kwargs[match.group("name")]))
            cursor = match.end()
        parts.append(_unescape_literal_braces(self.template[cursor:]))
        return "".join(parts)
