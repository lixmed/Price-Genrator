import streamlit as st
import pandas as pd
import re
import math
import hashlib
import requests
from io import BytesIO
from PIL import Image as PILImage
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Spacer, Paragraph, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A3
from reportlab.lib import colors
import tempfile
import os
from datetime import datetime, timedelta
import gspread
from gspread_dataframe import get_as_dataframe, set_with_dataframe
# ========== Page Config ==========
st.set_page_config(page_title="Quotation Builder", page_icon="🪑", layout="wide")

# ========== User Credentials ==========
USERS = {
    "admin1@example.com": {"username": "admin1", "password": "admin123", "role": "admin"},
    "admin2@example.com": {"username": "admin2", "password": "admin456", "role": "admin"},
    "user1@example.com":  {"username": "user1",  "password": "user123",  "role": "buyer"},
    "user2@example.com":  {"username": "user2",  "password": "user456",  "role": "buyer"},
}

# ========== Session State Initialization ==========
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
    }
    for key, default_value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default_value

init_session_state()

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
@st.cache_resource
def get_gsheet_connection():
    """Cached Google Sheets connection"""
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.readonly']
        sa = gspread.service_account(scopes=scopes)
        return sa.open("testspreadsheet2").sheet1
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
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
                st.image(img, caption=prod, use_container_width=False)
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
    st.title("Login")
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submit_login = st.form_submit_button("Login")
        if submit_login:
            user = USERS.get(email)
            if user and user["password"] == password:
                st.session_state.logged_in = True
                st.session_state.user_email = email
                st.session_state.username = user["username"]
                st.session_state.role = user["role"]
                st.rerun()
            else:
                st.error("❌ Incorrect email or password.")
    st.stop()

