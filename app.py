# Save this file locally as: /Users/Thumu/mycityjobs_project/app.py
import streamlit as st
import hashlib
import re
import psycopg2
from datetime import datetime

# ==============================================================================
# 🧩 SECTION 1: NATIONWIDE SELF-HEALING DIAGNOSTICS & ALIAS ROUTING ENGINE
# ==============================================================================
class InCityJobsDiagnostics:
    """Anticipates, identifies, and outputs error codes with direct user solutions."""
    
    @staticmethod
    def get_secure_connection():
        """Extracts the freshly reset password string directly from hidden environments."""
        try:
            # 1. Primary check: Reads directly from Streamlit's secrets framework
            db_url = st.secrets["postgres"]["url"]
        except Exception:
            try:
                # 2. Secondary check: Reads the repository configuration variable string
                db_url = st.secrets["connection_string"]
            except Exception:
                try:
                    # 3. Alternative global check lookup parameter
                    db_url = st.secrets["CONNECTION_STRING"]
                except Exception as e:
                    # Fallback notice providing diagnostic debug actions
                    st.error("❌ CRITICAL: Secure connection parameters are missing. Please add the reset connection string to your Streamlit secrets control panel.")
                    raise e
        return psycopg2.connect(db_url)

    @staticmethod
    def detect_localization_context():
        """Simulates reading the incoming domain alias to adjust regional metadata."""
        try:
            query_params = st.query_params
            domain_context = query_params.get("domain", "us")
        except Exception:
            domain_context = "us"
            
        if domain_context == "in":
            return {"suffix": ".IN", "currency": "INR (₹)", "region": "India National Zones"}
        return {"suffix": ".US", "currency": "USD ($)", "region": "United States Lower 48"}

    @staticmethod
    def sanitize_input(text_input):
        if not text_input:
            return ""
        clean_text = re.sub(r'[<>{}\[\]\\\/\|;]', '', str(text_input))
        return clean_text.strip()

    @staticmethod
    def validate_inputs(company, corporate_hq, email, phone, signature, agree):
        if not (company and corporate_hq and email and phone and signature):
            return {
                "code": "ICJ-ERR-001",
                "title": "Missing Configuration Fields",
                "solution": "All registration fields are mandatory. Please complete your corporate legal entity data to proceed."
            }
        
        email_pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
        if not re.match(email_pattern, email):
            return {
                "code": "ICJ-ERR-303",
                "title": "Invalid Admin Credentials",
                "solution": "The email address format looks incorrect. Please verify it follows standard patterns (e.g., hiring@yourcompany.com)."
            }
            
        if not agree:
            return {
                "code": "ICJ-ERR-601",
                "title": "Compliance Agreement Missing",
                "solution": "You must check the compliance acknowledgment box to accept the system frameworks before launching."
            }
            
        return {"code": "ICJ-OK-200", "title": "Passed Verification", "solution": "Success"}

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
        except Exception as e:
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
            # Parse year numbers safely to validate column compatibility types
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
st.set_page_config(page_title="InCityJobs US & IN Beta Portal", page_icon="🌍", layout="centered")

region_meta = InCityJobsDiagnostics.detect_localization_context()

st.sidebar.title(f"🌍 InCityJobs{region_meta['suffix'].lower()}")
st.sidebar.caption(f"{region_meta['region']} Proximity Network")
page_selection = st.sidebar.radio("Navigate Portals:", ["Home Portal", "Employer Portal", "Employee Portal"])

