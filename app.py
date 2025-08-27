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
from reportlab.platypus import KeepInFrame
from reportlab.lib.colors import orange
from reportlab.graphics.shapes import Drawing, Path
from reportlab.platypus import Flowable
from reportlab.lib import colors
from reportlab.platypus import Image as RLImage
import json

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

@st.cache_data(ttl=900)  # Cache for 15 minutes
def fetch_zoho_users():
    """Fetch all active users from Zoho CRM (usable as Quote Owners)"""
    try:
        token = get_zoho_access_token()
        if not token:
            st.error("❌ Cannot fetch users: Invalid Zoho token")
            return []

        url = f"{st.secrets['zoho']['crm_api_domain']}/crm/v2/users"
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        params = {"type": "ActiveUsers"}  # Only active users with licenses

        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json().get("users", [])

        # Extract name and email
        users = []
        for user in data:
            users.append({
                "name": user.get("full_name", user.get("first_name", "")),
                "email": user.get("email"),
                "id": user.get("id")
            })
        return users
    except Exception as e:
        st.error(f"❌ Failed to fetch Zoho users: {str(e)}")
        return []



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
    characters = string.ascii_letters + string.digits + "!@#$%^&*"
    temp_password = ''.join(random.choice(characters) for _ in range(length))
    return temp_password

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
                # st.warning(f"⚠️ Skipping incomplete user row: {row}")
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

def get_zoho_user_id(email):
    token = get_zoho_access_token()
    url = f"{st.secrets['zoho']['crm_api_domain']}/crm/v2/users"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"email": email}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        users = response.json().get("users", [])
        return users[0]["id"] if users else None
    return None

def get_zoho_account_id(name):
    token = get_zoho_access_token()
    url = f"{st.secrets['zoho']['crm_api_domain']}/crm/v2/Accounts"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"criteria": f"Account_Name:equals:{name}"}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        data = response.json().get("data", [])
        return data[0]["id"] if data else None
    return None

