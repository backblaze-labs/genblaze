"""S3StorageBackend — works with any S3-compatible service (B2, R2, MinIO, AWS)."""

from __future__ import annotations

import io
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, BinaryIO

from botocore.exceptions import ClientError
from genblaze_core._version import __version__
from genblaze_core.exceptions import StorageError
from genblaze_core.storage.base import StorageBackend

from genblaze_s3.encryption import Encryption
from genblaze_s3.presigned import PresignedURL
from genblaze_s3.url_policy import URLPolicy, URLPolicyError

if TYPE_CHECKING:
    pass

logger = logging.getLogger("genblaze.s3")

# Sentinel used by ``get_url`` to distinguish "caller passed the default
# value 3600" from "caller didn't pass anything". The PUBLIC URLPolicy
# branch needs the distinction so passing ``expires_in`` *explicitly*
# while requesting a public URL raises (URLs from public_url_base
# don't carry an expiry); leaving it unset is the no-conflict case.
_EXPIRES_IN_UNSET: Any = object()
_DEFAULT_EXPIRES_IN_SEC = 3600

# User agent for B2/S3 API tracking
_USER_AGENT = f"b2ai-genblaze/{__version__}"

# Single-PUT vs multipart cutoff. Above this, boto3 splits into 16 MB parts
# and uploads up to _MAX_CONCURRENCY in parallel — each part individually
# retryable, which matters for multi-GB video payloads on flaky links.
_MULTIPART_THRESHOLD = 16 * 1024 * 1024
_MULTIPART_CHUNKSIZE = 16 * 1024 * 1024
_MAX_CONCURRENCY = 4

# urllib3 connection pool ceiling. With 4 asset workers × 4 part workers we
# can saturate 16 concurrent connections; headroom for HEAD/auth prefetch.
_MAX_POOL_CONNECTIONS = 20

# Lifecycle defaults applied by ensure_lifecycle_defaults() / auto_lifecycle=True.
# Orphaned multipart uploads from mid-stream failures otherwise sit billable
# indefinitely — a real cost vector once the multipart path is the default.
_DEFAULT_CANCEL_MULTIPART_DAYS = 7
_DEFAULT_NONCURRENT_EXPIRE_DAYS = 30


def _build_boto_config() -> Any:
    """BotoConfig factory — shared by the client constructor and reconfigure path.

    ``request/response_checksum_calculation="when_required"`` disables the
    default CRC32 trailer that boto3 >= 1.36 injects. That header broke B2
    uploads until B2 added support in July 2025; keeping it off avoids the
    landmine across any S3-compat endpoint that lags on checksum-header
    support (older B2, MinIO, Wasabi). We pass SHA-256 explicitly per upload
    via ExtraArgs instead.

    Explicit connect/read timeouts exist because boto3's 60s default
    read_timeout can fire mid-upload on slow links with GB-sized video payloads.
    """
    from botocore.config import Config as BotoConfig

    return BotoConfig(
        user_agent_extra=_USER_AGENT,
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=30,
        read_timeout=300,
        max_pool_connections=_MAX_POOL_CONNECTIONS,
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )


