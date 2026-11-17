# Security Policy

## Supported versions

Security fixes ship in the latest release of each package. Upgrade to the
newest umbrella `genblaze` (or the affected package) to receive them.

| Package | Supported |
|---------|-----------|
| `genblaze` 0.4.x, `genblaze-core` 0.3.x and current connector releases | Yes |
| Older releases | No |

## Reporting a vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not** open a public GitHub issue.
2. Use [GitHub Security Advisories](https://github.com/backblaze-labs/genblaze/security/advisories/new) to privately report the vulnerability, including:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
3. You will receive acknowledgment within 48 hours.
4. We will work with you on a fix and coordinate disclosure.

## Security considerations

genblaze handles API tokens and embeds data into media files. Key security boundaries:

- **Provider API tokens** are never stored in manifests or embedded media.
- **`EmbedPolicy`** controls what data gets embedded (prompt redaction, pointer mode).
- **Canonical JSON** ensures hash integrity across serialize/deserialize cycles.
- **Partition paths** in ParquetSink are sanitized to prevent directory traversal,
  and every file it writes or deletes must stay under its `base_dir`.
- **Identity fields** (`run_id`, `parent_run_id`, `tenant_id`, `step_id`,
  `asset_id`) are validated as single path components on every load, because
  they name files and storage keys and are excluded from the canonical hash.
- **File writes** use atomic temp-file-then-rename to prevent corruption.

## Scope

The following are in scope for security reports:

- Token/credential leakage into manifests or embedded media
- Path traversal in file operations
- Hash collision or integrity bypass in canonical JSON
- Injection attacks via manifest content embedded in media formats (XMP, ID3, etc.)