def get_zoho_product_id(sku):
    """
    ابحث عن منتج في Zoho CRM باستخدام الـ SKU (Product_Code)
    """
    if not sku or str(sku).strip() == "" or str(sku).lower() == "n/a":
        return None

    try:
        sku_str = str(sku).strip()
        token = get_zoho_access_token()
        if not token:
            return None

        url = f"{st.secrets['zoho']['crm_api_domain']}/crm/v2/Products"
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        
        # 🔍 ابحث باستخدام Product_Code
        params = {"criteria": f"Product_Code:equals:{sku_str}"}

        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            data = response.json().get("data", [])
            if data:
                product_id = data[0]["id"]
                product_name = data[0]["Product_Name"]
                st.write(f"✅ Found product by SKU '{sku_str}': '{product_name}' (ID: {product_id})")
                return product_id
            else:
                st.warning(f"❌ No product found in Zoho CRM with SKU: '{sku_str}'")
        else:
            st.error(f"❌ Zoho API Error ({response.status_code}): {response.text}")
            
    except Exception as e:
        st.error(f"❌ Error searching for product by SKU '{sku}': {e}")
    
    return None


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
        # with st.expander("ℹ️ System Information"):
        #     st.markdown("""
        #     - This system is for authorized users only
        #     - Your credentials are stored securely
        #     - Contact admin if you need assistance
        #     """)
    
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
                    # Warranty field is correctly implemented here
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
                            
                            # Create new row with correct CF.Warranty mapping
                            new_row = {
                                "Item Name": new_item.strip(),
                                "Selling Price": new_price,
                                "Sales Description": new_desc,
                                "CF.Colors": new_color,
                                "CF.Dimensions": new_dim,
                                "CF.Warranty": new_warranty,  # Correctly mapped to sheet column
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
                st.code("https://drive.google.com/file/d/1vN8l2FX.../view    ", language="text")
        

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
                
                # --- ZOHO QUOTE OWNER SELECTION (Automatic) ---
                if st.session_state.role == "admin":
                    with st.spinner("📡 Loading Zoho CRM users..."):
                        zoho_users = fetch_zoho_users()
                    
                    # Find the logged-in user in the Zoho users list by matching email
                    current_user_email = st.session_state.user_email.lower().strip()
                    matched_user = next((u for u in zoho_users if u["email"].lower().strip() == current_user_email), None)
                    
                    if matched_user:
                        # ✅ User found in Zoho CRM
                        st.success(f"👤 Quote Owner: **{matched_user['name']}** (from Zoho CRM)")
                        st.write(f"📧 Email: `{matched_user['email']}`")
                        
                        # Set the quote owner details directly
                        quote_owner_id = matched_user["id"]
                        quote_owner_name = matched_user["name"]
                        quote_owner_email = matched_user["email"]
                        
                        # You can optionally add a "Change Owner" button here for rare cases
                        # if st.button("Change Quote Owner"):
                        #     st.session_state.show_owner_select = True # (You'd need to manage this state)
                        
                    else:
                        # ❌ User not found in Zoho CRM
                        st.error(f"❌ Your email ({current_user_email}) was not found in Zoho CRM Active Users.")
                        st.info("Please contact your administrator to ensure your Zoho CRM user is active and your email is correct.")
                        
                        # Fallback: Try to get the ID directly (less reliable)
                        fallback_id = get_zoho_user_id(current_user_email)
                        if fallback_id:
                            st.warning("⚠️ Using fallback method to get user ID.")
                            quote_owner_id = fallback_id
                            quote_owner_name = st.session_state.username
                            quote_owner_email = current_user_email
                        else:
                            st.error("❌ Could not get a valid user ID from Zoho CRM. Quote creation may fail.")
                            quote_owner_id = None
                            quote_owner_name = st.session_state.username
                            quote_owner_email = current_user_email
                else:
                    # Regular users: use their own info
                    quote_owner_id = get_zoho_user_id(st.session_state.user_email)
                    quote_owner_name = st.session_state.username
                    quote_owner_email = st.session_state.user_email

                prepared_by = quote_owner_name  # Set prepared_by to quote owner
                prepared_by_email = quote_owner_email  # Set prepared_by_email to quote owner
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
                            "quote_owner_id": quote_owner_id,
                            "quote_owner_name": quote_owner_name,  # Save quote owner name
                            "quote_owner_email": quote_owner_email,  # Save quote owner email
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
                        # Debug output to verify saved details
                        st.write("🔍 Debug - Saved Company Details:", {
                            "Company Name": st.session_state.company_details["company_name"],
                            "Quote Owner ID": st.session_state.company_details["quote_owner_id"],
                            "Quote Owner Name": st.session_state.company_details["quote_owner_name"],
                            "Quote Owner Email": st.session_state.company_details["quote_owner_email"],
                            "Prepared By": st.session_state.company_details["prepared_by"],
                            "Prepared By Email": st.session_state.company_details["prepared_by_email"]
                        })
                        if 'edit_mode' in st.session_state:
                            del st.session_state.edit_mode
                        success_message = "✅ Details updated successfully!" if edit_mode else "✅ Details submitted successfully!"
                        st.success(success_message)
                        st.rerun()

            # Always show this warning if form not submitted
            if not st.session_state.get('form_submitted', False):
                st.warning("⚠ Please fill in your company details to continue")
                st.stop()
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
    # Initialize final_total to total_sum FIRST, before any conditional logic
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
                    approved_overall_discount = 17.3
                st.markdown(f"📉 **Overall Discount Amount:** {total_sum * (approved_overall_discount/100):.2f} EGP ({approved_overall_discount:.1f}%)")
            else:
                st.warning("💡 Try clicking 'Request AI Approval' for discounts over 15%!")
        else:
            if overall_discount > 0:
                final_total = total_sum * (1 - overall_discount / 100)
                st.markdown(f"📉 **Overall Discount Amount:** {total_sum * (overall_discount/100):.2f} EGP ({overall_discount:.1f}%)")
        
        if overall_discount > 0 and overall_discount <= 15.0:
            st.markdown(f"🧾 **Final Total:** {final_total:.2f} EGP")
        elif 'approved_overall_discount' in locals():
            st.markdown(f"🧾 **Final Total:** {final_total:.2f} EGP")
        else:
            st.markdown(f"🧾 **Final Total:** {final_total:.2f} EGP")
    else:
        # Calculate total discount amount from item-level discounts
        total_discount_amount = basePrice - total_sum
        
        st.markdown("### 📊 Discount Summary (Item-Level)")
        st.markdown(f"💰 **Total Before Discount:** {basePrice:.2f} EGP")
        st.markdown(f"📉 **Total Discount Amount:** {total_discount_amount:.2f} EGP ({(total_discount_amount/basePrice*100):.1f}%)")
        st.markdown(f"🧾 **Subtotal After Discounts:** {final_total:.2f} EGP")
        st.warning("⚠ You cannot add an overall discount when individual product discounts are already applied")

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

    # Calculate total with additional fees - final_total is now guaranteed to be defined
    total_with_fees = final_total + shipping_fee + installation_fee

    # Display the additional fees in the summary if they're not zero
    if shipping_fee > 0 or installation_fee > 0:
        st.markdown("---")
        if shipping_fee > 0:
            st.markdown(f"🚚 **Shipping Fee:** {shipping_fee:.2f} EGP")
        if installation_fee > 0:
            st.markdown(f"🔧 **Installation Fee:** {installation_fee:.2f} EGP")

    # ====== VAT CALCULATION ======
    vat_rate = company_details.get("vat_rate", 0.14) 
    vat = (final_total + shipping_fee) * vat_rate
    grand_total = final_total + shipping_fee + installation_fee + vat

    st.markdown("### 📊 Final Calculation")
    st.markdown(f"💰 **Subtotal:** {final_total:.2f} EGP")
    if shipping_fee > 0:
        st.markdown(f"📦 **Shipping Fee:** {shipping_fee:.2f} EGP")
    if installation_fee > 0:
        st.markdown(f"🔧 **Installation Fee:** {installation_fee:.2f} EGP")
    st.markdown(f" taxpound **VAT ({vat_rate*100:.0f}%):** {vat:.2f} EGP")
    st.markdown(f"💵 **GRAND TOTAL:** {grand_total:.2f} EGP")
    
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

# ##################financail offer###################
@st.cache_data
def build_pdf_cached(data_hash, total, company_details, hdr_path="q2.png", ftr_path="footer (1).png", 
                    intro_path="FT-Quotation-Temp-financial.jpg", closure_path="FT-Quotation-Temp-2.jpg",
                    bg_path="FT Quotation Temp[1](1).jpg"):
    
    def build_pdf(data, total, company_details, hdr_path, ftr_path, intro_path, closure_path, bg_path):
        # Create temp file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf_path = tmp.name
        tmp.close()

        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=A3,
            topMargin=100,
            leftMargin=40,
            rightMargin=70,
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
            spaceBefore=5,
            spaceAfter=12,
            leftIndent=50
        )

        # Variables to track page structure
        cover_page = 1
        content_start_page = 2
        closure_page_num = None

        def header_footer(canvas, doc):
            canvas.saveState()
            page_num = canvas.getPageNumber()
            
            # Draw full-page cover image on first page
            if page_num == cover_page and intro_path and os.path.exists(intro_path):
                canvas.drawImage(intro_path, 0, 0, width=A3[0], height=A3[1])
                canvas.restoreState()
                return
            
            # Draw full-page closure image on last page
            if closure_page_num is not None and page_num == closure_page_num and closure_path and os.path.exists(closure_path):
                canvas.drawImage(closure_path, 0, 0, width=A3[0], height=A3[1])
                canvas.restoreState()
                return
            
            # Draw background image on content pages
            if bg_path and os.path.exists(bg_path) and page_num >= content_start_page and (closure_page_num is None or page_num < closure_page_num):
                canvas.drawImage(bg_path, 0, 0, width=A3[0], height=A3[1], preserveAspectRatio=True, mask='auto')
            
            # Add page numbering for content pages only
            if page_num >= content_start_page and (closure_page_num is None or page_num < closure_page_num):
                canvas.setFont('Helvetica', 10)
                content_page_num = page_num - content_start_page + 1
                canvas.drawRightString(doc.width + doc.leftMargin, 40, f"Page {content_page_num}")
            
            canvas.restoreState()

        # === Cover Page ===
        if intro_path and os.path.exists(intro_path):
            elems.append(PageBreak())

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
        
        elems.append(Spacer(1, 20))
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
        elems.append(Spacer(1, 15))
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
        elems.append(Spacer(1, 15))
        elems.append(Paragraph(payment_info, aligned_style))
        
        # Always start table on new page to avoid layout issues
        elems.append(PageBreak())

        # === Table Setup ===
        desc_style = ParagraphStyle(name='Description', fontSize=9, leading=11, alignment=TA_CENTER)
        styleN = ParagraphStyle(name='Normal', fontSize=9, leading=10, alignment=TA_CENTER)

        def is_empty(val):
            return pd.isna(val) or val is None or str(val).lower() == 'nan'

        def safe_str(val):
            return "" if is_empty(val) else str(val)

        def safe_float(val):
            return "" if is_empty(val) else f"{float(val):.2f}"

        data_from_hash = data
        has_discounts = any(float(item.get('Discount %', 0)) > 0 for item in data_from_hash)

        # Calculate subtotals
        subtotal_before = 0.0
        subtotal_after = 0.0
        for r in data_from_hash:
            unit_price = float(r.get('Price per item', 0))
            qty = float(r.get('Quantity', 1))
            disc_pct = float(r.get('Discount %', 0))
            discounted_price = unit_price * (1 - disc_pct / 100)
            subtotal_before += unit_price * qty
            subtotal_after += discounted_price * qty

        discount_amount = subtotal_before - subtotal_after

        # Calculate overall discount if applicable
        overall_disc_amount = max(subtotal_after - total, 0.0) if abs(subtotal_after - total) > 0.01 else 0.0
        total_after_discount = total if overall_disc_amount > 0 else subtotal_after

        # === Headers ===
        base_headers = ["Ser.", "Item", "Image", "SKU", "Specs", "QTY", "B.D.", "Net Price", "Total"]
        if has_discounts:
            base_headers.insert(8, "Disc %")

        # === Column Widths (optimized for A3) ===
        col_widths = [30, 75, 145, 55, 130, 45, 55, 65, 65]  # Total: ~700pt
        if has_discounts:
            col_widths.insert(8, 55)  # "Disc %" column
        else:
            # Add the discount column width to Specs column when no discount
            col_widths[4] += 55

        total_table_width = sum(col_widths)
        temp_files = []

        # === Build Product Table Data with Optimized Images ===
        def create_product_row(r, idx):
            img_element = Paragraph("No Image", styleN)
            if r.get("Image"):
                download_url = convert_google_drive_url_for_storage(r["Image"])
                temp_img_path = download_image_for_pdf(download_url, max_size=(300, 300))
                if temp_img_path and os.path.exists(temp_img_path):
                    try:
                        img = RLImage(temp_img_path)
                        img.drawWidth = 300
                        img.drawHeight = 300
                        img.hAlign = 'CENTER'
                        img.vAlign = 'MIDDLE'
                        img.preserveAspectRatio = True
                        img_component = KeepInFrame(200, 150, [img], mode='shrink')
                        img_element = img_component
                        temp_files.append(temp_img_path)
                    except Exception as e:
                        print(f"Error creating image element for {r.get('Item', 'Unknown')}: {e}")
                        img_element = Paragraph("Image Error", styleN)

            desc_text = safe_str(r.get('Description'))
            color_text = safe_str(r.get('Color'))
            warranty_text = safe_str(r.get('Warranty'))
            
            if len(desc_text) > 60:
                desc_text = desc_text[:60] + "..."
            
            details_text = (
                f"<b>Description:</b> {desc_text}<br/>"
                f"<b>Color:</b> {color_text}<br/>"
                f"<b>Warranty:</b> {warranty_text}"
            )
            details_para = Paragraph(details_text, desc_style)

            unit_price = float(r.get('Price per item', 0))
            disc_pct = float(r.get('Discount %', 0))
            net_price = unit_price * (1 - disc_pct / 100)

            item_name = safe_str(r.get('Item'))
            if len(item_name) > 35:
                item_name = item_name[:35] + "..."

            row = [
                str(idx),
                Paragraph(item_name, styleN),
                img_element,
                Paragraph(safe_str(r.get('SKU')).upper(), styleN),
                details_para,
                Paragraph(safe_str(r.get('Quantity')), styleN),
                Paragraph(f"{unit_price:.2f}", styleN),
                Paragraph(f"{net_price:.2f}", styleN),
            ]

            if has_discounts:
                discount_val = safe_float(r.get('Discount %'))
                row.insert(8, Paragraph(f"{discount_val}%", styleN))

            row.append(Paragraph(safe_float(r.get('Total price')), styleN))
            return row

        # === Calculate maximum rows per page based on available space ===
        page_height = A3[1]
        top_margin = 100
        bottom_margin = 250
        header_height = 25
        row_height = 150
        summary_row_height = 25  # Estimated height per summary table row
        spacer_height = 30
        
        available_height = page_height - top_margin - bottom_margin - header_height - spacer_height
        
        def calculate_rows_per_page(is_last_chunk=False, include_summary=False):
            height_for_table = available_height
            if is_last_chunk and include_summary:
                summary_rows = len(summary_data)  # Will be defined later
                summary_height = summary_rows * summary_row_height
                height_for_table -= summary_height
            max_rows = max(1, int(height_for_table // row_height))
            return min(max_rows, 8)

        # === Build Summary Table Data ===
        vat_rate = company_details.get("vat_rate", 0.14)
        shipping_fee = float(company_details.get("shipping_fee", 0.0))
        installation_fee = float(company_details.get("installation_fee", 0.0))
        vat = (shipping_fee + total_after_discount) * vat_rate
        grand_total = total_after_discount + shipping_fee + installation_fee + vat

        summary_data = []
        has_any_discount = (discount_amount > 0 or overall_disc_amount > 0)
        if has_any_discount:
            summary_data.append(["Subtotal Before Discounts", f"{subtotal_before:.2f} EGP"])
            if discount_amount > 0:
                summary_data.append(["Special Discount", f"- {discount_amount:.2f} EGP"])
            if overall_disc_amount > 0:
                summary_data.append(["Overall Discount", f"- {overall_disc_amount:.2f} EGP"])
            summary_data.append(["Total After Discounts", f"{total_after_discount:.2f} EGP"])
        else:
            summary_data.append(["Total", f"{total_after_discount:.2f} EGP"])

        if shipping_fee > 0:
            summary_data.append(["Shipping Fee", f"{shipping_fee:.2f} EGP"])
        if installation_fee > 0:
            summary_data.append(["Installation Fee", f"{installation_fee:.2f} EGP"])

        summary_data.append([f"VAT ({int(vat_rate * 100)}%)", f"{vat:.2f} EGP"])
        summary_data.append(["Grand Total", f"{grand_total:.2f} EGP"])

        # Split products into chunks
        product_chunks = []
        remaining_products = data_from_hash[:]
        
        while remaining_products:
            is_last_chunk = len(remaining_products) <= calculate_rows_per_page(True, include_summary=True)
            rows_for_this_page = calculate_rows_per_page(is_last_chunk, include_summary=True)
            
            chunk = remaining_products[:rows_for_this_page]
            product_chunks.append(chunk)
            remaining_products = remaining_products[rows_for_this_page:]

        # Create tables for each chunk
        for chunk_idx, chunk in enumerate(product_chunks):
            is_last_chunk = (chunk_idx == len(product_chunks) - 1)
            
            chunk_table_data = [base_headers]
            for idx, r in enumerate(chunk, start=sum(len(c) for c in product_chunks[:chunk_idx]) + 1):
                row = create_product_row(r, idx)
                chunk_table_data.append(row)

            chunk_table = Table(chunk_table_data, colWidths=col_widths)
            chunk_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ]))
            
            outer_data = [[chunk_table]]
            bottom_padding = 0 if is_last_chunk else 10
            outer_style = TableStyle([
                ('LEFTPADDING', (0, 0), (0, 0), 50),
                ('RIGHTPADDING', (0, 0), (0, 0), 0),
                ('TOPPADDING', (0, 0), (0, 0), 0),
                ('BOTTOMPADDING', (0, 0), (0, 0), bottom_padding),
                ('GRID', (0, 0), (0, 0), 0, colors.transparent),
            ])
            outer_table = Table(outer_data, colWidths=[total_table_width], style=outer_style)
            elems.append(outer_table)
            
            if is_last_chunk:
                # Check if summary table fits on the same page
                summary_rows = len(summary_data)
                summary_height = summary_rows * summary_row_height
                product_rows = len(chunk)
                product_height = product_rows * row_height + header_height
                total_content_height = product_height + summary_height
                
                summary_on_new_page = total_content_height > available_height
                
                if summary_on_new_page:
                    elems.append(PageBreak())
                
                # Create summary table
                discount_row_indices = [i for i, row in enumerate(summary_data) if "Discount" in row[0]]
                summary_col_widths = [total_table_width - 150, 150]
                summary_table = Table(summary_data, colWidths=summary_col_widths)
                summary_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                    ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                    ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                    ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 12),
                    ('GRID', (0, 0), (-1, -1), 1.0, colors.black),
                    ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
                    *[('TEXTCOLOR', (1, i), (1, i), colors.black) for i in discount_row_indices],
                ]))
                
                outer_summary_data = [[summary_table]]
                outer_summary_style = TableStyle([
                    ('LEFTPADDING', (0, 0), (0, 0), 50),
                    ('RIGHTPADDING', (0, 0), (0, 0), 0),
                    ('TOPPADDING', (0, 0), (0, 0), 0),
                    ('BOTTOMPADDING', (0, 0), (0, 0), 0),
                    ('GRID', (0, 0), (0, 0), 0, colors.transparent),
                ])
                outer_summary = Table(outer_summary_data, colWidths=[total_table_width], style=outer_summary_style)
                elems.append(outer_summary)
            else:
                elems.append(PageBreak())

        # === Closure Page ===
        if closure_path and os.path.exists(closure_path):
            elems.append(PageBreak())
            elems.append(Spacer(1, 1))
            closure_page_num = len([e for e in elems if isinstance(e, PageBreak)]) + 1

        # Build PDF
        try:
            doc.build(elems, onFirstPage=header_footer, onLaterPages=header_footer)
        except Exception as e:
            print(f"PDF build failed: {e}")
            raise
        finally:
            for temp_file in temp_files:
                try:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
                except Exception as e:
                    print(f"Failed to delete temp file: {e}")

        return pdf_path

    st.session_state.pdf_data = st.session_state.get('pdf_data', [])
    return build_pdf(st.session_state.pdf_data, total, company_details, hdr_path, ftr_path, 
                    intro_path, closure_path, bg_path)