class S3StorageBackend(StorageBackend):
    """S3-compatible object storage backend.

    Supports Backblaze B2, Cloudflare R2, MinIO, and AWS S3 via endpoint_url.

    Args:
        bucket: S3 bucket name.
        endpoint_url: S3-compatible endpoint (e.g. "https://s3.us-west-004.backblazeb2.com").
            None for standard AWS S3.
        region: AWS region name (e.g. "us-west-004").
        public_url_base: Base URL for public/friendly URLs (e.g. B2 friendly URL).
            If set, get_url() returns {public_url_base}/{key} instead of pre-signed URLs.
        aws_access_key_id: Override credentials (else uses boto3 defaults).
        aws_secret_access_key: Override credentials.
        access_key_id: Alias of ``aws_access_key_id``. Accepts either name —
            the README and several ecosystem examples use the unprefixed
            form, but boto3's native kwarg uses the ``aws_`` prefix. Passing
            both raises ``TypeError``; no silent precedence.
        secret_access_key: Alias of ``aws_secret_access_key`` (same rules).
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        public_url_base: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ):
        # Resolve aliases. Both names refer to the same boto3 credential —
        # passing both is a sign of caller confusion (which one wins?), so
        # we raise rather than silently picking one. Closes bug #10.
        resolved_access_key = self._resolve_alias(
            "aws_access_key_id",
            aws_access_key_id,
            "access_key_id",
            access_key_id,
        )
        resolved_secret_key = self._resolve_alias(
            "aws_secret_access_key",
            aws_secret_access_key,
            "secret_access_key",
            secret_access_key,
        )

        self._bucket = bucket
        self._public_url_base = public_url_base.rstrip("/") if public_url_base else None
        # Region may be updated later by auto-detection (see _ensure_region_verified).
        self._region = region
        self._region_verified = False
        # Serializes first-use preflight. ObjectStorageSink calls put() from a
        # ThreadPoolExecutor, so multiple workers race through the preflight
        # on a fresh backend. Without the lock we'd issue N HEADs and N
        # client rebuilds instead of 1.
        self._region_lock = threading.Lock()
        # Sticky non-redirect preflight failure (bad creds, wrong bucket, etc.)
        # — cache once so call 2+ gets the same helpful message as call 1
        # without re-HEADing.
        self._preflight_error: StorageError | None = None
        # boto3's Config object does NOT hold credentials (they live in the
        # session's resolver), so we can't recover them from self._client
        # after construction — persist them explicitly for _reconfigure_for_region.
        self._aws_access_key_id = resolved_access_key
        self._aws_secret_access_key = resolved_secret_key
        self._endpoint_url = endpoint_url

        try:
            import boto3
        except ImportError as exc:
            raise ImportError(
                "boto3 is required for S3StorageBackend. "
                'Install it with: pip install "genblaze-s3"'
            ) from exc

        self._client = boto3.client("s3", **self._client_kwargs())
        self._transfer_config = self._build_transfer_config()

    @staticmethod
    def _resolve_alias(
        primary_name: str,
        primary_value: str | None,
        alias_name: str,
        alias_value: str | None,
    ) -> str | None:
        """Resolve a kwarg + its alias; raise if both passed.

        ``access_key_id`` is documented as an alias of ``aws_access_key_id``
        (the README uses the short form; boto3 expects the ``aws_`` prefix).
        Accept either, but raise ``TypeError`` when both are passed —
        silent precedence between two names for the same value is a
        debugging trap.
        """
        if primary_value is not None and alias_value is not None:
            raise TypeError(
                f"S3StorageBackend received both {primary_name}= and {alias_name}=; "
                f"these are aliases — pass only one."
            )
        return primary_value if primary_value is not None else alias_value

    @property
    def _is_b2(self) -> bool:
        """True when the endpoint points at B2 — gates B2-specific behaviors."""
        return bool(self._endpoint_url and "backblazeb2.com" in self._endpoint_url)

    def _client_kwargs(self) -> dict[str, Any]:
        """Assemble boto3.client kwargs from current instance state."""
        kwargs: dict[str, Any] = {"config": _build_boto_config()}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._region:
            kwargs["region_name"] = self._region
        if self._aws_access_key_id:
            kwargs["aws_access_key_id"] = self._aws_access_key_id
        if self._aws_secret_access_key:
            kwargs["aws_secret_access_key"] = self._aws_secret_access_key
        return kwargs

    @staticmethod
    def _build_transfer_config() -> Any:
        """TransferConfig factory — shared by __init__ and _reconfigure_for_region."""
        from boto3.s3.transfer import TransferConfig

        return TransferConfig(
            multipart_threshold=_MULTIPART_THRESHOLD,
            multipart_chunksize=_MULTIPART_CHUNKSIZE,
            max_concurrency=_MAX_CONCURRENCY,
            use_threads=True,
        )

    def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        extra_args: dict[str, Any] | None = None,
        encryption: Encryption | None = None,
    ) -> str:
        """Upload an object to S3. Returns the storage key.

        Uses boto3's managed ``upload_fileobj`` so large payloads are
        automatically split into multipart uploads (16 MB parts, 4-way
        parallel) and each part is individually retryable. Per-part SHA-256
        checksums are negotiated server-side via ``ChecksumAlgorithm``.

        If the caller pins an explicit ``ChecksumSHA256`` via ``extra_args``,
        we route through ``put_object`` (single-PUT) instead — whole-object
        SHA-256 checksums are only valid on single-part uploads. Callers
        that need whole-object verification on large payloads should
        compute and pass it with a ``bytes`` payload under the multipart
        threshold, or omit and rely on the per-part default.

        **Return shape changed in 0.3.0:** previously returned a presigned
        URL via :meth:`get_url` for the just-uploaded object. That shape
        leaked the access-key-id into anything that persisted the value
        and broke canonical-hash stability for CAS layouts. The current
        return is the storage key — call :meth:`get_durable_url` on it
        to get a credential-free URL safe to persist.
        """
        # Validate caller kwargs BEFORE the network try/except wrapper.
        # ``_build_extra_args`` may raise ``ValueError`` for caller API
        # misuse (e.g. SSE envelope conflict between ``encryption=`` and
        # overlapping ``extra_args``). API misuse should propagate as
        # ``ValueError``, not get masked as ``StorageError`` — those have
        # different debugging semantics for the caller.
        merged = self._build_extra_args(content_type, metadata, extra_args, encryption)
        try:
            self._ensure_region_verified()
            if "ChecksumSHA256" in merged:
                return self._put_single(key, data, merged)
            stream: BinaryIO = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
            self._client.upload_fileobj(
                stream,
                self._bucket,
                key,
                ExtraArgs=merged,
                Config=self._transfer_config,
            )
            return key
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 put failed for {key}: {exc}") from exc

    def _put_single(
        self,
        key: str,
        data: bytes | BinaryIO,
        extra_args: dict[str, Any],
    ) -> str:
        """Single-PUT path used when the caller pinned a whole-object checksum."""
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, **extra_args)
        return key

    # Boto3 keys that participate in the SSE envelope. Any overlap between
    # an ``Encryption`` value object and a caller's ``extra_args`` produces a
    # mismatched envelope (e.g. wrong KMS key, missing customer-key MD5)
    # which silently encrypts the object with the wrong material — the
    # request still succeeds at the API level, so callers wouldn't notice
    # until they try to decrypt. Detect the overlap and raise upfront.
    _SSE_KEYS_FROZEN: frozenset[str] = frozenset(
        {
            "ServerSideEncryption",
            "SSEKMSKeyId",
            "SSEKMSEncryptionContext",
            "SSECustomerAlgorithm",
            "SSECustomerKey",
            "SSECustomerKeyMD5",
            "BucketKeyEnabled",
        }
    )

    @classmethod
    def _build_extra_args(
        cls,
        content_type: str | None,
        metadata: dict[str, str] | None,
        extra_args: dict[str, Any] | None,
        encryption: Encryption | None = None,
    ) -> dict[str, Any]:
        """Merge kwargs into a boto3-style ExtraArgs dict, honoring caller overrides.

        Precedence (low → high): encryption value-object → caller
        ``extra_args`` → built-in defaults (ContentType / Metadata /
        ChecksumAlgorithm). The high end winning matches the historic
        contract for non-SSE keys: an explicit
        ``extra_args={"CacheControl": "..."}`` continues to override.

        **SSE keys are special**: an ``encryption=`` value object plus an
        overlapping ``extra_args`` SSE key produces a mismatched envelope
        (wrong KMS key, partial customer-key state, etc.). Rather than
        silently encrypting with the wrong material, this path raises
        ``ValueError`` so the caller can pick exactly one source of
        truth for the SSE envelope.
        """
        if encryption is not None and extra_args:
            overlap = cls._SSE_KEYS_FROZEN & extra_args.keys()
            if overlap:
                raise ValueError(
                    "S3StorageBackend.put: SSE envelope conflict — "
                    f"`encryption=` is set AND `extra_args` overlaps SSE "
                    f"keys {sorted(overlap)}. Pass exactly one source for "
                    "the SSE envelope; mixing them silently encrypts with "
                    "the wrong material on partial-override scenarios."
                )
        merged: dict[str, Any] = {}
        if encryption is not None:
            merged.update(encryption.to_put_extra_args())
        if content_type:
            merged["ContentType"] = content_type
        if metadata:
            merged["Metadata"] = metadata
        # Caller-provided non-SSE keys win (e.g. ChecksumAlgorithm,
        # CacheControl, CopySource — anything not in the SSE frozen set).
        if extra_args:
            merged.update(extra_args)
        # Default to SHA-256 per-part integrity unless caller pinned a checksum.
        if "ChecksumAlgorithm" not in merged and "ChecksumSHA256" not in merged:
            merged["ChecksumAlgorithm"] = "SHA256"
        return merged

    def get(self, key: str, *, encryption: Encryption | None = None) -> bytes:
        """Download an object from S3.

        ``encryption`` is required for SSE-C-encrypted objects (the
        same customer key + MD5 the put used). SSE-S3 / SSE-KMS objects
        decrypt server-side and don't need anything on the read path.

        Phase 1D adds the kwarg to close bug #3's read-side asymmetry —
        previously SSE-C uploads silently failed to round-trip because
        ``get_object`` was never plumbed with the customer key.
        """
        extra: dict[str, Any] = {}
        if encryption is not None:
            extra.update(encryption.to_get_extra_args())
        try:
            self._ensure_region_verified()
            resp = self._client.get_object(Bucket=self._bucket, Key=key, **extra)
            return resp["Body"].read()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 get failed for {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        """Check if an object exists in S3.

        Treats 404 and 403/AccessDenied as "does not exist". Scoped B2/AWS
        application keys commonly have ReadFiles without ListFiles, which
        returns 403 for HEAD on non-existent keys. Raising would break CAS
        dedup for least-privilege credentials — we log at DEBUG so real
        permission failures still leave a breadcrumb.
        """
        try:
            self._ensure_region_verified()
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "404":
                return False
            if code in ("403", "AccessDenied"):
                logger.debug(
                    "head_object %s returned 403/AccessDenied — treating as not-exist. "
                    "If the key should be visible, check bucket/prefix permissions.",
                    key,
                )
                return False
            raise StorageError(f"S3 exists check failed for {key}: {exc}") from exc
        except StorageError:
            raise

    def delete(self, key: str) -> None:
        """Delete an object from S3."""
        try:
            self._ensure_region_verified()
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 delete failed for {key}: {exc}") from exc

    def copy(self, src_key: str, dst_key: str, *, encryption: Encryption | None = None) -> None:
        """Server-side copy — bytes never transit the client.

        Used by the pipelined CAS transfer path to promote a temp upload
        to its content-addressed final key once the hash is known. B2's
        S3 API supports this natively and charges nothing for server-side
        bandwidth (just two transaction class-C calls).

        ``encryption`` re-applies the same SSE config to the destination.
        For SSE-C the value object also supplies the
        ``CopySourceSSECustomerKey``-shaped kwargs that S3 needs to read
        the source object (see :meth:`Encryption.to_copy_extra_args`).

        Note: single-call ``copy_object`` has a **5 GB source limit**
        per AWS S3 semantics (B2 matches). Objects larger than that
        require ``UploadPartCopy`` multipart orchestration — future
        work. Until then, assets approaching the 5 GB
        ``_DEFAULT_MAX_DOWNLOAD_BYTES`` ceiling should prefer
        HIERARCHICAL key strategy with pipelined_transfer, which
        skips the copy step entirely.
        """
        copy_kwargs: dict[str, Any] = {}
        if encryption is not None:
            copy_kwargs.update(encryption.to_copy_extra_args())
        try:
            self._ensure_region_verified()
            self._client.copy_object(
                Bucket=self._bucket,
                Key=dst_key,
                CopySource={"Bucket": self._bucket, "Key": src_key},
                **copy_kwargs,
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 copy failed for {src_key} -> {dst_key}: {exc}") from exc

    def get_url(
        self,
        key: str,
        *,
        expires_in: int = _EXPIRES_IN_UNSET,  # type: ignore[assignment]
        policy: URLPolicy = URLPolicy.AUTO,
    ) -> str:
        """Get a short-lived URL for the object — flavor selected by ``policy``.

        Args:
            key: The storage key.
            expires_in: Seconds until the presigned URL expires. Default
                is 3600 in the PRESIGNED and AUTO-presigned paths.
                Cannot be passed explicitly when ``policy=URLPolicy.PUBLIC``
                — public URLs don't carry an expiry; the conflict raises
                :class:`URLPolicyError` rather than silently being
                ignored.
            policy: One of :class:`URLPolicy.AUTO` (default — public when
                ``public_url_base`` is set, presigned otherwise),
                :class:`URLPolicy.PUBLIC` (force public; requires
                ``public_url_base``), or :class:`URLPolicy.PRESIGNED`
                (force a SigV4 presigned URL even if ``public_url_base``
                is configured).

        Returns:
            URL string. Do NOT persist the result if it's a presigned URL —
            presigned URLs embed the access-key-id in ``X-Amz-Credential``
            and break canonical-hash stability if hashed. Use
            :meth:`get_durable_url` for the persistable form.

        **Phase 1D fixes for bug #2** (silent precedence) **and bug #7**
        (HeadBucket on every public URL): the public-URL path no longer
        triggers a region-verify, and the conflict between an explicit
        ``expires_in`` and a public URL is now a typed error.
        """
        explicit_expires = expires_in is not _EXPIRES_IN_UNSET
        resolved_expires = expires_in if explicit_expires else _DEFAULT_EXPIRES_IN_SEC

        # ----- Resolve effective branch ----------------------------------
        if policy is URLPolicy.PUBLIC:
            if not self._public_url_base:
                raise URLPolicyError(
                    "URLPolicy.PUBLIC requires public_url_base on the backend; "
                    "none configured. Pass policy=URLPolicy.PRESIGNED or omit "
                    "policy= to fall back to AUTO."
                )
            if explicit_expires:
                raise URLPolicyError(
                    "URLPolicy.PUBLIC does not honor expires_in — public URLs "
                    "served via public_url_base do not carry an expiry. Drop "
                    "expires_in= or use policy=URLPolicy.PRESIGNED if you need "
                    "a time-limited URL."
                )
            return self._build_public_url(key)

        if policy is URLPolicy.PRESIGNED:
            return self._build_presigned_url(key, resolved_expires)

        # AUTO
        if self._public_url_base:
            # Historical behavior preserved: AUTO ignores expires_in when
            # public_url_base is set. Callers that want a strict
            # honor-expires-in error should pass policy=URLPolicy.PUBLIC
            # (raises on conflict) or policy=URLPolicy.PRESIGNED (always
            # honors).
            return self._build_public_url(key)
        return self._build_presigned_url(key, resolved_expires)

    def _build_public_url(self, key: str) -> str:
        """Render ``{public_url_base}/{key}`` with safe URL-encoding.

        No region-verify call here — that was the bug #7 hot-path
        regression; signing public URLs is pure string concatenation.
        """
        # Both call sites in ``get_url`` already guard ``_public_url_base``
        # before reaching here. The redundant check is intentional: an
        # ``assert`` would be stripped by ``python -O`` and a future
        # refactor that bypasses one of the call-site guards would
        # silently produce ``None/key`` URLs.
        if self._public_url_base is None:
            raise URLPolicyError(
                "_build_public_url reached without public_url_base set; "
                "this is a bug in the call-site dispatch logic."
            )
        from urllib.parse import quote

        return f"{self._public_url_base}/{quote(key, safe='/')}"

    def _build_presigned_url(self, key: str, expires_in: int) -> str:
        """Render a SigV4 GET URL via boto3."""
        try:
            self._ensure_region_verified()
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 get_url failed for {key}: {exc}") from exc

    def presigned_get(
        self, key: str, *, expires_in: int = _DEFAULT_EXPIRES_IN_SEC
    ) -> PresignedURL:
        """Return a typed, redaction-safe presigned GET URL for ``key``.

        Use this instead of ``get_url(policy=URLPolicy.PRESIGNED)`` when
        you want the URL value to default-redact in logs / repr / str
        (the :class:`PresignedURL` value object strips
        ``X-Amz-Signature`` / ``X-Amz-Credential`` from its formatted
        output). Access the unredacted URL via the ``.url`` attribute
        when handing it to an HTTP client — that makes every
        unredacted-leak site a deliberate decision rather than a default
        string interpolation.

        Args:
            key: The storage key to sign a fetch URL for.
            expires_in: Seconds until expiry. Defaults to 3600 (1h).
        """
        try:
            self._ensure_region_verified()
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 presigned_get failed for {key}: {exc}") from exc
        return PresignedURL(
            url=url,
            method="GET",
            key=key,
            bucket=self._bucket,
            expires_in=expires_in,
        )

    def presigned_put(
        self,
        key: str,
        *,
        expires_in: int = _DEFAULT_EXPIRES_IN_SEC,
        content_type: str | None = None,
    ) -> PresignedURL:
        """Return a typed, redaction-safe presigned PUT URL for ``key``.

        SigV4 binds the ``Content-Type`` header into the signature when
        present in ``Params`` — the upload must send the same value or
        the signature check fails. Pass ``content_type=`` here to lock
        it in; omit to let the upload pick.

        Args:
            key: The storage key to sign an upload URL for.
            expires_in: Seconds until expiry. Defaults to 3600 (1h).
            content_type: Optional Content-Type to bind into the
                signature. If set, the upload MUST send this exact
                ``Content-Type`` header.
        """
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if content_type is not None:
            params["ContentType"] = content_type
        try:
            self._ensure_region_verified()
            url = self._client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=expires_in,
            )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"S3 presigned_put failed for {key}: {exc}") from exc
        return PresignedURL(
            url=url,
            method="PUT",
            key=key,
            bucket=self._bucket,
            expires_in=expires_in,
        )

    def get_durable_url(self, key: str) -> str:
        """Return a credential-free, never-expiring URL safe to persist.

        Uses ``public_url_base`` when set (e.g. B2 friendly URL).
        Otherwise builds the canonical S3 path from the verified endpoint.
        The URL alone does not grant access — consumers either rely on
        public bucket read or call :meth:`get_url` later for a presigned
        fetch. This is what gets written into ``asset.url`` so manifests
        and embedded media never carry SigV4 signatures.
        """
        from urllib.parse import quote

        encoded = quote(key, safe="/")
        if self._public_url_base:
            return f"{self._public_url_base}/{encoded}"
        # Verify region first so endpoint_url is the correct one (B2 buckets
        # may live in a different region than the constructor hint).
        self._ensure_region_verified()
        endpoint = (self._client.meta.endpoint_url or "").rstrip("/")
        return f"{endpoint}/{self._bucket}/{encoded}"

    def key_from_url(self, url: str) -> str | None:
        """Inverse of :meth:`get_durable_url` — None for foreign URLs.

        Recognizes both URL shapes the backend can emit:

        * ``{public_url_base}/{key}`` (Cloudflare CDN / friendly-URL setups)
        * ``{endpoint}/{bucket}/{key}`` (raw S3-compatible endpoint)

        Both shapes are tried regardless of the current ``public_url_base``
        setting — a URL written when ``public_url_base`` was set still
        round-trips after it's been removed (and vice versa), as long as
        host+bucket still match.

        Returns ``None`` for URLs that clearly belong elsewhere (different
        host, different bucket, malformed) so callers can route across
        backends without try/except gymnastics.
        """
        from urllib.parse import unquote, urlparse

        # Public-base shape — tried first because public_url_base may have
        # been set when the URL was written even if it's None now.
        if self._public_url_base and url.startswith(self._public_url_base + "/"):
            return unquote(url[len(self._public_url_base) + 1 :])

        # Raw endpoint shape: {endpoint}/{bucket}/{key}.
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None
        self._ensure_region_verified()
        endpoint_host = urlparse(self._client.meta.endpoint_url or "").netloc
        if parsed.netloc != endpoint_host:
            return None
        path = parsed.path.lstrip("/")
        bucket_prefix = self._bucket + "/"
        if not path.startswith(bucket_prefix):
            return None
        return unquote(path[len(bucket_prefix) :])

    def _ensure_region_verified(self) -> None:
        """Lazy-verify the bucket region on first use; follow redirect if wrong.

        B2 (and AWS) return 301 ``PermanentRedirect`` with an
        ``x-amz-bucket-region`` header when the client is pointed at the
        wrong region. For B2 endpoints we rewrite to the correct B2 regional
        endpoint — users stop needing to hand-pick the right ``region=`` on
        ``for_backblaze``. For other endpoints (AWS S3, R2, MinIO) we don't
        attempt to rebuild the endpoint URL since we can't synthesize it;
        we just surface the error with guidance.

        Uses double-checked locking so concurrent first-use callers (the
        ``ObjectStorageSink`` thread pool) only run preflight once.
        """
        # Fast path — uncontended attribute read, no lock.
        if self._region_verified:
            if self._preflight_error is not None:
                raise self._preflight_error
            return

        with self._region_lock:
            # Second check after acquiring the lock — another thread may
            # have verified while we were waiting.
            if self._region_verified:
                if self._preflight_error is not None:
                    raise self._preflight_error
                return

            try:
                self._client.head_bucket(Bucket=self._bucket)
                self._region_verified = True
                return
            except ClientError as exc:
                from genblaze_s3._preflight_classify import is_sticky_preflight_error

                actual = (
                    exc.response.get("ResponseMetadata", {})
                    .get("HTTPHeaders", {})
                    .get("x-amz-bucket-region")
                )
                code = exc.response.get("Error", {}).get("Code")
                is_redirect = code in {"301", "PermanentRedirect"}
                if self._is_b2 and is_redirect and actual and actual != self._region:
                    logger.info(
                        "Bucket %s lives in %s (client was pointed at %s); reconfiguring.",
                        self._bucket,
                        actual,
                        self._region,
                    )
                    self._reconfigure_for_region(actual)
                    self._region_verified = True
                    return
                err = StorageError(
                    f"Bucket {self._bucket!r} preflight failed: {exc}. "
                    "Check bucket name, region, and credentials."
                )
                if is_sticky_preflight_error(exc):
                    # Sticky failure (bad creds, missing bucket, sig mismatch):
                    # cache once and re-raise the same helpful message on every
                    # subsequent call. Avoids the repeated HEAD cost and the
                    # inconsistent (helpful-on-call-1, raw-on-call-2+) error
                    # the previous implementation produced.
                    self._preflight_error = err
                    self._region_verified = True
                else:
                    # Transient failure (5xx, throttle, network blip): do NOT
                    # cache — the next call will retry the HeadBucket and may
                    # succeed. The retry helper / outer ObjectStorageSink
                    # thread pool gets a fair shot at the upstream recovery.
                    logger.info(
                        "Bucket %s preflight got transient %r — leaving "
                        "unverified so the next call retries.",
                        self._bucket,
                        code,
                    )
                raise err from exc

    def _reconfigure_for_region(self, region: str) -> None:
        """Rebuild the boto3 client for a different B2 region.

        Called only for B2 endpoints (see ``_is_b2``) — synthesizes the
        correct B2 regional endpoint URL. Credentials come from the
        instance (stored in ``__init__``) since ``boto3.client.meta.config``
        does not hold them.
        """
        import boto3

        self._region = region
        self._endpoint_url = f"https://s3.{region}.backblazeb2.com"
        self._client = boto3.client("s3", **self._client_kwargs())
        self._transfer_config = self._build_transfer_config()

    def ensure_lifecycle_defaults(
        self,
        *,
        cancel_multipart_after_days: int = _DEFAULT_CANCEL_MULTIPART_DAYS,
        noncurrent_version_expire_days: int | None = _DEFAULT_NONCURRENT_EXPIRE_DAYS,
    ) -> None:
        """Apply idempotent lifecycle rules tuned for a genblaze workload.

        Orphaned multipart uploads from failed video transfers otherwise
        accumulate billable storage forever. Noncurrent-version expiry
        tidies the per-run manifest history that B2's always-on versioning
        creates when manifests are rewritten.

        Pass ``noncurrent_version_expire_days=None`` to keep all manifest
        versions forever (full provenance history, higher storage cost).
        """
        rules: list[dict[str, Any]] = [
            {
                "ID": "genblaze-cancel-unfinished-multipart",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": cancel_multipart_after_days
                },
            }
        ]
        if noncurrent_version_expire_days is not None:
            rules.append(
                {
                    "ID": "genblaze-expire-noncurrent-versions",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "NoncurrentVersionExpiration": {
                        "NoncurrentDays": noncurrent_version_expire_days
                    },
                }
            )
        try:
            self._client.put_bucket_lifecycle_configuration(
                Bucket=self._bucket,
                LifecycleConfiguration={"Rules": rules},
            )
            logger.info("Applied %d lifecycle rule(s) to bucket %s", len(rules), self._bucket)
        except Exception as exc:
            # Non-fatal — users with read-only keys or managed infra should
            # still be able to upload. Log and continue.
            logger.warning("Failed to apply lifecycle defaults to %s: %s", self._bucket, exc)

    @classmethod
    def for_backblaze(
        cls,
        bucket: str | None = None,
        *,
        region: str | None = None,
        key_id: str | None = None,
        app_key: str | None = None,
        public_url_base: str | None = None,
        auto_lifecycle: bool = False,
        preflight: bool = True,
    ) -> S3StorageBackend:
        """Construct an S3StorageBackend preconfigured for Backblaze B2.

        Derives B2's S3 endpoint from ``region`` and falls back to the
        ``B2_BUCKET`` / ``B2_REGION`` / ``B2_KEY_ID`` / ``B2_APP_KEY``
        environment variables when arguments are not passed explicitly.
        Raises ``ValueError`` if bucket or credentials are missing entirely
        — prefer a clear error at construction over a cryptic
        ``NoCredentialsError`` mid-upload.

        The first ``put()`` / ``exists()`` call auto-detects the bucket's
        actual region when B2 returns a redirect; passing ``region=`` (or
        setting ``$B2_REGION``) is an optimization hint. Regions that reject
        cross-region requests with 403 instead of 301 (e.g. ``us-east-005``)
        must still be specified — auto-detect can't read a header that isn't
        sent.

        **Default change in 0.3.0:** ``auto_lifecycle`` now defaults to
        ``False``. The previous default applied bucket-wide lifecycle rules
        on every construction — a hidden side effect that could surprise
        callers managing lifecycle out-of-band (Terraform, console, IaC).
        Pass ``auto_lifecycle=True`` explicitly to opt in, or call
        :meth:`ensure_lifecycle_defaults` after construction for the same
        effect with explicit intent. Preflight failures now raise instead
        of warning-and-continuing — placeholder credentials no longer
        construct a "working" backend that fails on first I/O.

        Args:
            bucket: B2 bucket name. Defaults to ``$B2_BUCKET``.
            region: B2 region slug (e.g. "us-west-004", "us-east-005",
                "eu-central-003"). Defaults to ``$B2_REGION``, then
                ``"us-west-004"``. Auto-corrected on first use if B2
                returns a redirect.
            key_id: B2 application key ID. Defaults to ``$B2_KEY_ID``.
            app_key: B2 application key. Defaults to ``$B2_APP_KEY``.
            public_url_base: Optional B2 friendly-URL base for public buckets,
                e.g. ``"https://f004.backblazeb2.com/file/my-bucket"``. When
                set, :meth:`get_url` returns these instead of pre-signed URLs.
            auto_lifecycle: When True, apply recommended lifecycle rules on
                construction — cancel orphaned multipart uploads after 7
                days and expire noncurrent manifest versions after 30
                days. **Default False as of 0.3.0** (was True). Requires
                ``preflight=True``.
            preflight: When True (default), verify bucket region on
                construction and raise on auth/region failure. Set False
                for offline tests or placeholder credentials — defers
                the verify to the first real I/O call. Cannot be
                combined with ``auto_lifecycle=True`` (lifecycle requires
                a verified region).

        Example::

            # All config from environment (B2_BUCKET, B2_REGION, B2_KEY_ID, B2_APP_KEY)
            backend = S3StorageBackend.for_backblaze()
            # Or pass explicitly + opt into lifecycle defaults:
            backend = S3StorageBackend.for_backblaze(
                "my-bucket", region="us-east-005", auto_lifecycle=True,
            )
            sink = ObjectStorageSink(backend, key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
        """
        if not preflight and auto_lifecycle:
            raise ValueError(
                "for_backblaze: preflight=False and auto_lifecycle=True are "
                "incompatible — lifecycle application requires a verified region. "
                "Either keep preflight=True, or apply lifecycle later via "
                "backend.ensure_lifecycle_defaults() once the bucket is reachable."
            )
        resolved_bucket = bucket or os.environ.get("B2_BUCKET")
        if not resolved_bucket:
            raise ValueError(
                "Backblaze B2 bucket missing. Set B2_BUCKET environment "
                "variable, or pass bucket= explicitly to for_backblaze()."
            )
        resolved_region = region or os.environ.get("B2_REGION") or "us-west-004"
        resolved_key = key_id or os.environ.get("B2_KEY_ID")
        resolved_secret = app_key or os.environ.get("B2_APP_KEY")
        if not resolved_key or not resolved_secret:
            raise ValueError(
                "Backblaze B2 credentials missing. Set B2_KEY_ID / B2_APP_KEY "
                "environment variables, or pass key_id= and app_key= "
                "explicitly to for_backblaze()."
            )
        backend = cls(
            bucket=resolved_bucket,
            endpoint_url=f"https://s3.{resolved_region}.backblazeb2.com",
            region=resolved_region,
            public_url_base=public_url_base,
            aws_access_key_id=resolved_key,
            aws_secret_access_key=resolved_secret,
        )
        if not preflight:
            # Caller opted out — leave the verify-on-first-use machinery
            # alone so a real I/O call later still surfaces auth/region
            # failures with the usual error path.
            return backend
        # preflight=True: verify region (raises StorageError on failure
        # instead of warn-and-continue) and optionally apply lifecycle.
        backend._ensure_region_verified()
        if auto_lifecycle:
            backend.ensure_lifecycle_defaults()
        return backend
