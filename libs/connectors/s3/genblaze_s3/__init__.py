"""S3-compatible storage backend for genblaze (B2, R2, MinIO, AWS S3)."""

from genblaze_s3.backend import S3StorageBackend
from genblaze_s3.encryption import Encryption, EncryptionMode
from genblaze_s3.presigned import PresignedURL
from genblaze_s3.url_policy import URLPolicy, URLPolicyError

from ._version import __version__  # noqa: F401 — re-exported

# AsyncS3StorageBackend is exposed from this module via lazy ``__getattr__``
# so the bare ``import genblaze_s3`` doesn't pay the import cost (or
# fail with ImportError) on minimal installs without the ``[async]``
# extra. Callers reach it through ``from genblaze_s3 import
# AsyncS3StorageBackend`` which triggers the lazy load.
_LAZY_ATTRS = {
    "AsyncS3StorageBackend": ("genblaze_s3.async_backend", "AsyncS3StorageBackend"),
}


def __getattr__(name: str):  # PEP 562 module-level lazy attribute access
    if name in _LAZY_ATTRS:
        module_path, attr = _LAZY_ATTRS[name]
        import importlib

        mod = importlib.import_module(module_path)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'genblaze_s3' has no attribute {name!r}")


__all__ = [
    "S3StorageBackend",
    "URLPolicy",
    "URLPolicyError",
    "Encryption",
    "EncryptionMode",
    "PresignedURL",
    "AsyncS3StorageBackend",
]