# ========== Logout Sidebar ==========
st.sidebar.success(f"Logged in as: {st.session_state.user_email} ({st.session_state.role})")
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
            st.rerun()
    with col2:
        if st.session_state.admin_choice == "database":
            st.markdown("Current Mode: 🗃 Database Management")
        else:
            st.markdown("Current Mode: 📋 Quotation Creation")
    
    st.markdown("---")
    
    if st.session_state.admin_choice == "database":
        tab1, tab2, tab3 = st.tabs(["➕ Add Product", "🗑 Delete Product", "✏ Update Product"])
        with tab1:
            st.subheader("Add New Product")
            form_col, image_col = st.columns([2, 1])
            with form_col:
                with st.form("add_product_form"):
                    new_item = st.text_input("Product Name")
                    new_price = st.number_input("Price per Item", min_value=0.0, format="%.2f")
                    new_desc = st.text_area("Material / Description")
                    new_color = st.text_input("Color")
                    new_dim = st.text_input("Dimensions (Optional)")
                    new_image = st.text_input("Image URL (Optional)", help="Paste Google Drive link or direct image URL")
                    if st.form_submit_button("✅ Add to Sheet"):
                        if not new_item:
                            st.warning("Product name is required.")
                        else:
                            converted_image_url = convert_google_drive_url_for_storage(new_image) if new_image else ""
                            new_row = {
                                "Item Name": new_item,
                                "Selling Price": new_price,
                                "Sales Description": new_desc,
                                "CF.Colors": new_color,
                                "CF.Dimensions": new_dim,
                                "CF.image url": converted_image_url
                            }
                            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                            set_with_dataframe(sheet, df)
                            st.cache_data.clear()
                            st.success(f"✅ '{new_item}' added successfully!")
                            st.rerun()
            with image_col:
                st.markdown("Image Preview:")
                display_admin_preview(new_image if 'new_image' in locals() else "")
                st.markdown("Supported formats:")
                st.markdown("• Direct image URLs (jpg, png, etc.)")
                st.markdown("• Google Drive share links")
                st.markdown("Example Google Drive URL:")
                st.code("https://drive.google.com/file/d/1vN8l2FX.../view", language="text")
        
        with tab2:
            st.subheader("Delete Product")
            with st.form("delete_product_form"):
                product_to_delete = st.selectbox("Select product to delete", df["Item Name"].tolist())
                st.warning("⚠ This will permanently delete the product from the spreadsheet!")
                confirm_delete = st.checkbox("I confirm I want to delete this product")
                submitted = st.form_submit_button("❌ Delete Product")
                if submitted and confirm_delete:
                    matching_rows = df[df["Item Name"] == product_to_delete]
                    if len(matching_rows) == 0:
                        st.error(f"Product '{product_to_delete}' not found.")
                    else:
                        row_index = matching_rows.index[0] + 2
                        sheet.delete_rows(int(row_index))
                        st.cache_data.clear()
                        st.success(f"❌ '{product_to_delete}' deleted successfully!")
                        st.rerun()
                    if not confirm_delete:
                        st.error("Please check the confirmation box to delete")
        
        with tab3:
            st.subheader("Update Product")
            form_col, image_col = st.columns([2, 1])
            with form_col:
                selected_product = st.selectbox("Select product to update", df["Item Name"].tolist(), key="update_product_select")
                existing_row = df[df["Item Name"] == selected_product].iloc[0] if selected_product else None
                with st.form("update_product_form"):
                    if existing_row is not None:
                        updated_name = st.text_input("Update Product Name", value=selected_product)
                        updated_price = st.number_input("Update Price", value=float(existing_row["Selling Price"]))
                        updated_desc = st.text_area("Update Description", value=existing_row.get("Sales Description", ""))
                        updated_color = st.text_input("Update Color", value=existing_row.get("CF.Colors", ""))
                        updated_dim = st.text_input("Update Dimensions", value=existing_row.get("CF.Dimensions", ""))
                        updated_image = st.text_input("Update Image URL", value=existing_row.get("CF.image url", ""), help="Paste Google Drive link or direct image URL")
                    else:
                        updated_name = st.text_input("Update Product Name", value="")
                        updated_price = st.number_input("Update Price", value=0.0)
                        updated_desc = st.text_area("Update Description", value="")
                        updated_color = st.text_input("Update Color", value="")
                        updated_dim = st.text_input("Update Dimensions", value="")
                        updated_image = st.text_input("Update Image URL", value="", help="Paste Google Drive link or direct image URL")
                    if st.form_submit_button("✅ Apply Update"):
                        if selected_product and updated_name.strip():
                            if updated_name != selected_product and updated_name in df["Item Name"].values:
                                st.error(f"❌ Product name '{updated_name}' already exists!")
                            else:
                                converted_image_url = convert_google_drive_url_for_storage(updated_image) if updated_image else ""
                                df.loc[df["Item Name"] == selected_product, 
                                       ["Item Name", "Selling Price", "Sales Description", "CF.Colors", "CF.Dimensions", "CF.image url"]] = \
                                    [updated_name.strip(), updated_price, updated_desc, updated_color, updated_dim, converted_image_url]
                                set_with_dataframe(sheet, df)
                                st.cache_data.clear()
                                st.success(f"✅ '{selected_product}' updated successfully!")
                                st.rerun()
                        elif not updated_name.strip():
                            st.error("❌ Product name cannot be empty!")
                        else:
                            st.error("Please select a product to update")
            with image_col:
                st.markdown("Current Product Data:")
                if selected_product and existing_row is not None:
                    st.write(f"Product: {selected_product}")
                    st.write(f"Current Price: ${existing_row['Selling Price']:.2f}")
                    if existing_row.get("Sales Description", ""):
                        st.write(f"Description: {existing_row['Sales Description']}")
                    if existing_row.get("CF.Colors", ""):
                        st.write(f"Color: {existing_row['CF.Colors']}")
                    if existing_row.get("CF.Dimensions", ""):
                        st.write(f"Dimensions: {existing_row['CF.Dimensions']}")
                    st.markdown("---")
                    st.markdown("Current Image:")
                    current_image = existing_row.get("CF.image url", "")
                    if current_image:
                        display_admin_preview(current_image, f"Current image for {selected_product}")
                    else:
                        st.info("📷 No image set for this product")
                else:
                    st.info("👆 Select a product above to see its current data")
                st.markdown("Updated Image Preview:")
                if selected_product and 'updated_image' in locals() and updated_image:
                    display_admin_preview(updated_image, "Updated Image Preview")
                elif selected_product:
                    st.info("📷 Enter a new image URL above to see preview")
        st.stop()
    
    elif st.session_state.admin_choice == "quotation":
        st.header("🛍 Create Quotation")
        if not st.session_state.get('form_submitted', False):
            st.subheader("Company and Contact Details")
            with st.form(key="admin_company_details_form"):
                company_name = st.text_input("🏢 Company Name")
                contact_person = st.text_input("Contact Person")
                contact_email = st.text_input("Contact Email (Optional)")
                contact_phone = st.text_input("Contact Cell Phone")
                address = st.text_area("Address (Optional)", placeholder="Enter address (optional)")
                
                st.subheader("Terms and Conditions")
                warranty = st.text_input("Warranty", value="1 year")
                down_payment = st.number_input("Down payment (%)", min_value=0.0, max_value=100.0, value=50.0)
                delivery = st.text_input("Delivery", value="Expected in 3–4 weeks")
                vat_note = st.text_input("VAT Note", value="Prices exclude 14% VAT")
                shipping_note = st.text_input("Shipping Note", value="Shipping & Installation fees to be added")
                
                st.subheader("Payment Info")
                bank = st.text_input("Bank", value="CIB")
                iban = st.text_input("IBAN", value="EG340010015100000100049865966")
                account_number = st.text_input("Account Number", value="100049865966")
                company = st.text_input("Company", value="FlakeTech for Trading Company")
                tax_id = st.text_input("Tax ID", value="626180228")
                reg_no = st.text_input("Commercial/Chamber Reg. No", value="15971")
                
                email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
                phone_pattern = r'^\+?\d+$'
                
                prepared_by = st.session_state.username
                prepared_by_email = st.session_state.user_email
                current_date = datetime.now().strftime("%A, %B %d, %Y")
                valid_till = (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y")
                quotation_validity = "30 days"
                
                if st.form_submit_button("Submit Details"):
                    # if not re.match(email_pattern, contact_email):
                    #     st.error("❌ Invalid email format.")
                    if not re.match(phone_pattern, contact_phone):
                        st.error("❌ Invalid phone number.")
                    elif not all([company_name, contact_person, contact_phone, prepared_by, prepared_by_email]):
                        st.warning("⚠ Please fill in all required fields.")
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
                            "shipping_note": shipping_note,
                            "bank": bank,
                            "iban": iban,
                            "account_number": account_number,
                            "company": company,
                            "tax_id": tax_id,
                            "reg_no": reg_no
                        }
                        st.rerun()
            if not st.session_state.get('form_submitted', False):
                st.warning("⚠ Please fill in all company and contact details.")
                st.stop()

