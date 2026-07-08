"""PromptTemplate — reusable prompt with {variable} placeholders."""

from __future__ import annotations

import string
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

_FORMATTER = string.Formatter()
_MAX_TEMPLATE_FIELD_LENGTH = 256
_SUPPORTED_CONVERSIONS = {None, "a", "r", "s"}


def _unescape_literal_braces(text: str) -> str:
    """Collapse doubled braces in literal template segments."""
    return text.replace("{{", "{").replace("}}", "}")


def _looks_like_field_start(char: str) -> bool:
    return char == "_" or char.isalpha()


def _parse_single_field(field: str) -> tuple[str, str, str | None]:
    if len(field) > _MAX_TEMPLATE_FIELD_LENGTH:
        raise ValueError(
            "Template field exceeds maximum length "
            f"({_MAX_TEMPLATE_FIELD_LENGTH} characters): {{{field[:32]}...}}"
        )

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

    if not field_name.isidentifier():
        raise ValueError(
            f"Unsupported template field: {text}. PromptTemplate supports only "
            "top-level variables such as {name}; pass flattened values instead "
            "of using attribute or item lookups."
        )

    if conversion not in _SUPPORTED_CONVERSIONS:
        raise ValueError(
            f"Unsupported template conversion in {text}. Supported conversions are !s, !r, and !a."
        )

    if format_spec and ("{" in format_spec or "}" in format_spec):
        raise ValueError(f"Nested template fields in format specs are not supported: {text}")

    return field_name, format_spec or "", conversion


def _field_preview(template: str, start: int) -> str:
    return template[start : start + _MAX_TEMPLATE_FIELD_LENGTH + 1]


def _find_field_end(template: str, start: int) -> int:
    search_from = start + 1
    search_until = min(len(template), start + _MAX_TEMPLATE_FIELD_LENGTH + 2)
    last_error: ValueError | None = None

    while True:
        end = template.find("}", search_from, search_until)
        if end == -1:
            if len(template) - (start + 1) > _MAX_TEMPLATE_FIELD_LENGTH:
                raise ValueError(
                    "Template field exceeds maximum length "
                    f"({_MAX_TEMPLATE_FIELD_LENGTH} characters) starting at: "
                    f"{_field_preview(template, start)}..."
                )
            if last_error is not None:
                raise last_error
            raise ValueError(f"Invalid template field starting at: {template[start:]}")

        try:
            _parse_single_field(template[start + 1 : end])
        except ValueError as exc:
            last_error = exc
            search_from = end + 1
        else:
            return end


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
    name, _, _ = _parse_single_field(field)
    return {name}


def _apply_conversion(value: Any, conversion: str | None) -> Any:
    if conversion == "a":
        return ascii(value)
    if conversion == "r":
        return repr(value)
    if conversion == "s":
        return str(value)
    return value


def _render_field(field: str, kwargs: dict[str, Any]) -> str:
    name, format_spec, conversion = _parse_single_field(field)
    text = "{" + field + "}"
    try:
        value = _apply_conversion(kwargs[name], conversion)
        return format(value, format_spec)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Could not render template field {text}: {exc}") from exc


class PromptTemplate(BaseModel):
    """A prompt template with {variable} placeholders for batch workflows.

    Supports top-level named fields such as ``{name}``, plus safe Python
    format specs and conversions such as ``{price:.2f}`` and ``{name!r}``.
    Attribute and item traversal are rejected; pass flattened values instead.
    Braces that do not start a named field are literal prompt text, which keeps
    JSON/code with quoted keys, whitespace, or punctuation after ``{`` intact.
    Literal text that starts like a field, such as ``{name: "cat"}``, must use
    doubled braces.

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
