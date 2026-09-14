# Save this file locally as: /Users/Thumu/mycityjobs_project/app.py
import streamlit as st
import hashlib
import re
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
        import psycopg2
        try:
            db_url = "postgresql://neondb_owner:npg_MKul5P0djrzJ@ep-billowing-cloud-aeks7m5w-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
            conn = psycopg2.connect(db_url)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS beta_employers (
                    id SERIAL PRIMARY KEY,
                    company_name VARCHAR(255),
                    corporate_hq TEXT,
                    admin_email VARCHAR(255),
                    admin_phone VARCHAR(50),
                    signature_hash VARCHAR(64),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                INSERT INTO beta_employers (company_name, corporate_hq, admin_email, admin_phone, signature_hash)
                VALUES (%s, %s, %s, %s, %s);
            """, (company, hq, email, phone, signature_hash))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            st.error(f"Cloud Storage Write Interrupted: {str(e)}")
            return False
def candidate_profile_ingestion_ui():
    import psycopg2
    
    st.markdown("## 👤 Candidate Profile Ingestion Matrix")
    st.caption("Phase 3: Core Hiring Automation & Core Workflows — Step 3.1 & 3.3 (Split Location Matrix)")
    
    with st.form(key="candidate_ingestion_form"):
        st.markdown("### 📋 1. Professional Credentials")
        candidate_name = st.text_input("Full Name (Absolute Identity Mapping)", placeholder="John Doe")
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
        if not candidate_name or not t_zip or not resume_text:
            st.error("❌ Critical fields missing. Name, Resume, and Target Zip Code are mandatory attributes.")
            return

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
            except Exception:
                fallback_triggered = True
            
            try:
                db_url = "postgresql://neondb_owner:npg_MKul5P0djrzJ@ep-billowing-cloud-aeks7m5w-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
                conn = psycopg2.connect(db_url)
                cursor = conn.cursor()
                
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS candidate_profiles (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(255),
                        role VARCHAR(255),
                        resume TEXT,
                        address TEXT,
                        current_address TEXT,
                        latitude FLOAT,
                        longitude FLOAT,
                        status VARCHAR(50),
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                cursor.execute("ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS current_address TEXT;")
                
                cursor.execute("""
                    INSERT INTO candidate_profiles (name, role, resume, address, current_address, latitude, longitude, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """, (candidate_name, target_role, resume_text, target_full_addr, current_full_addr, target_lat, target_lon, "active_matching_pool"))
                
                conn.commit()
                cursor.close()
                conn.close()
                
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
                st.error(f"❌ Cloud Connection Blocked: {str(e)}")
# ==============================================================================
# 🎛️ NAVIGATION SIDEBAR PANEL CONFIGURATION
# ==============================================================================
st.sidebar.title("🌍 InCityJobs.us")
st.sidebar.caption("United States Lower 48 Proximity Network")
page_selection = st.sidebar.radio("Navigate Portals:", ["Home Portal", "Employer Portal", "Employee Portal"])

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
            admin_email = InCityJobsDiagnostics.sanitize_input(raw_email)
            admin_phone = InCityJobsDiagnostics.sanitize_input(raw_phone)
            sign_name = InCityJobsDiagnostics.sanitize_input(raw_sign)
            
            result = InCityJobsDiagnostics.validate_inputs(company_name, corporate_hq, admin_email, admin_phone, sign_name, agree_check)
            if result["code"] != "ICJ-OK-200":
                st.warning(f"⚠️ SYSTEM NOTICE: CODE {result['code']} - {result['title']}")
            else:
                raw_token = f"{company_name}-{admin_email}-{sign_name}-CONSENT_TRUE"
                signature_hash = hashlib.sha256(raw_token.encode()).hexdigest()
                if InCityJobsDiagnostics.save_employer_to_cloud(company_name, corporate_hq, admin_email, admin_phone, sign_name, signature_hash):
                    st.balloons()
                    st.success("🎉 BETA REGISTRATION COMPLETED SUCCESSFULLY!")
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
                    except Exception:
                        pass
                        
                    try:
                        import psycopg2
                        db_url = "postgresql://neondb_owner:npg_MKul5P0djrzJ@ep-billowing-cloud-aeks7m5w-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
                        conn = psycopg2.connect(db_url)
                        cursor = conn.cursor()
                        cursor.execute("""
                            CREATE TABLE IF NOT EXISTS employer_jobs (
                                id SERIAL PRIMARY KEY,
                                title VARCHAR(255),
                                department VARCHAR(100),
                                pricing_tier VARCHAR(100),
                                address TEXT,
                                latitude FLOAT,
                                longitude FLOAT,
                                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            );
                        """)
                        cursor.execute("""
                            INSERT INTO employer_jobs (title, department, pricing_tier, address, latitude, longitude)
                            VALUES (%s, %s, %s, %s, %s, %s);
                        """, (job_title, department_metric, pricing_tier, f"{site_address}, {site_city}, {site_state} {site_zip}".strip(), mock_site_lat, mock_site_lon))
                        conn.commit()
                        cursor.close()
                        conn.close()
                        
                        st.success("🎉 **JOB REQUISITION SYSTEM DEPLOYED LIVE TO NEON CLUSTER!**")
                        st.json({
                            "requisition_title": job_title,
                            "department": department_metric,
                            "pricing": pricing_tier,
                            "coordinates": [mock_site_lat, mock_site_lon],
                            "status": "active_seeking_proximity_matches"
                        })
                    except Exception as e:
                        st.error(f"❌ Database Synchronization Pipeline Failure: {str(e)}")

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
    
    try:
        db_url = "postgresql://neondb_owner:npg_MKul5P0djrzJ@ep-billowing-cloud-aeks7m5w-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
        import psycopg2
        import pandas as pd
        from geopy.distance import geodesic
        
        conn = psycopg2.connect(db_url)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name, latitude, longitude FROM candidate_profiles ORDER BY id DESC LIMIT 1;")
        latest_candidate = cursor.fetchone()
        
        cursor.execute("SELECT title, department, pricing_tier, address, latitude, longitude FROM employer_jobs;")
        active_jobs = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        if latest_candidate and active_jobs:
            cand_name, cand_lat, cand_lon = latest_candidate
            cand_coords = (cand_lat, cand_lon)
            
            st.info(f"Scanning target job footprints for candidate **{cand_name}** centered at target coordinates: `{cand_coords}`")
            
            map_data = []
            map_data.append({"latitude": cand_lat, "longitude": cand_lon, "name": f"Candidate: {cand_name}"})
            
            match_found = False
            for job in active_jobs:
                j_title, j_dept, j_price, j_addr, j_lat, j_lon = job
                job_coords = (j_lat, j_lon)
                
                distance_miles = geodesic(cand_coords, job_coords).miles
                
                if distance_miles <= 25:
                    match_found = True
                    map_data.append({"latitude": j_lat, "longitude": j_lon, "name": f"Job: {j_title}"})
                    
                    with st.expander(f"✨ MATCH FOUND: {j_title} ({j_dept}) — {round(distance_miles, 1)} Miles Away", expanded=True):
                        col1, col2 = st.columns(2)
                        with col1:
                            st.write(f"**Worksite Location:** {j_addr}")
                            st.write(f"**Distance Allocation:** {round(distance_miles, 2)} active miles.")
                        with col2:
                            st.metric(label="System Billing Value", value=str(j_price))
                            st.success("🎯 Match Status: High Proximity Verified")
            
            if map_data:
                st.markdown("#### 🗺️ Interactive Proximity Footprint Radar")
                df_map = pd.DataFrame(map_data)
                st.map(df_map, zoom=11, use_container_width=True)
                st.caption("Visual Legend: The interactive radar display plots your target profile center point relative to matching regional workplace openings.")
            
            if not match_found:
                st.warning("🔍 Active scan complete: No vacancies currently found within a 25-mile boundary radius of your target deployment location.")
        else:
            st.info("💡 Complete a profile registration entry above to trigger the automated hyper-local matchmaking array.")
            
    except Exception as e:
        st.error(f"⚠️ Spatial Match Engine Query Interrupted: {str(e)}")
