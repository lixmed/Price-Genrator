import streamlit as st
import pandas as pd
import hashlib
import math
from datetime import datetime, timedelta
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Spacer, Paragraph, Image as RLImage, PageBreak, KeepInFrame
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A3
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from io import BytesIO
import requests
import tempfile
import os
import re
from PIL import Image as PILImage
import time
import gspread
from gspread_dataframe import get_as_dataframe
import json

# Helper function to safely convert any value to lowercase string
def safe_lower(value):
    """Safely convert any value to lowercase string, handling None and NaN values"""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).lower()

# ========== Page Config ==========
st.set_page_config(page_title="Quotation History", page_icon="📜", layout="wide")

# ========== Protect Access ==========
if "logged_in" not in st.session_state or not st.session_state.logged_in:
    st.error("Please log in first.")
    st.stop()

# ========== Initialize Session State (if not exists) ==========
if 'history' not in st.session_state:
    st.session_state.history = []

# ========== Google Sheets Connection ==========
@st.cache_resource
def get_history_sheet():
    """Connect to the Quotation History Google Sheet using the correct ID"""
    try:
        # Load service account info from Streamlit secrets
        creds_dict = st.secrets["gcp_service_account"]
        gc = gspread.service_account_from_dict(creds_dict)
        
        # Open the spreadsheet by ID (from the provided link)
        sh = gc.open_by_key("1RxKb_qj5JgXPy8bz9Fur1Jj6178fEXrP5d0W6BqwjDw")
        return sh.sheet1  # Assumes history is in first sheet
    except gspread.SpreadsheetNotFound:
        st.error(f"❌ Spreadsheet with ID '1RxKb_qj5JgXPy8bz9Fur1Jj6178fEXrP5d0W6BqwjDw' not found.")
        st.info("💡 Make sure:")
        st.markdown("""
        - The spreadsheet ID is correct
        - It is shared with: `quotationappserviceaccount@quotationapp-465511.iam.gserviceaccount.com`  
        - The service account has **Editor** access
        """)
        return None
    except Exception as e:
        st.error(f"❌ Failed to connect to history sheet: {e}")
        return None

