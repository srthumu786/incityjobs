# Save this file locally as: /Users/Thumu/mycityjobs_project/app.py
import streamlit as st
import hashlib
import html
import re
import json
import logging
import time
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import psycopg2.pool

import auth
import db_schema
from diagnostics import (
    DIAGNOSTIC_CODES,
    DatabaseConnectionError,
    SchemaMigrationError,
    StartupDiagnosticError,
    install_redacting_filter,
    redact,
)
from recommendations import (
    CONSENT_FOOTNOTE,
    CONSENT_LABEL,
    consent_state,
    set_matching_consent,
)
from matching import (
    CandidateRecord,
    JobCriteria,
    LEGACY_FALLBACK_COORDS,
    MIN_SEMANTIC_SCORE,
    build_push_payload,
    evaluate_match,
    geodesic_miles,
)

logger = logging.getLogger("incityjobs")


def _configured_secret_values():
    """The configured connection string and its password, so log redaction can
    mask them even if a driver error message echoes them back."""
    try:
        dsn = str(st.secrets.get("connection_string") or "")
    except Exception:
        return ()
    values = [dsn]
    try:
        values.append(urlsplit(dsn).password or "")
    except ValueError:
        pass
    return tuple(v for v in values if v)


# Every record from this logger (message, traceback, chained causes) is redacted.
install_redacting_filter(logger, _configured_secret_values)

# ==============================================================================
# 🗄️ CENTRALIZED DATABASE ACCESS (single pool + one-time schema setup)
# ==============================================================================
# Production tables (schema and migrations live in db_schema.py):
#   beta_employers        - employer accounts       (Employer Portal)
#   employer_jobs         - job requisitions        (Employer Portal, signed-in employers only)
#   candidate_profiles    - candidate profiles      (Employee Portal + Manager Console)
#   semantic_matches      - match ledger / 3-Way Push Engine queue
#   system_telemetry_logs - handled runtime failures
#
# Startup failures are raised as diagnostics.DatabaseConnectionError (ICJ-ERR-500)
# or diagnostics.SchemaMigrationError (ICJ-ERR-511). Only errors raised inside a
# request's own `with get_db_cursor() as cur:` block keep the caller's code.


@st.cache_resource(show_spinner=False)
def _get_db_pool():
    """One thread-safe connection pool per server process, shared by every session."""
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=st.secrets["connection_string"],
        connect_timeout=10,
    )


def _open_pool():
    """The shared pool, or DatabaseConnectionError (ICJ-ERR-500) if it can't be opened."""
    try:
        return _get_db_pool()
    except (KeyError, FileNotFoundError) as exc:
        raise DatabaseConnectionError("Streamlit secret 'connection_string' is missing") from exc
    except Exception as exc:
        raise DatabaseConnectionError(f"could not open the database connection pool ({type(exc).__name__})") from exc


def _checkout_connection(pool):
    """Borrow a connection, replacing it if the server dropped it while idle
    (Neon closes idle connections when the compute scales to zero)."""
    conn = pool.getconn()
    try:
        if conn.closed:
            raise psycopg2.InterfaceError("connection already closed")
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        conn.rollback()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn


def _open_connection(pool):
    """A healthy pooled connection, or DatabaseConnectionError (ICJ-ERR-500)."""
    try:
        return _checkout_connection(pool)
    except Exception as exc:
        raise DatabaseConnectionError(f"could not obtain a database connection ({type(exc).__name__})") from exc


def _release_connection(pool, conn):
    pool.putconn(conn, close=bool(conn.closed))


@st.cache_resource(show_spinner=False)
def ensure_schema() -> dict:
    """Create/migrate the production tables once per server process and return the
    migration report. A failure is not cached, so the next request retries.
    Raises DatabaseConnectionError (ICJ-ERR-500) or SchemaMigrationError (ICJ-ERR-511)."""
    pool = _open_pool()
    conn = _open_connection(pool)
    try:
        report = db_schema.apply_schema(conn)
        if not report["complete"]:
            # Uniqueness is not verified active; app-level duplicate checks still run
            detail = "; ".join(
                f"{t}.{e['column']}: {e['status']} ({len(e['duplicates'])} duplicate email groups; "
                f"{', '.join(e['verification']['problems']) or 'verified'}"
                f"{'; ' + e['error'] if e.get('error') else ''})"
                for t, e in report["constraints"].items() if e["status"] != "enforced" or not e["verification"]["active"]
            )
            logger.warning("ICJ-ERR-518 [startup.schema.unique_email] %s", detail)
            # Written directly: the normal telemetry path depends on this function
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system_telemetry_logs (diagnostic_error_code, error_context_vector, error_detail) VALUES (%s, %s, %s);",
                    ("ICJ-ERR-518", "startup.schema.unique_email", redact(detail, _configured_secret_values())),
                )
            conn.commit()
    except Exception as exc:
        if not conn.closed:
            conn.rollback()
        raise SchemaMigrationError(f"schema migration failed ({type(exc).__name__})") from exc
    finally:
        _release_connection(pool, conn)
    return report


@contextmanager
def get_db_cursor():
    """Yield a cursor on a pooled connection.
    Commits when the block finishes, rolls back on error, and always returns the
    connection to the pool. Usage:
        with get_db_cursor() as cur:
            cur.execute("SELECT ...", (param,))
    Startup problems raise DatabaseConnectionError / SchemaMigrationError before
    the block runs; errors from the caller's own queries propagate unchanged.
    """
    ensure_schema()
    pool = _open_pool()
    conn = _open_connection(pool)
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except BaseException:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        _release_connection(pool, conn)


# ==============================================================================
# 🩺 TRANSPARENT ERROR REPORTING (logs + system_telemetry_logs)
# ==============================================================================
# Codes are defined in diagnostics.py. Details and tracebacks go only to the
# protected server log (redacted); users see a plain message, the code and what to do.


def log_system_event(code: str, context: str, exc: BaseException = None) -> None:
    """Record a handled failure in the server log (with traceback) and in
    system_telemetry_logs. Never raises, so reporting an error can't break the
    request that hit it."""
    secrets = _configured_secret_values()
    if isinstance(exc, StartupDiagnosticError):
        cause = exc.__cause__
        detail = f"{exc.reason}; cause: {type(cause).__name__}: {cause}" if cause else exc.reason
    else:
        detail = f"{type(exc).__name__}: {exc}" if exc else DIAGNOSTIC_CODES.get(code, "")
    detail = redact(detail, secrets)[:2000]
    if exc is not None:
        logger.exception("%s [%s] %s", code, context, detail, exc_info=exc)
    else:
        logger.error("%s [%s] %s", code, context, detail)
    if isinstance(exc, (StartupDiagnosticError, psycopg2.OperationalError, psycopg2.InterfaceError)):
        # The database is unreachable or not initialised; a telemetry write would
        # fail too (and make the user wait for another connection attempt).
        return
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "INSERT INTO system_telemetry_logs (diagnostic_error_code, error_context_vector, error_detail) VALUES (%s, %s, %s);",
                (code, context, detail),
            )
    except Exception:
        logger.exception("Could not write telemetry row for %s [%s]", code, context)


def report_failure(code: str, context: str, user_message: str, exc: BaseException = None, level: str = "error") -> None:
    """Log the failure and show the user a plain message with its diagnostic code.
    A startup failure always keeps its own code (ICJ-ERR-500/511) and message,
    whatever operation was running when it surfaced."""
    if isinstance(exc, StartupDiagnosticError):
        code, context, user_message, level = exc.code, f"{exc.stage} (while {context})", exc.user_message, "error"
    log_system_event(code, context, exc)
    show = st.warning if level == "warning" else st.error
    show(f"{user_message} (Diagnostic code: {code})")

