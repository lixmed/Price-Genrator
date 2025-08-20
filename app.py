import streamlit as st
import pandas as pd
import re, math, hashlib, requests, time
from io import BytesIO
from PIL import Image as PILImage
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Spacer, Paragraph, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A3
from reportlab.lib import colors
import tempfile, os
from datetime import datetime, timedelta
import gspread
from gspread_dataframe import get_as_dataframe, set_with_dataframe
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import Paragraph, Spacer, PageBreak
from reportlab.platypus import KeepInFrame
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ========== Page Config ==========
st.set_page_config(page_title="Quotation Builder", page_icon="🪑", layout="wide")

# ========== User Credentials ==========
def init_session_state():
    """Initialize session state variables"""
    defaults = {
        "logged_in": False,
        "user_email": None,
        "role": None,
        "form_submitted": False,
        "company_details": {},
        "rows": 1,
        "row_indices": [0],
        "selected_products": {},
        "sheet_data": None,
        "last_sheet_update": 0,
        "history": [],
        "pdf_data": [],  
        "cart": [],    
        "edit_mode": False,
        "reset_in_progress": False,
        "reset_step": None,
        "reset_email": None,
        "reset_token": None,
        "reset_token_expiry": None,
    }
    for key, default_value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default_value

init_session_state()


REFRESH_INTERVAL = 55 * 60

def _request_new_token():
    url = f"{st.secrets['zoho']['accounts_domain']}/oauth/v2/token"
    payload = {
        "refresh_token": st.secrets["zoho"]["refresh_token"],
        "client_id": st.secrets["zoho"]["client_id"],
        "client_secret": st.secrets["zoho"]["client_secret"],
        "grant_type": "refresh_token"
    }
    r = requests.post(url, data=payload)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("No access_token in Zoho response: " + str(data))

    now = time.time()
    st.session_state["zoho_access_token"] = token
    st.session_state["zoho_token_ts"] = now
    expires_in = data.get("expires_in")
    if expires_in:
        st.session_state["zoho_token_expires_at"] = now + int(expires_in)
    return token

def get_zoho_access_token():
    """Return a cached access token, refreshing if older than 55 minutes."""
    try:
        now = time.time()
        token = st.session_state.get("zoho_access_token")
        ts = st.session_state.get("zoho_token_ts", 0)

        # If we have a cached token and it's younger than REFRESH_INTERVAL, return it
        if token and (now - ts) < REFRESH_INTERVAL:
            return token

        # Otherwise request a fresh token and cache it
        return _request_new_token()

    except requests.exceptions.RequestException as e:
        st.error(f"Token refresh failed: {e.response.text if e.response else str(e)}")
        raise
    except Exception as e:
        st.error(f"Unexpected error while getting Zoho token: {str(e)}")
        raise


def fetch_zoho_accounts():
    """Fetch Account_Name, Phone, Owner, and Billing_Street from Zoho CRM"""
    try:
        token = get_zoho_access_token()
        url = f"{st.secrets['zoho']['crm_api_domain']}/crm/v2/Accounts"
        params = {"fields": "Account_Name,Phone,Owner,Billing_Street"}
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json().get("data", [])
    except Exception as e:
        st.error(f"⚠️ Failed to fetch accounts: {str(e)}")
        return []
    


def generate_temp_password(length=12):
    """Generate a secure temporary password with letters, digits and special characters"""
    characters = string.ascii_letters + string.digits + "!@#$%^&*"
    temp_password = ''.join(random.choice(characters) for _ in range(length))
    return temp_password


# st.write("🔍 DEBUG: Checking secrets configuration...")
# try:
#     # Check if SMTP section exists
#     if "smtp" in st.secrets:
#         st.write("✅ SMTP section found in secrets")
#         # Check individual SMTP values
#         st.write(f"SMTP server: {st.secrets['smtp'].get('server', 'NOT FOUND')}")
#         st.write(f"SMTP port: {st.secrets['smtp'].get('port', 'NOT FOUND')}")
#         st.write(f"SMTP username: {st.secrets['smtp'].get('username', 'NOT FOUND')}")
#         st.write(f"SMTP from_email: {st.secrets['smtp'].get('from_email', 'NOT FOUND')}")
#     else:
#         st.error("❌ SMTP section NOT FOUND in secrets")
    
#     # Check if entire secrets dictionary is empty
#     if not st.secrets:
#         st.error("❌ st.secrets is completely empty!")
    
# except Exception as e:
#     st.error(f"❌ Error checking secrets: {str(e)}")


