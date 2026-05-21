import os
import re
from flask import Flask, request, jsonify
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

def process_and_upload(image_content):
    print("\n[SERVER] Processing new image...")
    try:
        client = vision.ImageAnnotatorClient()
        
        # Send bytes directly to Vision API - no temporary file needed!
        image = vision.Image(content=image_content)
        response = client.text_detection(image=image)
        
        texts = response.text_annotations
        if not texts:
            print("[SERVER] Error: No text found.")
            return {"status": "error", "message": "No text detected in the image."}

        raw_text = texts[0].description
        print(f"\n[SERVER] Raw OCR Text:\n{raw_text}")
        
        # Helper function now supports decimal numbers for blood sugar
        def find_value_near_label(text, label_pattern, allow_decimal=False):
            number_pattern = r'(\d{1,3}(?:\.\d{1,2})?)' if allow_decimal else r'(\d{2,3})'
            
            # 1. Look for Label followed by Number
            match = re.search(fr'(?i){label_pattern}[^\d]{{0,10}}{number_pattern}', text)
            if match:
                val_str = match.group(1)
                return float(val_str) if '.' in val_str else int(val_str)
            
            # 2. Look for Number followed by Label
            match_reverse = re.search(fr'{number_pattern}[^\d]{{0,10}}(?i){label_pattern}', text)
            if match_reverse:
                val_str = match_reverse.group(1)
                return float(val_str) if '.' in val_str else int(val_str)
            
            return None

        # Look for BP and Pulse (Integers only)
        systolic = find_value_near_label(raw_text, r'(?:sys|systolic)')
        diastolic = find_value_near_label(raw_text, r'(?:dia|diastolic)')
        heart_rate = find_value_near_label(raw_text, r'(?:pulse|pul|bpm|pr)')
        
        # Look for Blood Sugar (allow decimals)
        blood_sugar = find_value_near_label(raw_text, r'(?:glu|glucose|bg|bs|mg/dl|mmol/l)', allow_decimal=True)

        # FALLBACK: If the photo is blurry and labels aren't found
        if not systolic and not diastolic and not blood_sugar:
            print("[SERVER] Labels missing or unreadable. Using fallback logic...")
            all_numbers = re.findall(r'\b\d{1,3}(?:\.\d{1,2})?\b', raw_text)
            
            bp_candidates = [int(float(n)) for n in all_numbers if '.' not in n and 30 <= int(float(n)) <= 250]
            
            if len(bp_candidates) >= 2:
                systolic = bp_candidates[0]
                diastolic = bp_candidates[1]
                heart_rate = bp_candidates[2] if len(bp_candidates) >= 3 else None
            elif len(all_numbers) == 1:
                val = float(all_numbers[0])
                if (3.0 <= val <= 33.3) or (40 <= val <= 600):
                    blood_sugar = val

        # Final check: We need EITHER a valid BP reading OR a valid Blood Sugar reading
        if (systolic and diastolic) or blood_sugar:
            print(f"[SERVER] Parsed -> SYS: {systolic}, DIA: {diastolic}, BPM: {heart_rate}, SUGAR: {blood_sugar}")
            
            health_data = {
                'patient_id': 'patient_001',
                'timestamp': firestore.SERVER_TIMESTAMP 
            }
            
            # Dynamically add only the fields we actually found
            if systolic: health_data['systolic'] = systolic
            if diastolic: health_data['diastolic'] = diastolic
            if heart_rate: health_data['heart_rate'] = heart_rate
            if blood_sugar: health_data['blood_sugar'] = blood_sugar
            
            db.collection('readings').add(health_data)
            print("[SERVER] SUCCESS: Uploaded to Firebase!")
            return {"status": "success", "message": "Data saved successfully."}
        else:
            print("[SERVER] Error: Could not determine health readings.")
            return {"status": "error", "message": "Could not extract vitals."}
            
    except Exception as e:
        print(f"[SERVER] Exception occurred: {str(e)}")
        return {"status": "error", "message": "Server error during processing."}

# --- 3. THE LISTENER ---
@app.route('/upload', methods=['POST'])
def handle_upload():
    print("\n[SERVER] Receiving connection from app...")
    
    file_data = request.get_data()
    
    if not file_data:
        return jsonify({"status": "error", "message": "No image data received"}), 400
        
    # Process the binary data directly
    result = process_and_upload(file_data)
    
    # Let the mobile app know if it actually worked
    if result["status"] == "success":
        return jsonify(result), 200
    else:
        return jsonify(result), 400

if __name__ == '__main__':
    print("=========================================")
    print(" MediSnap Server is RUNNING and LISTENING ")
    print("=========================================")
    app.run(host='0.0.0.0', port=5000)