# ==============================================================================
# 🔐 AUTHENTICATED SESSIONS (signed, expiring tokens; see auth.py)
# ==============================================================================
EMPLOYER_SESSION_SECONDS = 8 * 3600
MANAGER_SESSION_SECONDS = 4 * 3600
MAX_SIGN_IN_ATTEMPTS = 5


@st.cache_resource(show_spinner=False)
def _session_signing_key() -> bytes:
    """Random per-server key for signing session tokens. Sessions live in server
    memory, so a restart ends them anyway; no extra secret is needed."""
    import secrets as _pysecrets
    return _pysecrets.token_bytes(32)


def _too_many_attempts(kind: str) -> bool:
    return st.session_state.get(f"{kind}_failed_attempts", 0) >= MAX_SIGN_IN_ATTEMPTS


def _register_failed_attempt(kind: str) -> None:
    st.session_state[f"{kind}_failed_attempts"] = st.session_state.get(f"{kind}_failed_attempts", 0) + 1
    logger.warning("Failed %s sign-in attempt", kind)
    time.sleep(1)  # slow down password guessing


def _manager_accounts() -> dict:
    """{username: password_hash} from the [managers] section of Streamlit secrets."""
    try:
        accounts = st.secrets.get("managers")
    except Exception:
        # No secrets file at all (e.g. a fresh local checkout): no manager accounts
        logger.warning("Streamlit secrets unavailable; Manager Console disabled")
        return {}
    return {str(k): str(v) for k, v in dict(accounts).items()} if accounts else {}


def current_manager():
    """Username of the signed-in manager, or None. Checks the token's signature,
    role and expiry, that the account still exists in secrets, and that its
    password hash hasn't been replaced since sign-in (replacing it revokes the session)."""
    key = _session_signing_key()
    claims = auth.verify_token(key, st.session_state.get("manager_token"), auth.ROLE_MANAGER)
    if not claims:
        return None
    stored_hash = _manager_accounts().get(claims["sub"])
    if not stored_hash or claims.get("cred") != auth.credential_fingerprint(key, stored_hash):
        return None
    return claims["sub"]


def _start_manager_session(username: str, stored_hash: str) -> None:
    key = _session_signing_key()
    st.session_state["manager_token"] = auth.issue_token(
        key, username, auth.ROLE_MANAGER, MANAGER_SESSION_SECONDS,
        extra={"cred": auth.credential_fingerprint(key, stored_hash)},
    )
    st.session_state["manager_failed_attempts"] = 0


def _start_employer_session(employer_id: int, company_name: str, password_hash: str) -> None:
    key = _session_signing_key()
    st.session_state["employer_token"] = auth.issue_token(
        key, employer_id, auth.ROLE_EMPLOYER, EMPLOYER_SESSION_SECONDS,
        extra={"cred": auth.credential_fingerprint(key, password_hash)},
    )
    st.session_state["employer_company"] = company_name
    st.session_state["employer_failed_attempts"] = 0


def end_employer_session() -> None:
    for key in ("employer_token", "employer_company"):
        st.session_state.pop(key, None)


def current_employer():
    """The signed-in employer as {"id", "company", "verified"}, or None.

    Valid only if the signed token checks out AND the account still exists with
    the same password hash it had at sign-in. Any credential change (new password,
    password removed) revokes every existing session for that employer."""
    key = _session_signing_key()
    claims = auth.verify_token(key, st.session_state.get("employer_token"), auth.ROLE_EMPLOYER)
    if not claims:
        return None
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT company_name, password_hash, verified_at IS NOT NULL FROM beta_employers WHERE id = %s;",
                (int(claims["sub"]),),
            )
            row = cur.fetchone()
    except Exception as e:
        report_failure("ICJ-ERR-517", "employer_portal.session.verify", "❌ Your employer session could not be verified. Please try again.", e)
        return None
    if not row or not row[1] or claims.get("cred") != auth.credential_fingerprint(key, row[1]):
        return None
    return {"id": int(claims["sub"]), "company": row[0], "verified": bool(row[2])}


# ==============================================================================
# 📍 GEOCODING (no fallback coordinates: unknown locations are rejected)
# ==============================================================================
LOCATION_FORMAT_HINT = (
    "Enter the full city name, a two-letter state code and a 5-digit ZIP code "
    "(for example: Priceville, AL 35603)."
)


class LocationNotFound(Exception):
    """Nominatim returned no result for any of the address variants."""


def geocode_location(*queries):
    """Return (lat, lon) for the first query Nominatim can resolve.
    Raises LocationNotFound if none resolve; geopy errors (service down, rate
    limit) propagate so callers can report them separately."""
    from geopy.geocoders import Nominatim
    geolocator = Nominatim(user_agent="incityjobs_spatial_resolver_v4")
    for query in queries:
        query = (query or "").strip(" ,")
        if query:
            location = geolocator.geocode(query, timeout=5)
            if location:
                return location.latitude, location.longitude
    raise LocationNotFound(queries[0] if queries else "")


# Initialize deep session cache flags to prevent layout resetting loops
if 'last_ingested_candidate' not in st.session_state:
    st.session_state['last_ingested_candidate'] = None

# Hardcoded global simulation values matching regional deployment settings
region_meta = {
    "region": "United States Lower 48",
    "currency": "USD",
    "symbol": "$"
}
prelaunch_message = "System Notice: Direct recruitment matrix engines are active in Beta Testing configurations."

