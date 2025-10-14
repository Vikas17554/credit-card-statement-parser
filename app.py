from flask import Flask, render_template, request, jsonify
import os
import pdfplumber
import re
import json
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Create uploads folder if not exists
if not os.path.exists('uploads'):
    os.makedirs('uploads')

# ===== YOUR EXISTING PARSING CODE =====
ISSUER_KEYWORDS = {
    "HDFC": ["HDFC Bank", "HDFC Bank Credit Card", "HDFC"],
    "SBI": ["SBI Card", "State Bank of India", "SBI"],
    "ICICI": ["ICICI Bank", "ICICI Bank Credit Card", "ICICI"],
    "AXIS": ["Axis Bank", "Axis Bank Credit Card", "AXIS"],
    "AMEX": ["American Express", "AMERICAN EXPRESS", "Amex"]
}

GENERIC_REGEX = {
    "card_last4": [
        re.compile(r"Card\s*(?:No\.?|Number|No\.?|#)?\s*[:\-]?\s*(?:\*+|X+|\d{4}\s*\*+|\d{8})\s*(\d{4})", re.I),
        re.compile(r"(?:\*{4}[-\s]*|\*{8}[-\s]*|\*{12}[-\s]*)(\d{4})"),
        re.compile(r"ending\s*(?:in|with)?\s*[:\-]?\s*(\d{4})", re.I),
        re.compile(r"Last\s*4\s*Digits?\s*[:\-]?\s*(\d{4})", re.I),
        re.compile(r"Card\s*[:\-]\s*(?:\*+\s*)?(\d{4})", re.I),
        re.compile(r"Account\s*Number\s*[:\-]?\s*(?:\*+|X+)?\s*(\d{4})", re.I),
        re.compile(r"(\d{4})\s*(?:\)|\]|\.)?\s*(?:ending|card|$)", re.I),
        re.compile(r"XXXX\s*XXXX\s*XXXX\s*(\d{4})", re.I),
        re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?(\d{4})\b"),
    ],
    "statement_date": [
        re.compile(r"Statement\s*Date[:\s]*([A-Za-z0-9,\s\/-]+)"),
        re.compile(r"Billing\s*Cycle[:\s]*([A-Za-z0-9,\s\/-]+)"),
        re.compile(r"Statement\s*Period[:\s]*([A-Za-z0-9,\s\/-]+)"),
    ],
    "due_date": [
        re.compile(r"Payment\s*Due\s*Date[:\s]*([A-Za-z0-9,\s\/-]+)"),
        re.compile(r"Due\s*Date[:\s]*([A-Za-z0-9,\s\/-]+)"),
    ],
    "total_balance": [
        re.compile(r"(?:Total\s+Amount\s+Due|Total\s+Due|Outstanding\s+Balance|Amount\s+Due)[:\s]*₹?\s*([0-9,]+\.\d{2})"),
        re.compile(r"(?:Total|Balance)[:\s]*₹\s*([0-9,]+\.\d{2})"),
        re.compile(r"₹\s*([0-9,]+\.\d{2})\s*(?:total|due|outstanding)?", re.I)
    ]
}