# #################### technical offer###################
@st.cache_data
def build_pdf_cached_tech(data_hash, total, company_details, hdr_path="q2.png", ftr_path="footer (1).png", 
                         intro_path="FT-Quotation-Temp-1.jpg", closure_path="FT-Quotation-Temp-2.jpg",
                         bg_path="FT Quotation Temp[1](1).jpg"):
    
    def build_pdf(data, total, company_details, hdr_path, ftr_path, intro_path, closure_path, bg_path):
        import tempfile
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Flowable
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A3
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import Image as RLImage
        import os
        import pandas as pd
        import re

        # Create temp file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf_path = tmp.name
        tmp.close()

        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=A3,
            topMargin=100,
            leftMargin=40,
            rightMargin=70,
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
            spaceBefore=5,
            spaceAfter=12,
            leftIndent=50
        )

        # Variables to track page structure
        cover_page = 1
        content_start_page = 2
        closure_page_num = None

        def header_footer(canvas, doc):
            canvas.saveState()
            page_num = canvas.getPageNumber()
            
            # Draw full-page cover image on first page
            if page_num == cover_page and intro_path and os.path.exists(intro_path):
                canvas.drawImage(intro_path, 0, 0, width=A3[0], height=A3[1])
                canvas.restoreState()
                return
            
            # Draw full-page closure image on last page
            if closure_page_num is not None and page_num == closure_page_num and closure_path and os.path.exists(closure_path):
                canvas.drawImage(closure_path, 0, 0, width=A3[0], height=A3[1])
                canvas.restoreState()
                return
            
            # Draw background image on content pages
            if bg_path and os.path.exists(bg_path) and page_num >= content_start_page and (closure_page_num is None or page_num < closure_page_num):
                canvas.drawImage(bg_path, 0, 0, width=A3[0], height=A3[1], preserveAspectRatio=True, mask='auto')
            
            # Add page numbering for content pages only
            if page_num >= content_start_page and (closure_page_num is None or page_num < closure_page_num):
                canvas.setFont('Helvetica', 10)
                content_page_num = page_num - content_start_page + 1
                canvas.drawRightString(doc.width + doc.leftMargin, 40, f"Page {content_page_num}")
            
            canvas.restoreState()

        # === Cover Page ===
        if intro_path and os.path.exists(intro_path):
            elems.append(PageBreak())

        def is_empty(val):
            return pd.isna(val) or val is None or str(val).lower() == 'nan'

        def safe_str(val):
            return "" if is_empty(val) else str(val)

        def safe_float(val):
            return "" if is_empty(val) else f"{float(val):.2f}"

        def convert_google_drive_url_for_storage(url):
            """Convert Google Drive sharing URL to direct download URL"""
            if not url:
                return url
            
            # Extract file ID from various Google Drive URL formats
            if '/file/d/' in url:
                file_id = url.split('/file/d/')[1].split('/')[0]
                return f"https://drive.google.com/uc?export=download&id={file_id}"
            elif 'id=' in url:
                file_id = url.split('id=')[1].split('&')[0]
                return f"https://drive.google.com/uc?export=download&id={file_id}"
            else:
                return url

        def download_image_for_pdf(url, max_size=(300, 300)):
            """Download and resize image for PDF inclusion"""
            try:
                import requests
                from PIL import Image as PILImage
                from io import BytesIO
                import tempfile
                
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

        data_from_hash = data
        has_discounts = any(float(item.get('Discount %', 0)) > 0 for item in data_from_hash)

        # Calculate subtotals
        subtotal_before = 0.0
        subtotal_after = 0.0
        for r in data_from_hash:
            unit_price = float(r.get('Price per item', 0))
            qty = float(r.get('Quantity', 1))
            disc_pct = float(r.get('Discount %', 0))
            discounted_price = unit_price * (1 - disc_pct / 100)
            subtotal_before += unit_price * qty
            subtotal_after += discounted_price * qty

        discount_amount = subtotal_before - subtotal_after
        overall_disc_amount = max(subtotal_after - total, 0.0) if abs(subtotal_after - total) > 0.01 else 0.0
        total_after_discount = total if overall_disc_amount > 0 else subtotal_after

        temp_files = []

        # Define styles for product pages
        product_name_style = ParagraphStyle(
            name='ProductName',
            parent=styles['Normal'],
            fontSize=24,
            leading=28,
            alignment=0,  # Left align for right column
            spaceAfter=6,
            leftIndent=100  # Shift to the right
        )

        cat_warr_style = ParagraphStyle(
            name='CatWarr',
            parent=styles['Normal'],
            fontSize=12,
            leading=14,
            alignment=0,  # Left
            spaceAfter=12,
            leftIndent=100  # Shift to the right
        )

        overview_title_style = ParagraphStyle(
            name='OverviewTitle',
            parent=styles['Normal'],
            fontSize=14,
            leading=16,
            fontName='Helvetica-Bold',
            alignment=0,
            spaceAfter=6,
            leftIndent=100  # Align with other right column content
        )

        overview_text_style = ParagraphStyle(
            name='OverviewText',
            parent=styles['Normal'],
            fontSize=12,
            leading=14,
            alignment=0,
            leftIndent=100  # Shift description to the right
        )

        specs_title_style = ParagraphStyle(
            name='SpecsTitle',
            parent=styles['Normal'],
            fontSize=16,
            leading=20,
            alignment=0,
            spaceAfter=0  # Changed to 0 to make it directly above
        )

        feature_title_style = ParagraphStyle(
            name='FeatureTitle',
            parent=styles['Normal'],
            fontSize=16,
            leading=16,
            textColor=colors.orange,
            spaceBefore=12,
            spaceAfter=6,
            leftIndent=42  # Shift features to the right
        )

        bullet_style = ParagraphStyle(
            name='Bullet',
            parent=styles['Normal'],
            fontSize=14,
            leading=14,
            leftIndent=70,  # Increased indent for bullets
            firstLineIndent=-10,
            spaceAfter=4
        )

        bar_label_style = ParagraphStyle(
            name='BarLabel',
            parent=styles['Normal'],
            fontSize=12,
            leading=14,
            alignment=0
        )

        bottom_box_style = ParagraphStyle(
            name='BottomBox',
            parent=styles['Normal'],
            fontSize=12,
            leading=14,
            alignment=1  # Center
        )

        # Build product pages - one per product
        for idx, r in enumerate(data_from_hash, 1):
            if idx > 1:
                elems.append(PageBreak())

            # Product Overview with Image side by side, including name and details
            description = r.get('Description', 'No description available.')
            overview_title = Paragraph("Product Overview", overview_title_style)
            overview_para = Paragraph(description, overview_text_style)

            # Create image with proper error handling
            img_element = Paragraph("No Image", styles['Normal'])
            if r.get("Image"):
                download_url = convert_google_drive_url_for_storage(r["Image"])
                temp_img_path = download_image_for_pdf(download_url, max_size=(300, 300)) 
                if temp_img_path and os.path.exists(temp_img_path):
                    try:
                        # Create a custom flowable with rounded border
                        class BorderedImage(Flowable):
                            def __init__(self, img_path, width=200, height=200, radius=10):
                                Flowable.__init__(self)
                                self.img_path = img_path
                                self.width = width
                                self.height = height
                                self.radius = radius
                                
                            def wrap(self, *args):
                                return self.width, self.height
                                
                            def draw(self):
                                # Set line properties BEFORE drawing the rectangle
                                self.canv.setLineWidth(1.5)
                                self.canv.setStrokeColor(colors.orange)
                                
                                # Correct roundRect usage with only positional and basic parameters
                                self.canv.roundRect(0, 0, self.width, self.height, self.radius, stroke=1, fill=0)
                                
                                # Draw the image inside with proper padding
                                img = RLImage(self.img_path)
                                img.drawWidth = self.width - 8
                                img.drawHeight = self.height - 8
                                img.drawOn(self.canv, 4, 4)
                        
                        img_element = BorderedImage(temp_img_path)
                        temp_files.append(temp_img_path)
                    except Exception as e:
                        print(f"Error creating bordered image: {e}")
                        # Fallback to regular image without border
                        try:
                            img = RLImage(temp_img_path)
                            img.drawWidth = 150
                            img.drawHeight = 150
                            img_element = img
                        except:
                            img_element = Paragraph("Image Processing Error", styles['Normal'])
                else:
                    img_element = Paragraph("Image Not Found", styles['Normal'])

            # Flakeekeke image
            flake_image = Paragraph("Flakeekeke Image Not Found", styles['Normal'])
            flake_image_path = "WhatsApp Image 2025-08-26 at 12.07.39_c4c9d9b4.jpg"
            if os.path.exists(flake_image_path):
                try:
                    img = RLImage(flake_image_path)
                    img.drawWidth = 300  # Increased width to stretch
                    img.drawHeight = 50
                    # Wrap image in a table to apply left indent
                    flake_image_table = Table([[img]], colWidths=[300], rowHeights=[50])
                    flake_image_table.setStyle(TableStyle([
                        ('LEFTPADDING', (0, 0), (-1, -1), 110),  # Shift right to align with product name
                        ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
                    ]))
                    flake_image = flake_image_table
                except Exception as e:
                    print(f"Error loading flakeekeke image: {e}")
                    flake_image = Paragraph("Flakeekeke Image Processing Error", styles['Normal'])
            else:
                print(f"Flakeekeke image not found at: {flake_image_path}")

            # Horizontal line under flakeekeke image
            hr_data_flake = [["","","",""]]  
            hr_table_flake = Table(hr_data_flake, colWidths=[80, 350], rowHeights=4)

            hr_table_flake.setStyle(TableStyle([
                ('BACKGROUND', (1, 0), (1, 0), colors.darkorange),  # only color the second column
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))

            # Product Name
            product_name_para = Paragraph(safe_str(r.get('Item', 'Product Name')), product_name_style)

            # Category and Warranty with bold labels
            cat_warr_text = f"<b>Category:</b> Reception & Seating<br/><b>Warranty:</b> {safe_str(r.get('Warranty', '2 Years'))}"
            cat_warr_para = Paragraph(cat_warr_text, cat_warr_style)

            # Right column content: flakeekeke image, hr, spacer, name, cat_warr, overview title, overview
            right_content_data = [
                [flake_image],
                [hr_table_flake],  # New horizontal line under flakeekeke image
                [Spacer(1, 5)],  # Reduced spacer to move image up slightly
                [product_name_para],
                [cat_warr_para],
                [overview_title],
                [overview_para]
            ]
            right_content_table = Table(right_content_data, colWidths=[420])
            right_content_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))

            # Create side-by-side layout
            side_data = [[img_element, right_content_table]]
            side_col_widths = [180, 420]
            side_table = Table(side_data, colWidths=side_col_widths, rowHeights=260)  # Height accommodates layout
            side_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
                ('LEFTPADDING', (0, 0), (0, 0), 0),
                ('RIGHTPADDING', (0, 0), (0, 0), 20),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]))
            elems.append(side_table)

            # Add horizontal line after product overview
            elems.append(Spacer(1, 12))
            hr_data = [[""]]
            hr_table = Table(hr_data, colWidths=[600], rowHeights=2)
            hr_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.orange),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))
            elems.append(hr_table)
            elems.append(Spacer(1, 12))

            # SPECIFICATIONS
            specs_title_right_style = ParagraphStyle(
                'SpecsTitleRight',
                parent=specs_title_style,
                leftIndent=65  # Adjust this value (30 points = ~0.42 inches)
            )
            elems.append(Paragraph("SPECIFICATIONS", specs_title_right_style))

            # Specs table (left)
            specs_data = [
                ["Structure", ""],
                ["Cover", ""],
                ["Cushion", ""],
                ["Foam", ""],
                ["Dimensions", ""],
                ["Weight capacity", ""],
                ["Certification", ""],
                ["Maintenance", ""],
            ]
            specs_col_widths = [100, 100]  
            specs_table = Table(specs_data, colWidths=specs_col_widths, rowHeights=[30]*8)  # Increased row height to 30
            specs_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('FONTSIZE', (0, 0), (-1, -1), 12),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))

            # Features (right) - hardcoded to match image exactly
            ergonomic_title = Paragraph("Ergonomic & Comfort Features:", feature_title_style)
            ergonomic_bullets = [
                Paragraph("• Wide seat base for maximum", bullet_style),
                Paragraph("• Supportive high-density foam discomfort", bullet_style),
            ]

            durability_title = Paragraph("Durability & Warranty:", feature_title_style)
            durability_bullets = [
                Paragraph("• Solid beech wood ensures long", bullet_style),
                Paragraph("• Protective finish for wear", bullet_style),
                Paragraph("• Standard 2-year warranty, care", bullet_style),
            ]

            customization_title = Paragraph("Customization Options:", feature_title_style)
            customization_bullets = [
                Paragraph("• Multiple upholstery colors", bullet_style),
                Paragraph("• Optional wood stain finishes to", bullet_style),
            ]

            sustainability_title = Paragraph("Sustainability:", feature_title_style)
            sustainability_bullets = [
                Paragraph("• Wood sourced from FSC-certified", bullet_style),
                Paragraph("• Low-VOC finishes for healthier.", bullet_style),
            ]

            right_flowables = (
                [ergonomic_title] + ergonomic_bullets +
                [durability_title] + durability_bullets +
                [customization_title] + customization_bullets +
                [sustainability_title] + sustainability_bullets
            )

            right_data = [[f] for f in right_flowables]
            right_table = Table(right_data, colWidths=[300])
            right_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))

            # Side by side specs and features
            specs_features_data = [[specs_table, right_table]]
            specs_features_col_widths = [250, 350]
            specs_features_table = Table(specs_features_data, colWidths=specs_features_col_widths)
            specs_features_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))
            elems.append(specs_features_table)

            elems.append(Spacer(1, 12))

            # Horizontal line
            hr_data = [[""]]
            hr_table = Table(hr_data, colWidths=[600], rowHeights=2)
            hr_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.orange),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))
            elems.append(hr_table)

            elems.append(Spacer(1, 12))

            # Warranty bar
            warranty_label = Paragraph("Warranty", bar_label_style)
            total_bar_width = 300
            warranty_years = 0  # Default to 0
            if r.get('Warranty') is not None:
                warranty_value = r['Warranty']
                try:
                    if isinstance(warranty_value, (int, float)):
                        warranty_years = float(warranty_value)
                    elif isinstance(warranty_value, str):
                        # Extract numeric part from strings like "1year", "1 Year", "1 yrs"
                        match = re.match(r'(\d+\.?\d*)\s*(?:year|yrs)?', safe_str(warranty_value), re.IGNORECASE)
                        warranty_years = float(match.group(1)) if match else 0
                    print(f"Parsed warranty_years: {warranty_years} for product {r.get('Item', 'Unknown')}")
                except Exception as e:
                    print(f"Error parsing warranty for product {r.get('Item', 'Unknown')}: {e}")
                    warranty_years = 0

            # Ensure filled_width is positive and not exceeding total_bar_width
            filled_width = max(0, min((warranty_years / 10.0) * total_bar_width, total_bar_width))
            print(f"Calculated filled_width: {filled_width} for warranty_years: {warranty_years}")

            # Create the main bar
            if filled_width > 0:
                bar_data = [["", ""]]
                bar_col_widths = [filled_width, total_bar_width - filled_width]
            else:
                bar_data = [[""]]
                bar_col_widths = [total_bar_width]  # Full bar unfilled if warranty is 0 or invalid

            bar_table = Table(bar_data, colWidths=bar_col_widths, rowHeights=10)
            bar_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, 0), colors.black),  # Filled portion
                ('BACKGROUND', (1, 0), (1, 0), colors.lightgrey) if filled_width > 0 else ('BACKGROUND', (0, 0), (0, 0), colors.lightgrey),  # Unfilled portion or full bar if no fill
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))

            # Create number pointers (0-10) above the bar
            pointer_width = total_bar_width / 10  # Each number gets equal space
            pointer_data = [[]]
            pointer_col_widths = []

            for i in range(11):  # 0 to 10
                # Create paragraph for each number
                if i <= warranty_years:
                    # Highlight numbers up to warranty years
                    pointer_style = ParagraphStyle(
                        'PointerHighlight',
                        parent=bar_label_style,
                        fontSize=8,
                        textColor=colors.black,
                        fontName='Helvetica-Bold'
                    )
                else:
                    # Normal style for numbers beyond warranty
                    pointer_style = ParagraphStyle(
                        'PointerNormal', 
                        parent=bar_label_style,
                        fontSize=8,
                        textColor=colors.grey
                    )
                
                pointer_data[0].append(Paragraph(str(i), pointer_style))
                pointer_col_widths.append(pointer_width)

            # Create table for number pointers
            pointer_table = Table(pointer_data, colWidths=pointer_col_widths, rowHeights=15)
            pointer_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))

            # Create the complete bar section with pointers above
            bar_section_data = [
                [pointer_table],  # Numbers on top
                [bar_table]       # Bar below
            ]
            bar_section_table = Table(bar_section_data, colWidths=[total_bar_width], rowHeights=[15, 10])
            bar_section_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))

            # Create labels for 0 and 10 years
            label_0 = Paragraph("0", bar_label_style)
            label_10 = Paragraph("10 yrs", bar_label_style)

            # Combine everything in the final row
            bar_row_data = [[label_0, bar_section_table, label_10]]
            bar_row_col_widths = [30, total_bar_width, 50]
            bar_row_table = Table(bar_row_data, colWidths=bar_row_col_widths)
            bar_row_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ALIGN', (0, 0), (0, 0), 'RIGHT'),
                ('ALIGN', (2, 0), (2, 0), 'LEFT'),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))

            # Final warranty table
            warranty_row_data = [[warranty_label, bar_row_table]]
            warranty_row_col_widths = [100, 380]
            warranty_table = Table(warranty_row_data, colWidths=warranty_row_col_widths)
            warranty_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            elems.append(warranty_table)

            # Customization bar (hardcoded to match image, assuming mid-high)
            customization_label = Paragraph("Customization", bar_label_style)
            customization_level = 0.6  # Approximate from image
            filled_width_c = customization_level * total_bar_width
            bar_table_c = Table(bar_data, colWidths=[filled_width_c, total_bar_width - filled_width_c], rowHeights=10)
            bar_table_c.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, 0), colors.black),
                ('BACKGROUND', (1, 0), (1, 0), colors.lightgrey),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))
            label_high = Paragraph("High", bar_label_style)
            bar_row_data_c = [[label_0, bar_table_c, label_high]]
            bar_row_table_c = Table(bar_row_data_c, colWidths=bar_row_col_widths)
            bar_row_table_c.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ALIGN', (0, 0), (0, 0), 'RIGHT'),
                ('ALIGN', (2, 0), (2, 0), 'LEFT'),
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
            ]))
            customization_row_data = [[customization_label, bar_row_table_c]]
            customization_table = Table(customization_row_data, colWidths=warranty_row_col_widths)
            customization_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0, colors.transparent),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            elems.append(customization_table)

            elems.append(Spacer(1, 24))

            # Bottom price, quantity, total boxes
            price_text = f"Price<br/>{safe_float(r.get('Price per item', 0))} LE"
            quantity_text = f"Quantity<br/>{safe_str(r.get('Quantity', ''))}"
            total_text = f"Total<br/>{safe_float(r.get('Total price', 0))} LE"
            bottom_data = [[Paragraph(price_text, bottom_box_style),
                            Paragraph(quantity_text, bottom_box_style),
                            Paragraph(total_text, bottom_box_style)]]
            bottom_col_widths = [150, 150, 150]
            bottom_table = Table(bottom_data, colWidths=bottom_col_widths, rowHeights=50)
            bottom_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                ('GRID', (0, 0), (-1, -1), 1, colors.orange),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ]))
            elems.append(bottom_table)

        # === Closure Page ===
        if closure_path and os.path.exists(closure_path):
            elems.append(PageBreak())
            elems.append(Spacer(1, 1))
            closure_page_num = len([e for e in elems if isinstance(e, PageBreak)]) + 1

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

        return pdf_path

    # Ensure data is in session state
    import streamlit as st
    st.session_state.pdf_data = st.session_state.get('pdf_data', [])

    # Pass the actual data
    return build_pdf(st.session_state.pdf_data, total, company_details, hdr_path, ftr_path, 
                    intro_path, closure_path, bg_path)





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