class InCityJobsDiagnostics:
    @staticmethod
    def sanitize_input(value: str) -> str:
        if not value:
            return ""
        return re.sub(r'[<>\'"\\;]', '', value.strip())

    @staticmethod
    def validate_inputs(company, hq, email, phone, sign, agree) -> dict:
        if not all([company, hq, email, phone, sign]):
            return {"code": "ICJ-ERR-401", "title": "Missing Required Values", "solution": "Fill out all text input matrices completely."}
        if not agree:
            return {"code": "ICJ-ERR-402", "title": "Compliance Rejection", "solution": "You must explicitly check the authorization checkbox."}
        if "@" not in email or "." not in email:
            return {"code": "ICJ-ERR-403", "title": "Invalid Communication Node", "solution": "Verify the structural syntax of your email address."}
        return {"code": "ICJ-OK-200", "title": "Validation Success", "solution": "Proceeding to cloud secure mapping protocols."}

    @staticmethod
    def save_employer_to_cloud(company, hq, email, phone, sign, signature_hash, password_hash):
        """Insert a new employer. Returns the new id, "duplicate", or None on failure."""
        try:
            with get_db_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO beta_employers (company_name, corporate_hq, admin_email, admin_phone, signature_hash, password_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id;
                """, (company, hq, email, phone, signature_hash, password_hash))
                return cursor.fetchone()[0]
        except Exception as e:
            if db_schema.is_unique_violation(e):
                return "duplicate"
            report_failure("ICJ-ERR-505", "employer_portal.registration.save", "❌ Cloud Storage Write Interrupted: your registration was not saved.", e)
            return None


def candidate_profile_ingestion_ui():

    st.markdown("## 👤 Candidate Profile Ingestion Matrix")
    st.caption("Phase 3: Core Hiring Automation & Core Workflows — Step 3.1 & 3.3 (Split Location Matrix)")

    with st.form(key="candidate_ingestion_form"):
        st.markdown("### 📋 1. Professional Credentials")
        candidate_name = st.text_input("Full Name (Absolute Identity Mapping)", placeholder="John Doe")
        candidate_email = st.text_input("Secure Communication Email Node", placeholder="your.email@example.com")
        target_role = st.text_input("Target Role / Job Title", placeholder="Software Engineer")
        resume_text = st.text_area("Paste Resume Text / Background Metrics", height=120)

        st.markdown("### 📍 2. Current Physical Location (Where you live right now)")
        c_street = st.text_input("Current Street Address", placeholder="e.g., 500 Congress Ave")
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            c_city = st.text_input("Current City", placeholder="Austin")
        with col_c2:
            c_state = st.text_input("Current State", placeholder="TX")

        st.markdown("### 🎯 3. Target Job Location (Where you want to find work)")
        st.info("💡 **Dynamic Geo-Mapping System Active:** You may enter any physical street address, regional city footprint, or residential zip code. Our real-time spatial calculation engine will dynamically parse whatever information you provide, calculate absolute coordinate parameters, and securely lock your profile into the proximity routing network.")
        t_street = st.text_input("Target Worksite Proximity Street Address", placeholder="e.g., 100 Main St")
        col_t1, col_t2, col_t3 = st.columns(3)
        with col_t1:
            t_city = st.text_input("Target City", placeholder="Priceville")
        with col_t2:
            t_state = st.text_input("Target State", placeholder="AL")
        with col_t3:
            t_zip = st.text_input("Target Zip Code", placeholder="35603")

        st.markdown("### 🛡️ 4. Employer Matching Consent (optional)")
        matching_consent = st.checkbox(CONSENT_LABEL, value=False, key="candidate_matching_consent")
        st.caption(CONSENT_FOOTNOTE)

        submit_button = st.form_submit_button(label="Lock Profile & Validate Location Paths")

    if not submit_button:
        return

    # 1. ENFORCE MANDATORY FIELDS: Prevent blank email strings from hitting the database
    if not candidate_name or not candidate_email or not t_zip or not resume_text:
        st.error("❌ Critical Fields Missing: Candidate Name, Secure Email, Resume Text, and Target Zip Code are mandatory attributes.")
        return

    # Normalize the email so duplicate checks are case-insensitive
    candidate_email = candidate_email.strip().lower()

    # 2. STRUCTURAL NODE VERIFICATION: Ensure the email is formatted correctly before ingestion
    if "@" not in candidate_email or "." not in candidate_email:
        st.error("❌ Invalid Communication Node: Please verify the structural syntax of your candidate email address.")
        return

    # 3. LEDGER CHECK: is this email already registered in candidate_profiles?
    #    (The database also enforces this with a UNIQUE index; see db_schema.py.)
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT 1 FROM candidate_profiles WHERE lower(btrim(contact_email)) = %s LIMIT 1;", (candidate_email,))
            duplicate_candidate_found = cur.fetchone() is not None
    except Exception as e:
        report_failure("ICJ-ERR-501", "employee_portal.intake.duplicate_check", "❌ Could not verify existing profiles, so your profile was not saved. Please try again.", e)
        return

    if duplicate_candidate_found:
        _show_candidate_duplicate_notice()
        return

    # 4. Geocode the target location. No fallback: an unknown location is rejected.
    current_full_addr = f"{c_street}, {c_city}, {c_state}".strip(", ")
    target_full_addr = f"{t_street}, {t_city}, {t_state} {t_zip}".strip(" ,")
    with st.spinner("Executing dual-coordinate spatial parsing..."):
        try:
            target_lat, target_lon = geocode_location(target_full_addr, f"{t_city}, {t_state} {t_zip}")
        except LocationNotFound:
            st.warning(f"📍 **Location not found:** we couldn't place your target location on the map, so your profile was not saved. {LOCATION_FORMAT_HINT}")
            return
        except Exception as geo_err:
            report_failure("ICJ-ERR-503", "employee_portal.intake.geocode", "⚠️ The location lookup service is unavailable right now, so your profile was not saved. Please try again in a minute.", geo_err, level="warning")
            return

    # 5. Save the profile
    try:
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO candidate_profiles (name, contact_email, role, resume, address, current_address, latitude, longitude, status, application_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
            """, (candidate_name, candidate_email, target_role, resume_text, target_full_addr, current_full_addr, target_lat, target_lon, "active_matching_pool", "PENDING"))
            new_candidate_id = cursor.fetchone()[0]
            # Consent columns default to FALSE; only an explicit opt-in grants it (and is audited)
            if matching_consent:
                set_matching_consent(cursor, new_candidate_id, True, "candidate", "intake_form")
    except Exception as e:
        if db_schema.is_unique_violation(e):
            _show_candidate_duplicate_notice()
        else:
            report_failure("ICJ-ERR-502", "employee_portal.intake.save_profile", "❌ Cloud Connection Blocked: your profile was not saved. Please try again.", e)
        return

    # Remember this visitor's own row so the proximity map shows only their data
    st.session_state['last_ingested_candidate'] = new_candidate_id
    st.success("🎉 **DYNAMIC DUAL-LOCATION INGESTION COMPLETED SUCCESSFULLY!**")
    if matching_consent:
        st.info("🛡️ **Employer matching: ON.** Verified employers with matching openings may see your anonymized profile. You can withdraw below.")
    else:
        st.info("🛡️ **Employer matching: OFF.** Employers will not see your profile. You can opt in below.")

    safe = html.escape  # user input is placed inside HTML below
    st.markdown(f"""
    <div style="background-color:#f9f9f9; padding:20px; border-radius:10px; border:1px solid #eee; margin-top:15px;">
        <h4 style="margin-top:0; color:#2e7d32;">🔒 Secure Split-Location Profile Locked</h4>
        <ul style="list-style-type:none; padding-left:0; line-height:1.8;">
            <li>👤 <b>Profile Identity:</b> {safe(candidate_name)}</li>
            <li>💼 <b>Target Position Vector:</b> {safe(target_role)}</li>
            <li>🏠 <b>Current Address Tracked:</b> {safe(current_full_addr) if current_full_addr else "Not Provided"}</li>
            <li>🎯 <b>Target Deployment Zone:</b> {safe(target_full_addr)}</li>
            <li>🛰️ <b>Geo-Mapping Status:</b> Active Radius Matching Pool Anchored</li>
        </ul>
    </div>
    """, unsafe_allow_html=True)


def _show_candidate_duplicate_notice():
    st.warning("⚠️ **SYSTEM NOTICE: TALENT MATRIX REDUNDANCY**")
    st.markdown("**Employee portal:** *Profile already verified and processed in an earlier scanning ledger row transaction.*")


def change_candidate_consent(candidate_id: int, granted: bool, actor_type: str, actor_ref: str, context: str) -> bool:
    """Grant or withdraw employer-matching consent (audited). Returns True on success."""
    try:
        with get_db_cursor() as cur:
            set_matching_consent(cur, candidate_id, granted, actor_type, actor_ref)
    except Exception as e:
        report_failure("ICJ-ERR-521", context, "❌ Your consent choice could not be saved. Nothing was changed. Please try again.", e)
        return False
    return True


def render_own_consent_controls(candidate_id: int):
    """Consent controls for the profile submitted in THIS browser session only."""
    st.markdown("### 🛡️ Your Employer Matching Consent")
    try:
        with get_db_cursor() as cur:
            state = consent_state(cur, candidate_id)
    except Exception as e:
        report_failure("ICJ-ERR-521", "employee_portal.consent.load", "❌ Your consent status could not be loaded.", e)
        return
    if state is None:
        return
    consented, changed_at, policy = state
    if consented:
        st.success(f"Employer matching is **ON** (policy {policy}). Verified employers with matching openings may see your anonymized profile.")
        if st.button("Withdraw consent", key="withdraw_own_consent"):
            if change_candidate_consent(candidate_id, False, "candidate", "own_session", "employee_portal.consent.withdraw"):
                st.rerun()
    else:
        st.info("Employer matching is **OFF**. Employers do not see your profile.")
        st.caption(CONSENT_LABEL)
        st.caption(CONSENT_FOOTNOTE)
        if st.button("I agree: turn on anonymized employer matching", key="grant_own_consent"):
            if change_candidate_consent(candidate_id, True, "candidate", "own_session", "employee_portal.consent.grant"):
                st.rerun()