# ========== Regular Buyer Panel ==========
elif st.session_state.role == "buyer":
    st.header("🛍 Buy Products & Get Quotation")
    if not st.session_state.get('form_submitted', False):
        st.subheader("Company and Contact Details")
        with st.form(key="buyer_company_details_form"):
            company_name = st.text_input("🏢 Company Name")
            contact_person = st.text_input("Contact Person")
            contact_email = st.text_input("Contact Email")
            contact_phone = st.text_input("Contact Cell Phone")
            address = st.text_area("Address (Optional)", placeholder="Enter address (optional)")
            
            st.subheader("Terms and Conditions")
            warranty = st.text_input("Warranty", value="1 year")
            down_payment = st.number_input("Down payment (%)", min_value=0.0, max_value=100.0, value=50.0)
            delivery = st.text_input("Delivery", value="Expected in 3–4 weeks")
            vat_note = st.text_input("VAT Note", value="Prices exclude 14% VAT")
            shipping_note = st.text_input("Shipping Note", value="Shipping & Installation fees to be added")
            
            st.subheader("Payment Info")
            bank = st.text_input("Bank", value="CIB")
            iban = st.text_input("IBAN", value="EG340010015100000100049865966")
            account_number = st.text_input("Account Number", value="100049865966")
            company = st.text_input("Company", value="FlakeTech for Trading Company")
            tax_id = st.text_input("Tax ID", value="626180228")
            reg_no = st.text_input("Commercial/Chamber Reg. No", value="15971")
            
            email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            phone_pattern = r'^\+?\d+$'
            
            prepared_by = st.session_state.username
            prepared_by_email = st.session_state.user_email
            current_date = datetime.now().strftime("%A, %B %d, %Y")
            valid_till = (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y")
            quotation_validity = "30 days"
            
            
            if st.form_submit_button("Submit Details"):
                # if not re.match(email_pattern, contact_email):
                #     st.error("❌ Invalid email format.")
                if not re.match(phone_pattern, contact_phone):
                    st.error("❌ Invalid phone number.")
                elif not all([company_name, contact_person, contact_phone, prepared_by, prepared_by_email]):
                    st.warning("⚠ Please fill in all required fields.")
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
                        "shipping_note": shipping_note,
                        "bank": bank,
                        "iban": iban,
                        "account_number": account_number,
                        "company": company,
                        "tax_id": tax_id,
                        "reg_no": reg_no
                    }
                    st.rerun()
        if not st.session_state.get('form_submitted', False):
            st.warning("⚠ Please fill in all company and contact details.")
            st.stop()

