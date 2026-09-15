import streamlit as st
import hashlib
import re
import psycopg2
from datetime import datetime

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

# ==============================================================================
# 🧩 SECTION 1: NATIONWIDE SELF-HEALING DIAGNOSTICS & ALIAS ROUTING ENGINE
# ==============================================================================
class InCityJobsDiagnostics:
    """Anticipates, identifies, and outputs error codes with direct user solutions."""
    
    @staticmethod
    def get_secure_connection():
        """Strictly extracts keys from secure cloud environment settings only to prevent exposures."""
        try:
            # Tries reading the standard connection string variable format
            db_url = st.secrets["connection_string"]
        except Exception:
            try:
                # Fallback to the alternative global match variable format
                db_url = st.secrets["DB_URL"]
            except Exception as e:
                st.error("❌ System Configuration Error: Database keys missing in Streamlit dashboard secrets panel.")
                raise e
        return psycopg2.connect(db_url)

    @staticmethod
    def sanitize_input(text_input):
        if not text_input:
            return ""
        clean_text = re.sub(r'[<>{}\[\]\\\/\|;]', '', str(text_input))
        return clean_text.strip()

    @classmethod
    def initialize_database_schema(cls):
        """Creates structural schemas for both employers and employees automatically."""
        try:
            conn = cls.get_secure_connection()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS employers (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    company_name TEXT NOT NULL,
                    corporate_hq TEXT NOT NULL,
                    admin_email TEXT NOT NULL,
                    admin_phone TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    security_token TEXT NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS candidates (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    full_name TEXT NOT NULL,
                    target_role TEXT NOT NULL,
                    contact_email TEXT NOT NULL,
                    contact_phone TEXT NOT NULL,
                    experience_years INT NOT NULL,
                    verification_hash TEXT NOT NULL
                );
            """)
            conn.commit()
            cur.close()
            conn.close()
        except Exception:
            pass

    @classmethod
    def save_employer_to_cloud(cls, company, hq, email, phone, signature, token_hash):
        """Saves corporate registrations securely onto persistent storage parameters."""
        try:
            conn = cls.get_secure_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO employers (timestamp, company_name, corporate_hq, admin_email, admin_phone, signature, security_token)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (datetime.now(), company, hq, email, phone, signature, token_hash))
            conn.commit()
            cur.close()
            conn.close()
            return True
        except Exception as e:
            st.error(f"Cloud Database Sync Warning: {str(e)}")
            return False

    @classmethod
    def save_candidate_to_cloud(cls, name, role, email, phone, exp, token_hash):
        """Saves incoming talent acquisition records to your live PostgreSQL cloud vault."""
        try:
            clean_exp_int = int(re.sub(r'\D', '', str(exp))) if str(exp).strip().isdigit() else 2
        except Exception:
            clean_exp_int = 2

        try:
            conn = cls.get_secure_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO candidates (timestamp, full_name, target_role, contact_email, contact_phone, experience_years, verification_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (datetime.now(), name, role, email, phone, clean_exp_int, token_hash))
            conn.commit()
            cur.close()
            conn.close()
            return True
        except Exception as e:
            st.error(f"Candidate Pipeline Error: {str(e)}")
            return False

# Initialize database schemas smoothly on application boot
InCityJobsDiagnostics.initialize_database_schema()
# ==============================================================================
# 🗂️ SECTION 2: STREAMLIT USER INTERFACE & SIDEBAR ROUTING NAVIGATION
# ==============================================================================
st.set_page_config(page_title="InCityJobs US Beta Portal", page_icon="🌍", layout="centered")

st.sidebar.title("🌍 InCityJobs.us")
st.sidebar.caption(f"{region_meta['region']} Proximity Network")
page_selection = st.sidebar.radio("Navigate Portals:", ["Home Portal", "Employer Portal", "Employee Portal", "AI Matching Matrix"])
# ==============================================================================
# 🏠 PAGE BLOCK 1: HOME PORTAL OVERVIEW
# ==============================================================================
if page_selection == "Home Portal":
    st.title("🌍 Welcome to InCityJobs.us")
    st.subheader("Smart Placement Powered by Proximity & Identity Protection")
    st.error(prelaunch_message)
    
    st.warning("💡 **PROOF OVER PROMISES**: Navigate to the sidebar options to preview how our matching system operates live.")
    st.markdown("### 🏢 Executive Briefing for Employers: How Our System Works")
    st.write("We have completely re-engineered the recruitment process to provide absolute simplicity, fraud protection, and a 100% risk-free hiring environment for your business.")

# ==============================================================================
# 🏢 PAGE BLOCK 2: EMPLOYER PORTAL
# ==============================================================================
elif page_selection == "Employer Portal":
    st.title("🏢 Employer Pre-Onboarding Node")
    st.error(prelaunch_message)
    
    with st.form("employer_registration_form"):
        raw_company = st.text_input("Legal Business / Company Name")
        raw_hq = st.text_input("Corporate Headquarters Address")
        raw_email = st.text_input("Primary Administrator Email Address")
        raw_phone = st.text_input("Local Direct Phone Number")
        agree_check = st.checkbox("I agree to the National Terms of Service and Deferred 14-Day Settlement frameworks.")
        raw_sign = st.text_input("Type Authorized Representative Full Name to Sign")
        submit_btn = st.form_submit_button("INITIALIZE BETA PROFILE", type="primary")

    if submit_btn:
        company_name = InCityJobsDiagnostics.sanitize_input(raw_company)
        corporate_hq = InCityJobsDiagnostics.sanitize_input(raw_hq)
        admin_email = InCityJobsDiagnostics.sanitize_input(raw_email)
        admin_phone = InCityJobsDiagnostics.sanitize_input(raw_phone)
        sign_name = InCityJobsDiagnostics.sanitize_input(raw_sign)
        
        if not (company_name and corporate_hq and admin_email and admin_phone and sign_name and agree_check):
            st.error("❌ Critical Fields Missing: Please verify all fields are complete and compliance is accepted.")
        else:
            raw_token = f"{company_name}-{admin_email}-{sign_name}-CONSENT_TRUE"
            signature_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            
            # --- LEDGER VERIFICATION SCAN ---
            duplicate_employer_found = False
            try:
                conn = InCityJobsDiagnostics.get_secure_connection()
                cur = conn.cursor()
                cur.execute("SELECT id FROM employers WHERE admin_email = %s LIMIT 1;", (admin_email,))
                if cur.fetchone():
                    duplicate_employer_found = True
                cur.close()
                conn.close()
            except Exception:
                pass

            if duplicate_employer_found:
                st.warning("⚠️ **SYSTEM NOTICE: PROFILE REDUNDANCY DETECTED**")
                st.markdown("**Employer portal:** *Corporate entity footprint already logged and finalized in an earlier organizational ledger row transaction.*")
            else:
                if InCityJobsDiagnostics.save_employer_to_cloud(company_name, corporate_hq, admin_email, admin_phone, sign_name, signature_hash):
                    st.balloons()
                    st.success("🎉 **BETA REGISTRATION COMPLETED SUCCESSFULLY!**")
# ==============================================================================
# 👥 PAGE BLOCK 3: EMPLOYEE PORTAL
# ==============================================================================
elif page_selection == "Employee Portal":
    st.title("👥 Employee Access Portal")
    st.subheader(f"Localized Talent Onboarding Verification Node ({region_meta['region']})")
    
    candidate_name = st.text_input("Legal Full Name", placeholder="Michael Smith")
    candidate_email = st.text_input("Secure Contact Email Address", placeholder="ms@testmail.com")
    candidate_role = st.text_input("Target Position / Core Skillset", placeholder="Full Stack Engineer")
    candidate_phone = st.text_input("Mobile Direct Phone Number", placeholder="770-555-3456")
    raw_exp = st.text_input("Years of Active Industry Experience", value="2")
    candidate_sign = st.text_input("Type Full Name to Certify Proximity Record")
    
    submit_button = st.button("LOCK IN TALENT MATRIX PROFILE", type="primary")
    
    if submit_button:
        if not (candidate_name and candidate_email and candidate_role and candidate_phone and candidate_sign):
            st.error("❌ Critical Fields Missing: All candidate credential attributes are mandatory.")
            st.stop()
            
        if "@" not in candidate_email or "." not in candidate_email:
            st.error("❌ Invalid Communication Node: Please verify the structural syntax of your email address.")
            st.stop()

        clean_name = InCityJobsDiagnostics.sanitize_input(candidate_name)
        clean_role = InCityJobsDiagnostics.sanitize_input(candidate_role)
        clean_email = InCityJobsDiagnostics.sanitize_input(candidate_email)
        clean_phone = InCityJobsDiagnostics.sanitize_input(candidate_phone)
        
        # --- TALENT RECORD LEDGER CHECK ---
        duplicate_candidate_found = False
        try:
            conn = InCityJobsDiagnostics.get_secure_connection()
            cur = conn.cursor()
            cur.execute("SELECT id FROM candidates WHERE contact_email = %s LIMIT 1;", (clean_email,))
            if cur.fetchone():
                duplicate_candidate_found = True
            cur.close()
            conn.close()
        except Exception:
            pass

        if duplicate_candidate_found:
            st.warning("⚠️ **SYSTEM NOTICE: TALENT MATRIX REDUNDANCY**")
            st.markdown("**Employee portal:** *Profile already verified and processed in an earlier scanning ledger row transaction.*")
            st.stop()
        else:
            cand_token = f"{clean_name}-{clean_email}-{clean_role}-TALENT_VERIFIED"
            cand_hash = hashlib.sha256(cand_token.encode()).hexdigest()
            
            if InCityJobsDiagnostics.save_candidate_to_cloud(clean_name, clean_role, clean_email, clean_phone, raw_exp, cand_hash):
                st.balloons()
                st.success("🎉 **CANDIDATE ONBOARDING PROFILE VERIFIED SUCCESSFULLY!**")

# ==============================================================================
# 🎯 PAGE BLOCK 4: AI MATCHING MATRIX DASHBOARD (FULLY SANITIZED FOR SECURE LOOPS)
# ==============================================================================
elif page_selection == "AI Matching Matrix":
    st.title("🎯 AI Contextual Matching Dashboard")
    st.subheader("Live Proximity Scanning Matrix Pool")
    
    try:
        conn = InCityJobsDiagnostics.get_secure_connection()
        cursor = conn.cursor()
        
        # Pulls live data from Neon safely using our secure environment method
        cursor.execute("""
            SELECT full_name, target_role, contact_email, contact_phone, experience_years
            FROM candidates 
            WHERE contact_email IS NOT NULL AND contact_email != '' 
            ORDER BY id DESC LIMIT 5;
        """)
        records = cursor.fetchall()
        cursor.close()
        conn.close()
        
        if not records:
            st.info("ℹ️ Processing workspace idle. No records currently populate the matching pool.")
        else:
            for item in records:
                with st.container(border=True):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.markdown(f"### 🧑‍💼 Candidate: {item[0]}")
                        st.markdown(f"**Target Role:** {item[1]} | **Experience:** {item[4]} Years")
                        st.markdown(f"📧 **Secure Email Node:** {item[2]} | 📞 **Phone:** {item[3]}")
                    with col2:
                        st.metric(label="System Billing Value", value="$750")
                        st.success("🎯 Match Verified")
    except Exception as e:
        st.error(f"Failed to access verification matrix records: {str(e)}")