# ==============================================================================
# 🏢 EMPLOYER AUTHENTICATION
# ==============================================================================
def render_employer_sign_in():
    """Sign-in (and setup-code redemption) shown instead of the job form."""
    st.info("🔐 **Only registered, signed-in employers can post jobs.** Sign in with the email and password from your registration.")

    if _too_many_attempts("employer"):
        st.error("Too many failed sign-in attempts in this session. Please contact an InCityJobs manager.")
        return

    with st.form("employer_sign_in_form", clear_on_submit=True):
        email = st.text_input("Employer Administrator Email", key="employer_sign_in_email")
        password = st.text_input("Employer Password", type="password", key="employer_sign_in_password")
        sign_in = st.form_submit_button("Sign In")
    if sign_in:
        _handle_employer_sign_in(email, password)

    with st.expander("Registered before employer passwords existed? Use a setup code"):
        st.caption("Ask an InCityJobs manager for a one-time setup code. It works once and expires after 48 hours.")
        with st.form("employer_setup_code_form", clear_on_submit=True):
            code_email = st.text_input("Employer Administrator Email", key="setup_code_email")
            code = st.text_input("One-time setup code", key="setup_code_value")
            new_password = st.text_input("New password (at least 12 characters)", type="password", key="setup_code_password")
            new_password_confirm = st.text_input("Repeat new password", type="password", key="setup_code_password_confirm")
            redeem = st.form_submit_button("Set Password & Sign In")
        if redeem:
            _handle_setup_code(code_email, code, new_password, new_password_confirm)


def _handle_employer_sign_in(email: str, password: str) -> None:
    email_key = (email or "").strip().lower()
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT id, company_name, password_hash FROM beta_employers WHERE lower(btrim(admin_email)) = %s ORDER BY id LIMIT 1;",
                (email_key,),
            )
            row = cur.fetchone()
    except Exception as e:
        report_failure("ICJ-ERR-512", "employer_portal.sign_in.lookup", "❌ Sign-in is unavailable right now. Please try again.", e)
        return

    if row and row[2] and auth.verify_password(password or "", row[2]):
        _start_employer_session(row[0], row[1], row[2])
        st.rerun()
    if not (row and row[2]):
        auth.burn_verification_time(password or "")  # same timing whether or not the account exists
    _register_failed_attempt("employer")
    st.error("Incorrect email or password.")


def _handle_setup_code(email: str, code: str, new_password: str, confirm: str) -> None:
    problem = auth.password_problem(new_password, confirm)
    if problem:
        st.error(problem)
        return
    email_key = (email or "").strip().lower()
    code_hash = auth.hash_setup_code(code)
    new_hash = auth.hash_password(new_password)
    try:
        with get_db_cursor() as cur:
            # One-time: the UPDATE only succeeds while the unexpired code still matches.
            # The new password hash revokes every existing session for this employer.
            cur.execute("""
                UPDATE beta_employers
                SET password_hash = %s, setup_code_hash = NULL, setup_code_expires_at = NULL
                WHERE id = (
                    SELECT id FROM beta_employers
                    WHERE lower(btrim(admin_email)) = %s
                      AND setup_code_hash = %s
                      AND setup_code_expires_at > now()
                    ORDER BY id LIMIT 1
                )
                RETURNING id, company_name;
            """, (new_hash, email_key, code_hash))
            row = cur.fetchone()
    except Exception as e:
        report_failure("ICJ-ERR-513", "employer_portal.setup_code.redeem", "❌ The setup code could not be processed. Please try again.", e)
        return

    if row:
        _start_employer_session(row[0], row[1], new_hash)
        st.rerun()
    _register_failed_attempt("employer")
    st.error("Invalid or expired setup code for that email.")


# ==============================================================================
# 🔐 MANAGER ACCESS (named accounts from secrets + signed session token)
# ==============================================================================
def render_manager_access_panel():
    """Sidebar sign-in that gates the Manager Console."""
    with st.sidebar.expander("🔐 Manager Access", expanded=False):
        accounts = _manager_accounts()
        if not accounts:
            st.caption("Manager Console is disabled. Add a `[managers]` section to the secrets to enable it.")
            st.session_state.pop("manager_token", None)
            return

        manager = current_manager()
        if manager:
            st.success(f"Signed in as **{manager}**.")
            if st.button("Sign out", key="manager_sign_out"):
                st.session_state.pop("manager_token", None)
                st.rerun()
            return
        st.session_state.pop("manager_token", None)  # drop expired or invalid tokens

        if _too_many_attempts("manager"):
            st.error("Too many failed attempts in this session.")
            return

        with st.form("manager_login_form", clear_on_submit=True):
            username = st.text_input("Manager username")
            password = st.text_input("Manager password", type="password")
            submitted = st.form_submit_button("Sign in")
        if submitted:
            stored_hash = accounts.get((username or "").strip())
            if stored_hash and auth.verify_password(password or "", stored_hash):
                _start_manager_session(username.strip(), stored_hash)
                st.rerun()
            if not stored_hash:
                auth.burn_verification_time(password or "")
            _register_failed_attempt("manager")
            st.error("Incorrect username or password.")


# ==============================================================================
# 🧠 MANAGER CONSOLE (live matching, simulation sandbox, employer access, checks)
# ==============================================================================
MATCH_CANDIDATE_LIMIT = 500
JOB_LIST_LIMIT = 200
# Simulation sandbox only: demo worksite and requirements. Never used by live matching.
SIMULATION_WORKSITE = (34.6029, -86.9820)
SIMULATION_REQUIREMENTS = "Looking for a specialized Python developer with strong expertise running relational SQL databases and deploying microservices inside Docker containers."


def load_match_candidates():
    """Candidates eligible for matching (most recent first, capped). Historical rows
    stored at the old geocoding fallback point are excluded (kept in the table)."""
    with get_db_cursor() as cur:
        cur.execute(f"""
            SELECT id, name, contact_email, role, resume, latitude, longitude
            FROM candidate_profiles
            WHERE contact_email IS NOT NULL AND btrim(contact_email) <> ''
              AND application_status <> 'REJECTED'
              AND {db_schema.trusted_location_sql()}
            ORDER BY id DESC
            LIMIT %s;
        """, (MATCH_CANDIDATE_LIMIT,))
        return [CandidateRecord(*row) for row in cur.fetchall()]


def _results_table(results):
    return [
        {
            "Candidate": r.candidate.name,
            "Email": r.candidate.contact_email,
            "Semantic score %": r.semantic_percentage,
            "Distance (mi)": r.distance_miles,
            "Skills matched": ", ".join(r.matched_skills) or "None",
            "Result": "✅ Match" if r.passed else "❌ Gated",
            "Reason": r.reason,
        }
        for r in sorted(results, key=lambda r: (not r.passed, -r.semantic_percentage))
    ]