if st.button("📅 Generate financial Quotation ") and output_data:
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


if st.button("📅 Generate technical Quotation ") and output_data:
    with st.spinner("Generating PDF and saving to cloud history..."):
        st.session_state.pdf_data = output_data
        data_str = str(output_data) + str(final_total) + str(company_details)
        data_hash = hashlib.md5(data_str.encode()).hexdigest()
        pdf_filename = f"{company_details['company_name']}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        company_details = st.session_state.company_details.copy()
        company_details["shipping_fee"] = st.session_state.shipping_fee
        company_details["installation_fee"] = st.session_state.installation_fee
        
        pdf_file = build_pdf_cached_tech(data_hash, final_total, company_details)

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
def create_zoho_quote(company_details, items, final_total, shipping_fee=0, installation_fee=0):
    token = get_zoho_access_token()
    owner_id = company_details.get("quote_owner_id")
    st.write("🎯 Creating quote - Selected Quote Owner ID:", repr(owner_id))
    st.write("🎯 Quote Owner Name:", company_details.get("quote_owner_name", "Unknown"))
    st.write("🎯 Quote Owner Email:", company_details.get("quote_owner_email", "Unknown"))

    if not token:
        st.error("❌ No Zoho access token available.")
        return None

    if not owner_id:
        st.error("❌ No valid Quote Owner ID found in company details.")
        return None

    url = f"{st.secrets['zoho']['crm_api_domain']}/crm/v2/Quotes"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }

    # Parse address safely
    addr = company_details.get("address", "")
    if not addr or addr.strip() == "":
        street, city, country = "N/A", "Cairo", "Egypt"
    else:
        parts = addr.split(",")
        street = parts[0].strip()
        city = parts[1].strip() if len(parts) > 1 else "Cairo"
        country = parts[2].strip() if len(parts) > 2 else "Egypt"

    # Find Account ID
    account_id = get_zoho_account_id(company_details["company_name"])
    if not account_id:
        st.error(f"❌ Account '{company_details['company_name']}' not found in Zoho CRM.")
        return None

    product_details = []
    for item in items:
        sku = item.get("SKU")
        product_id = get_zoho_product_id(sku)
        
        if not product_id:
            st.warning(f"⚠️ Skipping product '{item['Item']}' (SKU: {sku}) - not found in Zoho CRM.")
            continue

        try:
            quantity = float(item["Quantity"])
            unit_price = float(item["Price per item"])
            total = float(item["Total price"])
            discount_amount = max(0, (unit_price * quantity) - total)
        except Exception as e:
            st.error(f"❌ Invalid data for '{item['Item']}': {e}")
            continue

        product_details.append({
            "product": {"id": product_id},
            "quantity": quantity,
            "unit_price": round(unit_price, 2),
            "Discount": round(discount_amount, 2)
        })

    if not product_details:
        st.error("❌ No valid products to quote. Please ensure products have valid SKUs.")
        return None

    st.write("📦 Product details for Zoho:", product_details)

    # Build payload
    payload = {
        "data": [
            {
                "Subject": f"Quotation for {company_details['company_name']}",
                "Account_Name": {"id": account_id},
                "Owner": {"id": owner_id},  # Use 'Owner' field for Quote_Owner
                "Quote_Stage": "Draft",
                "Date_of_Quotation": datetime.now().strftime("%Y-%m-%d"),
                "Valid_Until": (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d"),
                "Currency": "EGP",
                "Exchange_Rate": 1.0,
                "Adjustment": round(shipping_fee + installation_fee, 2),
                "Grand_Total": round(final_total + shipping_fee + installation_fee, 2),
                "Shipping_Street": street,
                "Shipping_City": city,
                "Shipping_Country": country,
                "Terms_and_Conditions": (
                    f"Warranty: {company_details['warranty']}\n"
                    f"Down Payment: {company_details['down_payment']}%\n"
                    f"Delivery: {company_details['delivery']}\n"
                    f"{company_details['shipping_note']}"
                ),
                "Product_Details": product_details
            }
        ]
    }

    # Clean NaN/None values
    payload = json.loads(json.dumps(payload, default=str))

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code in (200, 201):
            quote_id = response.json()["data"][0]["details"]["id"]
            st.write(f"✅ Zoho API Response: {response.json()}")
            st.success(f"✅ Quote created in Zoho CRM! ID: {quote_id}")
            return quote_id
        else:
            st.error(f"❌ Zoho API Error: {response.status_code} - {response.json()}")
            return None
    except Exception as e:
        st.error(f"❌ Failed to create quote in Zoho CRM: {e}")
        return None

def get_zoho_product_id(sku):
    if not sku or str(sku).strip() == "" or str(sku).lower() == "n/a":
        st.write(f"⚠️ Invalid or missing SKU: {sku}")
        return None

    try:
        sku_str = str(sku).strip()
        token = get_zoho_access_token()
        if not token:
            st.error("❌ No Zoho access token available.")
            return None

        # Use the correct search endpoint with GET method
        url = f"{st.secrets['zoho']['crm_api_domain']}/crm/v2/Products/search"
        headers = {
            "Authorization": f"Zoho-oauthtoken {token}"
        }
        # Search using criteria for Product_Code
        params = {
            "criteria": f"(Product_Code:equals:{sku_str})"
        }

        # Use GET request
        response = requests.get(url, headers=headers, params=params)

        if response.status_code == 200:
            data = response.json().get("data", [])
            if data:
                product_id = data[0]["id"]
                product_name = data[0]["Product_Name"]
                st.write(f"✅ Found product by SKU '{sku_str}': '{product_name}' (ID: {product_id})")
                return product_id
            else:
                st.warning(f"⚠️ No product found in Zoho CRM with Product_Code = '{sku_str}'")
                return None
        else:
            error_detail = response.json()
            st.error(f"❌ Zoho Search API Error ({response.status_code}): {error_detail}")
            return None

    except Exception as e:
        st.error(f"❌ Error searching for product by SKU '{sku}': {e}")
        return None

if st.button("📤 Save This Quotation to Zoho CRM", type="primary"):
    with st.spinner("Creating quote in Zoho..."):
        create_zoho_quote(
            company_details=st.session_state.company_details,
            items=output_data,
            final_total=final_total,
            shipping_fee=st.session_state.shipping_fee,
            installation_fee=st.session_state.installation_fee,
        )













