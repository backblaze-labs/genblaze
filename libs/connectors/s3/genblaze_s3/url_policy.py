"""Back-compat re-export of :class:`URLPolicy` and :class:`URLPolicyError`.

The enum was originally introduced in this module during the storage-
backend-hardening tranche, then relocated to
``genblaze_core.storage.url_policy`` in 0.3.1 so ``ObjectStorageSink``
(which lives in ``genblaze-core``) could reference it without
``genblaze-core`` developing a circular dependency on ``genblaze-s3``.

External callers importing ``from genblaze_s3.url_policy import URLPolicy``
continue to work — this module simply re-exports the canonical
definition. New code should import from
``genblaze_core.storage.url_policy`` (or the convenience re-export at
``genblaze_core``).
"""

from __future__ import annotations

from genblaze_core.storage.url_policy import URLPolicy, URLPolicyError

__all__ = ["URLPolicy", "URLPolicyError"]