def live_matching_tab():
    st.markdown("### 🎯 Live Job Matching")
    st.caption(
        "Matches candidates against a real posted job using that job's own requirements, location and radius. "
        "Verified matches are logged to the `semantic_matches` ledger for the upcoming 3-Way Candidate Push Engine. "
        "No notifications are sent: the mail gateway is not connected yet."
    )
    try:
        with get_db_cursor() as cur:
            cur.execute(f"""
                SELECT j.id, j.title, j.department, j.requirements, j.latitude, j.longitude,
                       j.match_radius_miles, j.employer_id, e.admin_email, e.company_name,
                       e.verified_at IS NOT NULL, NOT ({db_schema.trusted_location_sql('j')})
                FROM employer_jobs j
                LEFT JOIN beta_employers e ON e.id = j.employer_id
                ORDER BY j.id DESC
                LIMIT %s;
            """, (JOB_LIST_LIMIT,))
            jobs = cur.fetchall()
    except Exception as e:
        report_failure("ICJ-ERR-514", "manager_console.live_matching.load_jobs", "❌ Could not load posted jobs.", e)
        return
    if not jobs:
        st.info("No jobs have been posted yet.")
        return

    options = {f"#{r[0]} · {r[1]} · {r[9] or 'legacy job (no employer account)'}": r for r in jobs}
    row = options[st.selectbox("Job to match", list(options))]
    job = JobCriteria(
        job_id=row[0], title=row[1] or "", department=row[2] or "", requirements=row[3] or "",
        latitude=row[4], longitude=row[5], radius_miles=float(row[6] or 25),
        employer_id=row[7], employer_email=row[8], company_name=row[9],
    )
    st.write(f"**Radius:** {job.radius_miles:g} mi · **Minimum semantic score:** {job.min_semantic_score:g}% · "
             f"**Requirements:** {job.requirements or '_none recorded (legacy job): title and department only_'}")

    employer_verified, legacy_fallback = bool(row[10]), bool(row[11])
    if not job.has_coordinates:
        st.warning("This job has no worksite coordinates, so it can't be matched.")
        return
    if legacy_fallback:
        st.warning("This historical job is stored at the old unverified fallback location, so it is excluded from live matching. The record is kept unchanged.")
        return
    if job.employer_id is None or not job.employer_email:
        st.warning("This legacy job has no employer account, so it is excluded from live matching.")
        return
    if not employer_verified:
        st.warning("This job's employer is not verified, so it is excluded from live matching.")
        return
    can_log = True

    if not st.button("Run matching for this job", key="run_live_matching"):
        return
    try:
        candidates = load_match_candidates()
    except Exception as e:
        report_failure("ICJ-ERR-509", "manager_console.live_matching.load_candidates", "❌ Failed to access candidate records.", e)
        return

    results = [evaluate_match(job, c) for c in candidates]
    passed = [r for r in results if r.passed]
    newly_logged = already_logged = 0
    if can_log and passed:
        logged_at = datetime.now(timezone.utc).isoformat()
        try:
            with get_db_cursor() as cur:
                for r in passed:
                    cur.execute("""
                        INSERT INTO semantic_matches (candidate_profile_id, job_id, candidate_email, employer_email,
                                                      match_score, distance_miles, status, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (candidate_profile_id, job_id)
                            WHERE candidate_profile_id IS NOT NULL AND job_id IS NOT NULL
                        DO NOTHING
                        RETURNING id;
                    """, (r.candidate.profile_id, job.job_id, r.candidate.contact_email, job.employer_email,
                          r.semantic_percentage, r.distance_miles, "QUEUED_PENDING_GATEWAY",
                          json.dumps(build_push_payload(job, r, logged_at))))
                    if cur.fetchone():
                        newly_logged += 1
                    else:
                        already_logged += 1
        except Exception as e:
            report_failure("ICJ-ERR-510", "manager_console.live_matching.log_matches", "❌ The matches could not be logged.", e)
            newly_logged = already_logged = 0

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Candidates evaluated", len(results))
    with col2:
        st.metric("Matches", len(passed))
    with col3:
        st.metric("Newly logged", newly_logged)
    if newly_logged:
        st.info(f"📝 {newly_logged} match payload(s) logged for the 3-Way Candidate Push Engine. **No notifications were sent.**")
    if already_logged:
        st.caption(f"{already_logged} match(es) were already in the ledger for this job.")
    if len(candidates) == MATCH_CANDIDATE_LIMIT:
        st.caption(f"Only the {MATCH_CANDIDATE_LIMIT} most recent candidates were evaluated.")
    if results:
        st.dataframe(_results_table(results), width="stretch")


def simulation_tab():
    st.markdown("### 🧪 Simulation Sandbox")
    st.warning("Simulation only: demo requirements and a demo worksite. **Nothing is saved or logged.**")
    col_job1, col_job2 = st.columns(2)
    with col_job1:
        requirements = st.text_area("Demo job requirements", value=SIMULATION_REQUIREMENTS, height=90)
    with col_job2:
        radius = st.slider("Demo radius (miles)", 1, 50, 15)
        st.caption(f"Demo worksite: {SIMULATION_WORKSITE[0]}, {SIMULATION_WORKSITE[1]}")
    if not st.button("Run simulation", key="run_simulation"):
        return
    try:
        candidates = load_match_candidates()
    except Exception as e:
        report_failure("ICJ-ERR-509", "manager_console.simulation.load_candidates", "❌ Failed to access candidate records.", e)
        return
    demo_job = JobCriteria(job_id=None, title="Simulation", requirements=requirements,
                           latitude=SIMULATION_WORKSITE[0], longitude=SIMULATION_WORKSITE[1], radius_miles=float(radius))
    results = [evaluate_match(demo_job, c) for c in candidates]
    st.metric("Would match", sum(r.passed for r in results))
    if results:
        st.dataframe(_results_table(results), width="stretch")


