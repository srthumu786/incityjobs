# Save this file locally as: /Users/Thumu/mycityjobs_project/app.py
import streamlit as st
import hashlib
import re
from datetime import datetime, timezone
import math
import hmac
import json
import logging
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

logger = logging.getLogger("incityjobs")

# ==============================================================================
# 🗄️ CENTRALIZED DATABASE ACCESS (single pool + one-time schema setup)
# ==============================================================================
# Production tables used by every read, duplicate check and write in this app:
#   beta_employers      - employer registrations (Employer Portal, tab 1)
#   employer_jobs       - job requisitions        (Employer Portal, tab 2)
#   candidate_profiles  - candidate profiles      (Employee Portal + Phase 4)
#   semantic_matches    - Phase 4 match ledger / 3-Way Push Engine queue
#   system_telemetry_logs - handled runtime failures (Manual Diagnostic Core Override groundwork)
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS beta_employers (
    id SERIAL PRIMARY KEY,
    company_name VARCHAR(255),
    corporate_hq TEXT,
    admin_email VARCHAR(255),
    admin_phone VARCHAR(50),
    signature_hash VARCHAR(64),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_beta_employers_admin_email
    ON beta_employers (lower(admin_email));

CREATE TABLE IF NOT EXISTS candidate_profiles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255),
    contact_email VARCHAR(255),
    role VARCHAR(255),
    resume TEXT,
    address TEXT,
    current_address TEXT,
    latitude FLOAT,
    longitude FLOAT,
    status VARCHAR(50),
    application_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Migrations for tables created by older versions of this app
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS contact_email VARCHAR(255);
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS current_address TEXT;
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS application_status VARCHAR(32) NOT NULL DEFAULT 'PENDING';
CREATE INDEX IF NOT EXISTS idx_candidate_profiles_contact_email
    ON candidate_profiles (lower(contact_email));

CREATE TABLE IF NOT EXISTS employer_jobs (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255),
    department VARCHAR(100),
    pricing_tier VARCHAR(100),
    address TEXT,
    latitude FLOAT,
    longitude FLOAT,
    source_channel VARCHAR(64),
    external_tracking_id VARCHAR(128),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Attribution metrics for the Multi-Channel Push Architecture
ALTER TABLE employer_jobs ADD COLUMN IF NOT EXISTS source_channel VARCHAR(64);
ALTER TABLE employer_jobs ADD COLUMN IF NOT EXISTS external_tracking_id VARCHAR(128);