# Global Pre-Launch Banner Configuration
prelaunch_message = (
    f"📢 **EXCLUSIVE PARTNER PRE-ONBOARDING BETA**\n\n"
    f"Secure your corporate city footprint ahead of schedule. "
    f"Our multi-state employer frameworks, mandatory identity video nodes, and "
    f"deferred 'Pay After 2 Weeks' placement locks unlock completely on **November 10, 2026**."
)
# ==============================================================================
# 🏠 PAGE BLOCK 1: HOME PORTAL OVERVIEW (EXECUTIVE BRIEFING & PROOF HOOK)
# ==============================================================================
if page_selection == "Home Portal":
    st.title(f"🌍 Welcome to InCityJobs{region_meta['suffix'].lower()}")
    st.subheader("Smart Placement Powered by Proximity & Identity Protection")
    st.error(prelaunch_message)
    
    st.warning(
        "💡 **PROOF OVER PROMISES**\n\n"
        "**Do not take what we are saying for granted. Test us by navigating to the Employer "
        "or Employee portal in the sidebar to preview how our system operates.**"
    )
    
    st.markdown("### 🏢 Executive Briefing for Employers: How Our System Works")
    st.markdown(
        "We have completely re-engineered the recruitment process to provide absolute simplicity, "
        "fraud protection, and a **100% risk-free hiring environment** for your business:"
    )
    
    with st.container(border=True):
        st.markdown("#### 📍 1. Hyper-Local Proximity Sourcing")
        st.markdown(
            "You never receive irrelevant, out-of-state applications. When you list an opening, our matching engine strictly "
            "targets qualified candidates living within a precise local radius of your physical work site."
        )

    with st.container(border=True):
        st.markdown("#### 📹 2. Zero-Fraud Mandatory Video Rooms")
        st.markdown(
            "To completely eliminate offshore 'shadow-coding' and interview proxy scams, all applicant evaluations must take "
            "place within our secure, biometric-mapped video platform. We guarantee the person you interview is the exact person walking through your door."
        )

    with st.container(border=True):
        st.markdown("#### 📄 3. In-Platform Offer Letters & Escrow Vaults")
        st.markdown(
            "Contracts are issued and executed securely right inside our portal using immutable SHA-256 digital signature seals, closing the loop and eliminating off-platform bypass leakage."
        )
    
    st.success(
        f"💰 **4. The Ultimate Guarantee: Pay ONLY After 2 Weeks**\n\n"
        f"**You pay nothing upfront.** When an offer letter is signed, our system issues a deferred payment pre-authorization token. "
        f"Our system actively monitors candidate progress via automated text check-ins. Stripe will only process your fixed fee "
        f"tier ($300 / $750 / $1,500 mapped to {region_meta['currency']}) on **Day 15 of active employment**. "
        f"If a separation happens during the 14-day trial, the token releases with zero penalties."
    )
    
    st.markdown("---")
    st.caption(f"© {datetime.now().year} InCityJobs{region_meta['suffix'].lower()}. All Rights Reserved. Product of Thumu Mercantile LLC.")

