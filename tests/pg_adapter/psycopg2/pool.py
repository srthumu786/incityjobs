"""TEST-ONLY ThreadedConnectionPool stand-in backed by the psql adapter."""
from . import connect


class PoolError(Exception):
    pass


class ThreadedConnectionPool:
    def __init__(self, minconn, maxconn, dsn=None, **kwargs):
        self.maxconn = maxconn
        self.dsn = dsn
        self._free = [connect(dsn) for _ in range(minconn)]
        self._used = set()

    def getconn(self):
        if len(self._used) >= self.maxconn:
            raise PoolError("connection pool exhausted")
        conn = self._free.pop() if self._free else connect(self.dsn)
        self._used.add(id(conn))
        return conn

    def putconn(self, conn, close=False):
        self._used.discard(id(conn))
        if close or conn.closed:
            conn.close()
        else:
            self._free.append(conn)

    def closeall(self):
        for conn in self._free:
            conn.close()
        self._free = []
