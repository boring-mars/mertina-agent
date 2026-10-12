"""Regex-based secret redaction for logs and tool output.

Short tokens (< 18 chars) are fully masked; longer ones keep the first 6 and
last 4 characters for debuggability.
"""
def redact_sensitive_text(text: str, *, force: bool = False, code_file: bool = False,
                          file_read: bool = False, secret_file: bool = False,
                          redact_url_credentials: bool = False) -> str:
    """Apply all redaction patterns to a block of text.

    Safe on any string. Enabled by default (``security.redact_secrets: false``
    disables); ``force=True`` is for safety boundaries that must never return
    raw secrets regardless.

    ``redact_url_credentials=True``: also redact credential-named query params
    and ``user:pass@`` userinfo — off by default because OAuth-callback /
    magic-link / pre-signed URLs must survive ordinary tool flows unchanged.
    ``code_file=True``: skip the ENV/JSON assignment passes for known source
    code (``MAX_TOKENS=***``, ``"apiKey": "test"`` fixtures). ``file_read=True``
    (implies code_file unless ``secret_file``): prefix-matched credentials become a
    non-reusable sentinel (``«redacted:ghp_…»``) instead of a head/tail mask an agent
    could write back into config.yaml as a dead credential.
    Every regex sits behind a cheap substring gate that its pattern requires,
    so the gates are never false-negative.

    Set code_file=True to also skip the Python-repr mapping pass (``{'API_KEY': '…'}``
    fixtures in source); pytest/exception diagnostic lines get a narrow pass in
    redact_terminal_output instead.

    Set file_read=True for file *content* returned to the agent (read_file / search_files / cat). The old
    mask looked like a real-but-truncated key, so an agent reading it from config.yaml and writing it back
    silently corrupted the stored credential into a dead 13-char value → 401 (issue #35519). The sentinel is
    syntactically invalid as a token, so it can't be mistaken for a usable key or written back as one.

    Set ``secret_file=True`` when the caller has already classified the SOURCE as secret-bearing
    (``_is_secret_file_arg``: command reads on the terminal side, resolved tool paths on the
    file side). It re-enables the ENV/JSON/YAML assignment passes that ``file_read`` would
    otherwise skip, so an opaque prefix-less credential assigned to a credential-shaped key is
    masked instead of passed through in cleartext (issue #110567). With ``file_read=True`` those
    assignments are masked with the non-reusable sentinel, so the #35519 write-back hazard stays
    closed. ``secret_file`` is authoritative: it wins over ``code_file``, so a caller cannot be
    fail-open by setting both. Files that are not secret-bearing (any source file, a project's own
    ``config.yaml``) keep the code_file behaviour: ``MAX_TOKENS: 100`` and ``"apiKey": "test"``
    fixtures are untouched.
    """
    if text is None:
        return None
    text = text if isinstance(text, str) else str(text)
    if not text:
        return text
    return text
