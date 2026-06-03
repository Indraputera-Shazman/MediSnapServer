import os
import re
import datetime
from flask import Flask, request
from google.cloud import vision
from PIL import Image, ImageEnhance, ImageFilter
import io
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


def preprocess_image(image_path):
    """
    Preprocess the image to improve OCR accuracy on LCD displays.
    LCD segments can be misread as CJK characters (e.g. "100" → "昌") when
    the image has low contrast or color noise. This converts to high-contrast
    grayscale which forces Vision to treat segments as Latin digits.

    Returns: image bytes ready for the Vision API.
    """
    img = Image.open(image_path).convert('L')  # grayscale

    # Boost contrast so LCD digits are crisp black on white background
    img = ImageEnhance.Contrast(img).enhance(2.5)
    img = ImageEnhance.Sharpness(img).enhance(2.0)

    # Upscale if image is small — Vision performs better on larger text
    w, h = img.size
    if w < 800:
        img = img.resize((w * 2, h * 2), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95)
    return buf.getvalue()


def parse_full_text(full_text):
    """
    PRIMARY METHOD: Run regex on the complete OCR text string.
    Handles common BP monitor formats like:
      - "120/80"  or  "120 / 80"
      - "SYS 120  DIA 80  BPM 72"
      - "Sys:120  Dia:80  Pul:72"
      - "SYS mmHg DIA mmHg PULSE /min 115 78 63"  (label section then number section)
    Returns (systolic, diastolic, heart_rate) or (None, None, None).
    """
    systolic = diastolic = heart_rate = None
    text = full_text.upper()

    # Pattern 1: label immediately followed by its number (e.g. "SYS 120", "SYS:120")
    sys_match = re.search(r'SYS[:\s]*(\d{2,3})', text)
    dia_match = re.search(r'DIA[:\s]*(\d{2,3})', text)
    bpm_match = re.search(r'(?:PUL|BPM|HR|PULSE)[:/\s]*(\d{2,3})', text)

    if sys_match and dia_match:
        systolic = int(sys_match.group(1))
        diastolic = int(dia_match.group(1))
        if bpm_match:
            heart_rate = int(bpm_match.group(1))
        return systolic, diastolic, heart_rate

    # Pattern 2: fraction format "120/80" (optionally followed by heart rate)
    # Exclude date-like patterns (single/double digit on both sides, e.g. "9/24")
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


def parse_sequential(full_text):
    """
    SECONDARY METHOD: For devices where labels appear in one block and numbers
    appear later (e.g. "SYS mmHg DIA mmHg PULSE /min 115 78 □ 63 9/24").

    Strategy:
      1. Find the text positions of SYS, DIA, PULSE labels (in that order).
      2. Collect all 2-3 digit numbers that appear AFTER the last label.
      3. Pair them positionally: 1st number → SYS, 2nd → DIA, 3rd → PULSE.
      4. Validate against physiological ranges before accepting.
    """
    RANGES = {'sys': (60, 250), 'dia': (40, 150), 'bpm': (30, 220)}
    text = full_text.upper()

    # Find where each label appears
    label_positions = {}
    for label, pattern in [('sys', r'SYS'), ('dia', r'DIA'), ('bpm', r'PULSE|PUL\b|BPM\b')]:
        m = re.search(pattern, text)
        if m:
            label_positions[label] = m.start()

    if 'sys' not in label_positions or 'dia' not in label_positions:
        return None, None, None

    # All numbers must appear after the last detected label
    last_label_pos = max(label_positions.values())
    tail = text[last_label_pos:]

    # Find all standalone 2-3 digit numbers in tail (skip dates like "9/24")
    numbers = []
    for m in re.finditer(r'(?<!\d)(\d{2,3})(?!\d)', tail):
        # Skip if it's part of a date pattern like 9/24 or 12/31
        context = tail[max(0, m.start()-2):m.end()+2]
        if re.search(r'\d/\d|\d{1,2}/\d{2}', context):
            continue
        numbers.append(int(m.group(1)))

    if len(numbers) < 2:
        return None, None, None

    # Match positionally; validate ranges
    label_order = [k for k in ['sys', 'dia', 'bpm'] if k in label_positions]
    results = {}
    for i, label in enumerate(label_order):
        if i < len(numbers) and RANGES[label][0] <= numbers[i] <= RANGES[label][1]:
            results[label] = numbers[i]

    if 'sys' in results and 'dia' in results:
        print("[SERVER] Parsed via sequential positional method.")
        return results.get('sys'), results.get('dia'), results.get('bpm')

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

    # Preprocess to high-contrast grayscale — prevents LCD digits being
    # misread as CJK characters (e.g. "100" → "昌")
    content = preprocess_image(image_path)
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
        print("[SERVER] Full-text regex failed; trying sequential positional method...")
        systolic, diastolic, heart_rate = parse_sequential(full_text)

    if not (systolic and diastolic):
        print("[SERVER] Sequential method failed; trying coordinate matching...")
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