CREATE TABLE IF NOT EXISTS semantic_matches (
    id SERIAL PRIMARY KEY,
    candidate_profile_id INTEGER,
    candidate_email VARCHAR(255),
    employer_email VARCHAR(255),
    match_score FLOAT,
    distance_miles FLOAT,
    status VARCHAR(100),
    payload JSONB,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE semantic_matches ADD COLUMN IF NOT EXISTS candidate_profile_id INTEGER;
ALTER TABLE semantic_matches ADD COLUMN IF NOT EXISTS payload JSONB;

CREATE TABLE IF NOT EXISTS system_telemetry_logs (
    id SERIAL PRIMARY KEY,
    diagnostic_error_code VARCHAR(32) NOT NULL,
    error_context_vector TEXT NOT NULL,
    error_detail TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@st.cache_resource(show_spinner=False)
def _get_db_pool():
    """One thread-safe connection pool per server process, shared by every session."""
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=st.secrets["connection_string"],
        connect_timeout=10,
    )


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


def _release_connection(pool, conn):
    pool.putconn(conn, close=bool(conn.closed))


@st.cache_resource(show_spinner=False)
def ensure_schema() -> bool:
    """Create/migrate the production tables once per server process.
    A failure is not cached, so the next request simply retries."""
    pool = _get_db_pool()
    conn = _checkout_connection(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    except Exception:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        _release_connection(pool, conn)
    return True


@contextmanager
def get_db_cursor():
    """Yield a cursor on a pooled connection.
    Commits when the block finishes, rolls back on error, and always returns the
    connection to the pool. Usage:
        with get_db_cursor() as cur:
            cur.execute("SELECT ...", (param,))
    """
    ensure_schema()
    pool = _get_db_pool()
    conn = _checkout_connection(pool)
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
# 🩺 TRANSPARENT ERROR REPORTING (system_telemetry_logs)
# ==============================================================================
# Diagnostic codes for handled runtime failures. Each one is written to the Python
# log and to system_telemetry_logs, and shown to the user with the matching code.
DIAGNOSTIC_CODES = {
    "ICJ-ERR-501": "Candidate duplicate check failed",
    "ICJ-ERR-502": "Candidate profile save failed",
    "ICJ-ERR-503": "Candidate target-location geocoding failed",
    "ICJ-ERR-504": "Employer duplicate check failed",
    "ICJ-ERR-505": "Employer registration save failed",
    "ICJ-ERR-506": "Job worksite geocoding failed",
    "ICJ-ERR-507": "Job requisition save failed",
    "ICJ-ERR-508": "Employee proximity map query failed",
    "ICJ-ERR-509": "Manager console candidate read failed",
    "ICJ-ERR-510": "Manager console match logging failed",
}


def log_system_event(code: str, context: str, exc: BaseException = None) -> None:
    """Record a handled failure in the Python log and in system_telemetry_logs.
    Never raises, so reporting an error can't break the request that hit it."""
    detail = f"{type(exc).__name__}: {exc}"[:2000] if exc else None
    logger.error("%s [%s] %s", code, context, detail or DIAGNOSTIC_CODES.get(code, ""), exc_info=exc)
    if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
        # The database itself is unreachable; skip the telemetry write instead of
        # making the user wait for a second connection timeout.
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
    """Log the failure and show the user a message carrying its diagnostic code."""
    log_system_event(code, context, exc)
    show = st.warning if level == "warning" else st.error
    show(f"{user_message} (Diagnostic code: {code})")


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
    def save_employer_to_cloud(company, hq, email, phone, sign, signature_hash) -> bool:
        try:
            with get_db_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO beta_employers (company_name, corporate_hq, admin_email, admin_phone, signature_hash)
                    VALUES (%s, %s, %s, %s, %s);
                """, (company, hq, email, phone, signature_hash))
            return True
        except Exception as e:
            report_failure("ICJ-ERR-505", "employer_portal.registration.save", "❌ Cloud Storage Write Interrupted: your registration was not saved.", e)
            return False


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
            
        submit_button = st.form_submit_button(label="Lock Profile & Validate Location Paths")
        
    if submit_button:
        # 1. ENFORCE MANDATORY FIELDS: Prevent blank email strings from hitting the database
        if not candidate_name or not candidate_email or not t_zip or not resume_text:
            st.error("❌ Critical Fields Missing: Candidate Name, Secure Email, Resume Text, and Target Zip Code are mandatory attributes.")
            st.stop()
            
        # Normalize the email so duplicate checks are case-insensitive
        candidate_email = candidate_email.strip().lower()

        # 2. STRUCTURAL NODE VERIFICATION: Ensure the email is formatted correctly before ingestion
        if "@" not in candidate_email or "." not in candidate_email:
            st.error("❌ Invalid Communication Node: Please verify the structural syntax of your candidate email address.")
            st.stop()

        # 3. LEDGER CHECK: is this email already registered in candidate_profiles?
        try:
            with get_db_cursor() as cur:
                cur.execute("SELECT 1 FROM candidate_profiles WHERE lower(contact_email) = %s LIMIT 1;", (candidate_email,))
                duplicate_candidate_found = cur.fetchone() is not None
        except Exception as e:
            report_failure("ICJ-ERR-501", "employee_portal.intake.duplicate_check", "❌ Could not verify existing profiles, so your profile was not saved. Please try again.", e)
            st.stop()

        if duplicate_candidate_found:
            st.warning("⚠️ **SYSTEM NOTICE: TALENT MATRIX REDUNDANCY**")
            st.markdown("**Employee portal:** *Profile already verified and processed in an earlier scanning ledger row transaction.*")
            st.stop()

        # 4. Geocode the target location, then save the profile
        with st.spinner("Executing dual-coordinate spatial parsing..."):
            current_full_addr = f"{c_street}, {c_city}, {c_state}".strip(", ")
            target_full_addr = f"{t_street}, {t_city}, {t_state} {t_zip}".strip()
            
            target_lat, target_lon = 34.5262, -86.9556
            fallback_triggered = False
            
            try:
                from geopy.geocoders import Nominatim
                geolocator = Nominatim(user_agent="incityjobs_spatial_resolver_v4")
                location = geolocator.geocode(target_full_addr, timeout=5)
                
                if location:
                    target_lat = location.latitude
                    target_lon = location.longitude
                else:
                    backup_location = geolocator.geocode(f"{t_city}, {t_state} {t_zip}".strip(), timeout=5)
                    if backup_location:
                        target_lat = backup_location.latitude
                        target_lon = backup_location.longitude
                    else:
                        fallback_triggered = True
            except Exception as geo_err:
                # Fallback coordinates are used; the user is told below via fallback_triggered
                fallback_triggered = True
                log_system_event("ICJ-ERR-503", "employee_portal.intake.geocode", geo_err)

            try:
                with get_db_cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO candidate_profiles (name, contact_email, role, resume, address, current_address, latitude, longitude, status, application_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id;
                    """, (candidate_name, candidate_email, target_role, resume_text, target_full_addr, current_full_addr, target_lat, target_lon, "active_matching_pool", "PENDING"))
                    new_candidate_id = cursor.fetchone()[0]

                # Remember this visitor's own row so the proximity map shows only their data
                st.session_state['last_ingested_candidate'] = new_candidate_id

                if fallback_triggered:
                    st.warning("ℹ️ **Proximity Mapping Complete:** The system couldn't verify that target street combo. Pinned to regional coordinates.")
                else:
                    st.success("🎉 **DYNAMIC DUAL-LOCATION INGESTION COMPLETED SUCCESSFULLY!**")
                
                st.markdown(f"""
                <div style="background-color:#f9f9f9; padding:20px; border-radius:10px; border:1px solid #eee; margin-top:15px;">
                    <h4 style="margin-top:0; color:#2e7d32;">🔒 Secure Split-Location Profile Locked</h4>
                    <ul style="list-style-type:none; padding-left:0; line-height:1.8;">
                        <li>👤 <b>Profile Identity:</b> {candidate_name}</li>
                        <li>💼 <b>Target Position Vector:</b> {target_role}</li>
                        <li>🏠 <b>Current Address Tracked:</b> {current_full_addr if current_full_addr else "Not Provided"}</li>
                        <li>🎯 <b>Target Deployment Zone:</b> {target_full_addr}</li>
                        <li>🛰️ <b>Geo-Mapping Status:</b> Active Radius Matching Pool Anchored</li>
                    </ul>
                </div>
                """, unsafe_allow_html=True)
                
            except Exception as e:
                report_failure("ICJ-ERR-502", "employee_portal.intake.save_profile", "❌ Cloud Connection Blocked: your profile was not saved. Please try again.", e)
# ==============================================================================
# 🎛️ NAVIGATION SIDEBAR PANEL CONFIGURATION
# ==============================================================================
MAX_MANAGER_LOGIN_ATTEMPTS = 5


def _manager_password():
    """The manager password from Streamlit secrets, or None when it isn't configured
    (in which case the Manager Console stays disabled for everyone)."""
    try:
        return st.secrets.get("admin_password") or None
    except Exception:
        # No secrets file at all (e.g. a fresh local checkout): keep the console disabled
        logger.warning("Streamlit secrets unavailable; Manager Console disabled")
        return None


def render_manager_access_panel():
    """Sidebar sign-in that gates the Phase 4 Manager Console."""
    st.session_state.setdefault("is_manager", False)
    st.session_state.setdefault("manager_failed_attempts", 0)

    with st.sidebar.expander("🔐 Manager Access", expanded=False):
        expected = _manager_password()
        if not expected:
            st.caption("Manager Console is disabled. Add an `admin_password` secret to enable it.")
            st.session_state["is_manager"] = False
            return

        if st.session_state["is_manager"]:
            st.success("Signed in as manager.")
            if st.button("Sign out", key="manager_sign_out"):
                st.session_state["is_manager"] = False
                st.rerun()
            return

        if st.session_state["manager_failed_attempts"] >= MAX_MANAGER_LOGIN_ATTEMPTS:
            st.error("Too many failed attempts in this session.")
            return

        with st.form("manager_login_form", clear_on_submit=True):
            password = st.text_input("Manager password", type="password")
            if st.form_submit_button("Sign in"):
                if hmac.compare_digest(password.encode(), str(expected).encode()):
                    st.session_state["is_manager"] = True
                    st.session_state["manager_failed_attempts"] = 0
                    st.rerun()
                else:
                    st.session_state["manager_failed_attempts"] += 1
                    logger.warning("Failed manager sign-in attempt")
                    time.sleep(1)  # slow down password guessing
                    st.error("Incorrect password.")


st.sidebar.title("🌍 InCityJobs.us")
st.sidebar.caption("United States Lower 48 Proximity Network")
nav_slot = st.sidebar.container()  # keeps navigation above the sign-in panel
render_manager_access_panel()

portal_options = ["Home Portal", "Employer Portal", "Employee Portal"]
if st.session_state.get("is_manager"):
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
        
            if result["code"] != "ICJ-OK-200":
                st.warning(f"⚠️ **SYSTEM NOTICE: CODE {result['code']}**")
                st.markdown(f"**Issue:** *{result['title']}*\n\n💡 **Solution:** {result['solution']}")
            else:
                raw_token = f"{company_name}-{admin_email}-{sign_name}-CONSENT_TRUE"
                signature_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            
                # --- LEDGER CHECK: is this administrator email already in beta_employers? ---
                duplicate_employer_found = False
                dup_check_failed = False
                try:
                    with get_db_cursor() as cur:
                        cur.execute("SELECT 1 FROM beta_employers WHERE lower(admin_email) = %s LIMIT 1;", (admin_email,))
                        duplicate_employer_found = cur.fetchone() is not None
                except Exception as e:
                    dup_check_failed = True
                    report_failure("ICJ-ERR-504", "employer_portal.registration.duplicate_check", "❌ Could not verify existing registrations, so this registration was not saved. Please try again.", e)

                if dup_check_failed:
                    st.info("No changes were made. Your details are still in the form above.")
                elif duplicate_employer_found:
                    st.warning("⚠️ **SYSTEM NOTICE: PROFILE REDUNDANCY DETECTED**")
                    st.markdown("**Employer portal:** *Corporate entity footprint already logged and finalized in an earlier organizational ledger row transaction.*")
                else:
                    # Save the new signup record permanently to the persistent cloud database vault
                    db_save_success = InCityJobsDiagnostics.save_employer_to_cloud(
                        company_name, corporate_hq, admin_email, admin_phone, sign_name, signature_hash
                    )
                
                    if db_save_success:
                        st.balloons()
                        st.success("🎉 **BETA REGISTRATION COMPLETED SUCCESSFULLY!**")
                        st.markdown(
                            f"""
                            ---
                            ### 🔒 Your Account is Locked in Secure Travel Mode
                            * **Company Status:** Fully Verified & Logged Nationwide
                            * **Monetization Engine:** Deferred Settlement Active ('Pay After 2 Weeks')
                            * **Merchant Routing Network:** Vaulted through Thumu Mercantile LLC
                            * **Database Routing ID:** `ICJ-GLOBAL-{signature_hash[:8].upper()}`
                            * **Cryptographic Contract Seal:** `{signature_hash}`
                        
                            **What Happens Next?**
                            * Your corporate city footprint is locked securely inside your database tables.
                            * Automated matching notifications will be routed directly to **{admin_email}**.
                            """
                        )
        
    with emp_tab2:
        st.markdown("### 💼 Job Requisition & Footprint Ingestion Node")
        with st.form("job_requisition_form"):
            job_title = st.text_input("Job Position Title", placeholder="e.g., Heavy Equipment Operator")
            department_metric = st.selectbox("Department Category Matrix", ["Logistics", "Manufacturing", "Technical Services", "Administrative", "Retail/Fulfillment"])
            pricing_tier = st.radio("Placement Fee Pricing Tier Assignment", ["Tier 1 ($300 Fee Locked)", "Tier 2 ($750 Fee Locked)", "Tier 3 ($1,500 Fee Locked)"])
            
            st.markdown("#### 📍 Physical Worksite Proximity Parameters")
            site_address = st.text_input("Worksite Street Address", placeholder="e.g., 450 Logistics Blvd")
            site_city = st.text_input("Worksite City", placeholder="e.g., Priceville")
            site_state = st.text_input("Worksite State", placeholder="e.g., AL")
            site_zip = st.text_input("Worksite Zip Code", placeholder="e.g., 35603")
            job_submit = st.form_submit_button("PUBLISH LIVE REQUISITION", type="primary")
            
        if job_submit:
            if not job_title or not site_zip or not site_address:
                st.error("❌ Operational criteria missing. Mandated elements unchecked.")
            else:
                with st.spinner("Injecting job blueprint parameters..."):
                    mock_site_lat, mock_site_lon = 34.5262, -86.9556
                    try:
                        from geopy.geocoders import Nominatim
                        geolocator = Nominatim(user_agent="incityjobs_spatial_resolver_v4")
                        loc = geolocator.geocode(f"{site_address}, {site_city}, {site_state} {site_zip}".strip(), timeout=5)
                        if loc:
                            mock_site_lat, mock_site_lon = loc.latitude, loc.longitude
                        else:
                            st.warning("ℹ️ The worksite address could not be matched, so the job was pinned to default regional coordinates.")
                    except Exception as geo_err:
                        report_failure("ICJ-ERR-506", "employer_portal.job_requisition.geocode", "⚠️ Worksite address lookup failed, so the job was pinned to default regional coordinates.", geo_err, level="warning")

                    try:
                        with get_db_cursor() as cursor:
                            cursor.execute("""
                                INSERT INTO employer_jobs (title, department, pricing_tier, address, latitude, longitude, source_channel, external_tracking_id)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                            """, (job_title, department_metric, pricing_tier, f"{site_address}, {site_city}, {site_state} {site_zip}".strip(), mock_site_lat, mock_site_lon, "employer_portal", None))

                        st.success("🎉 **JOB REQUISITION SYSTEM DEPLOYED LIVE TO NEON CLUSTER!**")
                        st.json({
                            "requisition_title": job_title,
                            "department": department_metric,
                            "pricing": pricing_tier,
                            "coordinates": [mock_site_lat, mock_site_lon],
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
            from geopy.distance import geodesic

            with get_db_cursor() as cursor:
                # Fetch this visitor's own candidate profile by its row ID
                cursor.execute("SELECT name, latitude, longitude FROM candidate_profiles WHERE id = %s;", (my_candidate_id,))
                latest_candidate = cursor.fetchone()

                # Pull all active job listings from the employer database table
                cursor.execute("SELECT title, department, pricing_tier, address, latitude, longitude FROM employer_jobs;")
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
                    j_title, j_dept, j_price, j_addr, j_lat, j_lon = job
                    if j_lat is None or j_lon is None:
                        continue  # job without coordinates can't be distance-matched
                    job_coords = (j_lat, j_lon)
                
                    # Compute exact geographical mileage distance
                    distance_miles = geodesic(cand_coords, job_coords).miles
                
                    # Dynamic matching threshold rule (25-mile operational boundaries)
                    if distance_miles <= 25:
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
                if map_data:
                    st.markdown("#### 🗺️ Interactive Proximity Footprint Radar")
                    df_map = pd.DataFrame(map_data)
                    # Native mapping widget calls zero compilation libraries and loads instantly
                    st.map(df_map, zoom=11, width="stretch")
                    st.caption("Visual Legend: The interactive radar display plots your target profile center point relative to matching regional workplace openings.")

                if not match_found:
                    st.warning("🔍 Active scan complete: No vacancies currently found within a 25-mile boundary radius of your target deployment location.")

        except Exception as e:
            report_failure("ICJ-ERR-508", "employee_portal.proximity_map.query", "⚠️ Spatial Match Engine Query Interrupted: your job matches could not be loaded.", e)
class Phase4SemanticEngine:
    """Pillars 4.1, 4.2 & 4.3: Local Natural Language Parsing & Cosine Math Matrix."""

    def __init__(self):
        # Local keyword vocabulary tracking
        self.skill_keywords = [
            "python", "javascript", "typescript", "react", "node", "sql", 
            "aws", "docker", "kubernetes", "machine learning", "nlp"
        ]
        # Common structural noise words to exclude from mathematical matrix
        self.stopwords = {"a", "an", "the", "and", "or", "of", "at", "by", "for", "with", "in", "to", "is"}

    def clean_text(self, text: str) -> str:
        """Strips noise, lowercases everything, and collapses white spaces."""
        if not text:
            return ""
        cleaned = re.sub(r"[^\w\s\-\.]", " ", text)
        return " ".join(cleaned.lower().split())

    def parse_profile(self, text: str) -> dict:
        """Extracts text structures and cross-references explicitly targeted skill metrics."""
        cleaned = self.clean_text(text)
        detected_skills = [
            s for s in self.skill_keywords if re.search(rf"\b{re.escape(s)}\b", cleaned)
        ]
        return {"cleaned_text": cleaned, "skills": detected_skills}

    def calculate_match(self, resume_text: str, job_text: str) -> dict:
        """Runs Pillar 4.3 geometric cosine vector similarity entirely in system memory."""
        res = self.parse_profile(resume_text)
        job = self.parse_profile(job_text)

        # Break text down into cleaned vocabulary keys
        res_tokens = [w for w in res["cleaned_text"].split() if w not in self.stopwords]
        job_tokens = [w for w in job["cleaned_text"].split() if w not in self.stopwords]

        # Assemble coordinates dynamically
        vocabulary = sorted(list(set(res_tokens + job_tokens)))
        if not vocabulary:
            # Same keys as the normal result so callers never hit a KeyError
            return {"semantic_percentage": 0.0, "matched_skills": []}

        # Mathematical feature coordinates
        res_vec = [float(res_tokens.count(word)) for word in vocabulary]
        job_vec = [float(job_tokens.count(word)) for word in vocabulary]

        # Geometry dot product calculations
        dot_product = sum(a * b for a, b in zip(res_vec, job_vec))
        mag_a = math.sqrt(sum(a * a for a in res_vec))
        mag_b = math.sqrt(sum(b * b for b in job_vec))

        semantic_score = (dot_product / (mag_a * mag_b)) if mag_a and mag_b else 0.0
        
        return {
            "semantic_percentage": round(semantic_score * 100, 2),
            "matched_skills": list(set(res["skills"]).intersection(set(job["skills"])))
        }


def ai_contextual_matching_dashboard():
    """Renders the final visual matrix workspace comparing jobs vs candidates.
    Managers only: it shows candidate names, emails and scores."""
    if not st.session_state.get("is_manager"):
        st.error("🔒 Manager sign-in required.")
        return

    from geopy.distance import geodesic  # Ensure geopy is utilized for precise radius gates

    st.markdown("---")
    st.markdown("## 🧠 Phase 4: Artificial Intelligence & Profile Matching Matrix")
    st.caption("Autonomous Context Engine — Self-Triggered Real-Time Connection Matrix")

    # 1. Setup the Reference Employer Baseline (Job Requirements & Worksite Location)
    st.markdown("### 🏢 Employer Reference Standard (Baseline)")
    col_job1, col_job2 = st.columns(2)
    with col_job1:
        sandbox_job_requirement = st.text_area(
            "Target Job Opening Requirements Matrix",
            value="Looking for a specialized Python developer with strong expertise running relational SQL databases and deploying microservices inside Docker containers.",
            height=70
        )
    with col_job2:
        employer_lat = 34.6029
        employer_lon = -86.9820
        max_allowed_radius = st.slider("Maximum Allowed Radius (Miles)", 1, 30, 15)

    if st.button("Run Autonomous AI & Geospatial Scanning Loop"):
        try:
            with get_db_cursor() as cursor:
                # Read the same production table the Employee Portal writes to
                cursor.execute("""
                    SELECT id, name, role, resume, contact_email, latitude, longitude
                    FROM candidate_profiles
                    WHERE contact_email IS NOT NULL AND contact_email <> ''
                    ORDER BY id DESC LIMIT 5;
                """)
                records = cursor.fetchall()
        except Exception as e:
            report_failure("ICJ-ERR-509", "manager_console.phase4.read_candidates", "❌ Failed to access verification matrix records.", e)
            return

        if not records:
            st.info("💡 Processing workspace idle. No records currently populate the matching pool.")
            return

        ai_engine = Phase4SemanticEngine()
        employer_email = "recruiter@mycityjobs.us"  # Production recruiter communication endpoint node

        st.markdown("### 📊 Live AI Scan Execution Metrics")
        for cand_id, name, role, resume, email, c_lat, c_lon in records:

            # A. Run AI Contextual Text Parsing and Vector Math
            analysis = ai_engine.calculate_match(resume, sandbox_job_requirement)
            semantic_pct = analysis['semantic_percentage']

            # B. Run Real-Time Geospatial Distance Verification
            distance_miles = 0.0
            if c_lat and c_lon:
                candidate_coords = (c_lat, c_lon)
                employer_coords = (employer_lat, employer_lon)
                distance_miles = round(geodesic(candidate_coords, employer_coords).miles, 1)

            # C. Check if profile satisfies the multi-dimensional match thresholds
            is_geo_valid = distance_miles <= max_allowed_radius
            is_semantic_valid = semantic_pct >= 50.0  # Core threshold boundary
            ai_match_verified = is_geo_valid and is_semantic_valid and email

            # Visual rendering container card based on AI processing decision
            status_emoji = "✅ MATCH LOGGED" if ai_match_verified else "❌ GATED (Sub-optimal Match)"

            with st.expander(f"👤 Candidate: {name} — Strategy: {status_emoji}"):
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("AI Semantic Score", f"{semantic_pct}%")
                with col2:
                    st.metric("Physical Proximity", f"{distance_miles} Miles")
                with col3:
                    st.write("**Target Skills Found:**")
                    st.text(", ".join(analysis['matched_skills']) if analysis['matched_skills'] else "None")

                st.markdown("---")
                st.write("### 🤖 AI Processing Log Output")

                if not email:
                    st.error("🔒 Profile skipped: Missing a valid candidate communication email endpoint node.")
                elif not is_geo_valid:
                    st.error(f"🔒 Profile skipped: Out of bounds. Located {distance_miles} miles away (Max limit: {max_allowed_radius} miles).")
                elif not is_semantic_valid:
                    st.error(f"🔒 Profile skipped: AI semantic text profile score ({semantic_pct}%) fails to clear target match benchmark threshold.")

                # 🚀 AUTOMATIC SELF-TRIGGER ACTIVATION NODE (only for verified matches)
                else:
                    st.success("⚡ **AI VERIFICATION PASSED:** Logging match payload for the 3-Way Candidate Push Engine...")

                    # Everything the upcoming push engine will need to notify both sides.
                    # Nothing is sent yet: the mail gateway is not connected.
                    match_payload = {
                        "engine": "3-way-candidate-push",
                        "delivery_status": "NOT_SENT_GATEWAY_PENDING",
                        "push_targets": ["candidate", "employer", "platform_ledger"],
                        "candidate": {
                            "profile_id": cand_id,
                            "name": name,
                            "contact_email": email,
                            "target_role": role,
                        },
                        "employer": {
                            "contact_email": employer_email,
                            "worksite_coordinates": [employer_lat, employer_lon],
                        },
                        "job_requirements": sandbox_job_requirement,
                        "scores": {
                            "semantic_percentage": semantic_pct,
                            "matched_skills": sorted(analysis['matched_skills']),
                            "distance_miles": distance_miles,
                            "max_radius_miles": max_allowed_radius,
                        },
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                    }

                    try:
                        with get_db_cursor() as cursor:
                            # One ledger row per candidate/employer pair (older rows count too)
                            cursor.execute("""
                                SELECT 1 FROM semantic_matches
                                WHERE lower(candidate_email) = lower(%s) AND employer_email = %s
                                LIMIT 1;
                            """, (email, employer_email))
                            already_logged = cursor.fetchone() is not None

                            if not already_logged:
                                cursor.execute("""
                                    INSERT INTO semantic_matches (candidate_profile_id, candidate_email, employer_email, match_score, distance_miles, status, payload)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb);
                                """, (cand_id, email, employer_email, semantic_pct, distance_miles, "QUEUED_PENDING_GATEWAY", json.dumps(match_payload)))

                        if not already_logged:
                            st.toast(f"Match logged for {name}")
                            st.info("📝 Match payload logged to the `semantic_matches` ledger for the upcoming 3-Way Candidate Push Engine. **No notifications were sent** — the mail gateway is not connected yet.")
                        else:
                            st.warning("ℹ️ This candidate/employer pair is already in the match ledger.")

                    except Exception as log_err:
                        report_failure("ICJ-ERR-510", "manager_console.phase4.log_match", "❌ The match could not be logged.", log_err)

# ==============================================================================
# 🧠 PAGE BLOCK 4: MANAGER CONSOLE (Phase 4, managers only)
# ==============================================================================
# Rendered here, after the functions it uses are defined. The page only appears in
# the navigation after a manager signs in, and the dashboard re-checks the role.
if page_selection == "Manager Console":
    st.title("🧠 Manager Console")
    ai_contextual_matching_dashboard()






                   
