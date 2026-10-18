"""WRDS connection wrapper. Credentials come from ~/.pgpass (never from this repository)."""
import time
import wrds
import pandas as pd

_conn = None

def conn():
    global _conn
    if _conn is None:
        _conn = wrds.Connection(autoconnect=True)
    return _conn

def sql(q: str, retries: int = 3) -> pd.DataFrame:
    """Run a query; '%' must be written as '%%' inside q (psycopg2 param style)."""
    last = None
    for i in range(retries):
        try:
            return conn().raw_sql(q)
        except Exception as e:  # noqa
            last = e
            global _conn
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            time.sleep(5 * (i + 1))
    raise last

def in_list(values) -> str:
    vals = sorted({str(v).replace("'", "''") for v in values if v is not None and str(v) != ""})
    if not vals:
        return "('__none__')"
    return "(" + ",".join(f"'{v}'" for v in vals) + ")"
