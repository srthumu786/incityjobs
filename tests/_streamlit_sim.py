"""Simulated runtime shared by the end-to-end and diagnostics suites.

Installs stand-ins for `streamlit`, `geopy` (no network) and, if missing, `pandas`
into sys.modules, so app.py can be executed with runpy exactly as Streamlit runs
it: one full script run per interaction, with session state, reruns and
st.cache_resource semantics (cached per "server process", failures not cached).

LOG captures everything app.py writes to its "incityjobs" logger, i.e. what the
protected Streamlit server log would contain (after the app's redaction filter).
"""
import io
import json
import logging
import math
import sys
import time
import types

# ------------------------------------------------------------------ simulated geopy (no network)
class GeocoderServiceError(Exception): pass
PLACES = {"priceville": (34.5251, -86.8947), "decatur": (34.6059, -86.9833), "austin": (30.2672, -97.7431)}
class _Loc:
    def __init__(self, la, lo): self.latitude, self.longitude = la, lo
class Nominatim:
    def __init__(self, user_agent): pass
    def geocode(self, query, timeout=5):
        if "SERVICEDOWN" in query: raise GeocoderServiceError("503 Service Unavailable")
        for name, coords in PLACES.items():
            if name in query.lower(): return _Loc(*coords)
        return None
class geodesic:
    def __init__(self, a, b):
        r = 3958.8; la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
        h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        self.miles = 2 * r * math.asin(math.sqrt(h))
geopy = types.ModuleType("geopy"); _gc = types.ModuleType("geopy.geocoders"); _gd = types.ModuleType("geopy.distance")
_gc.Nominatim, _gd.geodesic = Nominatim, geodesic
geopy.geocoders, geopy.distance = _gc, _gd
sys.modules.update({"geopy": geopy, "geopy.geocoders": _gc, "geopy.distance": _gd})

try:
    import pandas  # noqa: F401  (used by st.map in the Employee Portal)
except ImportError:  # tiny stand-in so the suite also runs without pandas
    _pd = types.ModuleType("pandas")
    class DataFrame(list):
        def to_dict(self, orient): return list(self)
    _pd.DataFrame = DataFrame
    sys.modules["pandas"] = _pd

# ------------------------------------------------------------------ simulated streamlit
class StopScript(BaseException): pass
class Rerun(BaseException): pass
class SessionState(dict):
    __getattr__ = dict.get
    def __setattr__(self, k, v): self[k] = v
class _Ctx:
    def __init__(self, ui): self.ui = ui
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __getattr__(self, n): return getattr(self.ui, n)

RESOURCE_CACHE = {}  # like st.cache_resource: survives reruns, per server process
class UI:
    def __init__(self):
        self.out, self.inputs, self.clicks, self.forms = [], {}, set(), set()
        self.nav, self.radio_options = None, None
        self.session_state, self.secrets = SessionState(), {}
    def _rec(kind):
        def f(self, *a, **k): self.out.append((kind, " ".join(str(x) for x in a)))
        return f
    title = _rec("title"); subheader = _rec("subheader"); markdown = _rec("markdown"); write = _rec("write")
    caption = _rec("caption"); info = _rec("info"); success = _rec("success"); warning = _rec("warning")
    error = _rec("error"); text = _rec("text"); toast = _rec("toast"); balloons = _rec("balloons"); code = _rec("code")
    def json(self, obj): self.out.append(("json", json.dumps(obj)))
    def metric(self, label, value, **k): self.out.append(("metric", f"{label}={value}"))
    def dataframe(self, data, **k): self.out.append(("dataframe", json.dumps(data, default=str)))
    def map(self, df, **k):
        assert "use_container_width" not in k
        self.out.append(("map", json.dumps(df.to_dict("records"))))
    def columns(self, spec): return [_Ctx(self) for _ in range(spec if isinstance(spec, int) else len(spec))]
    def tabs(self, labels): return [_Ctx(self) for _ in labels]
    def form(self, key, **k): self.forms.add(key); return _Ctx(self)
    def expander(self, label, **k): self.out.append(("expander", label)); return _Ctx(self)
    def spinner(self, *a, **k): return _Ctx(self)
    def container(self): return _Ctx(self)
    def text_input(self, label, key=None, **k): return self.inputs.get(key or label, "")
    def text_area(self, label, value="", **k): return self.inputs.get(label, value)
    def checkbox(self, label, key=None, value=False, **k): return self.inputs.get(key, self.inputs.get(label, value)) if key else self.inputs.get(label, value)
    def selectbox(self, label, options, **k):
        want = self.inputs.get(label)
        return next((o for o in options if want and o.startswith(want)), options[0])
    def slider(self, label, a, b, v, **k): return self.inputs.get(label, v)
    def radio(self, label, options, **k):
        if label == "Navigate Portals:":
            self.radio_options = list(options)
            return self.nav if self.nav in options else options[0]
        return options[0]
    def form_submit_button(self, label="", **k): return label in self.clicks
    def button(self, label, key=None, disabled=False, **k):
        return not disabled and (label in self.clicks or (key in self.clicks if key else False))
    def stop(self): raise StopScript()
    def rerun(self): raise Rerun()
    def cache_resource(self, show_spinner=False):
        def deco(fn):
            def wrapper():
                if fn.__name__ not in RESOURCE_CACHE:
                    RESOURCE_CACHE[fn.__name__] = fn()  # exceptions are not cached
                return RESOURCE_CACHE[fn.__name__]
            wrapper.__name__ = fn.__name__
            return wrapper
        return deco

ui = UI()
st = types.ModuleType("streamlit")
for _name in dir(UI):
    if not _name.startswith("__"):
        setattr(st, _name, getattr(ui, _name))
st.session_state, st.secrets, st.sidebar = ui.session_state, ui.secrets, ui
sys.modules["streamlit"] = st
time.sleep = lambda s: None  # skip the 1-second failed-login delay

# ------------------------------------------------------------------ server-log capture
LOG = io.StringIO()
_log_handler = logging.StreamHandler(LOG)
_log_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
_app_logger = logging.getLogger("incityjobs")
_app_logger.addHandler(_log_handler)
_app_logger.propagate = False


def server_log() -> str:
    return LOG.getvalue()


def clear_server_log() -> None:
    LOG.seek(0)
    LOG.truncate()
