import os
import re
import datetime
from flask import Flask, request
from google.cloud import vision
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
 
# --- 1. SETUP CLOUD SERVICES ---
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'google_key.json'
cred = credentials.Certificate('firebase_key.json')
firebase_admin.initialize_app(cred)
db = firestore.client()
 
# --- 2. SETUP FLASK SERVER ---
app = Flask(__name__)
 
 
def parse_full_text(full_text):
    """
    PRIMARY METHOD: Run regex on the complete OCR text string.
    Handles common BP monitor formats like:
      - "120/80"  or  "120 / 80"
      - "SYS 120  DIA 80  BPM 72"
      - "Sys:120  Dia:80  Pul:72"
    Returns (systolic, diastolic, heart_rate) or (None, None, None).
    """
    systolic = diastolic = heart_rate = None
    text = full_text.upper()
 
    # Pattern 1: classic "SYS ... DIA ... (PUL/BPM/HR) ..." with optional separators
    sys_match = re.search(r'SYS[:\s]*(\d{2,3})', text)
    dia_match = re.search(r'DIA[:\s]*(\d{2,3})', text)
    bpm_match = re.search(r'(?:PUL|BPM|HR|PULSE)[:\s]*(\d{2,3})', text)
 
    if sys_match and dia_match:
        systolic = int(sys_match.group(1))
        diastolic = int(dia_match.group(1))
        if bpm_match:
            heart_rate = int(bpm_match.group(1))
        return systolic, diastolic, heart_rate
 
    # Pattern 2: fraction format "120/80" (optionally followed by heart rate)
    fraction_match = re.search(r'(\d{2,3})\s*/\s*(\d{2,3})', text)
    if fraction_match:
        systolic = int(fraction_match.group(1))
        diastolic = int(fraction_match.group(2))
        if bpm_match:
            heart_rate = int(bpm_match.group(1))
        # Also try to pick up a standalone 2-digit number as heart rate
        if heart_rate is None:
            remaining = text[fraction_match.end():]
            hr_match = re.search(r'\b(\d{2,3})\b', remaining)
            if hr_match:
                heart_rate = int(hr_match.group(1))
        return systolic, diastolic, heart_rate
 
    return None, None, None
 
 
def parse_by_coordinates(word_annotations):
    """
    FALLBACK METHOD: Use word bounding-box Y coordinates to match
    labels to nearby numbers. More robust than before:
      - Searches within a Y-tolerance window instead of "closest" only.
      - Validates physiological ranges before assigning.
    """
    VALID_RANGES = {
        'sys': (60, 250),
        'dia': (40, 150),
        'bpm': (30, 220),
    }
    Y_TOLERANCE = 30  # pixels; tune if needed
 
    label_positions = {}   # label -> y_coord
    numbers = []           # list of {'value': int, 'y': int}
 
    LABEL_PATTERNS = {
        'sys': re.compile(r'sys', re.I),
        'dia': re.compile(r'dia', re.I),
        'bpm': re.compile(r'pul|bpm|pulse|hr\b', re.I),
    }
 
    for word in word_annotations:
        raw = word.description
        y = word.bounding_poly.vertices[0].y
 
        for label, pattern in LABEL_PATTERNS.items():
            if pattern.search(raw):
                label_positions[label] = y
 
        if raw.isdigit() and 2 <= len(raw) <= 3:
            numbers.append({'value': int(raw), 'y': y})
 
    results = {}
    used = set()
 
    for label, label_y in label_positions.items():
        lo, hi = VALID_RANGES[label]
        candidates = [
            n for i, n in enumerate(numbers)
            if i not in used
            and abs(n['y'] - label_y) <= Y_TOLERANCE
            and lo <= n['value'] <= hi
        ]
        if candidates:
            best = min(candidates, key=lambda n: abs(n['y'] - label_y))
            results[label] = best['value']
            used.add(numbers.index(best))
 
    # Last-resort: assign leftover numbers by vertical position & plausible range
    if 'sys' not in results or 'dia' not in results:
        unused = sorted(
            [n for i, n in enumerate(numbers) if i not in used],
            key=lambda n: n['y']
        )
        for n in unused:
            v = n['value']
            if 'sys' not in results and 60 <= v <= 250:
                results['sys'] = v
            elif 'dia' not in results and 40 <= v <= 150:
                results['dia'] = v
            elif 'bpm' not in results and 30 <= v <= 220:
                results['bpm'] = v
 
    return results.get('sys'), results.get('dia'), results.get('bpm')
 
 
def process_and_upload(image_path, patient_id):
    print(f"\n[SERVER] Processing new image for patient: {patient_id}")
 
    client = vision.ImageAnnotatorClient()
    with open(image_path, "rb") as image_file:
        content = image_file.read()
 
    image = vision.Image(content=content)
    response = client.text_detection(image=image)
    texts = response.text_annotations
 
    if not texts:
        print("[SERVER] No text found in image.")
        return
 
    # --- Try primary method first ---
    full_text = texts[0].description
    print(f"[SERVER] Full OCR text:\n{full_text}\n")
 
    systolic, diastolic, heart_rate = parse_full_text(full_text)
 
    if systolic and diastolic:
        print("[SERVER] Parsed via full-text regex.")
    else:
        print("[SERVER] Full-text regex failed; trying coordinate matching...")
        systolic, diastolic, heart_rate = parse_by_coordinates(texts[1:])
 
    print(f"[SERVER] Parsed -> SYS: {systolic}, DIA: {diastolic}, BPM: {heart_rate}")
 
    if systolic and diastolic:
        health_data = {
            'patient_id': patient_id,
            'systolic': systolic,
            'diastolic': diastolic,
            'heart_rate': heart_rate,
            'timestamp': datetime.datetime.now(),
            'raw_ocr': full_text,   # store raw text for debugging
        }
        db.collection('readings').add(health_data)
        print("[SERVER] SUCCESS: Uploaded to Firebase!")
    else:
        print("[SERVER] Error: Could not extract SYS/DIA values.")
        print(f"[SERVER] Raw OCR for debugging:\n{full_text}")
 
 
# --- 3. THE LISTENER ---
@app.route('/upload', methods=['POST'])
def handle_upload():
    print("\n[SERVER] Receiving connection from app...")
 
    patient_id = request.args.get('patient_id', 'unknown_patient')
    file_data = request.get_data()
 
    if not file_data:
        return "No image data received", 400
 
    temp_image_path = f"temp_{patient_id}_reading.jpg"
    with open(temp_image_path, "wb") as f:
        f.write(file_data)
 
    process_and_upload(temp_image_path, patient_id)
 
    # Clean up temp file
    try:
        os.remove(temp_image_path)
    except OSError:
        pass
 
    return "Upload and Processing Complete!", 200
 
 
if __name__ == '__main__':
    print("=========================================")
    print(" MediSnap Server is RUNNING and LISTENING ")
    print("=========================================")
    app.run(host='0.0.0.0', port=5000)