ISSUER_PATTERNS = {
    "HDFC": {
        "card_last4": [
            re.compile(r"Card\s*Number\s*[:\s]*XXXX\s*XXXX\s*XXXX\s*(\d{4})", re.I),
            re.compile(r"Card\s*[:\-]\s*(?:\*+\s*)?(\d{4})", re.I),
            re.compile(r"ending\s*in\s*(\d{4})", re.I)
        ],
        "due_date": [re.compile(r"Payment\s*Due\s*Date[:\s]*([A-Za-z0-9,\s\/-]+)", re.I)]
    },
    "SBI": {
        "card_last4": [
            re.compile(r"Card\s*Number\s*[:\-]\s*(?:\*+\s*)?(\d{4})", re.I),
            re.compile(r"SBI\s*Card\s*[:\-]\s*(?:\*+\s*)?(\d{4})", re.I)
        ],
        "total_balance": [re.compile(r"Total\s*Due\s*[:\s]*₹?\s*([0-9,]+\.\d{2})", re.I)]
    },
    "ICICI": {
        "card_last4": [
            re.compile(r"Card\s*Ending\s*[:\-]?\s*(\d{4})", re.I),
            re.compile(r"ICICI\s*Bank\s*Card\s*[:\-]\s*(?:\*+\s*)?(\d{4})", re.I)
        ],
    },
    "AXIS": {
        "card_last4": [
            re.compile(r"Card\s*No\.\s*\*+(\d{4})", re.I),
            re.compile(r"Axis\s*Bank\s*Card\s*[:\-]\s*(?:\*+\s*)?(\d{4})", re.I)
        ],
    },
    "AMEX": {
        "card_last4": [
            re.compile(r"Card\s*Number[:\s]*\*\*\*\*\s*(\d{4})", re.I),
            re.compile(r"American\s*Express\s*Card\s*[:\-]\s*(?:\*+\s*)?(\d{4})", re.I)
        ],
        "total_balance": [re.compile(r"Current\s*Balance[:\s]*\$?₹?\s*([0-9,]+\.\d{2})", re.I)]
    }
}

TRANSACTION_SAMPLE_COUNT = 5

def load_pdf_text(path: str) -> str:
    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)

def detect_issuer(text: str) -> str:
    t_upper = text.upper()
    for issuer, keys in ISSUER_KEYWORDS.items():
        for k in keys:
            if k.upper() in t_upper:
                return issuer
    return "UNKNOWN"

def apply_patterns(text: str, patterns):
    for p in patterns:
        matches = p.findall(text)
        if matches:
            if isinstance(matches[0], tuple):
                return matches[0][0].strip() if matches[0] else ""
            else:
                return matches[0].strip()
    return ""

def extract_transactions(text: str, max_lines: int = TRANSACTION_SAMPLE_COUNT):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    headers = ["date description", "transaction date", "txn date"]
    start_index = None
    for i, ln in enumerate(lines):
        if any(h in ln.lower() for h in headers):
            start_index = i + 1
            break

    sample = []
    if start_index:
        for ln in lines[start_index:]:
            if len(sample) >= max_lines:
                break
            if re.search(r"[₹$]\s*\d|\d+\.\d{2}", ln):
                sample.append(ln)
        return sample
    return lines[:max_lines]

def extract_fields_for_issuer(text: str, issuer: str):
    result = {
        "issuer": issuer,
        "card_last4": "",
        "statement_date_or_cycle": "",
        "payment_due_date": "",
        "total_balance": "",
        "transaction_samples": []
    }

    issuer_patterns = ISSUER_PATTERNS.get(issuer, {})

    result["card_last4"] = apply_patterns(text, issuer_patterns.get("card_last4", []) + GENERIC_REGEX["card_last4"])
    result["statement_date_or_cycle"] = apply_patterns(text, issuer_patterns.get("statement_date", []) + GENERIC_REGEX["statement_date"])
    result["payment_due_date"] = apply_patterns(text, issuer_patterns.get("due_date", []) + GENERIC_REGEX["due_date"])
    result["total_balance"] = apply_patterns(text, issuer_patterns.get("total_balance", []) + GENERIC_REGEX["total_balance"])
    result["transaction_samples"] = extract_transactions(text, TRANSACTION_SAMPLE_COUNT)

    return result
# ===== END OF PARSING CODE =====

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'})
    
    if file and file.filename.lower().endswith('.pdf'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        try:
            text = load_pdf_text(filepath)
            issuer = detect_issuer(text)
            data = extract_fields_for_issuer(text, issuer)
            data['filename'] = filename
            
            os.remove(filepath)
            return jsonify(data)
            
        except Exception as e:
            if os.path.exists(filepath):
                os.remove(filepath)
            return jsonify({'error': f'Processing failed: {str(e)}'})
    
    return jsonify({'error': 'Invalid file type. Please upload PDF.'})

if __name__ == '__main__':
    app.run(debug=True)