def load_user_history_from_sheet(user_email, sheet):
    """Load user's quotation history from Google Sheet with fallbacks"""
    if sheet is None:
        return []
    try:
        df = get_as_dataframe(sheet)
        df.dropna(how='all', inplace=True)  # Remove completely empty rows
        
        # Debug: Show available columns
        st.session_state.debug_columns = df.columns.tolist()
        
        # Filter by user email (case-insensitive)
        user_rows = df[df["User Email"].str.lower() == user_email.lower()]
        history = []
        for _, row in user_rows.iterrows():
            try:
                items = json.loads(row["Items JSON"])
                
                # Check if Company Details JSON exists
                company_details_raw = row.get("Company Details JSON", "{}")
                try:
                    company_details = json.loads(company_details_raw) if pd.notna(company_details_raw) and company_details_raw.strip() != "" else {}
                except:
                    company_details = {}
                
                # If company details is empty, reconstruct with defaults
                if not company_details:
                    company_details = {
                        "company_name": row["Company Name"],
                        "contact_person": row["Contact Person"],
                        "contact_email": "",  # Not stored in sheet
                        # "contact_phone": row["Contact Phone"],
                        "address": "",  # Not stored in sheet
                        "warranty": "1 year",  # Default value
                        "down_payment": 50.0,  # Default value
                        "delivery": "Expected in 3–4 weeks",  # Default value
                        "vat_note": "Prices exclude 14% VAT",  # Default value
                        "shipping_note": "Shipping & Installation fees to be added",  # Default value
                        "bank": "CIB",  # Default value
                        "iban": "EG340010015100000100049865966",  # Default value
                        "account_number": "100049865966",  # Default value
                        "company": "FlakeTech for Trading Company",  # Default value
                        "tax_id": "626180228",  # Default value
                        "reg_no": "15971",  # Default value
                        "prepared_by": st.session_state.username,
                        "prepared_by_email": st.session_state.user_email,
                        "current_date": datetime.now().strftime("%A, %B %d, %Y"),
                        "valid_till": (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y"),
                        "quotation_validity": "30 days",
                        "vat_rate": 0.14,  # Add VAT rate for advanced PDF
                        "shipping_fee": 0.0,  # Default shipping fee
                        "installation_fee": 0.0  # Default installation fee
                    }
                
                # Ensure a valid hash exists
                stored_hash = str(row.get("Quotation Hash", "")).strip()
                if pd.isna(row.get("Quotation Hash")) or not stored_hash or stored_hash.lower() in ("nan", "none", "null", ""):
                    # Fallback: deterministic hash from key fields
                    fallback_data = f"{row['Company Name']}{row['Timestamp']}{row['Total']}"
                    stored_hash = hashlib.md5(fallback_data.encode()).hexdigest()

                history.append({
                    "user_email": row["User Email"],
                    "timestamp": row["Timestamp"],
                    "company_name": row["Company Name"],
                    "contact_phone": row["Contact Phone"],
                    "contact_person": row["Contact Person"],
                    "total": float(row["Total"]),
                    "items": items,
                    "pdf_filename": row["PDF Filename"],
                    "hash": stored_hash,
                    "company_details": company_details
                })
            except Exception as e:
                # st.warning(f"⚠️ Skipping malformed row (Company: {row.get('Company Name', 'Unknown')}): {e}")
                continue
        return history
    except Exception as e:
        st.error(f"❌ Failed to load history: {e}")
        return []

def delete_history_record(quotation_hash):
    """Delete a specific quotation record from the history sheet"""
    try:
        history_sheet = get_history_sheet()
        if not history_sheet:
            st.error("❌ Failed to connect to history sheet")
            return False
            
        # Get all data from the sheet
        df = get_as_dataframe(history_sheet)
        if df.empty:
            st.error("❌ History sheet is empty")
            return False
            
        # Find the row with matching quotation hash
        normalized_hash = str(quotation_hash).strip()
        matching_rows = df[df["Quotation Hash"].astype(str).str.strip() == normalized_hash]
        
        if len(matching_rows) == 0:
            st.error("❌ Quotation record not found")
            return False
            
        # Get the row index (adding 2 because: 0-indexed DataFrame + header row + 1 for Google Sheets)
        row_index = matching_rows.index[0] + 2
        
        # Delete the row
        history_sheet.delete_rows(int(row_index))
        
        # Clear cache and refresh
        st.cache_data.clear()
        
        st.success(f"✅ Quotation record deleted successfully!")
        return True
        
    except Exception as e:
        st.error(f"❌ Failed to delete quotation record: {str(e)}")
        return False

# ========== Google Drive URL Conversion ==========
def convert_google_drive_url_for_storage(url):
    """Convert Google Drive view URL to direct download URL."""
    if not url or pd.isna(url):
        return url
    drive_pattern = r'https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)/view'
    match = re.search(drive_pattern, str(url))
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url

def download_image_for_pdf(url, max_size=(300, 300)):
    """Download and resize image for PDF embedding."""
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

# ========== Header ==========
st.title("📜 Quotation History")
st.markdown(f"**Welcome:** {st.session_state.user_email} ({st.session_state.role})")

if st.button("⬅️ Back to Quotation Builder"):
    st.switch_page("app.py")

# ========== Refresh Button ==========
st.markdown("---")
if st.button("🔄 Refresh History from Cloud"):
    history_sheet = get_history_sheet()
    if history_sheet:
        st.session_state.history = load_user_history_from_sheet(st.session_state.user_email, history_sheet)
        st.success("✅ History refreshed from Google Sheet!")
    else:
        st.error("Failed to connect to Google Sheets.")
    st.rerun()

# ========== Search Bar ==========
st.markdown("---")
search_col, clear_col = st.columns([4, 1])
with search_col:
    search_term = st.text_input("🔍 Search quotations", 
                               placeholder="Search by company name...",
                               key="search_input").strip().lower()
with clear_col:
    st.markdown('<div style="height: 25px;"></div>', unsafe_allow_html=True)
    if st.button("Clear Search", use_container_width=True, key="clear_search_btn"):
        st.rerun()

if search_term:
    filtered_history = [quote for quote in st.session_state.history 
                       if search_term in safe_lower(quote['company_name'])]
    st.caption(f"Found {len(filtered_history)} quotation(s) matching your search")
else:
    filtered_history = st.session_state.history
    if st.session_state.history:
        st.caption(f"Displaying all {len(st.session_state.history)} quotations")

st.markdown("---")

# ========== Display History ==========
if not filtered_history:
    if search_term:
        st.info(f"📭 No quotations found for '{search_term}'. Try a different search.")
    else:
        st.info("📭 No quotations created yet. Start building one!")
else:
    # Display filtered history instead of full history
    for idx, quote in enumerate(reversed(filtered_history)):
        with st.expander(f"📄 {quote['company_name']} – {quote['total']:.2f} EGP ({quote['timestamp']})"):
            st.write(f"**Contact:** {quote['contact_person']} | **Items:** {len(quote['items'])}")
            st.dataframe(pd.DataFrame(quote['items']), use_container_width=True)

            col1, col2, col3, col4 = st.columns([1, 1, 1, 3])

            # Delete Button
            with col2:
                if st.button("🗑️ Delete", key=f"del_{idx}_{quote['hash']}"):
                    if st.session_state.get(f"confirm_delete_{idx}"):
                        if delete_history_record(quote["hash"]):
                            # Refresh history after successful deletion
                            history_sheet = get_history_sheet()
                            if history_sheet:
                                st.session_state.history = load_user_history_from_sheet(
                                    st.session_state.user_email, 
                                    history_sheet
                                )
                        st.rerun()
                    else:
                        st.session_state[f"confirm_delete_{idx}"] = True
                        st.warning("⚠️ Press 'Delete' again to confirm.")
                        st.rerun()
            
            # Edit Button
            with col3:
                if st.button("✏️ Edit Quotation", key=f"edit_{idx}_{quote['hash']}"):
                    # Restore into session state
                    st.session_state.form_submitted = True
                    st.session_state.company_details = quote.get("company_details") or {
                        "company_name": quote["company_name"],
                        "contact_person": quote.get("contact_person", ""),
                        "contact_email": "",
                        "contact_phone": "",
                        "address": "",
                        "prepared_by": st.session_state.username,
                        "prepared_by_email": st.session_state.user_email,
                        "current_date": datetime.now().strftime("%A, %B %d, %Y"),
                        "valid_till": (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y"),
                        "quotation_validity": "30 days",
                        "warranty": "1 year",
                        "down_payment": 50.0,
                        "delivery": "Expected in 3–4 weeks",
                        "vat_note": "Prices exclude 14% VAT",
                        "shipping_note": "Shipping & Installation fees to be added",
                        "bank": "CIB",
                        "iban": "EG340010015100000100049865966",
                        "account_number": "100049865966",
                        "company": "FlakeTech for Trading Company",
                        "tax_id": "626180228",
                        "reg_no": "15971"
                    }

                    # Reset product rows
                    st.session_state.row_indices = list(range(len(quote["items"])))
                    st.session_state.selected_products = {}

                    # Restore each product and inputs
                    for i, item in enumerate(quote["items"]):
                        prod_key = f"prod_{i}"
                        qty_key = f"qty_{i}"
                        disc_key = f"disc_{i}"
                        st.session_state.selected_products[prod_key] = item["Item"]
                        st.session_state[qty_key] = item["Quantity"]
                        st.session_state[disc_key] = item["Discount %"]

                    st.success("🔄 Loading quotation into editor...")
                    time.sleep(1)
                    st.switch_page("app.py")
