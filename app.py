import streamlit as st
import hashlib
import re
from datetime import datetime

# ==============================================================================
# 🧩 SECTION 1: NATIONWIDE SELF-HEALING DIAGNOSTICS & ALIAS ROUTING ENGINE
# ==============================================================================
class MyCityJobsDiagnostics:
    """Anticipates, identifies, and outputs error codes with direct user solutions."""
    
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
                "code": "MCJ-ERR-001",
                "title": "Missing Configuration Fields",
                "solution": "All registration fields are mandatory. Please complete your corporate legal entity data to proceed."
            }
        
        email_pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
        if not re.match(email_pattern, email):
            return {
                "code": "MCJ-ERR-303",
                "title": "Invalid Admin Credentials",
                "solution": "The email address format looks incorrect. Please verify it follows the standard pattern (e.g., hiring@yourcompany.com)."
            }
            
        if not agree:
            return {
                "code": "MCJ-ERR-601",
                "title": "Compliance Agreement Missing",
                "solution": "You must check the compliance acknowledgment box to accept the system frameworks before launching."
            }
            
        return {"code": "MCJ-OK-200", "title": "Passed Verification", "solution": "Success"}

# ==============================================================================
# 🌍 SECTION 2: STREAMLIT USER INTERFACE & BETA LAYOUT
# ==============================================================================
st.set_page_config(page_title="MyCityJobs US & IN Beta Portal", st.write("---")

# 📢 THE CHALLENGE HOOK HEADER
st.warning(
    "💡 **PROOF OVER PROMISES**\n\n"
    "**Do not take what we are saying for granted. Test us by clicking the employer "
    "or employee portal below to see what our system gives.**"
)

st.write("---")
"🌍", layout="centered")

# Initialize Domain Alias Checking
region_meta = MyCityJobsDiagnostics.detect_localization_context()

st.title(f"🌍 MyCityJobs{region_meta['suffix'].lower()}")
st.subheader(f"Proximity Hiring & Payment Protection System ({region_meta['region']})")

# Global Pre-Launch Banner
st.error(
    f"📢 **EXCLUSIVE PARTNER PRE-ONBOARDING BETA**\n\n"
    f"Secure your corporate city footprint ahead of schedule. "
    f"Our multi-state employer frameworks, mandatory identity video nodes, and "
    f"deferred 'Pay After 2 Weeks' placement locks unlock completely on **November 10, 2026**."
)

st.write("---")

try:
    st.markdown("### 🏢 Step 1: Corporate Profile Registration")
    raw_company = st.text_input("Legal Business / Company Name", placeholder="e.g., Apex Enterprise Logistics")
    raw_hq = st.text_input("Corporate Headquarters Address (Billing/Primary Operations)", placeholder="e.g., 100 Main St, City, State")
    raw_email = st.text_input("Primary Administrator Email Address", placeholder="hiring@yourcompany.com")
    raw_phone = st.text_input("Local Direct Phone Number", placeholder="e.g., (256) 555-0199")

    company_name = MyCityJobsDiagnostics.sanitize_input(raw_company)
    corporate_hq = MyCityJobsDiagnostics.sanitize_input(raw_hq)
    admin_email = MyCityJobsDiagnostics.sanitize_input(raw_email)
    admin_phone = MyCityJobsDiagnostics.sanitize_input(raw_phone)

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
    sign_name = MyCityJobsDiagnostics.sanitize_input(raw_sign)

    st.write("")

    if st.button("INITIALIZE BETA PROFILE", type="primary"):
        result = MyCityJobsDiagnostics.validate_inputs(
            company_name, corporate_hq, admin_email, admin_phone, sign_name, agree_check
        )
        
        if result["code"] != "MCJ-OK-200":
            st.warning(f"⚠️ **SYSTEM NOTICE: CODE {result['code']}**")
            st.markdown(f"**Issue:** *{result['title']}*\n\n💡 **Solution:** {result['solution']}")
        else:
            raw_token = f"{company_name}-{admin_email}-{sign_name}-CONSENT_TRUE"
            signature_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            
            # Append signup securely to a local backup data file on your Mac
            with open("beta_employers_db.txt", "a") as db_file:
                db_file.write(f"[{datetime.now()}] CO: {company_name} | HQ: {corporate_hq} | MAIL: {admin_email} | HASH: {signature_hash}\n")
            
            st.balloons()
            st.success("🎉 **BETA REGISTRATION COMPLETED SUCCESSFULLY!**")
            st.markdown(
                f"""
                ***
                ### 🔒 Your Account is Locked in Secure Travel Mode
                * **Company Status:** Fully Verified & Logged Nationwide
                * **Monetization Engine:** Deferred Settlement Active ('Pay After 2 Weeks')
                * **Merchant Routing Network:** Vaulted through Thumu Mercantile LLC
                * **Database Routing ID:** `MCJ-GLOBAL-{signature_hash[:8].upper()}`
                * **Cryptographic Contract Seal:** `{signature_hash}`
                
                **What Happens Next?**
                * Your corporate city footprint is locked into our global directory database.
                * The candidate matching pool, secure video interview nodes, and deferred billing nodes activate on **November 10, 2026**.
                * A launch activation message will be delivered directly to **{admin_email}** on launch morning.
                ***
                """
            )
            st.caption("🛠️ Gated Production Modules (Launches Nov 10)")

except Exception as e:
    st.error(f"Execution Exception encountered: {e}")

# ==============================================================================
# 🔍 SECTION 3: STEP 4 - DYNAMIC FILTER & TRACK SELECTION ENGINE
# ==============================================================================
st.write("---")
st.markdown("### 🔍 Step 4: Proximity Matching & Placement Track Customization")

# 🗺️ 1. Proximity Radius Lock Component
target_zip = st.text_input("Enter Operations Target Zip Code", placeholder="e.g., 35601")
search_radius = st.slider("Select Proximity Hiring Radius (Miles)", min_value=50, max_value=100, value=75)

st.caption(f"📍 System Action: Matching candidates strictly within a {search_radius}-mile sandbox of Zip Code {target_zip}.")

# ⚡ 2. Free-Form Position Window
raw_position = st.text_input("What positions do you need to fill locally?", placeholder="e.g., CNC Machinist, Warehouse Operator")
position_needed = MyCityJobsDiagnostics.sanitize_input(raw_position)

# 🛠️ 3. The Two-Track Decision Toggle Switch
st.markdown("#### Choose Your Placement Framework Track")
track_selection = st.radio(
    "Select Platform Mode:",
    ["🛡️ Premium Protection Track (Full Arbitration & 14-Day Trial)", "⚡ Rapid Bypass Track (Low-Fee Connection, No Arbitration)"]
)

# 📊 4. Dynamic UI Layout Changes Based on Selected Track
if "Premium Protection" in track_selection:
    st.success(
        "💎 **PREMIUM TRACK ACTIVE**\n\n"
        "* **Monetization Framework:** Fixed placement tiers ($300 / $750 / $1,500) bound to Day 15 verification.\n"
        "* **Protections:** Full 14-day termination rules apply. Platform Dispute Arbitration Engine active if a split occurs."
    )
else:
    st.warning(
        "⚡ **RAPID BYPASS TRACK ACTIVE**\n\n"
        "* **Monetization Framework:** Charged a one-time, very low flat convenience fee instantly upon candidate connection.\n"
        "* **Arbitration Policy:** Strict 'As-Is' hiring. The 14-day trial monitoring and dispute processing modules are completely disabled."
    )
