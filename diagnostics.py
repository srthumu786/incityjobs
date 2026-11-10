"""Diagnostic codes, startup error types and log redaction for InCityJobs.

No Streamlit imports, so it can be unit-tested on its own.

Classification is by the OPERATION that failed:
- Opening the database connection (including a missing or invalid
  `connection_string` secret) raises DatabaseConnectionError -> ICJ-ERR-500.
- Running the schema migration raises SchemaMigrationError -> ICJ-ERR-511.
- Anything that fails afterwards, inside a request's own query, keeps the
  code of that operation (for example ICJ-ERR-504 for the employer
  duplicate-check query).
Because startup errors carry their own code, a caller can't relabel them as its
own failure: report_failure() in app.py always uses the startup code.

Redaction: every log record from the "incityjobs" logger, including its
traceback and chained causes, passes through RedactingFilter, which masks
connection-string passwords, `password=` values, the configured connection
string itself, email addresses and password hashes. Telemetry details are
redacted the same way.
"""
import logging
import re

DIAGNOSTIC_CODES = {
    "ICJ-ERR-500": "Database connection initialization failed (missing/invalid connection secret or unreachable database)",
    "ICJ-ERR-501": "Candidate duplicate check failed",
    "ICJ-ERR-502": "Candidate profile save failed",
    "ICJ-ERR-503": "Candidate target-location geocoding service failed",
    "ICJ-ERR-504": "Employer duplicate-check query failed",
    "ICJ-ERR-505": "Employer registration save failed",
    "ICJ-ERR-506": "Job worksite geocoding service failed",
    "ICJ-ERR-507": "Job requisition save failed",
    "ICJ-ERR-508": "Employee proximity map query failed",
    "ICJ-ERR-509": "Manager console candidate read failed",
    "ICJ-ERR-510": "Manager console match logging failed",
    "ICJ-ERR-511": "Database schema initialization or migration failed",
    "ICJ-ERR-512": "Employer sign-in lookup failed",
    "ICJ-ERR-513": "Employer setup-code redemption failed",
    "ICJ-ERR-514": "Manager console job list failed",
    "ICJ-ERR-515": "Employer setup-code issue failed",
    "ICJ-ERR-516": "Manager console system checks failed",
    "ICJ-ERR-517": "Employer account verification failed",
    "ICJ-ERR-518": "UNIQUE email constraint not verified active (e.g. existing duplicates); app-level checks remain on",
}


class StartupDiagnosticError(Exception):
    """A failure while initialising the database layer, before any request query ran."""
    code = ""
    stage = ""
    user_message = ""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DatabaseConnectionError(StartupDiagnosticError):
    code = "ICJ-ERR-500"
    stage = "startup.database_connection"
    user_message = ("⚠️ The service can't reach its database right now, so nothing was saved. "
                    "Please try again in a few minutes. If it keeps happening, contact InCityJobs support "
                    "and quote this code.")


class SchemaMigrationError(StartupDiagnosticError):
    code = "ICJ-ERR-511"
    stage = "startup.schema_migration"
    user_message = ("⚠️ The service's database is being updated and can't accept changes right now, "
                    "so nothing was saved. Please try again later. If it keeps happening, contact "
                    "InCityJobs support and quote this code.")


# --------------------------------------------------------------------------- redaction
_REDACTIONS = (
    # postgresql://user:PASSWORD@host  ->  postgresql://user:***@host
    (re.compile(r"(?i)\b(postgres(?:ql)?://[^:/@\s\"']*:)[^@\s\"']*@"), r"\1***@"),
    # password=VALUE / PGPASSWORD=VALUE in key/value connection strings or env dumps
    (re.compile(r"(?i)\b((?:pg)?password\s*=\s*)('[^']*'|\"[^\"]*\"|[^\s&\"']+)"), r"\1***"),
    # stored password hashes / session tokens
    (re.compile(r"\$?pbkdf2[_-]sha256\$[^\s\"']+"), "<password-hash>"),
    # email addresses (personal information)
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), "<email>"),
)


def redact(text, secrets=()) -> str:
    """Mask secrets and personal data in a log/telemetry string."""
    if text is None:
        return text
    out = str(text)
    for value in sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True):
        out = out.replace(value, "***")
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFilter(logging.Filter):
    """Redacts the message, its arguments and the formatted traceback (including
    chained causes) of every record, then drops the raw exception object so no
    handler can re-render it unredacted. The traceback text is kept, so
    logger.exception() output still shows where the failure happened."""

    def __init__(self, secret_provider=lambda: ()):
        super().__init__()
        self._secret_provider = secret_provider

    def _secrets(self):
        try:
            return tuple(self._secret_provider() or ())
        except Exception:
            return ()

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = self._secrets()
        record.msg = redact(record.getMessage(), secrets)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info), secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text, secrets)
        return True


def install_redacting_filter(logger: logging.Logger, secret_provider) -> None:
    """Attach the filter once (app.py runs again on every Streamlit rerun)."""
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter(secret_provider))