# ========== Quotation Display Section ==========
if ((st.session_state.role == "buyer") or 
    (st.session_state.role == "admin" and st.session_state.get('admin_choice') == "quotation")) and \
    st.session_state.get('form_submitted', False):
    
    company_details = st.session_state.company_details
    st.markdown(f"📋 Quotation for {company_details['company_name']}")
    st.subheader("Select Products")
    st.info("📝 Select products below to add them to your quotation")
    
    # Initialize cart if needed
    if 'cart' not in st.session_state:
        st.session_state.cart = []
    
    # Display cart contents
    if st.session_state.cart:
        st.subheader("🛒 Your Selected Products")
        total_amount = 0
        for item in st.session_state.cart:
            col1, col2, col3 = st.columns([3, 1, 1])
            with col1:
                st.write(f"{item['name']}")
            with col2:
                st.write(f"Qty: {item['quantity']}")
            with col3:
                st.write(f"${item['total']:.2f}")
            total_amount += item['total']
        st.markdown("---")
        st.markdown(f"Total Amount: ${total_amount:.2f}")
        if st.button("📄 Generate Final Quotation"):
            st.success("✅ Quotation generated successfully!")

# ========== Product Selection Interface ==========
# Create mappings from dataframe
products = df['Item Name'].tolist()
price_map = dict(zip(df['Item Name'], df['Selling Price']))
desc_map = dict(zip(df['Item Name'], df.get('Sales Description', '')))
color_map = dict(zip(df['Item Name'], df.get('CF.Colors', '')))
dim_map = dict(zip(df['Item Name'], df.get('CF.Dimensions', '')))
image_map = dict(zip(df['Item Name'], df.get('CF.image url', ''))) if 'CF.image url' in df.columns else {}
Warranty_map = dict(zip(df['Item Name'], df.get('CF.Warranty', '')))
SKU_map = dict(zip(df['Item Name'], df.get('SKU', '')))

st.markdown(f"Quotation for {company_details['company_name']}")
cols = st.columns([3.0, 1.8, 1.4, 2.5, 2.0, 2.0, 2.0, 2.0, 0.8])
headers = ["Product", "SKU", "Warranty", "Image", "Price per 1", "Quantity", "Discount %", "Total", "Clear"]
for i, header in enumerate(headers):
    cols[i].markdown(f"{header}")

# Initialize variables for product selection
output_data = []
total_sum = 0
checkDiscount = False
basePrice = 0.0