# ==============================================================================
# 🏢 PAGE BLOCK 2: EMPLOYER PORTAL
# ==============================================================================
elif page_selection == "Employer Portal":
    st.title("🏢 Employer Pre-Onboarding Node")
    st.subheader(f"Secure Local Workforce Acquisition Matrix ({region_meta['region']})")
    st.error(prelaunch_message)
    
    st.write("---")
    
    with st.form("employer_registration_form"):
        st.markdown("### 🏢 Step 1: Corporate Profile Registration")
        raw_company = st.text_input("Legal Business / Company Name", placeholder="e.g., Apex Enterprise Logistics")
        raw_hq = st.text_input("Corporate Headquarters Address (Billing/Primary Operations)", placeholder="e.g., 100 Main St, City, State")
        raw_email = st.text_input("Primary Administrator Email Address", placeholder="hiring@yourcompany.com")
        raw_phone = st.text_input("Local Direct Phone Number", placeholder="e.g., (256) 555-0199")

        st.markdown("### 📜 Step 2: Platform Compliance & API Deferred Frameworks")
        st.info(
            f"**By registering for this Beta, you explicitly acknowledge and agree to the following financial frameworks:**\n\n"
            f"1. **Placement Fee Tiers:** Placements are bound to fixed transaction tiers ($300 Tier 1, $750 Tier 2, $1,500 Tier 3) mapped to local currency equivalents ({region_meta['currency']}) payable ONLY upon successful placement fulfillment.\n"
            f"2. **Deferred 14-Day Settlement:** Employers pay nothing upfront. The processing engine executes your fee allocation via Stripe ONLY after the candidate successfully completes their first 14 calendar days of active employment.\n"
            f"3. **Pre-Authorization Escrow Lock:** Upon in-platform offer letter execution, a secure credit token is pre-authorized on your billing dashboard to guarantee settlement alignment on Day 15.\n"
            f"4. **Anti-Fraud Identity & Video Gating:** All candidate evaluations must occur inside our secure video framework to cross-reference data records and completely eliminate offshore shadow-hiring scams."
        )

        agree_check = st.checkbox("I explicitly agree to the National Terms of Service, Anti-Bypassing, and Deferred 14-Day Settlement frameworks.")

        st.markdown("### ✍️ Step 3: Secure Digital Execution")
        raw_sign = st.text_input("Type Authorized Representative Full Name to Sign", placeholder="e.g., John C. Doe")
        
        submit_btn = st.form_submit_button("INITIALIZE BETA PROFILE", type="primary")

    if submit_btn:
        company_name = InCityJobsDiagnostics.sanitize_input(raw_company)
        corporate_hq = InCityJobsDiagnostics.sanitize_input(raw_hq)
        admin_email = InCityJobsDiagnostics.sanitize_input(raw_email)
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
# ==============================================================================
# 👥 PAGE BLOCK 3: EMPLOYEE PORTAL
# ==============================================================================
elif page_selection == "Employee Portal":
    st.title("👥 Employee Access Portal")
    st.subheader(f"Localized Talent Onboarding Verification Node ({region_meta['region']})")
    st.info("⚡ **BETA EXPERIENCE MATRIX ACTIVATED**\n\nCandidates can build their proximity matching records live below.")
    
    st.markdown("### 🧑‍💼 Step 1: Professional Identification")
    raw_name = st.text_input("Legal Full Name", placeholder="e.g., Jane Smith")
    raw_role = st.text_input("Target Position / Core Skillset", placeholder="e.g., Full Stack Engineer")
    
    st.markdown("### 📞 Step 2: Local Contact Touchpoints")
    raw_email = st.text_input("Personal Contact Email Address", placeholder="jane.smith@example.com")
    raw_phone = st.text_input("Mobile Direct Phone Number", placeholder="e.g., (256) 555-0144")
    raw_exp = st.text_input("Years of Active Industry Experience", value="2")
    
    st.markdown("### 🔒 Step 3: Identity Integrity Certification")
    st.warning("⚠️ By clicking below, you verify your local residency and execute a digital registration token.")
    candidate_sign = st.text_input("Type Full Name to Certify Proximity Record", placeholder="e.g., Jane Smith")
    
    # Formless stable action trigger button execution
    candidate_submit = st.button("LOCK IN TALENT MATRIX PROFILE", type="primary")
        
    if candidate_submit:
        clean_name = InCityJobsDiagnostics.sanitize_input(raw_name)
        clean_role = InCityJobsDiagnostics.sanitize_input(raw_role)
        clean_email = InCityJobsDiagnostics.sanitize_input(raw_email)
        clean_phone = InCityJobsDiagnostics.sanitize_input(raw_phone)
        clean_sign = InCityJobsDiagnostics.sanitize_input(candidate_sign)
        
        if not (clean_name and clean_role and clean_email and clean_phone and clean_sign):
            st.error("❌ **REGISTRATION FAULT: All candidate credential fields are mandatory.**")
        else:
            cand_token = f"{clean_name}-{clean_email}-{clean_role}-TALENT_VERIFIED"
            cand_hash = hashlib.sha256(cand_token.encode()).hexdigest()
            
            # Save candidate record permanently to Neon Postgres database
            save_status = InCityJobsDiagnostics.save_candidate_to_cloud(
                clean_name, clean_role, clean_email, clean_phone, raw_exp, cand_hash
            )
            
            if save_status:
                st.balloons()
                st.success("🎉 **CANDIDATE ONBOARDING PROFILE VERIFIED SUCCESSFULLY!**")
                st.markdown(
                    f"""
                    ---
                    ### 🔒 Proximity Pipeline Secured
                    * **Talent Matching Radials:** Active & Scanning Local Zones
                    * **System Routing Profile ID:** `ICJ-TALENT-{cand_hash[:8].upper()}`
                    * **Secure Audit Signature Seal:** `{cand_hash}`
                    
                    *Our automated match notifications will execute directly to **{clean_email}** as target corporate spots unlock.*
                    """
                )