def send_password_reset_email(user_email, new_password):
    """Send new password to user's email"""
    try:
        # Get SMTP settings from secrets
        smtp_config = st.secrets["smtp"]
        
        msg = MIMEMultipart()
        msg['From'] = smtp_config["from_email"]
        msg['To'] = user_email
        msg['Subject'] = "Your New Password for Quotation System"
        
        body = f"""
        Hello,
        
        We received a request to reset your password. Your new password is:
        
        {new_password}
        
        Please log in with this password and change it immediately.
        
        If you didn't request this password reset, please contact admin immediately.
        
        Best regards,
        Quotation System Team
        """
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(smtp_config["server"], smtp_config["port"])
        server.starttls()
        server.login(smtp_config["username"], smtp_config["password"])
        server.sendmail(smtp_config["from_email"], user_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        st.error(f"❌ Failed to send email: {str(e)}")
        return False

def update_password_in_sheet(email, new_password):
    """Update user's password directly in the Google Sheet"""
    try:
        # Load service account credentials
        creds_dict = st.secrets["gcp_service_account"]
        gc = gspread.service_account_from_dict(creds_dict)
        
        # Open the user credentials spreadsheet
        sh = gc.open_by_key("1c2IZtKKszQBSVf_4VWjZNv6-h3O9IwDizCTBhnVd1JE")
        worksheet = sh.sheet1
        
        # Get all values
        all_values = worksheet.get_all_values()
        
        if not all_values:
            st.error("User sheet is empty.")
            return False
            
        # Headers are in the first row
        headers = [h.strip().lower() for h in all_values[0]]
        
        # Find email and password column indices
        email_col_idx = next((i for i, h in enumerate(headers) if h == "email"), None)
        password_col_idx = next((i for i, h in enumerate(headers) if h == "password"), None)
        
        if email_col_idx is None or password_col_idx is None:
            st.error("Required columns not found in user sheet.")
            return False
            
        # Find the user's row
        user_found = False
        for i, row in enumerate(all_values[1:], start=2):  # Start at 2 (row 1 is headers)
            if row[email_col_idx].lower() == email.lower():
                # Update password
                worksheet.update_cell(i, password_col_idx + 1, new_password)
                user_found = True
                break
                
        if not user_found:
            st.error("User not found in sheet.")
            return False
            
        # Clear the cache to reload users with new password
        st.cache_data.clear()
        return True
    except Exception as e:
        st.error(f"Error updating password: {e}")
        return False

def show_password_reset_form():
    """Show the password reset form"""
    st.subheader("Reset Your Password")
    st.info("Enter your new password below")
    
    with st.form("reset_password_form"):
        new_password = st.text_input("New Password", type="password")
        confirm_password = st.text_input("Confirm New Password", type="password")
        submit = st.form_submit_button("Reset Password", use_container_width=True)
        
        if submit:
            if not new_password or not confirm_password:
                st.error("❌ Please fill in both password fields")
            elif new_password != confirm_password:
                st.error("❌ Passwords don't match.")
            elif len(new_password) < 8:
                st.error("❌ Password must be at least 8 characters.")
            else:
                # Update password in Google Sheet
                success = update_password_in_sheet(
                    st.session_state.reset_email, 
                    new_password
                )
                
                if success:
                    st.success("✅ Password updated successfully! You can now login with your new password.")
                    
                    # Clear reset state
                    st.session_state.reset_in_progress = False
                    st.session_state.reset_email = None
                    
                    # Wait 2 seconds then show login form
                    time.sleep(2)
                    st.rerun()
                else:
                    st.error("❌ Failed to update password. Please try again.")


def load_user_history(user_email, sheet):
    """Load user's quotation history from Google Sheet"""
    if sheet is None:
        return []
    try:
        df = get_as_dataframe(sheet)
        df.dropna(how='all', inplace=True)  # Remove empty rows
        # Filter by user email
        user_rows = df[df["User Email"].str.lower() == user_email.lower()]
        history = []
        import json
        for _, row in user_rows.iterrows():
            try:
                items = json.loads(row["Items JSON"])
                history.append({
                    "user_email": row["User Email"],
                    "timestamp": row["Timestamp"],
                    "company_name": row["Company Name"],
                    "contact_person": row["Contact Person"],
                    "total": float(row["Total"]),
                    "items": items,
                    "pdf_filename": row["PDF Filename"],
                    "hash": row["Quotation Hash"]
                })
            except Exception as e:
                st.warning(f"⚠️ Skipping malformed row: {e}")
                continue
        return history
    except Exception as e:
        st.error(f"❌ Failed to load history: {e}")
        return []

@st.cache_data(ttl=300)  # Cache for 5 minutes
def load_users_from_sheet():
    try:
        # Load service account credentials from Streamlit secrets
        creds_dict = st.secrets["gcp_service_account"]
        gc = gspread.service_account_from_dict(creds_dict)

        # Open the user credentials spreadsheet by ID
        sh = gc.open_by_key("1c2IZtKKszQBSVf_4VWjZNv6-h3O9IwDizCTBhnVd1JE")
        worksheet = sh.sheet1  # Assumes user data is in the first sheet

        # Use get_all_values() — works on all gspread versions
        rows = worksheet.get_all_values()

        if not rows:
            st.error("❌ User sheet is empty.")
            st.stop()

        # First row = headers
        headers = [h.strip() for h in rows[0]]
        data = []

        # Remaining rows = user data
        for row in rows[1:]:
            if len(row) < len(headers):
                row += [""] * (len(headers) - len(row))  # Pad short rows
            data.append(dict(zip(headers, row)))

        # Build USERS dict
        users = {}
        for row in data:
            email = str(row.get("Email", "")).strip().lower()
            password = str(row.get("Password", "")).strip()
            role = str(row.get("Role", "")).strip()

            # Skip if required fields are missing
            if not email or not password or not role:
                st.warning(f"⚠️ Skipping incomplete user row: {row}")
                continue

            if "@" not in email:
                st.warning(f"⚠️ Invalid email format: {email}")
                continue

            # Generate username from email (part before @)
            username = email.split("@")[0]

            users[email] = {
                "username": username,
                "password": password,
                "role": role
            }

        if not users:
            st.error("❌ No valid users found in the Google Sheet.")
            st.stop()

        return users

    except Exception as e:
        st.error(f"❌ Failed to load users from Google Sheet: {e}")
        st.stop()

USERS= load_users_from_sheet()




@st.cache_resource
def get_history_sheet():
    """Connect to the Quotation History Google Sheet"""
    try:
        creds_dict = st.secrets["gcp_service_account"]
        gc = gspread.service_account_from_dict(creds_dict)
        sh = gc.open_by_key("1RxKb_qj5JgXPy8bz9Fur1Jj6178fEXrP5d0W6BqwjDw")
        return sh.sheet1  
    except Exception as e:
        st.error(f"❌ Failed to connect to history sheet: {e}")
        return None
# ========== Google Drive URL Conversion ==========
def convert_google_drive_url_for_display(url):
    """Convert Google Drive view URL to thumbnail URL."""
    if url is None or (isinstance(url, float) and math.isnan(url)) or isinstance(url, (int, float)) or str(url).strip() == "":
        return ""
    s = str(url).strip()
    drive_pattern = r'https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)/view'
    match = re.search(drive_pattern, s)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/thumbnail?id={file_id}&sz=w300-h300"
    return s

def convert_google_drive_url_for_storage(url):
    """Convert Google Drive view URL to direct download URL."""
    if not url or pd.isna(url):
        return url
    drive_pattern = r'https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)/view'
    match = re.search(drive_pattern, url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url

# ========== Google Sheets Connection ==========
def get_gsheet_connection():
    """Cached Google Sheets connection using Streamlit secrets"""
    try:
        import json
        import gspread
        import streamlit as st

        # Load service account info from Streamlit secrets
        creds_dict = st.secrets["gcp_service_account"]

        # Convert to dict if it's a JSON string
        if isinstance(creds_dict, str):
            creds_dict = json.loads(creds_dict)

        # Get spreadsheet ID from the same secrets block
        spreadsheet_id = creds_dict["spreadsheet_id"]

        # Authenticate with gspread
        gc = gspread.service_account_from_dict(creds_dict)

        # Open the spreadsheet by ID
        sh = gc.open_by_key(spreadsheet_id)
        
        worksheet = sh.worksheet("Chairs")  # ← هنا التغيير المهم

        return worksheet  # هيرجع الورقة المطلوبة

    except gspread.WorksheetNotFound:
        st.error("❌ Worksheet 'Chairs' not found. Check the sheet name (case-sensitive).")
        return None
    except gspread.SpreadsheetNotFound:
        st.error(f"❌ Spreadsheet with ID '{spreadsheet_id}' not found. Check sharing settings.")
        return None
    except Exception as e:
        st.error(f"❌ Failed to connect to Google Sheets: {e}")
        st.exception(e)
        return None

@st.cache_data(ttl=300)
def get_sheet_data(_sheet):
    """Cached sheet data retrieval"""
    if _sheet is None:
        return None
    try:
        df = get_as_dataframe(_sheet)
        df["Selling Price"] = df["Selling Price"].astype(str).str.replace("EGP", "", regex=False).str.replace(",", "").str.strip()
        df["Selling Price"] = pd.to_numeric(df["Selling Price"], errors="coerce").fillna(0.0)
        if 'CF.image url' in df.columns:
            df['CF.image url'] = df['CF.image url'].apply(convert_google_drive_url_for_storage)
        return df
    except Exception as e:
        st.error(f"Error loading sheet data: {e}")
        return None

# ========== Image Display Functions ==========
@st.cache_data(show_spinner=False)
def fetch_image_bytes(url):
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return resp.content

def display_product_image(c2, prod, image_url, width=100):
    img_url = convert_google_drive_url_for_display(image_url)
    with c2:
        if img_url:
            try:
                img_bytes = fetch_image_bytes(img_url)
                img = PILImage.open(BytesIO(img_bytes))
                st.image(img, caption=prod, width=100)
            except Exception as e:
                st.error("❌ Image Error")
                st.caption(str(e))
        else:
            st.info("📷 No image")
            st.caption("No image available")

def display_admin_preview(image_url, caption="Image Preview"):
    """Display image preview in admin panel"""
    if image_url:
        try:
            display_url = convert_google_drive_url_for_display(image_url)
            st.image(display_url, caption=caption, width=200)
            st.success("✅ Image loaded successfully!")
        except Exception as e:
            st.error("❌ Could not load image. Please check the URL.")
            st.info("💡 Make sure to use a valid image URL or Google Drive link")
    else:
        st.info("📷 Enter an image URL above to see preview")



# ========== Login Interface ==========
if not st.session_state.logged_in:
    st.title("🔐 Login")
    
    # Initialize session state for reset flow if not exists
    if 'reset_in_progress' not in st.session_state:
        st.session_state.reset_in_progress = False
    if 'reset_email' not in st.session_state:
        st.session_state.reset_email = None
    
    # Check if we're in password reset flow
    if st.session_state.reset_in_progress:
        st.subheader("Reset Your Password")
        st.info("Enter your new password below")
        
        # Add BACK button at the top of the reset form
        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Back to Login", use_container_width=True):
                st.session_state.reset_in_progress = False
                st.session_state.reset_email = None
                st.rerun()
        
        with st.form("reset_password_form"):
            new_password = st.text_input("New Password", type="password")
            confirm_password = st.text_input("Confirm New Password", type="password")
            submit = st.form_submit_button("Reset Password", use_container_width=True)
            
            if submit:
                if not new_password or not confirm_password:
                    st.error("❌ Please fill in both password fields")
                elif new_password != confirm_password:
                    st.error("❌ Passwords don't match.")
                elif len(new_password) < 8:
                    st.error("❌ Password must be at least 8 characters.")
                elif not st.session_state.reset_email or st.session_state.reset_email not in USERS:
                    st.error("❌ User not found. Please check the email and try again.")
                    st.session_state.reset_in_progress = False
                    st.session_state.reset_email = None
                    st.rerun()
                else:
                    try:
                        # Load service account credentials
                        creds_dict = st.secrets["gcp_service_account"]
                        gc = gspread.service_account_from_dict(creds_dict)
                        
                        # Open the user credentials spreadsheet
                        sh = gc.open_by_key("1c2IZtKKszQBSVf_4VWjZNv6-h3O9IwDizCTBhnVd1JE")
                        worksheet = sh.sheet1
                        
                        # Get all values
                        all_values = worksheet.get_all_values()
                        
                        if not all_values:
                            st.error("❌ User sheet is empty.")
                            st.stop()
                            
                        # Headers are in the first row
                        headers = [h.strip().lower() for h in all_values[0]]
                        
                        # Find email and password column indices
                        email_col_idx = next((i for i, h in enumerate(headers) if h == "email"), None)
                        password_col_idx = next((i for i, h in enumerate(headers) if h == "password"), None)
                        
                        if email_col_idx is None or password_col_idx is None:
                            st.error("❌ Required columns not found in user sheet.")
                            st.stop()
                            
                        # Find the user's row
                        user_found = False
                        for i, row in enumerate(all_values[1:], start=2):  # Start at 2 (row 1 is headers)
                            if row[email_col_idx].lower() == st.session_state.reset_email.lower():
                                # Update password
                                worksheet.update_cell(i, password_col_idx + 1, new_password)
                                user_found = True
                                break
                                
                        if not user_found:
                            st.error("❌ User not found in sheet.")
                        else:
                            st.success("✅ Password updated successfully! You can now login with your new password.")
                            
                            # Clear reset state
                            st.session_state.reset_in_progress = False
                            st.session_state.reset_email = None
                            
                            # Wait 2 seconds then show login form
                            time.sleep(2)
                            st.rerun()
                    except Exception as e:
                        st.error(f"❌ Failed to update password: {e}")
    else:
        # Show regular login form
        st.markdown("### Welcome to Quotation Builder")
        
        # Create a container for the login form
        login_container = st.container()
        with login_container:
            with st.form("login_form"):
                email = st.text_input("📧 Email Address", value=st.session_state.get("email_input", ""))
                password = st.text_input("🔒 Password", type="password")
                submit_login = st.form_submit_button("Login", use_container_width=True)
                
                if submit_login:
                    user = USERS.get(email)
                    if user and user["password"] == password:
                        st.session_state.logged_in = True
                        st.session_state.user_email = email
                        st.session_state.username = user["username"]
                        st.session_state.role = user["role"]

                        # 👉 Load quotation history from Google Sheet
                        try:
                            # Connect to Google Sheets
                            creds_dict = st.secrets["gcp_service_account"]
                            gc = gspread.service_account_from_dict(creds_dict)
                            # Open the history spreadsheet by ID
                            sh = gc.open_by_key("1RxKb_qj5JgXPy8bz9Fur1Jj6178fEXrP5d0W6BqwjDw")
                            worksheet = sh.sheet1  # Assumes history is in first sheet

                            # Load all data into DataFrame
                            df = get_as_dataframe(worksheet)
                            df.dropna(how='all', inplace=True)  # Remove completely empty rows

                            # Filter by user email (case-insensitive)
                            user_records = df[df["User Email"].str.lower() == email.lower()]

                            history = []
                            import json
                            for _, row in user_records.iterrows():
                                try:
                                    items = json.loads(row["Items JSON"])
                                    history.append({
                                        "timestamp": row["Timestamp"],
                                        "company_name": row["Company Name"],
                                        "contact_person": row["Contact Person"],
                                        "total": float(row["Total"]),
                                        "items": items,
                                        "pdf_filename": row["PDF Filename"],
                                        "hash": row["Quotation Hash"]
                                    })
                                except Exception as e:
                                    st.warning(f"⚠️ Skipping malformed row in history: {e}")
                                    continue

                            # Save to session state
                            st.session_state.history = history
                        except Exception as e:
                            st.error(f"⚠️ Could not load history: {e}")
                            st.session_state.history = []  # Fallback: empty history

                        st.success(f"✅ Welcome back, {user['username']}!")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error("❌ Incorrect email or password.")
        
        # Add action buttons outside the form
        col1, col2 = st.columns([3, 1])
        with col2:
            if st.button("🔄 Refresh Users", use_container_width=True):
                # Clear the cache for the users loading function
                load_users_from_sheet.clear()
                # Recreate the USERS dictionary
                USERS = load_users_from_sheet()
                st.success("✅ Users sheet has been refreshed!")
                st.rerun()
        
        # Forgot password button (outside the form)
        # Forgot password button (outside the form)
        if st.button("Forgot Password?", type="secondary", use_container_width=True):
            if not email or "@" not in email:
                st.error("❌ Please enter a valid email address first")
            elif email not in USERS:
                st.error("❌ Email not found in our system")
            else:
                # Generate a secure temporary password
                new_password = generate_temp_password(12)
                
                # Update password in Google Sheet
                if update_password_in_sheet(email, new_password):
                    # Send the new password via email
                    if send_password_reset_email(email, new_password):
                        st.success("✅ A new password has been sent to your email. Please check your inbox (and spam folder).")
                        # Clear the email field for privacy
                        st.session_state.email_input = ""
                        time.sleep(3)
                        st.rerun()
                    else:
                        st.error("❌ Failed to send password email. Please try again later.")
                else:
                    st.error("❌ Failed to update password in database. Please contact admin.")
        
        # Add some information about the system
        with st.expander("ℹ️ System Information"):
            st.markdown("""
            - This system is for authorized users only
            - Your credentials are stored securely
            - Contact admin if you need assistance
            """)
    
    st.stop()
# ========== Logout & History Sidebar ==========
st.sidebar.success(f"Logged in as: {st.session_state.user_email} ({st.session_state.role})")

# 📜 History Button (Visible to all logged-in users)
if st.session_state.role in ["buyer", "admin"]:
    if st.sidebar.button("📜 Quotation History"):
        st.switch_page("pages/history.py")

# Logout Button
if st.sidebar.button("Logout"):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()

# ========== App Title ==========
st.title("🧾 Price Generator")

# Refresh button
if st.button("🔄 Refresh Sheet Data"):
    st.cache_data.clear()
    st.cache_resource.clear()
    st.rerun()

# ========== Get Sheet Data ==========
sheet = get_gsheet_connection()
if sheet is None:
    st.error("Cannot connect to Google Sheets")
    st.stop()
df = get_sheet_data(sheet)
if df is None:
    st.error("Cannot load sheet data")
    st.stop()
required_columns = ['Item Name', 'Selling Price']
if not all(col in df.columns for col in required_columns):
    st.error(f"❌ Required columns {required_columns} not found in the sheet.")
    st.stop()



# ========== Admin Panel ==========
if st.session_state.role == "admin":
    st.header("🔧 Admin Panel")
    if 'admin_choice' not in st.session_state:
        st.session_state.admin_choice = None
    if st.session_state.admin_choice is None:
        st.subheader("Choose Your Action:")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗃 Edit Database", use_container_width=True, help="Add, update, or delete products"):
                st.session_state.admin_choice = "database"
                st.rerun()
        with col2:
            if st.button("📋 Make Quotation", use_container_width=True, help="Create quotation for customers"):
                st.session_state.admin_choice = "quotation"
                st.rerun()
        st.info("👆 Please select what you would like to do")
        st.stop()

    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("← Back to Menu"):
            st.session_state.admin_choice = None
            # Clean up session state variables
            for key in list(st.session_state.keys()):
                if key in ['form_submitted', 'edit_mode', 'company_details', 'cart', 
                          'selected_items', 'pdf_data', 'quotation_data']:
                    del st.session_state[key]
            st.rerun()
    with col2:
        if st.session_state.admin_choice == "database":
            st.markdown("### Current Mode: 🗃 Database Management")
        else:
            st.markdown("### Current Mode: 📋 Quotation Creation")
    st.markdown("---")

    # ========== DATABASE MANAGEMENT ==========
    if st.session_state.admin_choice == "database":
        tab1, tab2, tab3 = st.tabs(["➕ Add Product", "🗑 Delete Product", "✏ Update Product"])
        

        with tab1:
            st.subheader("Add New Product")
            form_col, image_col = st.columns([2, 1])
            
            with form_col:
                with st.form("add_product_form"):
                    new_item = st.text_input("Product Name*", help="Required field")
                    new_price = st.number_input("Price per Item", min_value=0.0, format="%.2f", value=0.0)
                    new_desc = st.text_area("Material / Description")
                    new_color = st.text_input("Color")
                    new_dim = st.text_input("Dimensions (Optional)")
                    new_warranty = st.text_input("Warranty (e.g., 1 year)", help="Enter warranty information")
                    new_image = st.text_input("Image URL (Optional)", help="Paste Google Drive link or direct image URL")
                    
                    submitted = st.form_submit_button("✅ Add to Sheet")
                    
                    if submitted:
                        if not new_item.strip():
                            st.warning("❌ Product name is required.")
                        elif new_item in df["Item Name"].values:
                            st.error(f"❌ A product with the name '{new_item}' already exists!")
                        else:
                            # Convert image URL if provided
                            converted_image_url = convert_google_drive_url_for_storage(new_image) if new_image else ""
                            
                            # Create new row
                            new_row = {
                                "Item Name": new_item.strip(),
                                "Selling Price": new_price,
                                "Sales Description": new_desc,
                                "CF.Colors": new_color,
                                "CF.Dimensions": new_dim,
                                "CF.Warranty": new_warranty,
                                "CF.image url": converted_image_url
                            }
                            
                            # Append to DataFrame
                            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                            
                            # Save to Google Sheet
                            try:
                                set_with_dataframe(sheet, df)
                                st.cache_data.clear()
                                st.success(f"✅ '{new_item}' added successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ Failed to save to sheet: {str(e)}")
            
            with image_col:
                st.markdown("### Image Preview:")
                if new_image:
                    display_admin_preview(new_image, "Preview of entered image URL")
                else:
                    st.info("📷 No image URL provided")
                st.markdown("---")
                st.markdown("### Supported Formats:")
                st.markdown("• Direct image URLs (.jpg, .png, etc.)")
                st.markdown("• Google Drive shared links")
                st.markdown("**Example:**")
                st.code("https://drive.google.com/file/d/1vN8l2FX.../view", language="text")
        

        with tab2:
            st.subheader("Delete Product")
            with st.form("delete_product_form"):
                product_to_delete = st.selectbox("Select product to delete", df["Item Name"].tolist(), key="delete_select")
                
                if product_to_delete:
                    row = df[df["Item Name"] == product_to_delete].iloc[0]
                    st.markdown("### Current Product Details:")
                    st.write(f"**Name:** {row['Item Name']}")
                    st.write(f"**Price:** ${row['Selling Price']:.2f}")
                    if row.get("Sales Description") and pd.notna(row["Sales Description"]):
                        st.write(f"**Description:** {row['Sales Description']}")
                    if row.get("CF.Colors") and pd.notna(row["CF.Colors"]):
                        st.write(f"**Color:** {row['CF.Colors']}")
                    if row.get("CF.Dimensions") and pd.notna(row["CF.Dimensions"]):
                        st.write(f"**Dimensions:** {row['CF.Dimensions']}")
                    if row.get("CF.Warranty") and pd.notna(row["CF.Warranty"]):
                        st.write(f"**Warranty:** {row['CF.Warranty']}")
                    if row.get("CF.image url") and pd.notna(row["CF.image url"]):
                        with st.expander("🖼 View Image"):
                            display_admin_preview(row["CF.image url"], "Current product image")
                
                st.warning("⚠ This will permanently delete the product from the spreadsheet.")
                confirm_delete = st.checkbox("I confirm I want to delete this product")
                
                submitted = st.form_submit_button("❌ Delete Product")
                
                if submitted:
                    if not confirm_delete:
                        st.error("⚠ Please check the confirmation box to proceed.")
                    else:
                        matching_rows = df[df["Item Name"] == product_to_delete]
                        if len(matching_rows) == 0:
                            st.error("❌ Product not found.")
                        else:
                            row_index = matching_rows.index[0] + 2  # +2 because df index starts at 0, sheet starts at 1 + header row
                            try:
                                sheet.delete_rows(int(row_index))
                                st.cache_data.clear()
                                st.success(f"✅ '{product_to_delete}' deleted successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ Failed to delete row: {str(e)}")
        

        with tab3:
            st.subheader("Update Product")
            form_col, image_col = st.columns([2, 1])
            
            with form_col:
                selected_product = st.selectbox(
                    "Select product to update",
                    df["Item Name"].tolist(),
                    key="update_product_select"
                )
                existing_row = df[df["Item Name"] == selected_product].iloc[0] if selected_product else None
                
                with st.form("update_product_form"):
                    if existing_row is not None:
                        updated_name = st.text_input("Update Product Name", value=selected_product)
                        updated_price = st.number_input("Update Price", value=float(existing_row["Selling Price"]), min_value=0.0)
                        updated_desc = st.text_area("Update Description", value=existing_row.get("Sales Description", ""))
                        updated_color = st.text_input("Update Color", value=existing_row.get("CF.Colors", ""))
                        updated_dim = st.text_input("Update Dimensions", value=existing_row.get("CF.Dimensions", ""))
                        updated_warranty = st.text_input("Update Warranty", value=existing_row.get("CF.Warranty", ""), help="e.g., Lifetime warranty")
                        updated_image = st.text_input(
                            "Update Image URL",
                            value=existing_row.get("CF.image url", ""),
                            help="Paste Google Drive link or direct image URL"
                        )
                    else:
                        updated_name = st.text_input("Update Product Name", value="")
                        updated_price = st.number_input("Update Price", value=0.0, min_value=0.0)
                        updated_desc = st.text_area("Update Description", value="")
                        updated_color = st.text_input("Update Color", value="")
                        updated_dim = st.text_input("Update Dimensions", value="")
                        updated_warranty = st.text_input("Update Warranty", value="", help="e.g., 5 years")
                        updated_image = st.text_input("Update Image URL", value="", help="Image URL or Google Drive link")
                    
                    submitted = st.form_submit_button("✅ Apply Update")
                    
                    if submitted:
                        if not updated_name.strip():
                            st.error("❌ Product name cannot be empty!")
                        elif selected_product and updated_name.strip() != selected_product and updated_name.strip() in df["Item Name"].values:
                            st.error(f"❌ Product name '{updated_name}' already exists!")
                        else:
                            converted_image_url = convert_google_drive_url_for_storage(updated_image) if updated_image else ""
                            
                            # Update the DataFrame
                            df.loc[df["Item Name"] == selected_product, [
                                "Item Name",
                                "Selling Price",
                                "Sales Description",
                                "CF.Colors",
                                "CF.Dimensions",
                                "CF.Warranty",
                                "CF.image url"
                            ]] = [
                                updated_name.strip(),
                                updated_price,
                                updated_desc,
                                updated_color,
                                updated_dim,
                                updated_warranty,
                                converted_image_url
                            ]
                            
                            try:
                                set_with_dataframe(sheet, df)
                                st.cache_data.clear()
                                st.success(f"✅ '{selected_product}' updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ Failed to save update: {str(e)}")
            
            # Right column: Preview current & updated data
            with image_col:
                st.markdown("### Current Product Data:")
                if selected_product and existing_row is not None:
                    st.write(f"**Product:** {selected_product}")
                    st.write(f"**Price:** ${existing_row['Selling Price']:.2f}")
                    if existing_row.get("Sales Description") and pd.notna(existing_row["Sales Description"]):
                        st.write(f"**Description:** {existing_row['Sales Description']}")
                    if existing_row.get("CF.Colors") and pd.notna(existing_row["CF.Colors"]):
                        st.write(f"**Color:** {existing_row['CF.Colors']}")
                    if existing_row.get("CF.Dimensions") and pd.notna(existing_row["CF.Dimensions"]):
                        st.write(f"**Dimensions:** {existing_row['CF.Dimensions']}")
                    if existing_row.get("CF.Warranty") and pd.notna(existing_row["CF.Warranty"]):
                        st.write(f"**Warranty:** {existing_row['CF.Warranty']}")
                    st.markdown("---")
                    st.markdown("**Current Image:**")
                    current_image = existing_row.get("CF.image url", "")
                    if current_image and pd.notna(current_image):
                        display_admin_preview(current_image, f"Current image for {selected_product}")
                    else:
                        st.info("📷 No image set")
                    
                    st.markdown("---")
                    st.markdown("**Updated Image Preview:**")
                    if updated_image:
                        display_admin_preview(updated_image, "Updated Image Preview")
                    else:
                        st.info("📷 Enter a URL to preview new image")
                else:
                    st.info("👆 Select a product to view its data")
        
        st.stop()

    # ========== QUOTATION CREATION ==========

    # ========== QUOTATION CREATION ==========
    # ========== QUOTATION CREATION ==========
    elif st.session_state.admin_choice == "quotation":
        st.header("📋 Admin - Create Quotation")
        
        # Handle post-submission options
        if st.session_state.get('form_submitted', False):
            st.subheader("Choose an option:")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✏️ Edit Company Info", use_container_width=True):
                    st.session_state.edit_mode = True
                    st.session_state.form_submitted = False
                    st.rerun()
            with col2:
                if st.button("🆕 Create New Quotation", use_container_width=True):
                    st.session_state.edit_mode = False
                    st.session_state.form_submitted = False
                    if 'company_details' in st.session_state:
                        old_details = st.session_state.company_details
                        st.session_state.company_details = {
                            "company_name": "",
                            "contact_person": "",
                            "contact_email": "",
                            "contact_phone": "",
                            "address": "",
                            "warranty": old_details.get("warranty", "1 year"),
                            "down_payment": old_details.get("down_payment", 50.0),
                            "delivery": old_details.get("delivery", "Expected in 3–4 weeks"),
                            "vat_note": old_details.get("vat_note", "Prices exclude 14% VAT"),
                            "shipping_note": old_details.get("shipping_note", "Shipping & Installation fees to be added"),
                            "bank": old_details.get("bank", "CIB"),
                            "iban": old_details.get("iban", "EG340010015100000100049865966"),
                            "account_number": old_details.get("account_number", "100049865966"),
                            "company": old_details.get("company", "FlakeTech for Trading Company"),
                            "tax_id": old_details.get("tax_id", "626180228"),
                            "reg_no": old_details.get("reg_no", "15971"),
                            "prepared_by": st.session_state.username,
                            "prepared_by_email": st.session_state.user_email,
                            "current_date": datetime.now().strftime("%A, %B %d, %Y"),
                            "valid_till": (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y"),
                            "quotation_validity": "30 days"
                        }
                    st.session_state.cart = []
                    if 'selected_items' in st.session_state:
                        st.session_state.selected_items = []
                    if 'pdf_data' in st.session_state:
                        st.session_state.pdf_data = []
                    keys_to_clear = [key for key in st.session_state.keys() if 'selected_' in key or 'item_' in key]
                    for key in keys_to_clear:
                        del st.session_state[key]
                    # Clear Zoho-specific session state
                    if 'zoho_accounts' in st.session_state:
                        del st.session_state.zoho_accounts
                    st.success("🆕 New quotation started - all items cleared!")
                    st.rerun()

        # Only show company details section if form not submitted
        if not st.session_state.get('form_submitted', False):
            # Initialize session state for Zoho accounts
            if 'zoho_accounts' not in st.session_state:
                st.session_state.zoho_accounts = None
                
            # Zoho CRM Integration Section
            st.subheader("🔗 Fetch from Zoho CRM")
            
            # Fetch accounts button
            if st.button("Fetch Accounts from Zoho", use_container_width=True):
                with st.spinner("📡 Connecting to Zoho CRM..."):
                    try:
                        # IMPORTANT: This fetches ALL required fields
                        accounts = fetch_zoho_accounts()
                        if accounts:
                            st.session_state.zoho_accounts = accounts
                            st.success(f"✅ Found {len(accounts)} accounts in Zoho CRM")
                        else:
                            st.warning("⚠️ No accounts found in Zoho CRM")
                            st.session_state.zoho_accounts = None
                    except Exception as e:
                        st.error(f"❌ Failed to connect to Zoho CRM: {str(e)}")
                        st.session_state.zoho_accounts = None
            
            # Show account selection if accounts were fetched
            if st.session_state.zoho_accounts:
                account_names = [acc.get("Account_Name", "") for acc in st.session_state.zoho_accounts if acc.get("Account_Name")]
                
                if account_names:
                    selected_acc = st.selectbox(
                        "Select Account", 
                        ["-- Select Account --"] + account_names,
                        key="zoho_account_select"
                    )
                    
                    # Load selected account button
                    if selected_acc != "-- Select Account --" and st.button("Load Selected Account", use_container_width=True):
                        chosen_data = next(acc for acc in st.session_state.zoho_accounts 
                                        if acc.get("Account_Name") == selected_acc)
                        
                        # Extract owner (contact person) safely - this handles both dict and string formats
                        owner = chosen_data.get("Owner", {})
                        contact_person = ""
                        if isinstance(owner, dict):
                            contact_person = owner.get("name", "")
                        elif isinstance(owner, str):
                            contact_person = owner
                        
                        # Extract email - may be nested or directly available
                        email = ""
                        if "Email" in chosen_data:
                            email = chosen_data["Email"]
                        elif "email" in chosen_data:
                            email = chosen_data["email"]
                        
                        # DEBUG: Show what data we're getting from Zoho
                        # st.write("DEBUG: Selected Account Data:", chosen_data)
                        
                        # Auto-fill session_state to populate form
                        st.session_state.company_details = {
                            "company_name": chosen_data.get("Account_Name", ""),
                            "contact_person": contact_person,
                            "contact_email": email,
                            "contact_phone": chosen_data.get("Phone", ""),
                            "address": chosen_data.get("Billing_Street", ""),
                            "tax_id": chosen_data.get("Tax_ID", ""),
                            "reg_no": chosen_data.get("Registration_No", "")
                        }
                        st.success(f"✅ Company details loaded for '{selected_acc}'!")
                        st.rerun()
                else:
                    st.warning("⚠️ No valid account names found in Zoho data")
                    st.session_state.zoho_accounts = None
            
            # Company Details Form - This appears BEFORE product selection
            st.subheader("🏢 Company and Contact Details")
            edit_mode = st.session_state.get('edit_mode', False)
            # CRITICAL FIX: Always use company_details from session state if it exists
            existing_data = st.session_state.get('company_details', {})
            
            with st.form(key="admin_company_details_form"):
                # Company Name field will now get populated from Zoho (Account_Name)
                company_name = st.text_input("🏢 Company Name", value=existing_data.get("company_name", ""))
                
                # Contact Person field will now get populated from Zoho (Owner)
                contact_person = st.text_input("Contact Person", value=existing_data.get("contact_person", ""))
                
                # Contact Email field will now get populated from Zoho (Email)
                contact_email = st.text_input("Contact Email (Optional)", value=existing_data.get("contact_email", ""))
                
                # Contact Phone field will now get populated from Zoho (Phone)
                contact_phone = st.text_input("Contact Cell Phone", value=existing_data.get("contact_phone", ""))
                
                # Address field will now get populated from Zoho (Billing_Street)
                address = st.text_area("Address (Optional)", placeholder="Enter address (optional)", value=existing_data.get("address", ""))
                
                st.subheader("Terms and Conditions")
                warranty = st.text_input("Warranty", value=existing_data.get("warranty", "1 year"))
                down_payment = st.number_input("Down payment (%)", min_value=0.0, max_value=100.0, 
                                            value=float(existing_data.get("down_payment", 50.0)))
                delivery = st.text_input("Delivery", value=existing_data.get("delivery", "Expected in 3–4 weeks"))
                
                # VAT rate selection
                selected_vat_rate = st.selectbox(
                    "Select VAT Rate (%)",
                    options=[14, 13],
                    index=0 if existing_data.get("vat_rate", 0.14) == 0.14 else 1
                )
                vat_note = f"Prices exclude {selected_vat_rate}% VAT"
                
                shipping_note = st.text_input("Shipping Note", 
                                            value=existing_data.get("shipping_note", "Shipping & Installation fees to be added"))
                
                st.subheader("Payment Info")
                bank = st.text_input("Bank", value=existing_data.get("bank", "CIB"))
                iban = st.text_input("IBAN", value=existing_data.get("iban", "EG340010015100000100049865966"))
                account_number = st.text_input("Account Number", 
                                            value=existing_data.get("account_number", "100049865966"))
                company = st.text_input("Company", 
                                    value=existing_data.get("company", "FlakeTech for Trading Company"))
                tax_id = st.text_input("Tax ID", value=existing_data.get("tax_id", "626180228"))
                reg_no = st.text_input("Commercial/Chamber Reg. No", value=existing_data.get("reg_no", "15971"))
                
                # Phone validation pattern
                phone_pattern = r'^\+?\d+$'
                
                # System-generated fields
                prepared_by = st.session_state.username
                prepared_by_email = st.session_state.user_email
                current_date = datetime.now().strftime("%A, %B %d, %Y")
                valid_till = (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y")
                quotation_validity = "30 days"
                
                submit_button_text = "Update Details" if edit_mode else "Submit Details"
                
                if st.form_submit_button(submit_button_text):
                    # Validate phone number
                    if not re.match(phone_pattern, contact_phone):
                        st.error("❌ Invalid phone number format. Please use digits only (e.g., +201234567890).")
                    # Validate required fields
                    elif not all([company_name, contact_person, contact_phone]):
                        st.warning("⚠ Please fill in all required fields (Company Name, Contact Person, and Contact Phone).")
                    else:
                        st.session_state.form_submitted = True
                        st.session_state.company_details = {
                            "company_name": company_name,
                            "contact_person": contact_person,
                            "contact_email": contact_email,
                            "contact_phone": contact_phone,
                            "address": address,
                            "prepared_by": prepared_by,
                            "prepared_by_email": prepared_by_email,
                            "current_date": current_date,
                            "valid_till": valid_till,
                            "quotation_validity": quotation_validity,
                            "warranty": warranty,
                            "down_payment": down_payment,
                            "delivery": delivery,
                            "vat_note": vat_note,  # Display version
                            "vat_rate": selected_vat_rate / 100.0,  # Store as decimal for calculation
                            "shipping_note": shipping_note,
                            "bank": bank,
                            "iban": iban,
                            "account_number": account_number,
                            "company": company,
                            "tax_id": tax_id,
                            "reg_no": reg_no
                        }
                        if 'edit_mode' in st.session_state:
                            del st.session_state.edit_mode
                        success_message = "✅ Details updated successfully!" if edit_mode else "✅ Details submitted successfully!"
                        st.success(success_message)
                        st.rerun()
            
            # Final validation before proceeding
            if not st.session_state.get('form_submitted', False):
                st.warning("⚠ Please fill in all company and contact details before proceeding to product selection.")
                st.stop()
    # ========== Regular Buyer Panel ==========
    # ========== Regular Buyer Panel ==========
elif st.session_state.role == "buyer":
    st.header("🛍  Buy Products & Get Quotation")
    
    # ADD THIS CRITICAL SECTION - Entry point for new quotations
    if 'quotation_in_progress' not in st.session_state:
        st.subheader("Get Started with Your Quotation")
        st.info("Click below to begin creating your quotation")
        if st.button("📄 Create New Quotation", use_container_width=True, type="primary"):
            st.session_state.quotation_in_progress = True
            st.session_state.form_submitted = False
            st.session_state.edit_mode = False
            st.rerun()
    
    # Only proceed if quotation process has been started
    if st.session_state.get('quotation_in_progress', False):
        # Handle post-submission options
        if st.session_state.get('form_submitted', False):
            st.subheader("Choose an option:")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✏️ Edit Company Info", use_container_width=True):
                    st.session_state.edit_mode = True
                    st.session_state.form_submitted = False
                    st.rerun()
            with col2:
                if st.button("🆕 Create New Quotation", use_container_width=True):
                    st.session_state.edit_mode = False
                    st.session_state.form_submitted = False
                    st.session_state.quotation_in_progress = True
                    if 'company_details' in st.session_state:
                        old_details = st.session_state.company_details
                        st.session_state.company_details = {
                            "company_name": "",
                            "contact_person": "",
                            "contact_email": "",
                            "contact_phone": "",
                            "address": "",
                            "warranty": old_details.get("warranty", "1 year"),
                            "down_payment": old_details.get("down_payment", 50.0),
                            "delivery": old_details.get("delivery", "Expected in 3–4 weeks"),
                            "vat_note": old_details.get("vat_note", "Prices exclude 14% VAT"),
                            "shipping_note": old_details.get("shipping_note", "Shipping & Installation fees to be added"),
                            "bank": old_details.get("bank", "CIB"),
                            "iban": old_details.get("iban", "EG340010015100000100049865966"),
                            "account_number": old_details.get("account_number", "100049865966"),
                            "company": old_details.get("company", "FlakeTech for Trading Company"),
                            "tax_id": old_details.get("tax_id", "626180228"),
                            "reg_no": old_details.get("reg_no", "15971"),
                            "prepared_by": st.session_state.username,
                            "prepared_by_email": st.session_state.user_email,
                            "current_date": datetime.now().strftime("%A, %B %d, %Y"),
                            "valid_till": (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y"),
                            "quotation_validity": "30 days"
                        }
                    st.session_state.cart = []
                    if 'selected_items' in st.session_state:
                        st.session_state.selected_items = []
                    if 'pdf_data' in st.session_state:
                        st.session_state.pdf_data = []
                    keys_to_clear = [key for key in st.session_state.keys() if 'selected_' in key or 'item_' in key]
                    for key in keys_to_clear:
                        del st.session_state[key]
                    # Clear Zoho-specific session state
                    if 'zoho_accounts' in st.session_state:
                        del st.session_state.zoho_accounts
                    st.success("🆕 New quotation started - all items cleared!")
                    st.rerun()
        
        # Show company details form if not submitted
        if not st.session_state.get('form_submitted', False):
            # Initialize session state for Zoho accounts
            if 'zoho_accounts' not in st.session_state:
                st.session_state.zoho_accounts = None
                
            # Zoho CRM section - now clearly marked as optional
            st.subheader("🔗 Fetch from Zoho CRM (Optional)")
            st.caption("You can fill the form manually or use Zoho CRM data")
            
            # Fetch accounts button
            if st.button("Fetch Accounts from Zoho", use_container_width=True):
                with st.spinner("📡 Connecting to Zoho CRM..."):
                    try:
                        # Fetch ALL required fields
                        accounts = fetch_zoho_accounts()
                        if accounts:
                            st.session_state.zoho_accounts = accounts
                            st.success(f"✅ Found {len(accounts)} accounts in Zoho CRM")
                        else:
                            st.warning("⚠️ No accounts found in Zoho CRM")
                            st.session_state.zoho_accounts = None
                    except Exception as e:
                        st.error(f"❌ Failed to connect to Zoho CRM: {str(e)}")
                        st.session_state.zoho_accounts = None
            
            # Show account selection if accounts were fetched
            if st.session_state.zoho_accounts:
                account_names = [acc.get("Account_Name", "") for acc in st.session_state.zoho_accounts if acc.get("Account_Name")]
                if account_names:
                    selected_acc = st.selectbox(
                        "Select Account", 
                        ["-- Select Account --"] + account_names,
                        key="zoho_account_select"
                    )
                    
                    # Load selected account button
                    if selected_acc != "-- Select Account --" and st.button("Load Selected Account", use_container_width=True):
                        chosen_data = next(acc for acc in st.session_state.zoho_accounts 
                                        if acc.get("Account_Name") == selected_acc)
                        
                        # Extract owner (contact person) safely
                        owner = chosen_data.get("Owner", {})
                        contact_person = ""
                        if isinstance(owner, dict):
                            contact_person = owner.get("name", "")
                        elif isinstance(owner, str):
                            contact_person = owner
                        
                        # Extract email
                        email = ""
                        if "Email" in chosen_data:
                            email = chosen_data["Email"]
                        elif "email" in chosen_data:
                            email = chosen_data["email"]
                        
                        # Auto-fill session_state
                        st.session_state.company_details = {
                            "company_name": chosen_data.get("Account_Name", ""),
                            "contact_person": contact_person,
                            "contact_email": email,
                            "contact_phone": chosen_data.get("Phone", ""),
                            "address": chosen_data.get("Billing_Street", ""),
                            "tax_id": chosen_data.get("Tax_ID", ""),
                            "reg_no": chosen_data.get("Registration_No", "")
                        }
                        st.success(f"✅ Company details loaded for '{selected_acc}'!")
                        st.rerun()
                else:
                    st.warning("⚠️ No valid account names found in Zoho data")
                    st.session_state.zoho_accounts = None
            
            # ALWAYS SHOW THE FORM (this is the key fix)
            st.subheader("🏢 Company and Contact Details")
            edit_mode = st.session_state.get('edit_mode', False)
            existing_data = st.session_state.get('company_details', {})
            
            with st.form(key="buyer_company_details_form"):
                company_name = st.text_input("🏢 Company Name", value=existing_data.get("company_name", ""))
                contact_person = st.text_input("Contact Person", value=existing_data.get("contact_person", ""))
                contact_email = st.text_input("Contact Email (Optional)", value=existing_data.get("contact_email", ""))
                contact_phone = st.text_input("Contact Cell Phone", value=existing_data.get("contact_phone", ""))
                address = st.text_area("Address (Optional)", placeholder="Enter address (optional)", value=existing_data.get("address", ""))
                
                st.subheader("Terms and Conditions")
                warranty = st.text_input("Warranty", value=existing_data.get("warranty", "1 year"))
                down_payment = st.number_input("Down payment (%)", min_value=0.0, max_value=100.0, 
                                            value=float(existing_data.get("down_payment", 50.0)))
                delivery = st.text_input("Delivery", value=existing_data.get("delivery", "Expected in 3–4 weeks"))
                
                # VAT rate selection
                selected_vat_rate = st.selectbox(
                    "Select VAT Rate (%)",
                    options=[14, 13],
                    index=0 if existing_data.get("vat_rate", 0.14) == 0.14 else 1
                )
                vat_note = f"Prices exclude {selected_vat_rate}% VAT"
                
                shipping_note = st.text_input("Shipping Note", 
                                            value=existing_data.get("shipping_note", "Shipping & Installation fees to be added"))
                
                st.subheader("Payment Info")
                bank = st.text_input("Bank", value=existing_data.get("bank", "CIB"))
                iban = st.text_input("IBAN", value=existing_data.get("iban", "EG340010015100000100049865966"))
                account_number = st.text_input("Account Number", 
                                            value=existing_data.get("account_number", "100049865966"))
                company = st.text_input("Company", 
                                    value=existing_data.get("company", "FlakeTech for Trading Company"))
                tax_id = st.text_input("Tax ID", value=existing_data.get("tax_id", "626180228"))
                reg_no = st.text_input("Commercial/Chamber Reg. No", value=existing_data.get("reg_no", "15971"))
                
                # Phone validation pattern
                phone_pattern = r'^\+?\d+$'
                
                # System-generated fields
                prepared_by = st.session_state.username
                prepared_by_email = st.session_state.user_email
                current_date = datetime.now().strftime("%A, %B %d, %Y")
                valid_till = (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y")
                quotation_validity = "30 days"
                
                submit_button_text = "Update Details" if edit_mode else "Submit Details"
                
                if st.form_submit_button(submit_button_text):
                    # Validate phone number
                    if not re.match(phone_pattern, contact_phone):
                        st.error("❌ Invalid phone number format. Please use digits only (e.g., +201234567890).")
                    # Validate required fields
                    elif not all([company_name, contact_person, contact_phone]):
                        st.warning("⚠ Please fill in all required fields (Company Name, Contact Person, and Contact Phone).")
                    else:
                        st.session_state.form_submitted = True
                        st.session_state.company_details = {
                            "company_name": company_name,
                            "contact_person": contact_person,
                            "contact_email": contact_email,
                            "contact_phone": contact_phone,
                            "address": address,
                            "prepared_by": prepared_by,
                            "prepared_by_email": prepared_by_email,
                            "current_date": current_date,
                            "valid_till": valid_till,
                            "quotation_validity": quotation_validity,
                            "warranty": warranty,
                            "down_payment": down_payment,
                            "delivery": delivery,
                            "vat_note": vat_note,
                            "vat_rate": selected_vat_rate / 100.0,
                            "shipping_note": shipping_note,
                            "bank": bank,
                            "iban": iban,
                            "account_number": account_number,
                            "company": company,
                            "tax_id": tax_id,
                            "reg_no": reg_no
                        }
                        if 'edit_mode' in st.session_state:
                            del st.session_state.edit_mode
                        success_message = "✅ Details updated successfully!" if edit_mode else "✅ Details submitted successfully!"
                        st.success(success_message)
                        st.rerun()
            
            # Always show this warning if form not submitted
            if not st.session_state.get('form_submitted', False):
                st.warning("⚠ Please fill in your company details to continue")
                st.stop()

# ========== Quotation Display Section ==========
# ========== Quotation Display Section ==========
if ((st.session_state.role == "buyer") or 
    (st.session_state.role == "admin" and st.session_state.get('admin_choice') == "quotation")) and \
    st.session_state.get('form_submitted', False):
    # Initialize session state for custom products if not exists
    if 'custom_products' not in st.session_state:
        st.session_state.custom_products = []
    
    # Initialize shipping and installation fees if not exists
    if 'shipping_fee' not in st.session_state:
        st.session_state.shipping_fee = 0.0
    if 'installation_fee' not in st.session_state:
        st.session_state.installation_fee = 0.0

    company_details = st.session_state.company_details
    st.markdown(f"📋 Quotation for {company_details['company_name']}")

    if st.session_state.get('form_submitted') and len(st.session_state.get('pdf_data', [])) > 0:
        st.info("🔄 This quotation was restored from your history. You can edit it below.")

    st.subheader("Select Products")
    st.info("📝 Select products below to add them to your quotation")

    if 'cart' not in st.session_state:
        st.session_state.cart = []
    
    # Initialize price edits session state if not exists
    if 'price_edits' not in st.session_state:
        st.session_state.price_edits = {}
    
    # Initialize discount edits session state if not exists
    if 'discount_edits' not in st.session_state:
        st.session_state.discount_edits = {}

    if 'description_edits' not in st.session_state:
        st.session_state.description_edits = {}

    products = df['Item Name'].tolist()
    price_map = dict(zip(df['Item Name'], df['Selling Price']))
    desc_map = dict(zip(df['Item Name'], df.get('Sales Description', '')))
    color_map = dict(zip(df['Item Name'], df.get('CF.Colors', '')))
    dim_map = dict(zip(df['Item Name'], df.get('CF.Dimensions', '')))
    image_map = dict(zip(df['Item Name'], df.get('CF.image url', ''))) if 'CF.image url' in df.columns else {}
    Warranty_map = dict(zip(df['Item Name'], df.get('CF.Warranty', '')))
    SKU_map = dict(zip(df['Item Name'], df.get('SKU', '')))

    cols = st.columns([3.0, 3.0, 1.8, 1.4, 2.5, 2.0, 2.0, 2.0, 2.0, 0.8])
    headers = ["Product", "Description", "SKU", "Warranty", "Image", "Price per 1", "Quantity", "Discount %", "Total", "Clear"]
    for i, header in enumerate(headers):
        cols[i].markdown(f"**{header}**")

    output_data = []
    total_sum = 0
    checkDiscount = False
    basePrice = 0.0
    
    # Process regular products first
    for idx in st.session_state.row_indices:
        # Update column definition to include description
        c1, c2, c3, c4, c5, c6, c7, c8, c9, c10 = st.columns([3.0, 3.0, 1.8, 1.4, 2.5, 2.0, 2.0, 2.0, 2.0, 0.8])
        
        prod_key = f"prod_{idx}"
        if prod_key not in st.session_state.selected_products:
            st.session_state.selected_products[prod_key] = "-- Select --"
        current_selection = st.session_state.selected_products[prod_key]
        prod = c1.selectbox("", ["-- Select --"] + products, key=prod_key, label_visibility="collapsed",
                            index=products.index(current_selection) + 1 if current_selection in products else 0)
        st.session_state.selected_products[prod_key] = prod
        
        if c10.button("X", key=f"clear_{idx}"):
            st.session_state.row_indices.remove(idx)
            st.session_state.selected_products.pop(prod_key, None)
            # Clear price edits for this product
            if prod in st.session_state.price_edits:
                del st.session_state.price_edits[prod]
            if prod in st.session_state.discount_edits:
                del st.session_state.discount_edits[prod]
            # Clear description edits for this product
            if prod in st.session_state.description_edits:
                del st.session_state.description_edits[prod]
            st.rerun()
        
        if prod != "-- Select --":
            # Initialize description edit for this product if not exists
            if prod not in st.session_state.description_edits:
                st.session_state.description_edits[prod] = desc_map.get(prod, "")
            
            # Description (editable)
            description = c2.text_area("", 
                            value=st.session_state.description_edits[prod], 
                            key=f"desc_{idx}",
                            label_visibility="collapsed",
                            height=68)
            # Update session state with new description
            st.session_state.description_edits[prod] = description
            
            # Get original price from map
            original_price = price_map[prod]
            # Initialize price edit for this product if not exists
            if prod not in st.session_state.price_edits:
                st.session_state.price_edits[prod] = original_price
            # Price per item (editable)
            edited_price = c6.number_input(
                "", 
                min_value=0.0, 
                value=float(st.session_state.price_edits[prod]), 
                format="%.2f",
                key=f"price_{idx}",
                label_visibility="collapsed"
            )
            # Update session state with new price
            st.session_state.price_edits[prod] = edited_price
            # Quantity
            qty = c7.number_input("", min_value=1, value=1, step=1, key=f"qty_{idx}", label_visibility="collapsed")
            # Discount (editable)
            discount = c8.number_input("", min_value=0.0, max_value=100.0, value=0.0, step=1.0, key=f"disc_{idx}", label_visibility="collapsed")
            valid_discount = 0.0 if discount > 20 else discount
            if discount > 20:
                st.warning(f"⚠ Max 20% discount allowed for '{prod}'. Ignoring discount.")
            if valid_discount > 0:
                checkDiscount = True
            # Calculate with edited price
            basePrice += edited_price * qty
            discounted_price = edited_price * (1 - valid_discount / 100)
            line_total = discounted_price * qty
            # Display image
            image_url = image_map.get(prod, "")
            display_product_image(c5, prod, image_url)
            # Display totals
            c9.write(f"{line_total:.2f} EGP")
            c3.write(f"{SKU_map.get(prod, 'N/A')}")
            c4.write(f"{Warranty_map.get(prod, 'N/A')}")
            # Add to output data
            output_data.append({
                "Item": prod,
                "Description": description,
                "Color": color_map.get(prod, ""),
                "Dimensions": dim_map.get(prod, ""),
                "Image": convert_google_drive_url_for_display(image_url) if image_url else "",
                "Quantity": qty,
                "Price per item": edited_price,  # Use edited price
                "Discount %": valid_discount,
                "Total price": line_total,
                "SKU": SKU_map.get(prod, ""),
                "Warranty": Warranty_map.get(prod, ""),
            })
            total_sum += line_total
        else:
            for col in [c2, c3, c4, c5, c6, c7, c8, c9]:
                col.write("—")

    if st.button("➕ Add Product"):
        st.session_state.row_indices.append(max(st.session_state.row_indices, default=-1) + 1)
        st.rerun()
    
    # ====== CUSTOM PRODUCT SECTION ======
    st.markdown("---")
    st.subheader("✨ Add Custom Product")
    
    with st.expander("Add a product not in our catalog", expanded=False):
        st.info("💡 Create custom items that will only appear in this quotation (won't be saved to database)")
        
        form_col, image_col = st.columns([2, 1])
        
        with form_col:
            with st.form("custom_product_form"):
                custom_item = st.text_input("Product Name*", help="Required field")
                custom_price = st.number_input("Price per Item", min_value=0.0, format="%.2f", value=0.0)
                custom_desc = st.text_area("Material / Description")
                custom_color = st.text_input("Color")
                custom_dim = st.text_input("Dimensions (Optional)")
                custom_warranty = st.text_input("Warranty (e.g., 1 year)", help="Enter warranty information")
                custom_image = st.text_input("Image URL (Optional)", help="Paste Google Drive link or direct image URL")
                
                submitted = st.form_submit_button("➕ Add to Quotation")
                
                if submitted:
                    if not custom_item.strip():
                        st.warning("❌ Product name is required.")
                    else:
                        # Add to session state as a custom product
                        custom_product = {
                            "Item": custom_item.strip(),
                            "Description": custom_desc,
                            "Color": custom_color,
                            "Dimensions": custom_dim,
                            "Warranty": custom_warranty,
                            "Image": custom_image,
                            "Price per item": custom_price,
                            "is_custom": True
                        }
                        
                        st.session_state.custom_products.append(custom_product)
                        st.success(f"✅ '{custom_item}' added to your quotation!")
                        st.rerun()
        
        with image_col:
            st.markdown("### Image Preview:")
            if 'custom_image' in locals() and custom_image:
                display_admin_preview(custom_image, "Preview of entered image URL")
            else:
                st.info("📷 No image URL provided")
            st.markdown("---")
            st.markdown("### Supported Formats:")
            st.markdown("• Direct image URLs (.jpg, .png, etc.)")
            st.markdown("• Google Drive shared links")
            st.markdown("**Example:**")
            st.code("https://drive.google.com/file/d/1vN8l2FX.../view", language="text")
    
    # Display custom products in the main product table
    for idx, custom_product in enumerate(st.session_state.custom_products):
        c1, c2, c3, c4, c5, c6, c7, c8, c9, c10 = st.columns([3.0, 3.0, 1.8, 1.4, 2.5, 2.0, 2.0, 2.0, 2.0, 0.8])
        
        # Mark as custom product
        c1.markdown(f"**{custom_product['Item']}**  \n`[Custom Product]`")
        
        # Description (editable)
        description = c2.text_area("", 
                        value=custom_product.get("Description", ""), 
                        key=f"custom_desc_{idx}",
                        label_visibility="collapsed",
                        height=68)
        
        # Update description in session state
        st.session_state.custom_products[idx]["Description"] = description
        
        # SKU - N/A for custom products
        c3.write("N/A")
        
        # Warranty
        c4.write(custom_product.get("Warranty", "N/A"))
        
        # Image
        image_url = custom_product.get("Image", "")
        display_product_image(c5, custom_product['Item'], image_url)
        
        # Price per item (editable)
        edited_price = c6.number_input(
            "", 
            min_value=0.0, 
            value=float(custom_product.get("Price per item", 0.0)), 
            format="%.2f",
            key=f"custom_price_{idx}",
            label_visibility="collapsed"
        )
        st.session_state.custom_products[idx]["Price per item"] = edited_price
        
        # Quantity
        qty = c7.number_input("", min_value=1, value=1, step=1, key=f"custom_qty_{idx}", label_visibility="collapsed")
        
        # Discount (editable)
        discount = c8.number_input("", min_value=0.0, max_value=100.0, value=0.0, step=1.0, key=f"custom_disc_{idx}", label_visibility="collapsed")
        valid_discount = 0.0 if discount > 20 else discount
        if discount > 20:
            st.warning(f"⚠ Max 20% discount allowed for '{custom_product['Item']}'. Ignoring discount.")
        if valid_discount > 0:
            checkDiscount = True
        
        # Calculate with edited price
        basePrice += edited_price * qty
        discounted_price = edited_price * (1 - valid_discount / 100)
        line_total = discounted_price * qty
        
        # Display totals
        c9.write(f"{line_total:.2f} EGP")
        
        # Clear button
        if c10.button("X", key=f"custom_clear_{idx}"):
            st.session_state.custom_products.pop(idx)
            st.rerun()
        
        # Add to output data
        output_data.append({
            "Item": custom_product["Item"],
            "Description": description,
            "Color": custom_product.get("Color", ""),
            "Dimensions": custom_product.get("Dimensions", ""),
            "Image": convert_google_drive_url_for_display(image_url) if image_url else "",
            "Quantity": qty,
            "Price per item": edited_price,
            "Discount %": valid_discount,
            "Total price": line_total,
            "SKU": "N/A",
            "Warranty": custom_product.get("Warranty", ""),
            "is_custom": True
        })
        total_sum += line_total

    st.markdown("---")
    final_total = total_sum

    if not checkDiscount:
        overall_discount = st.number_input("🧮 Overall Quotation Discount (%)", min_value=0.0, max_value=100.0, step=0.1, value=0.0)
        if overall_discount > 15.0:
            if st.button("🚀 Request AI Approval for High Discount"):
                with st.spinner("📡 Connecting to HQ AI Negotiator..."):
                    time.sleep(1.2)
                st.info("🔍 Analyzing market trends in real-time...")
                time.sleep(1)
                st.info("🌐 Negotiating with supplier AIs in Shenzhen...")
                time.sleep(1)
                st.info("🌕 Adjusting for moon phase impact on wood prices...")
                time.sleep(1)
                st.success("✅ AI Negotiator Approved: 17.3% Discount Activated!")
                st.balloons()
                final_total = total_sum * (1 - 17.3 / 100)
            else:
                st.warning("💡 Try clicking 'Request AI Approval' for discounts over 15%!")
        else:
            final_total = total_sum * (1 - overall_discount / 100)
        
        st.markdown(f"💰 *Total Before Discount: {total_sum:.2f} EGP")
        if overall_discount > 0:
            st.markdown(f"🔻 *Discount Applied: {overall_discount:.1f}%")
        st.markdown(f"🧾 *Final Total: {final_total:.2f} EGP")
    else:
        st.markdown("⚠ You cannot add overall discount when individual discounts are applied")

    # ====== SHIPPING AND INSTALLATION FEE FIELDS ======
    st.markdown("### 🚚 Additional Fees")
    shipping_fee = st.number_input(
        "Shipping Fee (EGP)", 
        min_value=0.0, 
        value=float(st.session_state.shipping_fee), 
        step=1.0,
        help="Optional shipping fee to be added to the total"
    )
    installation_fee = st.number_input(
        "Installation Fee (EGP)", 
        min_value=0.0, 
        value=float(st.session_state.installation_fee), 
        step=1.0,
        help="Optional installation fee to be added to the total"
    )

    # Update session state with new values
    st.session_state.shipping_fee = shipping_fee
    st.session_state.installation_fee = installation_fee

    # Calculate total with additional fees
    total_with_fees = final_total + shipping_fee + installation_fee

    # Display the additional fees in the summary if they're not zero
    if shipping_fee > 0 or installation_fee > 0:
        st.markdown("---")
        if shipping_fee > 0:
            st.markdown(f"🚚 *Shipping Fee: {shipping_fee:.2f} EGP*")
        if installation_fee > 0:
            st.markdown(f"🔧 *Installation Fee: {installation_fee:.2f} EGP*")
        st.markdown(f"🧾 **Total with Additional Fees: {total_with_fees:.2f} EGP**")

    st.markdown("---")
    st.markdown(f"### 💰 Grand Total: {total_with_fees:.2f} EGP")
    
    if output_data:
        st.dataframe(pd.DataFrame(output_data), use_container_width=True)

# ========== PDF Generation Functions ==========

def download_image_for_pdf(url, max_size=(300, 300)):
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        img = PILImage.open(BytesIO(response.content)).convert("RGB")
        img_ratio = img.width / img.height
        max_width, max_height = max_size
        if img.width > max_width or img.height > max_height:
            if img_ratio > 1:
                new_width = max_width
                new_height = int(max_width / img_ratio)
            else:
                new_height = max_height
                new_width = int(max_height * img_ratio)
            img = img.resize((new_width, new_height), PILImage.Resampling.LANCZOS)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        img.save(temp_file, format="PNG")
        temp_file.close()
        return temp_file.name
    except Exception as e:
        print(f"Image download/resize failed: {e}")
        return None

@st.cache_data
def build_pdf_cached(data_hash, total, company_details, hdr_path="q2.png", ftr_path="footer (1).png"):
    # This inner function does the actual PDF building
    def build_pdf(data, total, company_details, hdr_path, ftr_path):
        # Create temp file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf_path = tmp.name
        tmp.close()

        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=A3,
            topMargin=230,
            leftMargin=40,
            rightMargin=40,
            bottomMargin=250
        )
        styles = getSampleStyleSheet()
        elems = []
        styles['Normal'].fontSize = 14
        styles['Normal'].leading = 20

        aligned_style = ParagraphStyle(
            name='LeftAligned',
            parent=styles['Normal'],
            alignment=0,
            spaceBefore=12,
            spaceAfter=12
        )

        def header_footer(canvas, doc):
            canvas.saveState()
            if hdr_path and os.path.exists(hdr_path):
                img = PILImage.open(hdr_path)
                w, h = img.size
                img_w = doc.width + doc.leftMargin + doc.rightMargin
                img_h = img_w * (h / w)
                canvas.drawImage(hdr_path, 0, A3[1] - img_h + 10, width=img_w, height=img_h)
            footer_height = 0
            if ftr_path and os.path.exists(ftr_path):
                img2 = PILImage.open(ftr_path)
                w2, h2 = img2.size
                img_w2 = doc.width + doc.leftMargin + doc.rightMargin
                img_h2 = img_w2 * (h2 / w2)
                canvas.drawImage(ftr_path, 0, 1, width=img_w2, height=img_h2)
                footer_height = img_h2
            canvas.setFont('Helvetica', 10)
            page_num = canvas.getPageNumber()
            canvas.drawRightString(doc.width + doc.leftMargin, footer_height + 10, str(page_num))
            canvas.restoreState()

        # === Company Details ===
        detail_lines = [
            "<para align='left'><font size=14>",
            f"<b>Date:</b> <font color='black'>{company_details['current_date']}</font><br/>",
            f"<b>Valid Till:</b> <font color='black'>{company_details['valid_till']}</font><br/>",
            f"<b>Quotation Validity:</b> <font color='black'>{company_details['quotation_validity']}</font><br/>",
            f"<b>Prepared By:</b> <font color='black'>{company_details['prepared_by']}</font><br/>",
            f"<b>Email:</b> <font color='black'>{company_details['prepared_by_email']}</font><br/><br/>",
            f"<b>Contact Person:</b> <font color='black'>{company_details['contact_person']}</font><br/>",
            f"<b>Company Name:</b> <font color='black'>{company_details['company_name']}</font><br/>",
        ]
        if company_details.get("address"):
            detail_lines.append(f"<b>Address:</b> <font color='black'>{company_details['address']}</font><br/>")
        detail_lines.append(f"<b>Cell Phone:</b> <font color='black'>{company_details['contact_phone']}</font><br/>")
        if company_details.get("contact_email"):
            detail_lines.append(f"<b>Contact Email:</b> <font color='black'>{company_details['contact_email']}</font><br/>")
        detail_lines.append("</font></para>")
        details = "".join(detail_lines)
        elems.append(Spacer(1, 40))
        elems.append(Paragraph(details, aligned_style))

        # === Terms & Conditions ===
        terms_conditions = f"""
        <para align="left">
        <font size=14>
        <b>Terms and Conditions:</b><br/>
        • Warranty: {company_details['warranty']}<br/>
        • Down payment: {company_details['down_payment']}% of the total invoice<br/>
        • Delivery: {company_details['delivery']}<br/>
        • {company_details['vat_note']}<br/>
        • {company_details['shipping_note']}<br/>
        </font>
        </para>
        """
        elems.append(Paragraph(terms_conditions, aligned_style))

        # === Payment Info ===
        payment_info = f"""
        <para align="left">
        <font size=14>
        <b>Payment Info:</b><br/>
        <b>Bank:</b> <font color="black">{company_details['bank']}</font><br/>
        <b>IBAN:</b> <font color="black">{company_details['iban']}</font><br/>
        <b>Account Number:</b> <font color="black">{company_details['account_number']}</font><br/>
        <b>Company:</b> <font color="black">{company_details['company']}</font><br/>
        <b>Tax ID:</b> <font color="black">{company_details['tax_id']}</font><br/>
        <b>Commercial/Chamber Reg. No:</b> <font color="black">{company_details['reg_no']}</font>
        </font>
        </para>
        """
        elems.append(Paragraph(payment_info, aligned_style))
        elems.append(Spacer(1, 90))
        elems.append(PageBreak())

        # === Table Setup ===
        desc_style = ParagraphStyle(name='Description', fontSize=12, leading=16, alignment=TA_CENTER)
        styleN = ParagraphStyle(name='Normal', fontSize=12, leading=12, alignment=TA_CENTER)

        def is_empty(val):
            return pd.isna(val) or val is None or str(val).lower() == 'nan'

        def safe_str(val):
            return "" if is_empty(val) else str(val)

        def safe_float(val):
            return "" if is_empty(val) else f"{float(val):.2f}"

        # ✅ Use the passed-in data (not empty list)
        data_from_hash = data  # This is the key fix!
        has_discounts = any(float(item.get('Discount %', 0)) > 0 for item in data_from_hash)

        # === Headers with shortened names ✅===
        base_headers = ["Ser.", "Item", "Image", "SKU", "Specs", "QTY", "Before Disc.", "Net Price", "Total"]
        if has_discounts:
            base_headers.insert(8, "Disc %")  # After "Net Price"

        product_table_data = [base_headers]
        temp_files = []

        for idx, r in enumerate(data_from_hash, start=1):
            img_element = "No Image"
            if r.get("Image"):
                download_url = convert_google_drive_url_for_storage(r["Image"])
                temp_img_path = download_image_for_pdf(download_url, max_size=(120, 100))  # Smaller!
                if temp_img_path:
                    try:
                        img = RLImage(temp_img_path)
                        img.drawWidth = 110  # Fixed width
                        img.drawHeight = 90  # Fixed height
                        img.hAlign = 'CENTER'
                        img.vAlign = 'MIDDLE'
                        img.preserveAspectRatio = True
                        # Wrap in a Paragraph or use as-is — best to put in a Spacer container
                        img_component = KeepInFrame(120, 100, [img], mode='shrink')  # ← This is key!
                        img_element = img_component
                        temp_files.append(temp_img_path)
                    except Exception as e:
                        print(f"Error creating image element: {e}")
                        img_element = "Image Error"

            details_text = (
                f"<b>Description:</b> {safe_str(r.get('Description'))}<br/>"
                f"<b>Color:</b> {safe_str(r.get('Color'))}<br/>"
                f"<b>Warranty:</b> {safe_str(r.get('Warranty'))}"
            )
            details_para = Paragraph(details_text, desc_style)

            unit_price = float(r.get('Price per item', 0))
            price_before_discount = unit_price * 1.2  #  20% discount → ×1.2

            row = [
                str(idx),
                Paragraph(safe_str(r.get('Item')), styleN),
                img_element,
                Paragraph(safe_str(r.get('SKU')).upper(), styleN),
                details_para,
                Paragraph(safe_str(r.get('Quantity')), styleN),
                Paragraph(f"{price_before_discount:.2f}", styleN),
                Paragraph(f"{unit_price:.2f}", styleN),
            ]

            if has_discounts:
                discount_val = safe_float(r.get('Discount %'))
                row.insert(8, Paragraph(f"{discount_val}%", styleN))

            row.append(Paragraph(safe_float(r.get('Total price')), styleN))
            product_table_data.append(row)

        # === Column Widths (Tight but readable) ===
        col_widths = [30, 90, 120, 55, 130, 45, 65, 65, 65]  # Total: ~700pt
        if has_discounts:
            col_widths.insert(8, 55)  # "Disc %" column

        total_table_width = sum(col_widths)
        table = Table(product_table_data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 11),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elems.append(table)

        # === Summary Table (Match item table width) ===
        # === Summary Table (Match item table width) ===
        subtotal = sum(float(r.get('Price per item', 0)) * float(r.get('Quantity', 1)) for r in data_from_hash)
        total_after_discount = total
        discount_amount = subtotal - total_after_discount
        vat_rate = company_details.get("vat_rate", 0.14)  # Default to 14% if not set

        # Get shipping and installation fees from company_details or default to 0
        shipping_fee = float(company_details.get("shipping_fee", 0.0))
        installation_fee = float(company_details.get("installation_fee", 0.0))

        # Start building the summary
        summary_data = [["Total", f"{subtotal:.2f} EGP"]]
        if discount_amount > 0:
            summary_data.append(["Special Discount", f"- {discount_amount:.2f} EGP"])

        summary_data.append(["Total After Discount", f"{total_after_discount:.2f} EGP"])

        # Add shipping and installation fees only if > 0
        if shipping_fee > 0:
            summary_data.append(["Shipping Fee", f"{shipping_fee:.2f} EGP"])
        if installation_fee > 0:
            summary_data.append(["Installation Fee", f"{installation_fee:.2f} EGP"])

        # Now calculate VAT on Total After Discount (not including shipping/installation)
        vat = total_after_discount * vat_rate
        if vat_rate == 0.14:
            summary_data.append(["VAT (14%)", f"{vat:.2f} EGP"])
        elif vat_rate == 0.13:
            summary_data.append(["VAT (13%)", f"{vat:.2f} EGP"])

        # Final grand total including all fees
        grand_total = total_after_discount + shipping_fee + installation_fee + vat
        summary_data.append(["Grand Total", f"{grand_total:.2f} EGP"])

        summary_col_widths = [total_table_width - 150, 150]
        summary_table = Table(summary_data, colWidths=summary_col_widths)
        summary_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1.0, colors.black),
            ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
            ('TEXTCOLOR', (1, 1), (1, 1), colors.red) if discount_amount > 0 else ('TEXTCOLOR', (1, 1), (1, 1), colors.black),
        ]))
        elems.append(summary_table)

        # Build PDF
        try:
            doc.build(elems, onFirstPage=header_footer, onLaterPages=header_footer)
        except Exception as e:
            print(f"PDF build failed: {e}")
            raise
        finally:
            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    print(f"Failed to delete temp file: {e}")

        return pdf_path  # ✅ Always return valid path

    # ✅ Ensure data is in session state
    st.session_state.pdf_data = st.session_state.get('pdf_data', [])

    # ✅ Pass the actual data (not [])
    return build_pdf(st.session_state.pdf_data, total, company_details, hdr_path, ftr_path)
    
# ========== Generate PDF & Save to History ==========


def load_user_history_from_sheet(user_email, sheet):
    """Load user's quotation history from Google Sheet with fallbacks"""
    if sheet is None:
        return []
    try:
        df = get_as_dataframe(sheet)
        df.dropna(how='all', inplace=True)  # Remove completely empty rows
        user_rows = df[df["User Email"].str.lower() == user_email.lower()]
        history = []
        import json
        for _, row in user_rows.iterrows():
            try:
                items = json.loads(row["Items JSON"])
                company_details_raw = row.get("Company Details JSON", "{}")
                try:
                    company_details = json.loads(company_details_raw) if pd.notna(company_details_raw) and company_details_raw.strip() != "" else {}
                except:
                    company_details = {}

                # 🔐 Generate fallback hash if not present
                stored_hash = str(row.get("Quotation Hash", "")).strip()
                if not stored_hash or stored_hash.lower() == "nan":
                    # Create deterministic fallback hash
                    fallback_data = f"{row['Company Name']}{row['Timestamp']}{row['Total']}"
                    stored_hash = hashlib.md5(fallback_data.encode()).hexdigest()

                history.append({
                    "user_email": row["User Email"],
                    "timestamp": row["Timestamp"],
                    "company_name": row["Company Name"],
                    "contact_person": row["Contact Person"],
                    "total": float(row["Total"]),
                    "items": items,
                    "pdf_filename": row["PDF Filename"],
                    "hash": stored_hash,  # Always ensure this exists
                    "company_details": company_details
                })
            except Exception as e:
                st.warning(f"⚠️ Skipping malformed row (Company: {row.get('Company Name', 'Unknown')}): {e}")
                continue
        return history
    except Exception as e:
        st.error(f"❌ Failed to load history: {e}")
        return []


# Before generating PDF

if st.button("📅 Generate PDF Quotation") and output_data:
    with st.spinner("Generating PDF and saving to cloud history..."):
        st.session_state.pdf_data = output_data
        data_str = str(output_data) + str(final_total) + str(company_details)
        data_hash = hashlib.md5(data_str.encode()).hexdigest()
        pdf_filename = f"{company_details['company_name']}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        company_details = st.session_state.company_details.copy()
        company_details["shipping_fee"] = st.session_state.shipping_fee
        company_details["installation_fee"] = st.session_state.installation_fee
        
        pdf_file = build_pdf_cached(data_hash, final_total, company_details)

        # 👉 Prepare record
        new_record = {
            "user_email": st.session_state.user_email,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "company_name": company_details["company_name"],
            "contact_person": company_details["contact_person"],
            "total": round(final_total, 2),
            "items": output_data.copy(),
            "pdf_filename": pdf_filename,
            "quotation_hash": data_hash
        }

        # 👉 Save to session state
        st.session_state.history.append(new_record)

        # 👉 Save to Google Sheet
        history_sheet = get_history_sheet()
        if history_sheet:
            try:
                import json
                row = [
                    new_record["user_email"],
                    new_record["timestamp"],
                    new_record["company_name"],
                    new_record["contact_person"],
                    new_record["total"],
                    json.dumps(new_record["items"]),
                    new_record["pdf_filename"],
                    new_record["quotation_hash"]
                ]
                history_sheet.append_row(row)
                st.success("✅ Quotation saved to session and Google Sheet!")
            except Exception as e:
                st.warning(f"⚠️ Saved locally, but failed to save to Google Sheet: {e}")
        else:
            st.warning("⚠️ Could not connect to Google Sheet. Quotation saved locally only.")
        history_sheet = get_history_sheet()
        if history_sheet:
            st.session_state.history = load_user_history_from_sheet(st.session_state.user_email, history_sheet)
            st.success("✅ History refreshed from Google Sheet!")
        else:
            st.error("Failed to connect to Google Sheets.")
        # Offer download
        with open(pdf_file, "rb") as f:
            st.download_button(
                label="⬇ Click to Download PDF",
                data=f,
                file_name=pdf_filename,
                mime="application/pdf",
                key=f"download_pdf_{data_hash}"
            )