def employer_access_tab():
    st.markdown("### 🔑 Employer Access")
    st.caption("Employers who registered before passwords existed need a one-time setup code to sign in. "
               "A code can also reset a forgotten password.")
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT id, company_name, admin_email, password_hash IS NOT NULL,
                       coalesce(setup_code_expires_at > now(), false), verified_at IS NOT NULL
                FROM beta_employers ORDER BY id DESC LIMIT 500;
            """)
            employers = cur.fetchall()
    except Exception as e:
        report_failure("ICJ-ERR-515", "manager_console.employer_access.load", "❌ Could not load employers.", e)
        return
    if not employers:
        st.info("No employers have registered yet.")
        return

    def label(r):
        state = ["VERIFIED" if r[5] else "unverified", "has password" if r[3] else "no password yet"]
        if r[4]:
            state.append("setup code active")
        return f"#{r[0]} · {r[1]} · {r[2]} ({', '.join(state)})"

    options = {label(r): r for r in employers}
    chosen = options[st.selectbox("Employer", list(options))]

    st.caption("Verify an employer only after confirming the company and its administrator through a channel you trust. "
               "Unverified employers can sign in but cannot publish jobs.")
    col_verify, col_revoke = st.columns(2)
    with col_verify:
        verify_clicked = st.button("Mark as verified", key="verify_employer", disabled=bool(chosen[5]))
    with col_revoke:
        revoke_clicked = st.button("Revoke verification", key="revoke_employer_verification", disabled=not chosen[5])
    if verify_clicked or revoke_clicked:
        try:
            with get_db_cursor() as cur:
                if verify_clicked:
                    cur.execute("UPDATE beta_employers SET verified_at = now(), verified_by = %s WHERE id = %s;",
                                (current_manager(), chosen[0]))
                else:
                    cur.execute("UPDATE beta_employers SET verified_at = NULL, verified_by = NULL WHERE id = %s;", (chosen[0],))
        except Exception as e:
            report_failure("ICJ-ERR-515", "manager_console.employer_access.verification", "❌ The verification change was not saved.", e)
            return
        st.success(f"{chosen[1]} is now {'verified' if verify_clicked else 'unverified'}.")
        st.rerun()
    if st.button("Generate one-time setup code", key="issue_setup_code"):
        code = auth.new_setup_code()
        try:
            with get_db_cursor() as cur:
                cur.execute("""
                    UPDATE beta_employers
                    SET setup_code_hash = %s, setup_code_expires_at = now() + make_interval(hours => %s)
                    WHERE id = %s;
                """, (auth.hash_setup_code(code), auth.SETUP_CODE_TTL_HOURS, chosen[0]))
        except Exception as e:
            report_failure("ICJ-ERR-515", "manager_console.employer_access.issue_code", "❌ The setup code could not be created.", e)
            return
        st.success(f"Setup code for {chosen[1]} (shown once, valid for {auth.SETUP_CODE_TTL_HOURS} hours):")
        st.code(code)
        st.caption("Share it with the employer through a channel you trust. Generating a new code replaces this one.")


def _dependency_report():
    """Pinned versions from requirements.txt compared with what is installed here."""
    from importlib import metadata
    rows = []
    req_file = Path(__file__).with_name("requirements.txt")
    for line in req_file.read_text().splitlines():
        line = line.split("#")[0].strip()
        if "==" not in line:
            continue
        name, pinned = (part.strip() for part in line.split("==", 1))
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = "not installed"
        rows.append({"Package": name, "Pinned": pinned, "Installed": installed,
                     "Status": "✅ match" if installed == pinned else "❌ mismatch"})
    return rows


def system_checks_tab():
    st.markdown("### 🩺 System Checks")
    st.caption("Run this on the deployed app to confirm the live environment matches the pinned versions and the migration result.")
    try:
        report = ensure_schema()
        with get_db_cursor() as cur:
            cur.execute("SELECT version();")
            server_version = cur.fetchone()[0]
        deps = _dependency_report()
    except Exception as e:
        report_failure("ICJ-ERR-516", "manager_console.system_checks", "❌ System checks could not run.", e)
        return

    st.markdown("#### Dependencies")
    st.write(f"**Python:** {sys.version.split()[0]} · **Database:** {server_version}")
    st.dataframe(deps, width="stretch")

    st.markdown("#### UNIQUE email constraints")
    if report["complete"]:
        st.success("Migration complete: database-level email uniqueness verified active in the catalog.")
    else:
        st.error("Migration NOT complete: email uniqueness is not verified active. App-level duplicate checks remain on.")
    for table, entry in report["constraints"].items():
        status = entry["status"]
        icon = "✅" if status == "enforced" and entry["verification"]["active"] else "⚠️"
        st.write(f"{icon} `{table}.{entry['column']}`: **{status}** · {entry['profile']}")
        for problem in entry["verification"]["problems"]:
            st.caption(f"Verification: {problem}")
        if entry["duplicates"]:
            st.warning("Duplicate emails block the constraint. Merge or correct these rows; the constraint is added on the next app restart.")
            st.dataframe([{"Email": d[0], "Copies": d[1], "Row ids": ", ".join(map(str, d[2]))} for d in entry["duplicates"]], width="stretch")
        if entry.get("error"):
            st.error("Creating this UNIQUE index failed. Details are in the server log (Diagnostic code: ICJ-ERR-518).")

    st.markdown("#### Historical fallback-location records")
    fallback = report["legacy_fallback_rows"]
    st.write(f"Kept unchanged but excluded from matching and maps: **{fallback['candidate_profiles']}** candidate profile(s), "
             f"**{fallback['employer_jobs']}** job(s) stored at the old fallback point {LEGACY_FALLBACK_COORDS}.")


def manager_console():
    """Managers only: shows candidate names, emails and scores."""
    manager = current_manager()
    if not manager:
        st.error("🔒 Manager sign-in required.")
        return
    st.caption(f"Signed in as manager **{manager}**.")
    tab_live, tab_sim, tab_access, tab_consent, tab_checks = st.tabs(
        ["🎯 Live Job Matching", "🧪 Simulation Sandbox", "🔑 Employer Access", "🛡️ Consent & Audit", "🩺 System Checks"]
    )
    with tab_live:
        live_matching_tab()
    with tab_sim:
        simulation_tab()
    with tab_access:
        employer_access_tab()
    with tab_consent:
        consent_audit_tab(manager)
    with tab_checks:
        system_checks_tab()


AUDIT_DISPLAY_LIMIT = 100


def consent_audit_tab(manager: str):
    """Record a candidate-requested consent withdrawal, and show the append-only audit trail."""
    st.markdown("### 🛡️ Candidate Consent")
    st.caption("Use this only when a candidate asks to withdraw employer-matching consent. "
               "Managers can withdraw consent but cannot grant it on a candidate's behalf.")
    with st.form("manager_consent_lookup", clear_on_submit=False):
        lookup_email = st.text_input("Candidate email (exact)", key="consent_lookup_email")
        looked_up = st.form_submit_button("Look up consent status")
    candidate_id = st.session_state.get("consent_lookup_candidate")
    if looked_up:
        candidate_id = None
        try:
            with get_db_cursor() as cur:
                cur.execute("SELECT id FROM candidate_profiles WHERE lower(btrim(contact_email)) = %s ORDER BY id LIMIT 1;",
                            ((lookup_email or "").strip().lower(),))
                row = cur.fetchone()
        except Exception as e:
            report_failure("ICJ-ERR-521", "manager_console.consent.lookup", "❌ The lookup failed.", e)
            row = None
        candidate_id = row[0] if row else None
        st.session_state["consent_lookup_candidate"] = candidate_id
        if row is None:
            st.info("No candidate with that email.")
    if candidate_id:
        try:
            with get_db_cursor() as cur:
                state = consent_state(cur, candidate_id)
        except Exception as e:
            report_failure("ICJ-ERR-521", "manager_console.consent.state", "❌ The consent status could not be loaded.", e)
            state = None
        if state:
            consented, changed_at, policy = state
            st.write(f"Candidate #{candidate_id}: employer matching **{'ON' if consented else 'OFF'}**"
                     f"{f' (policy {policy}, since {changed_at})' if consented else ''}")
            if consented and st.button("Record consent withdrawal requested by the candidate", key="manager_withdraw_consent"):
                if change_candidate_consent(candidate_id, False, "manager", manager, "manager_console.consent.withdraw"):
                    st.success("Consent withdrawn and recorded in the audit log.")

    st.markdown("### 📜 Privacy Audit Log (append-only)")
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT id, created_at, event_type, actor_type, actor_ref, candidate_profile_id, employer_id, job_id, recommendation_id
                FROM privacy_audit_log ORDER BY id DESC LIMIT %s;
            """, (AUDIT_DISPLAY_LIMIT,))
            events = cur.fetchall()
    except Exception as e:
        report_failure("ICJ-ERR-521", "manager_console.audit.load", "❌ The audit log could not be loaded.", e)
        return
    if not events:
        st.info("No audit events yet.")
        return
    st.dataframe([{"#": e[0], "When": str(e[1]), "Event": e[2], "Actor": f"{e[3]}:{e[4] or ''}", "Candidate": e[5],
                   "Employer": e[6], "Job": e[7], "Recommendation": e[8]} for e in events], width="stretch")
    st.caption(f"Most recent {AUDIT_DISPLAY_LIMIT} events. Rows can't be edited or deleted.")


