import streamlit as st
import pandas as pd
import hashlib
import math
from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Spacer, Paragraph, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A3
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
# from reportlatypus import PageBreak
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
    """Connect to 'Amjad's history' Google Sheet"""
    try:
        gc = gspread.service_account(filename="amjad_quotation_service_account.json")
        sh = gc.open("Amjad's history")  # ← Spreadsheet name
        return sh.sheet1
    except gspread.SpreadsheetNotFound:
        st.error("❌ Spreadsheet **'Amjad's history'** not found.")
        st.info("💡 Make sure:")
        st.markdown("""
        - The spreadsheet is named exactly: `Amjad's history`  
        - It is shared with: `amjadquotation@quotationappamjad.iam.gserviceaccount.com`  
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
        user_rows = df[df["User Email"].str.lower() == user_email.lower()]
        history = []
        for _, row in user_rows.iterrows():
            try:
                items = json.loads(row["Items JSON"])
                company_details_raw = row.get("Company Details JSON", "{}")
                try:
                    company_details = json.loads(company_details_raw) if pd.notna(company_details_raw) and company_details_raw.strip() != "" else {}
                except:
                    company_details = {}

                # 🔐 Ensure a valid hash exists
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
                  "hash": stored_hash,  # ← Guaranteed to exist
                  "company_details": company_details
              })
            except Exception as e:
                st.warning(f"⚠️ Skipping malformed row (Company: {row.get('Company Name', 'Unknown')}): {e}")
                continue
        return history
    except Exception as e:
        st.error(f"❌ Failed to load history: {e}")
        return []

def save_quotation_to_sheet(quote, sheet):
    """
    Save a quotation record to Google Sheet
    quote: dict (same structure as st.session_state.history item)
    sheet: gspread worksheet object
    """
    row = [
        quote["user_email"],
        quote["timestamp"],
        quote["company_name"],
        quote["contact_person"],
        f"{quote['total']:.2f}",
        json.dumps(quote["items"]),
        json.dumps(quote.get("company_details", {})),
        quote["pdf_filename"],
        quote["hash"]
    ]
    try:
        sheet.append_row(row)
        return True
    except Exception as e:
        st.error(f"❌ Failed to save to Google Sheet: {e}")
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

def generate_pdf_from_data(items, total, company_details, hdr_path="q2.png", ftr_path="footer (1).png"):
    """Generate a professional PDF with conditional discount display."""
    def build_pdf(data, total, company_details, hdr_path, ftr_path):
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
            leftIndent=0,
            firstLineIndent=0,
            alignment=0,
            spaceBefore=12,
            spaceAfter=12
        )

        def header_footer(canvas, doc):
            canvas.saveState()
            # Header
            if hdr_path and os.path.exists(hdr_path):
                img = PILImage.open(hdr_path)
                w, h = img.size
                img_w = doc.width + doc.leftMargin + doc.rightMargin
                img_h = img_w * (h / w)
                canvas.drawImage(hdr_path, 0, A3[1] - img_h + 10, width=img_w, height=img_h)
            # Footer
            footer_height = 0
            if ftr_path and os.path.exists(ftr_path):
                img2 = PILImage.open(ftr_path)
                w2, h2 = img2.size
                img_w2 = doc.width + doc.leftMargin + doc.rightMargin
                img_h2 = img_w2 * (h2 / w2)
                canvas.drawImage(ftr_path, 0, 1, width=img_w2, height=img_h2)
                footer_height = img_h2
            # Page number
            canvas.setFont('Helvetica', 10)
            page_num = canvas.getPageNumber()
            canvas.drawRightString(doc.width + doc.leftMargin, footer_height + 10, str(page_num))
            canvas.restoreState()

        # Company & Contact Details
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
        if company_details.get("address"):
            detail_lines.append(f"<b>Address:</b> <font color='black'>{company_details['address']}</font><br/>")
        detail_lines.append(f"<b>Cell Phone:</b> <font color='black'>{company_details['contact_phone']}</font><br/>")
        if company_details.get("contact_email"):
            detail_lines.append(f"<b>Contact Email:</b> <font color='black'>{company_details['contact_email']}</font><br/>")
        detail_lines.append("</font>")
        detail_lines.append("</para>")
        details = "".join(detail_lines)
        elems.append(Spacer(1, 40))
        elems.append(Paragraph(details, aligned_style))

        # Terms & Conditions
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

        # Payment Info
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

        # Table styles
        desc_style = ParagraphStyle(name='Description', fontSize=12, leading=16, alignment=TA_CENTER)
        styleN = ParagraphStyle(name='Normal', fontSize=12, leading=12, alignment=TA_CENTER)

        def is_empty(val):
            return pd.isna(val) or val is None or str(val).lower() in ['nan', 'n/a', '']

        def safe_str(val):
            return "" if is_empty(val) else str(val)

        def safe_float(val):
            return "" if is_empty(val) else f"{float(val):.2f}"

        product_table_data = [["Ser.", "Product", "Image", "SKU", "Details", "QTY", "Unit Price", "Line Total"]]
        temp_files = []

        for idx, r in enumerate(items, start=1):
            img_element = "No Image"
            if r.get("Image"):
                download_url = convert_google_drive_url_for_storage(r["Image"])
                temp_img_path = download_image_for_pdf(download_url, max_size=(300, 300))
                if temp_img_path:
                    try:
                        img = RLImage(temp_img_path)
                        img._restrictSize(190, 180)
                        img.hAlign = 'CENTER'
                        img.vAlign = 'MIDDLE'
                        img_element = img
                        temp_files.append(temp_img_path)
                    except Exception as e:
                        print(f"Error creating image element: {e}")

            details_text = (
                f"<b>Description:</b> {safe_str(r.get('Description'))}<br/>"
                f"<b>Color:</b> {safe_str(r.get('Color'))}<br/>"
                f"<b>Warranty:</b> {safe_str(r.get('Warranty'))}"
            )
            details_para = Paragraph(details_text, desc_style)
            product_table_data.append([
                str(idx),
                Paragraph(safe_str(r.get('Item')), styleN),
                img_element,
                Paragraph(safe_str(r.get('SKU')).upper(), styleN),
                details_para,
                Paragraph(safe_str(r.get('Quantity')), styleN),
                Paragraph(safe_float(r.get('Price per item')), styleN),
                Paragraph(safe_float(r.get('Total price')), styleN),
            ])

        product_table = Table(product_table_data, colWidths=[30, 100, 150, 60, 200, 30, 60, 60])
        product_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 12),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEADING', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elems.append(product_table)

        # === Summary Section with Conditional Discount ===
        subtotal = sum(float(r.get('Price per item', 0)) * float(r.get('Quantity', 1)) for r in items)
        total_after_discount = total
        discount_amount = subtotal - total_after_discount
        vat = total_after_discount * 0.15
        grand_total = total_after_discount + vat

        summary_data = [
            ["Total", f"{subtotal:.2f} EGP"]
        ]
        if discount_amount > 0:
            summary_data.append(["Special Discount", f"- {discount_amount:.2f} EGP"])
        summary_data.append(["Total After Discount", f"{total_after_discount:.2f} EGP"])
        summary_data.append(["VAT (15%)", f"{vat:.2f} EGP"])
        summary_data.append(["Grand Total", f"{grand_total:.2f} EGP"])

        col_widths = [615, 150] if discount_amount > 0 else [540, 150]
        summary_table = Table(summary_data, colWidths=col_widths)
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

        try:
            doc.build(elems, onFirstPage=header_footer, onLaterPages=header_footer)
        finally:
            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except:
                    pass
        return pdf_path

    return build_pdf(items, total, company_details, hdr_path, ftr_path)

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

            # Regenerate PDF Button
            with col1:
                quote_hash = quote.get("hash", f"unknown_{idx}")
                if st.button(f"📄 Regenerate PDF", key=f"regen_{idx}_{quote_hash}"):
                    with st.spinner("Rebuilding PDF..."):
                        try:
                            temp_details = quote.get("company_details") or st.session_state.company_details
                            pdf_file = generate_pdf_from_data(quote["items"], quote["total"], temp_details)
                            if pdf_file:
                                with open(pdf_file, "rb") as f:
                                    st.download_button(
                                        "⬇ Download PDF",
                                        f,
                                        file_name=quote["pdf_filename"],
                                        mime="application/pdf",
                                        key=f"dl_hist_{idx}"
                                    )
                        except Exception as e:
                            st.error(f"Failed to generate PDF: {e}")

            # Delete Button
            with col2:
                if st.button("🗑️ Delete", key=f"del_{idx}_{quote['hash']}"):
                    if st.session_state.get(f"confirm_delete_{idx}"):
                        # Confirm and delete from both session and Google Sheet
                        try:
                            # 🔍 Get the history sheet
                            history_sheet = get_history_sheet()
                            if history_sheet is None:
                                st.error("❌ Cannot connect to Google Sheet.")
                            else:
                                # Load all data from sheet
                                df = get_as_dataframe(history_sheet)
                                df.dropna(how='all', inplace=True)

                                # Find row where Quotation Hash matches
                                matching_rows = df[df["Quotation Hash"] == quote["hash"]]

                                if len(matching_rows) == 0:
                                    st.warning("⚠️ This quotation was not found in the Google Sheet.")
                                else:
                                    # Get the first matching row index (Google Sheets is 1-indexed, +2 for header and 0-index)
                                    row_index = matching_rows.index[0] + 2  # +2 because: 0-index + 1 header row
                                    history_sheet.delete_rows(int(row_index))
                                    st.success("🗑️ Quotation deleted from Google Sheet!")

                            # ✅ Remove from session state
                            # Calculate the actual index in the original history list
                            original_idx = len(st.session_state.history) - 1 - idx
                            st.session_state.history.pop(original_idx)
                            st.success("🗑️ Quotation deleted from session!")

                            # 🔄 Optional: Refresh history to stay in sync
                            time.sleep(1)
                            st.rerun()

                        except Exception as e:
                            st.error(f"❌ Failed to delete from Google Sheet: {e}")
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
                        "valid_till": (datetime.now() + pd.Timedelta(days=10)).strftime("%A, %B %d, %Y"),
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
