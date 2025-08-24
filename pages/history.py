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
                        "contact_phone": "",  # Not stored in sheet
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
                    "contact_person": row["Contact Person"],
                    "total": float(row["Total"]),
                    "items": items,
                    "pdf_filename": row["PDF Filename"],
                    "hash": stored_hash,
                    "company_details": company_details
                })
            except Exception as e:
                st.warning(f"⚠️ Skipping malformed row (Company: {row.get('Company Name', 'Unknown')}): {e}")
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

# ========== Advanced PDF Generation (Same as main app) ==========
@st.cache_data
def build_pdf_cached_history(data, total, company_details, data_hash, hdr_path="q2.png", ftr_path="footer (1).png", 
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
        base_headers = ["Ser.", "Item", "Image", "SKU", "Specs", "QTY", "Before Disc.", "Net Price", "Total"]
        if has_discounts:
            base_headers.insert(8, "Disc %")

        # === Column Widths (optimized for A3) ===
        col_widths = [30, 90, 120, 55, 130, 45, 65, 65, 65]  # Total: ~700pt
        if has_discounts:
            col_widths.insert(8, 55)  # "Disc %" column
        else:
            # Add the discount column width to Specs column when no discount
            col_widths[4] += 55

        total_table_width = sum(col_widths)
        temp_files = []

        # === Build Product Table Data with Optimized Images ===
        def create_product_row(r, idx):
            img_element = "No Image"
            if r.get("Image"):
                download_url = convert_google_drive_url_for_storage(r["Image"])
                temp_img_path = download_image_for_pdf(download_url, max_size=(300, 300))  # Optimized size
                if temp_img_path:
                    try:
                        img = RLImage(temp_img_path)
                        img.drawWidth = 90   # Slightly larger for better quality
                        img.drawHeight = 70  # Fits within row height
                        img.hAlign = 'CENTER'
                        img.vAlign = 'MIDDLE'
                        img.preserveAspectRatio = True
                        img_component = KeepInFrame(95, 75, [img], mode='shrink')
                        img_element = img_component
                        temp_files.append(temp_img_path)
                    except Exception as e:
                        print(f"Error creating image element: {e}")
                        img_element = "Image Error"

            # Optimized description formatting
            desc_text = safe_str(r.get('Description'))
            color_text = safe_str(r.get('Color'))
            warranty_text = safe_str(r.get('Warranty'))
            
            # Truncate if too long but keep important info
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

            # Truncate item name if too long
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
        # Available page height calculation
        page_height = A3[1]  # A3 height
        top_margin = 100
        bottom_margin = 250
        header_height = 25  # Table header height
        row_height = 95     # Estimated height per row (including images)
        summary_table_height = 200  # Space reserved for summary table
        spacer_height = 30  # Space for spacers
        
        available_height = page_height - top_margin - bottom_margin - header_height - spacer_height
        
        # Calculate rows per page dynamically
        def calculate_rows_per_page(is_last_chunk=False):
            height_for_table = available_height
            if is_last_chunk:
                height_for_table -= summary_table_height  # Reserve space for summary on last page
            
            max_rows = max(1, int(height_for_table // row_height))
            return min(max_rows, 8)  # Cap at 8 rows for safety
        
        # Split products into optimized chunks
        product_chunks = []
        remaining_products = data_from_hash[:]
        
        while remaining_products:
            is_last_chunk = len(remaining_products) <= calculate_rows_per_page(True)
            rows_for_this_page = calculate_rows_per_page(is_last_chunk)
            
            # Take products for this page
            chunk = remaining_products[:rows_for_this_page]
            product_chunks.append(chunk)
            remaining_products = remaining_products[rows_for_this_page:]

        # Create tables for each optimized chunk
        for chunk_idx, chunk in enumerate(product_chunks):
            is_last_chunk = (chunk_idx == len(product_chunks) - 1)
            
            # Create table data for this chunk
            chunk_table_data = [base_headers]  # Always include headers
            
            for idx, r in enumerate(chunk, start=sum(len(c) for c in product_chunks[:chunk_idx]) + 1):
                row = create_product_row(r, idx)
                chunk_table_data.append(row)

            # Create table for this chunk with optimized styling
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
            
            # Wrap table with padding - CRITICAL CHANGES HERE
            outer_data = [[chunk_table]]
            # For the last chunk, remove bottom padding to eliminate space before summary table
            bottom_padding = 0 if is_last_chunk else 10
            outer_style = TableStyle([
                ('LEFTPADDING', (0, 0), (0, 0), 50),
                ('RIGHTPADDING', (0, 0), (0, 0), 0),
                ('TOPPADDING', (0, 0), (0, 0), 0),
                ('BOTTOMPADDING', (0, 0), (0, 0), bottom_padding),  # Set to 0 for last chunk
                ('GRID', (0, 0), (0, 0), 0, colors.transparent),
            ])
            outer_table = Table(outer_data, colWidths=[total_table_width], style=outer_style)
            elems.append(outer_table)
            
            # Add summary table only to the last chunk
            if is_last_chunk:
                # === Summary Table (on the same page as last product chunk) ===
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

                # Collect discount row indices for styling
                discount_row_indices = [i for i, row in enumerate(summary_data) if "Discount" in row[0]]

                summary_col_widths = [total_table_width - 150, 150]
                summary_table = Table(summary_data, colWidths=summary_col_widths)

                # Base styles
                summary_styles = [
                    ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                    ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                    ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
                    ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 12),
                    ('GRID', (0, 0), (-1, -1), 1.0, colors.black),
                    ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
                ]

                # Add red text for discount amounts
                for row_idx in discount_row_indices:
                    summary_styles.append(('TEXTCOLOR', (1, row_idx), (1, row_idx), colors.black))

                summary_table.setStyle(TableStyle(summary_styles))

                # Add summary table with NO TOP PADDING - CRITICAL CHANGE
                outer_summary_data = [[summary_table]]
                outer_summary_style = TableStyle([
                    ('LEFTPADDING', (0, 0), (0, 0), 50),
                    ('RIGHTPADDING', (0, 0), (0, 0), 0),
                    ('TOPPADDING', (0, 0), (0, 0), 0),  # Must be 0 to eliminate space
                    ('BOTTOMPADDING', (0, 0), (0, 0), 0),
                    ('GRID', (0, 0), (0, 0), 0, colors.transparent),
                ])
                outer_summary = Table(outer_summary_data, colWidths=[total_table_width], style=outer_summary_style)
                elems.append(outer_summary)
            else:
                # Add page break between chunks (except for the last chunk)
                elems.append(PageBreak())

        # === Closure Page - CRITICAL FIX: ADD CONTENT TO THIS PAGE ===
        if closure_path and os.path.exists(closure_path):
            # Add a page break followed by a spacer to create a non-empty page
            elems.append(PageBreak())
            # Add a spacer to ensure the page has content (prevents ReportLab from optimizing it away)
            elems.append(Spacer(1, 1))
            # Calculate closure page number AFTER adding all elements
            # The closure page is the last page (total number of PageBreaks + 1)
            closure_page_num = len([e for e in elems if isinstance(e, PageBreak)]) + 1

        # Build PDF - SIMPLIFIED TO A SINGLE BUILD PASS
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

    # Pass the actual data
    return build_pdf(data, total, company_details, hdr_path, ftr_path, 
                    intro_path, closure_path, bg_path)

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

            # Regenerate PDF Button (Updated to use advanced PDF generation)
            with col1:
                quote_hash = quote.get("hash", f"unknown_{idx}")
                if st.button(f"📄 Regenerate PDF", key=f"regen_{idx}_{quote_hash}"):
                    with st.spinner("Rebuilding PDF with advanced formatting..."):
                        try:
                            # Ensure company details have all required fields for advanced PDF
                            temp_details = quote.get("company_details") or {}
                            
                            # Add missing required fields with defaults
                            default_company_details = {
                                "company_name": quote["company_name"],
                                "contact_person": quote.get("contact_person", ""),
                                "contact_email": temp_details.get("contact_email", ""),
                                "contact_phone": temp_details.get("contact_phone", ""),
                                "address": temp_details.get("address", ""),
                                "prepared_by": st.session_state.username,
                                "prepared_by_email": st.session_state.user_email,
                                "current_date": datetime.now().strftime("%A, %B %d, %Y"),
                                "valid_till": (datetime.now() + timedelta(days=10)).strftime("%A, %B %d, %Y"),
                                "quotation_validity": "30 days",
                                "warranty": temp_details.get("warranty", "1 year"),
                                "down_payment": temp_details.get("down_payment", 50.0),
                                "delivery": temp_details.get("delivery", "Expected in 3–4 weeks"),
                                "vat_note": temp_details.get("vat_note", "Prices exclude 14% VAT"),
                                "shipping_note": temp_details.get("shipping_note", "Shipping & Installation fees to be added"),
                                "bank": temp_details.get("bank", "CIB"),
                                "iban": temp_details.get("iban", "EG340010015100000100049865966"),
                                "account_number": temp_details.get("account_number", "100049865966"),
                                "company": temp_details.get("company", "FlakeTech for Trading Company"),
                                "tax_id": temp_details.get("tax_id", "626180228"),
                                "reg_no": temp_details.get("reg_no", "15971"),
                                "vat_rate": temp_details.get("vat_rate", 0.14),  # Required for advanced PDF
                                "shipping_fee": temp_details.get("shipping_fee", 0.0),  # Required for advanced PDF
                                "installation_fee": temp_details.get("installation_fee", 0.0)  # Required for advanced PDF
                            }
                            
                            # Generate unique hash for caching
                            data_str = str(quote["items"]) + str(quote["total"]) + str(default_company_details)
                            data_hash = hashlib.md5(data_str.encode()).hexdigest()
                            
                            # Use the advanced PDF generation function
                            pdf_file = build_pdf_cached_history(
                                quote["items"], 
                                quote["total"], 
                                default_company_details,
                                data_hash
                            )
                            
                            if pdf_file:
                                with open(pdf_file, "rb") as f:
                                    st.download_button(
                                        "⬇ Download Advanced PDF",
                                        f,
                                        file_name=quote["pdf_filename"],
                                        mime="application/pdf",
                                        key=f"dl_hist_{idx}"
                                    )
                                st.success("✅ PDF generated with advanced formatting!")
                            else:
                                st.error("❌ Failed to generate PDF file")
                                
                        except Exception as e:
                            st.error(f"❌ Failed to generate PDF: {e}")
                            # Show detailed error for debugging
                            st.exception(e)

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
