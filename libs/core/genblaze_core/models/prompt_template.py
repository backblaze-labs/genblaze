"""PromptTemplate — reusable prompt with {variable} placeholders."""

from __future__ import annotations

import string
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

_FORMATTER = string.Formatter()


def _unescape_literal_braces(text: str) -> str:
    """Collapse doubled braces in literal template segments."""
    return text.replace("{{", "{").replace("}}", "}")


def _looks_like_field_start(char: str) -> bool:
    return char == "_" or char.isalpha()


def _parse_single_field(field: str) -> tuple[str, str, str | None]:
    text = "{" + field + "}"
    try:
        parsed = list(_FORMATTER.parse(text))
    except ValueError as exc:
        raise ValueError(f"Invalid template field: {text}") from exc

    if len(parsed) != 1:
        raise ValueError(f"Invalid template field: {text}")

    literal, field_name, format_spec, conversion = parsed[0]
    if literal or field_name is None:
        raise ValueError(f"Invalid template field: {text}")

    root = _root_variable_name(field_name)
    if root is None:
        raise ValueError(
            f"Unsupported template field: {text}. Use a named variable such as {{name}}."
        )

    return root, format_spec or "", conversion


def _root_variable_name(field_name: str) -> str | None:
    stop = len(field_name)
    for delimiter in (".", "["):
        index = field_name.find(delimiter)
        if index != -1:
            stop = min(stop, index)

    root = field_name[:stop]
    if root.isidentifier():
        return root
    return None


def _is_valid_single_field(text: str) -> bool:
    try:
        _parse_single_field(text[1:-1])
    except ValueError:
        return False
    return True


def _find_field_end(template: str, start: int) -> int:
    search_from = start + 1
    while True:
        end = template.find("}", search_from)
        if end == -1:
            raise ValueError(f"Invalid template field starting at: {template[start:]}")

        if _is_valid_single_field(template[start : end + 1]):
            return end

        search_from = end + 1


def _iter_template_parts(template: str) -> Iterator[tuple[str, str]]:
    cursor = 0
    index = 0

    while index < len(template):
        char = template[index]
        if char == "{":
            if index + 1 < len(template) and template[index + 1] == "{":
                index += 2
                continue

            if index + 1 < len(template) and _looks_like_field_start(template[index + 1]):
                end = _find_field_end(template, index)
                yield "literal", _unescape_literal_braces(template[cursor:index])
                yield "field", template[index + 1 : end]
                index = end + 1
                cursor = index
                continue

        index += 1

    yield "literal", _unescape_literal_braces(template[cursor:])


def _field_variables(field: str) -> set[str]:
    root, format_spec, _ = _parse_single_field(field)
    variables = {root}
    for kind, value in _iter_template_parts(format_spec):
        if kind == "field":
            variables.update(_field_variables(value))
    return variables


def _render_field(field: str, kwargs: dict[str, Any]) -> str:
    text = "{" + field + "}"
    try:
        return _FORMATTER.vformat(text, (), kwargs)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Could not render template field {text}: {exc}") from exc


class PromptTemplate(BaseModel):
    """A prompt template with {variable} placeholders for batch workflows.

    Supports named Python format fields such as ``{name}``, ``{price:.2f}``,
    ``{user.name}``, and ``{items[0]}``. Braces that do not start a named field
    are literal prompt text, which keeps JSON/code with quoted keys, whitespace,
    or punctuation after ``{`` intact. Literal text that starts like a field,
    such as ``{name: "cat"}``, must use doubled braces.

    Example::

        tpl = PromptTemplate(template="A {animal} in {style} style")
        tpl.render(animal="cat", style="watercolor")  # "A cat in watercolor style"
        tpl.variables  # {"animal", "style"}
    """

    template: str

    @property
    def variables(self) -> set[str]:
        """Return placeholder variable names found in the template."""
        variables: set[str] = set()
        for kind, value in _iter_template_parts(self.template):
            if kind == "field":
                variables.update(_field_variables(value))
        return variables

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

        rendered: list[str] = []
        for kind, value in _iter_template_parts(self.template):
            if kind == "literal":
                rendered.append(value)
            else:
                rendered.append(_render_field(value, kwargs))
        return "".join(rendered)