# ==============================================================================
# 🎛️ NAVIGATION SIDEBAR PANEL CONFIGURATION
# ==============================================================================
st.sidebar.title("🌍 InCityJobs.us")
st.sidebar.caption("United States Lower 48 Proximity Network")
nav_slot = st.sidebar.container()  # keeps navigation above the sign-in panel
render_manager_access_panel()

portal_options = ["Home Portal", "Employer Portal", "Employee Portal"]
if current_manager():
    portal_options.append("Manager Console")
page_selection = nav_slot.radio("Navigate Portals:", portal_options)

# ==============================================================================
# 🏠 PAGE BLOCK 1: HOME PORTAL
# ==============================================================================
if page_selection == "Home Portal":
    st.title("🚀 Hyper-Local Hiring Automation Platform")
    st.write("Welcome to the decentralized proximity routing platform interface dashboard.")

# ==============================================================================
# 🏢 PAGE BLOCK 2: EMPLOYER PORTAL
# ==============================================================================
elif page_selection == "Employer Portal":
    st.title("🏢 Employer Pre-Onboarding & Requisition Node")
    st.subheader(f"Secure Local Workforce Acquisition Matrix ({region_meta['region']})")

    emp_tab1, emp_tab2 = st.tabs(["📋 Corporate Registration", "💼 Post a Job (Requisition Form)"])

    with emp_tab1:
        st.error(prelaunch_message)
        st.write("---")
        with st.form("employer_registration_form"):
            st.markdown("### 🏢 Step 1: Corporate Profile Registration")
            raw_company = st.text_input("Legal Business / Company Name", placeholder="e.g., Apex Enterprise Logistics")
            raw_hq = st.text_input("Corporate Headquarters Address (Billing/Primary Operations)", placeholder="e.g., 100 Main St, City, State")
            raw_email = st.text_input("Primary Administrator Email Address", placeholder="hiring@yourcompany.com")
            raw_phone = st.text_input("Local Direct Phone Number", placeholder="e.g., (256) 555-0199")
            reg_password = st.text_input("Create Employer Password (at least 12 characters)", type="password")
            reg_password_confirm = st.text_input("Repeat Employer Password", type="password")

            st.markdown("### 📜 Step 2: Platform Compliance & API Deferred Frameworks")
            st.info("Deferred 14-Day Settlement active via Stripe framework parameters upon validation.")
            agree_check = st.checkbox("I explicitly agree to the National Terms of Service.")
            raw_sign = st.text_input("Type Authorized Representative Full Name to Sign")

            submit_btn = st.form_submit_button("INITIALIZE BETA PROFILE", type="primary")

        if submit_btn:
            company_name = InCityJobsDiagnostics.sanitize_input(raw_company)
            corporate_hq = InCityJobsDiagnostics.sanitize_input(raw_hq)
            admin_email = InCityJobsDiagnostics.sanitize_input(raw_email).lower()
            admin_phone = InCityJobsDiagnostics.sanitize_input(raw_phone)
            sign_name = InCityJobsDiagnostics.sanitize_input(raw_sign)

            result = InCityJobsDiagnostics.validate_inputs(
                company_name, corporate_hq, admin_email, admin_phone, sign_name, agree_check
            )
            password_issue = auth.password_problem(reg_password, reg_password_confirm)

            if result["code"] != "ICJ-OK-200":
                st.warning(f"⚠️ **SYSTEM NOTICE: CODE {result['code']}**")
                st.markdown(f"**Issue:** *{result['title']}*\n\n💡 **Solution:** {result['solution']}")
            elif password_issue:
                st.warning(f"⚠️ **Password:** {password_issue}")
            else:
                raw_token = f"{company_name}-{admin_email}-{sign_name}-CONSENT_TRUE"
                signature_hash = hashlib.sha256(raw_token.encode()).hexdigest()

                # --- LEDGER CHECK: is this administrator email already in beta_employers? ---
                # (The database also enforces this with a UNIQUE index; see db_schema.py.)
                duplicate_employer_found = False
                dup_check_failed = False
                try:
                    with get_db_cursor() as cur:
                        cur.execute("SELECT 1 FROM beta_employers WHERE lower(btrim(admin_email)) = %s LIMIT 1;", (admin_email,))
                        duplicate_employer_found = cur.fetchone() is not None
                except Exception as e:
                    dup_check_failed = True
                    report_failure("ICJ-ERR-504", "employer_portal.registration.duplicate_check", "❌ Could not verify existing registrations, so this registration was not saved. Please try again.", e)

                new_employer = None
                new_password_hash = auth.hash_password(reg_password)
                if dup_check_failed:
                    st.info("No changes were made. Your details are still in the form above.")
                elif not duplicate_employer_found:
                    # Save the new signup record permanently to the persistent cloud database vault.
                    # New accounts start UNVERIFIED (verified_at is NULL) and cannot post jobs yet.
                    new_employer = InCityJobsDiagnostics.save_employer_to_cloud(
                        company_name, corporate_hq, admin_email, admin_phone, sign_name, signature_hash,
                        new_password_hash,
                    )

                if duplicate_employer_found or new_employer == "duplicate":
                    st.warning("⚠️ **SYSTEM NOTICE: PROFILE REDUNDANCY DETECTED**")
                    st.markdown("**Employer portal:** *Corporate entity footprint already logged and finalized in an earlier organizational ledger row transaction.*")
                elif isinstance(new_employer, int):
                    _start_employer_session(new_employer, company_name, new_password_hash)
                    # Only confirmed facts: new accounts are always unverified (verified_at is NULL),
                    # so nothing here may claim verification, payments or notifications.
                    st.success("✅ **Registration successful.** You're signed in.")
                    st.info("⏳ **Verification pending.** Job posting is unavailable until an InCityJobs manager approves your company.")

    with emp_tab2:
        st.markdown("### 💼 Job Requisition & Footprint Ingestion Node")
        employer = current_employer()
        if employer is None:
            end_employer_session()  # clear expired, revoked or invalid tokens
            render_employer_sign_in()
        else:
            col_who, col_out = st.columns([3, 1])
            with col_who:
                st.success(f"Signed in as **{employer['company'] or 'employer'}**")
            with col_out:
                if st.button("Sign out", key="employer_sign_out"):
                    end_employer_session()
                    st.rerun()

        if employer is not None and not employer["verified"]:
            st.warning("⏳ **Verification pending.** Your company has not been verified yet, so job posting is locked. "
                       "An InCityJobs manager will verify your account; no jobs can be published until then.")
        elif employer is not None:
            with st.form("job_requisition_form"):
                job_title = st.text_input("Job Position Title", placeholder="e.g., Heavy Equipment Operator")
                department_metric = st.selectbox("Department Category Matrix", ["Logistics", "Manufacturing", "Technical Services", "Administrative", "Retail/Fulfillment"])
                pricing_tier = st.radio("Placement Fee Pricing Tier Assignment", ["Tier 1 ($300 Fee Locked)", "Tier 2 ($750 Fee Locked)", "Tier 3 ($1,500 Fee Locked)"])
                job_requirements = st.text_area("Job Requirements / Description (used for candidate matching)", height=100,
                                                placeholder="e.g., Forklift certification, 2+ years warehouse experience, inventory software")
                match_radius = st.slider("Candidate match radius (miles)", 5, 50, 25)

                st.markdown("#### 📍 Physical Worksite Proximity Parameters")
                site_address = st.text_input("Worksite Street Address", placeholder="e.g., 450 Logistics Blvd")
                site_city = st.text_input("Worksite City", placeholder="e.g., Priceville")
                site_state = st.text_input("Worksite State", placeholder="e.g., AL")
                site_zip = st.text_input("Worksite Zip Code", placeholder="e.g., 35603")
                job_submit = st.form_submit_button("PUBLISH LIVE REQUISITION", type="primary")

            if job_submit:
                # Authorization is re-checked at submit time from the database: valid
                # signed token, unchanged credentials, and a VERIFIED employer account.
                submitting_employer = current_employer()
                employer_id = submitting_employer["id"] if submitting_employer else None

                site_full_addr = f"{site_address}, {site_city}, {site_state} {site_zip}".strip(" ,")
                if submitting_employer is None or submitting_employer["id"] != employer["id"]:
                    end_employer_session()
                    st.error("🔒 Your employer session is no longer valid, so the job was not published. Please sign in again.")
                elif not submitting_employer["verified"]:
                    st.error("🔒 Your company is not verified, so the job was not published.")
                elif not job_title or not site_zip or not site_address or not job_requirements.strip():
                    st.error("❌ Operational criteria missing. Job title, requirements, worksite street address and ZIP code are required.")
                else:
                    with st.spinner("Injecting job blueprint parameters..."):
                        site_coords = None
                        try:
                            site_coords = geocode_location(site_full_addr, f"{site_city}, {site_state} {site_zip}")
                        except LocationNotFound:
                            st.warning(f"📍 **Worksite not found:** we couldn't place this worksite on the map, so the job was not published. {LOCATION_FORMAT_HINT}")
                        except Exception as geo_err:
                            report_failure("ICJ-ERR-506", "employer_portal.job_requisition.geocode", "⚠️ The location lookup service is unavailable right now, so the job was not published. Please try again in a minute.", geo_err, level="warning")

                    if site_coords is not None:
                        try:
                            with get_db_cursor() as cursor:
                                cursor.execute("""
                                    INSERT INTO employer_jobs (title, department, pricing_tier, address, latitude, longitude,
                                                               source_channel, external_tracking_id, employer_id, requirements, match_radius_miles)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                    RETURNING id;
                                """, (job_title, department_metric, pricing_tier, site_full_addr, site_coords[0], site_coords[1],
                                      "employer_portal", None, employer_id, job_requirements.strip(), match_radius))
                                new_job_id = cursor.fetchone()[0]

                            st.success("🎉 **JOB REQUISITION SYSTEM DEPLOYED LIVE TO NEON CLUSTER!**")
                            st.json({
                                "job_id": new_job_id,
                                "requisition_title": job_title,
                                "department": department_metric,
                                "pricing": pricing_tier,
                                "match_radius_miles": match_radius,
                                "coordinates": list(site_coords),
                                "status": "active_seeking_proximity_matches"
                            })
                        except Exception as e:
                            report_failure("ICJ-ERR-507", "employer_portal.job_requisition.save", "❌ Database Synchronization Pipeline Failure: the job was not published.", e)


