import os
import re
import json
import datetime
from flask import Flask, request
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

# --- 1. SETUP CLOUD SERVICES ---
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'google_key.json'
cred = credentials.Certificate('firebase_key.json')
firebase_admin.initialize_app(cred)
db = firestore.client()

# Configure Gemini
genai.configure(api_key=os.environ['GEMINI_API_KEY'])

# --- 2. SETUP FLASK SERVER ---
app = Flask(__name__)


def extract_with_gemini(image_path):
    """
    Use Gemini Vision to read SYS, DIA, and PULSE directly from the image.
    Far more reliable than OCR + regex for LCD 7-segment displays.
    Returns (systolic, diastolic, heart_rate) or (None, None, None).
    """
    model = genai.GenerativeModel('gemini-2.0-flash')

    with open(image_path, 'rb') as f:
        image_data = f.read()

    image_part = {
        "mime_type": "image/jpeg",
        "data": image_data
    }

    prompt = """This is a blood pressure monitor. Look only at the LCD display numbers.
Extract the SYS (systolic), DIA (diastolic), and PULSE/heart rate values.

Return ONLY a JSON object like this, nothing else:
{"systolic": 120, "diastolic": 80, "heart_rate": 72}

If a value is not visible, use null. Do not include any explanation."""

    try:
        response = model.generate_content([prompt, image_part])
        text = response.text.strip()

        # Strip markdown code fences if Gemini wraps in ```json ... ```
        text = re.sub(r'```json|```', '', text).strip()

        data = json.loads(text)
        systolic = data.get('systolic')
        diastolic = data.get('diastolic')
        heart_rate = data.get('heart_rate')

        print(f"[SERVER] Gemini extracted -> SYS: {systolic}, DIA: {diastolic}, BPM: {heart_rate}")
        return systolic, diastolic, heart_rate

    except json.JSONDecodeError as e:
        print(f"[SERVER] Gemini JSON parse error: {e}")
        print(f"[SERVER] Raw Gemini response: {response.text}")
        return None, None, None
    except Exception as e:
        print(f"[SERVER] Gemini error: {e}")
        return None, None, None


def process_and_upload(image_path, patient_id):
    print(f"\n[SERVER] Processing new image for patient: {patient_id}")

    systolic, diastolic, heart_rate = extract_with_gemini(image_path)

    if systolic and diastolic:
        health_data = {
            'patient_id': patient_id,
            'systolic': systolic,
            'diastolic': diastolic,
            'heart_rate': heart_rate,
            'timestamp': datetime.datetime.now(),
        }
        db.collection('readings').add(health_data)
        print("[SERVER] SUCCESS: Uploaded to Firebase!")
    else:
        print("[SERVER] Error: Gemini could not extract SYS/DIA values.")


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