# Display product selection rows
for idx in st.session_state.row_indices:
    c1, c2, c3, c4, c5, c6, c7, c8, c9 = st.columns([3.0, 1.8, 1.4, 2.5, 2.0, 2.0, 2.0, 2.0, 0.8])
    prod_key = f"prod_{idx}"
    if prod_key not in st.session_state.selected_products:
        st.session_state.selected_products[prod_key] = "-- Select --"
    current_selection = st.session_state.selected_products[prod_key]
    prod = c1.selectbox("", ["-- Select --"] + products, key=prod_key, label_visibility="collapsed",
                        index=products.index(current_selection) + 1 if current_selection in products else 0)
    st.session_state.selected_products[prod_key] = prod
    
    # Clear button for this row
    if c9.button("X", key=f"clear_{idx}"):
        st.session_state.row_indices.remove(idx)
        st.session_state.selected_products.pop(prod_key, None)
        st.rerun()
    
    if prod != "-- Select --":
        # Process selected product
        unit_price = price_map[prod]
        qty = c6.number_input("", min_value=1, value=1, step=1, key=f"qty_{idx}", label_visibility="collapsed")
        discount = c7.number_input("", min_value=0.0, max_value=100.0, value=0.0, step=1.0, key=f"disc_{idx}", label_visibility="collapsed")
        valid_discount = 0.0 if discount > 20 else discount
        if discount > 20:
            st.warning(f"⚠ Max 20% discount allowed for '{prod}'. Ignoring discount.")
        if valid_discount > 0:
            checkDiscount = True
        basePrice += unit_price * qty
        discounted_price = unit_price * (1 - valid_discount / 100)
        line_total = discounted_price * qty
        
        # Display product details
        image_url = image_map.get(prod, "")
        display_product_image(c4, prod, image_url)
        
        c5.write(f"{unit_price:.2f} EGP")
        c8.write(f"{line_total:.2f} EGP")
        c2.write(f"{SKU_map.get(prod, 'N/A')}")
        c3.write(f"{Warranty_map.get(prod, 'N/A')}")
        
        # Collect output data
        output_data.append({
            "Item": prod,
            "Description": desc_map.get(prod, ""),
            "Color": color_map.get(prod, ""),
            "Dimensions": dim_map.get(prod, ""),
            "Image": convert_google_drive_url_for_display(image_url) if image_url else "",
            "Quantity": qty,
            "Price per item": unit_price,
            "Discount %": valid_discount,
            "Total price": line_total,
            "SKU": SKU_map.get(prod, ""),
            "Warranty": Warranty_map.get(prod, ""),
        })
        total_sum += line_total
    else:
        # Empty placeholders for unselected products
        for col in [c2, c3, c4, c5, c6]:
            col.write("—")

# Add product button
if st.button("➕ Add Product"):
    st.session_state.row_indices.append(max(st.session_state.row_indices, default=-1) + 1)
    st.rerun()

st.markdown("---")

# Calculate final total with discounts
final_total = total_sum
if not checkDiscount:
    # Overall discount option (only if no individual discounts)
    overall_discount = st.number_input("🧮 Overall Quotation Discount (%)", min_value=0.0, max_value=100.0, step=1.0, value=0.0)
    if overall_discount > 20:
        st.warning("⚠ Overall discount cannot exceed 20%. Ignoring discount.")
        overall_discount = 0.0
    basePrice = total_sum
    final_total = total_sum * (1 - overall_discount / 100)
    st.markdown(f"💰 *Total Before Discount: {total_sum:.2f} EGP")
    st.markdown(f"🔻 *Discount Applied: {overall_discount:.0f}%")
    st.markdown(f"🧾 *Final Total: {final_total:.2f} EGP")
else:
    st.markdown("⚠ You cannot add overall discount when individual discounts are applied")

st.markdown("---")
st.markdown(f"### 💰 Grand Total: {final_total:.2f} EGP")
if output_data:
    st.dataframe(pd.DataFrame(output_data), use_container_width=True)

