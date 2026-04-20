# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not** open a public GitHub issue.
2. Use [GitHub Security Advisories](https://github.com/genblaze/genblaze/security/advisories/new) to privately report the vulnerability, including:
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
- **Partition paths** in ParquetSink are sanitized to prevent directory traversal.
- **File writes** use atomic temp-file-then-rename to prevent corruption.

## Scope

The following are in scope for security reports:

- Token/credential leakage into manifests or embedded media
- Path traversal in file operations
- Hash collision or integrity bypass in canonical JSON
- Injection attacks via manifest content embedded in media formats (XMP, ID3, etc.)