# ==============================================================================
# 👥 PAGE BLOCK 3: EMPLOYEE PORTAL
# ==============================================================================
elif page_selection == "Employee Portal":
    st.title("👥 Employee Access Portal")
    st.subheader(f"Localized Talent Onboarding Verification Node ({region_meta['region']})")
    candidate_profile_ingestion_ui()

    # Consent controls only for the profile submitted in this browser session
    if st.session_state.get('last_ingested_candidate') is not None:
        st.write("---")
        render_own_consent_controls(st.session_state['last_ingested_candidate'])

    st.write("---")
    st.markdown("### 📊 Live Proximity Matching Matrix & Spatial Map")
    st.caption("Step 3.4 & 3.5 — Real-Time Spatial Filtering Array & Native Geospatial Mapping Matrix")

    # PRIVACY: only ever show the profile this visitor submitted in this session
    my_candidate_id = st.session_state.get('last_ingested_candidate')
    if my_candidate_id is None:
        st.info("💡 Complete a profile registration entry above to trigger the automated hyper-local matchmaking array. The map only shows your own submission.")
    else:
        try:
            import pandas as pd

            with get_db_cursor() as cursor:
                # Fetch this visitor's own candidate profile by its row ID
                cursor.execute("SELECT name, latitude, longitude FROM candidate_profiles WHERE id = %s;", (my_candidate_id,))
                latest_candidate = cursor.fetchone()

                # Active jobs only: posted by a VERIFIED employer, with real (geocoded)
                # coordinates. Historical fallback-point and unattributed jobs are kept
                # in the table but excluded here.
                cursor.execute(f"""
                    SELECT j.title, j.department, j.pricing_tier, j.address, j.latitude, j.longitude, j.match_radius_miles
                    FROM employer_jobs j
                    JOIN beta_employers e ON e.id = j.employer_id AND e.verified_at IS NOT NULL
                    WHERE j.latitude IS NOT NULL AND j.longitude IS NOT NULL
                      AND {db_schema.trusted_location_sql('j')};
                """)
                active_jobs = cursor.fetchall()

            if latest_candidate is None:
                st.session_state['last_ingested_candidate'] = None
                st.info("💡 Your earlier profile could not be found. Please submit the form above again.")
            elif not active_jobs:
                st.info("💡 Your profile is saved. No job openings have been posted yet, so there is nothing to match against.")
            else:
                cand_name, cand_lat, cand_lon = latest_candidate
                cand_coords = (cand_lat, cand_lon)

                st.info(f"Scanning target job footprints for candidate **{cand_name}** centered at target coordinates: `{cand_coords}`")

                # Create a simple mapping coordinates collection array
                map_data = []
                # Instantly seed the candidate's personal target location spot into the map tracking array
                map_data.append({"latitude": cand_lat, "longitude": cand_lon, "name": f"Candidate: {cand_name}"})

                match_found = False
                for job in active_jobs:
                    j_title, j_dept, j_price, j_addr, j_lat, j_lon, j_radius = job
                    distance_miles = geodesic_miles(cand_coords, (j_lat, j_lon))

                    # Each job's own match radius (25 miles for jobs posted before radii existed)
                    if distance_miles <= (j_radius or 25):
                        match_found = True
                        # Append matching job coordinates to our layout map tracking list
                        map_data.append({"latitude": j_lat, "longitude": j_lon, "name": f"Job: {j_title}"})

                        with st.expander(f"✨ MATCH FOUND: {j_title} ({j_dept}) — {round(distance_miles, 1)} Miles Away", expanded=True):
                            col1, col2 = st.columns(2)
                            with col1:
                                st.write(f"**Worksite Location:** {j_addr}")
                                st.write(f"**Distance Allocation:** {round(distance_miles, 2)} active miles.")
                            with col2:
                                st.metric(label="System Billing Value", value=str(j_price))
                                st.success("🎯 Match Status: High Proximity Verified")

                # 🔥 STEP 3.5 VISUALIZATION LAYOUT: Render the interactive geospatial dot map canvas
                st.markdown("#### 🗺️ Interactive Proximity Footprint Radar")
                st.map(pd.DataFrame(map_data), zoom=11, width="stretch")
                st.caption("Visual Legend: The interactive radar display plots your target profile center point relative to matching regional workplace openings.")

                if not match_found:
                    st.warning("🔍 Active scan complete: No vacancies currently found within the match radius of your target deployment location.")

        except Exception as e:
            report_failure("ICJ-ERR-508", "employee_portal.proximity_map.query", "⚠️ Spatial Match Engine Query Interrupted: your job matches could not be loaded.", e)

# ==============================================================================
# 🧠 PAGE BLOCK 4: MANAGER CONSOLE (managers only)
# ==============================================================================
# The page only appears in the navigation for a signed-in manager, and
# manager_console() re-validates the signed token before showing anything.
elif page_selection == "Manager Console":
    st.title("🧠 Manager Console")
    manager_console()