# ========== PDF Generation Functions ==========
def download_image_for_pdf(url, max_size=(300, 300)):
    """Download and resize image for PDF inclusion"""
    try:
        # Get image from URL
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        img = PILImage.open(BytesIO(response.content)).convert("RGB")
        
        # Calculate resize dimensions
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
        
        # Save to temporary file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        img.save(temp_file, format="PNG")
        temp_file.close()
        return temp_file.name
    except Exception as e:
        print(f"Image download/resize failed: {e}")
        return None

@st.cache_data
def build_pdf_cached(data_hash, total, company_details, hdr_path="q2.png", ftr_path="footer (1).png"):
    """Cached function to build PDF quotation"""
    def build_pdf(data, total, company_details, hdr_path, ftr_path):
        # Create temporary PDF file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf_path = tmp.name
        tmp.close()
        
        # Initialize PDF document
        doc = SimpleDocTemplate(pdf_path, pagesize=A3, topMargin=230, leftMargin=40, rightMargin=40, bottomMargin=250)
        styles = getSampleStyleSheet()
        elems = []
        
        # Configure styles
        styles['Normal'].fontSize = 14
        styles['Normal'].leading = 20
        aligned_style = ParagraphStyle(name='LeftAligned', parent=styles['Normal'], leftIndent=0, firstLineIndent=0, alignment=0, spaceBefore=12, spaceAfter=12)
        
        def header_footer(canvas, doc):
            """Add header and footer to each page"""
            canvas.saveState()
            # Add header image
            if hdr_path and os.path.exists(hdr_path):
                img = PILImage.open(hdr_path)
                w, h = img.size
                img_w = doc.width + doc.leftMargin + doc.rightMargin
                img_h = img_w * (h / w)
                canvas.drawImage(hdr_path, 0, A3[1] - img_h + 10, width=img_w, height=img_h)
            
            # Add footer image
            footer_height = 0
            if ftr_path and os.path.exists(ftr_path):
                img2 = PILImage.open(ftr_path)
                w2, h2 = img2.size
                img_w2 = doc.width + doc.leftMargin + doc.rightMargin
                img_h2 = img_w2 * (h2 / w2)
                canvas.drawImage(ftr_path, 0, 1, width=img_w2, height=img_h2)
                footer_height = img_h2
            
            # Add page number
            canvas.setFont('Helvetica', 10)
            page_num = canvas.getPageNumber()
            text = f"{page_num}"
            canvas.drawRightString(doc.width + doc.leftMargin, footer_height + 10, text)
            canvas.restoreState()
        
        # Company and contact details section
        detail_lines = [
            "<para align='left'>",
            "<font size=14>",
            "<b>Company Address:</b> <font color='black'>Al Salam First, Cairo Governorate, Al Qahirah, Cairo</font><br/>",
            "<b>Company Phone:</b> <font color='black'>01025780717</font><br/><br/>",
            f"<b>Date:</b> <font color='black'>{company_details['current_date']}</font><br/>",
            f"<b>Valid Till:</b> <font color='black'>{company_details['valid_till']}</font><br/>",
            f"<b>Quotation Validity:</b> <font color='black'>{company_details['quotation_validity']}</font><br/>",
            f"<b>Prepared By:</b> <font color='black'>{company_details['prepared_by']}</font><br/>",
            f"<b>Email:</b> <font color='black'>{company_details['prepared_by_email']}</font><br/><br/>",
            f"<b>Contact Person:</b> <font color='black'>{company_details['contact_person']}</font><br/>",
            f"<b>Company Name:</b> <font color='black'>{company_details['company_name']}</font><br/>",
        ]

        # Add optional address if provided
        if company_details.get("address"):
            detail_lines.append(f"<b>Address:</b> <font color='black'>{company_details['address']}</font><br/>")

        detail_lines.append(f"<b>Cell Phone:</b> <font color='black'>{company_details['contact_phone']}</font><br/>")

        # Add optional email if provided
        if company_details.get("contact_email"):
            detail_lines.append(f"<b>Contact Email:</b> <font color='black'>{company_details['contact_email']}</font><br/>")

        detail_lines.append("</font>")
        detail_lines.append("</para>")

        details = "\n".join(detail_lines)

        elems.append(Spacer(1, 40))
        elems.append(Paragraph(details, aligned_style))
        
        # Terms and conditions section
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
        
        # Payment information section
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
        elems.append(Spacer(1, 70))
        
        # Configure table styles
        desc_style = ParagraphStyle(name='Description', fontSize=12, leading=16, alignment=1)
        styleN = ParagraphStyle(name='Normal', fontSize=12, leading=12, alignment=1)
        
        # Get product data from session state
        data_from_hash = st.session_state.get('pdf_data', [])
        product_table_data = [["Ser.", "SKU", "Warranty", "Image", "Product", "Color", "Description", "QTY", "Unit Price", "Line Total"]]
        temp_files = []
        
        # Build product table rows
        for idx, r in enumerate(data_from_hash, start=1):
            img_element = ""
            if "Image" in r and r["Image"] and r["Image"] != "":
                # Process product image
                download_url = convert_google_drive_url_for_storage(r["Image"])
                temp_img_path = download_image_for_pdf(download_url, max_size=(300, 300))
                if temp_img_path:
                    try:
                        img_element = RLImage(temp_img_path)
                        img_element._restrictSize(120, 80)
                        temp_files.append(temp_img_path)
                    except Exception as e:
                        print(f"Error creating image element: {e}")
                        img_element = "No Image"
                else:
                    img_element = "No Image"
            else:
                img_element = "No Image"
            
            # Add product row to table
            product_table_data.append([
                str(idx),
                Paragraph(str(r.get('SKU', '')).upper(), styleN),
                Paragraph(str(r.get('Warranty', '')), styleN),
                img_element,
                Paragraph(str(r.get('Item', '')), styleN),
                Paragraph(str(r.get('Color', '')), styleN),
                Paragraph(str(r.get('Description', '')), desc_style),
                str(r['Quantity']),
                f"{r['Price per item']:.2f}",
                f"{r['Total price']:.2f}"
            ])
        
        # Create product table with styling
        product_table = Table(product_table_data, 
                              colWidths=[30, 60, 60, 100, 100, 60, 180, 30, 60, 60],
                              rowHeights=[25] + [None] * (len(product_table_data) - 1))
        product_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 12),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('LEADING', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7)
        ]))
        elems.append(product_table)
        
        # Calculate totals
        subtotal = basePrice
        total_after_discount = final_total
        discount = subtotal - total_after_discount
        vat = total_after_discount * 0.15
        grand_total = total_after_discount + vat
        
        # Create summary table
        summary_data = [
            ["Total", f" {subtotal:.2f}"],
            ["Special Discount", f" {discount:.2f}"],
            ["Total After Discount", f" {total_after_discount:.2f}"],
            ["VAT (15%)", f" {vat:.2f}"],
            ["Grand Total", f" {grand_total:.2f}"]
        ]
        summary_table = Table(summary_data, colWidths=[590, 150])
        summary_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -2), 'Helvetica'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1.0, colors.black),
            ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
        ]))
        elems.append(summary_table)
        
        # Build the PDF document
        try:
            doc.build(elems, onFirstPage=header_footer, onLaterPages=header_footer)
        finally:
            # Clean up temporary files
            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except:
                    pass
        return pdf_path
    
    # Store data in session state for PDF generation
    st.session_state.pdf_data = st.session_state.get('pdf_data', [])
    return build_pdf([], total, company_details, hdr_path, ftr_path)

# Generate PDF button
if st.button("📅 Generate PDF Quotation") and output_data:
    with st.spinner("Generating PDF..."):
        # Store data for PDF generation
        st.session_state.pdf_data = output_data
        data_str = str(output_data) + str(final_total) + str(company_details)
        data_hash = hashlib.md5(data_str.encode()).hexdigest()
        pdf_file = build_pdf_cached(data_hash, final_total, company_details)
        # Provide download button for generated PDF
        with open(pdf_file, "rb") as f:
            st.download_button(
                label="⬇ Click to Download PDF",
                data=f,
                file_name=f"{company_details['company_name']}_quotation.pdf",
                mime="application/pdf"

            )

