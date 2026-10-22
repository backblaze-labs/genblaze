"""Installed package version."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("genblaze-litellm")
except PackageNotFoundError:
    __version__ = "0.0.0"
