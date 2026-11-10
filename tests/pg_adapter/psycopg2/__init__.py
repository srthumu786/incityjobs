"""TEST-ONLY psycopg2 stand-in that drives a real PostgreSQL server through a
persistent `psql` process, so the test suites run without compiling psycopg2.

SAFETY: it only connects to a LOCAL server over a Unix socket directory
(PGSHIM_HOST must be an existing directory). From the DSN the app passes, only
the user name and password are used (so wrong credentials fail exactly as they
would against Neon); its host and database are ignored, so these tests can never
reach Neon or any other network database. A DSN that isn't a URI or key=value
string is rejected with libpq's own error text, which echoes the DSN.
tests/run_local.sh creates a throwaway cluster and sets PGSHIM_* for you. Implements just what InCityJobs uses: connect,
cursor (context manager, execute with %s params, fetchone/fetchall),
commit/rollback with psycopg2's implicit-transaction semantics, error classes
with .pgcode, and closed. Connection target comes from PGSHIM_* env vars."""
import os
import re
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

END = "__PGSHIM_END__"
NULL = "\x01NULL\x01"


class Error(Exception):
    def __init__(self, message="", pgcode=None):
        super().__init__(message)
        self.pgcode = pgcode


class Warning(Exception):  # noqa: A001 (psycopg2 API name)
    pass


class InterfaceError(Error): pass
class DatabaseError(Error): pass
class OperationalError(DatabaseError): pass
class ProgrammingError(DatabaseError): pass
class IntegrityError(DatabaseError): pass
class DataError(DatabaseError): pass
class InternalError(DatabaseError): pass
class NotSupportedError(DatabaseError): pass


def _error_for(sqlstate, message):
    cls = {"23": IntegrityError, "08": OperationalError, "22": DataError, "0A": NotSupportedError}.get(
        (sqlstate or "")[:2], ProgrammingError)
    return cls(message, pgcode=sqlstate)


def _literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _convert(field):
    if field == NULL:
        return None
    if re.fullmatch(r"-?\d+", field):
        return int(field)
    if re.fullmatch(r"-?\d+\.\d*(e[-+]?\d+)?", field):
        return float(field)
    if field in ("t", "f"):
        return field == "t"
    if re.fullmatch(r"\{-?\d+(,-?\d+)*\}", field):
        return [int(x) for x in field[1:-1].split(",")]
    return field


def _credentials_from_dsn(dsn):
    """(user, password) from a postgresql:// URI or key=value DSN; mimics libpq's
    'invalid dsn' error (which echoes the DSN) for anything else."""
    text = (dsn or "").strip()
    if text.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(text)
        return (unquote(parts.username) if parts.username else None,
                unquote(parts.password) if parts.password is not None else None)
    if "=" in text:
        pairs = dict(re.findall(r"(\w+)\s*=\s*(\S+)", text))
        return pairs.get("user"), pairs.get("password")
    if not text:
        return None, None
    raise OperationalError(f'invalid dsn: missing "=" after "{text}" in connection info string\n')


class _Psql:
    def __init__(self, dsn):
        host = os.environ.get("PGSHIM_HOST", "")
        if not host or not os.path.isdir(host):
            raise OperationalError("test adapter refuses to connect: PGSHIM_HOST must be a local "
                                   "Unix-socket directory (run tests via tests/run_local.sh)")
        user, password = _credentials_from_dsn(dsn)
        cmd = ["psql", "-X", "-q", "-A", "-t", "-F", "\x1f", "-R", "\x1e", "-v", "ON_ERROR_STOP=0",
               "-h", os.environ["PGSHIM_HOST"], "-p", os.environ.get("PGSHIM_PORT", "5432"),
               "-U", user or os.environ.get("PGSHIM_USER", "postgres"), "-d", os.environ["PGSHIM_DB"]]
        env = dict(os.environ)
        if password is not None:
            env["PGPASSWORD"] = password
        self._stderr = tempfile.TemporaryFile(mode="w+")  # a file, so notices can't fill a pipe
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, text=True, bufsize=1, env=env)
        self.run(f"\\pset null '{NULL}'", meta=True)
        self.run("\\set VERBOSITY verbose", meta=True)  # stderr lines carry the SQLSTATE

    def _first_error_since(self, offset):
        """(sqlstate, message) of the FIRST error psql reported after `offset`.
        A multi-statement script keeps running after an error inside a
        transaction, so its LAST error is just 'current transaction is aborted';
        the real psycopg2 reports the first one, and so does this adapter."""
        fd = self._stderr.fileno()
        size = os.fstat(fd).st_size
        text = os.pread(fd, max(size - offset, 0), offset).decode("utf-8", "replace") if size > offset else ""
        match = re.search(r"ERROR:\s+([0-9A-Z]{5}):\s+([^\n]*)", text)
        return (match.group(1), match.group(2)) if match else (None, None)

    def run(self, sql, meta=False):
        stderr_offset = os.fstat(self._stderr.fileno()).st_size
        body = sql if meta else sql.rstrip().rstrip(";") + ";"
        self.proc.stdin.write(body + f"\n\\echo {END} :ERROR :SQLSTATE :'LAST_ERROR_MESSAGE'\n")
        self.proc.stdin.flush()
        out = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                self.proc.wait(timeout=5)
                self._stderr.seek(0)
                message = self._stderr.read().strip().replace("psql: error: ", "") or "psql process ended"
                raise OperationalError(message)
            if END in line:
                head, tail = line.split(END, 1)
                out.append(head)
                break
            out.append(line)
        error, sqlstate, message = (tail.strip().split(" ", 2) + ["", "", ""])[:3]
        if error == "true":
            first_state, first_message = self._first_error_since(stderr_offset)
            if first_state:
                sqlstate, message = first_state, first_message
        data = "".join(out).rstrip("\n").strip("\x1e")
        rows = [tuple(_convert(f) for f in rec.split("\x1f")) for rec in data.split("\x1e") if rec.strip("\n") != ""] if data else []
        return rows, error == "true", sqlstate, message.strip("'")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class cursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.conn.closed:
            raise InterfaceError("connection already closed")
        params = list(params or ())
        if sql.count("%s") != len(params):
            raise ProgrammingError(f"{sql.count('%s')} placeholders, {len(params)} params")
        rendered = re.sub(r"%s", lambda m: _literal(params.pop(0)), sql)
        if not self.conn._in_tx:
            self.conn._psql.run("BEGIN")
            self.conn._in_tx = True
        rows, failed, sqlstate, message = self.conn._psql.run(rendered)
        self.conn.log.append(" ".join(rendered.split()))
        if failed:
            raise _error_for(sqlstate, message)
        self._rows = rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class connection:
    log = []  # shared statement log (tests inspect it)

    def __init__(self, dsn=None, **kwargs):
        self.dsn = dsn
        self.closed = 0
        self._in_tx = False
        self._psql = _Psql(dsn)

    def cursor(self):
        return cursor(self)

    def commit(self):
        if self._in_tx:
            rows, failed, sqlstate, message = self._psql.run("COMMIT")
            self._in_tx = False
            if failed:
                raise _error_for(sqlstate, message)

    def rollback(self):
        if self._in_tx:
            self._psql.run("ROLLBACK")
            self._in_tx = False

    def close(self):
        if not self.closed:
            self._psql.close()
            self.closed = 1


def connect(dsn=None, **kwargs):
    return connection(dsn, **kwargs)
