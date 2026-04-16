# Security Audit Checklist

Run through every item. Mark `[x]` when verified, `[!]` when a problem is found.

## Secrets & Credentials
- [ ] No hardcoded API keys, tokens, or secrets in source code
- [ ] No credentials in test fixtures or example files
- [ ] `.env` files are in `.gitignore`
- [ ] No secrets in git history (check recent commits)
- [ ] Provider adapters accept credentials via env vars or constructor args only
- [ ] Manifests NEVER contain API tokens (architecture invariant)

## Input Validation
- [ ] All user-facing inputs are validated via Pydantic models
- [ ] No raw string interpolation in SQL/shell/URL construction
- [ ] FFmpeg commands use proper argument escaping (no shell injection)
- [ ] File paths are validated/sanitized before use
- [ ] URL inputs are validated before HTTP requests

## Dangerous Functions
- [ ] No `eval()` on user/external input
- [ ] No `exec()` on user/external input
- [ ] No `pickle.loads()` on untrusted data
- [ ] No `os.system()` — use `subprocess.run()` with `shell=False`
- [ ] No `__import__()` with user-controlled strings
- [ ] No `yaml.load()` without `Loader=SafeLoader`

## Network Security
- [ ] All HTTP requests include timeouts
- [ ] No SSRF vulnerabilities in URL construction
- [ ] Webhook URLs are validated before use
- [ ] S3/storage URLs are constructed safely
- [ ] No sensitive data in URL query parameters

## Dependency Security
- [ ] Run `pip-audit` if available (or manually check known CVEs)
- [ ] All dependencies pinned to minimum versions (not exact pins)
- [ ] No known vulnerable versions of Pydantic, Pillow, etc.
- [ ] Dev dependencies don't leak into production installs

## File System Security
- [ ] Temp files are created securely (SpooledTemporaryFile, tempfile)
- [ ] No path traversal vulnerabilities in media handlers
- [ ] File permissions are appropriate on created files
- [ ] Large file handling doesn't cause OOM (streaming/chunked)

## Serialization Safety
- [ ] Canonical JSON serialization is deterministic and safe
- [ ] No deserialization of untrusted formats (pickle, marshal)
- [ ] JSON schema validation prevents malformed input
- [ ] Pydantic strict mode used where appropriate

## Error Handling
- [ ] Exceptions don't leak sensitive information (stack traces, credentials)
- [ ] Error messages don't include raw user input that could be reflected
- [ ] Failed operations clean up temp files and resources
- [ ] No bare `except:` that could swallow security-relevant errors
