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

def process_and_upload(image_path):
    print("\n[SERVER] Processing new image...")
    client = vision.ImageAnnotatorClient()

    with open(image_path, "rb") as image_file:
        content = image_file.read()

    image = vision.Image(content=content)
    response = client.text_detection(image=image)
    
    texts = response.text_annotations
    if not texts:
        print("[SERVER] No text found.")
        return

    word_annotations = texts[1:]
    
    valid_numbers = []
    labels_y = {} # Store the Y-coordinates of found labels
    
    # STEP 1: Scan all words for numbers and labels
    for word in word_annotations:
        text = word.description.lower()
        y_coord = word.bounding_poly.vertices[0].y
        
        # Strip out punctuation to catch things like "SYS." or "SYS/DIA"
        clean_text = re.sub(r'[^a-z]', '', text)
        
        # Check for labels
        if 'sys' in clean_text:
            labels_y['sys'] = y_coord
        elif 'dia' in clean_text:
            labels_y['dia'] = y_coord
        elif 'pul' in clean_text or 'bpm' in clean_text or 'bp' in clean_text:
            labels_y['bpm'] = y_coord
            
        # Check for valid BP/Pulse numbers (2-3 digits)
        if word.description.isdigit() and 2 <= len(word.description) <= 3:
            valid_numbers.append({'value': int(word.description), 'y': y_coord})

    systolic = None
    diastolic = None
    heart_rate = None

    # STEP 2: Match numbers to labels
    if len(valid_numbers) >= 2:
        
        # Helper function to find and remove the number closest to a label's Y-coordinate
        def pop_closest_number(target_y):
            if not valid_numbers: return None
            # Find the number with the smallest difference in Y-coordinate
            closest_idx = min(range(len(valid_numbers)), key=lambda i: abs(valid_numbers[i]['y'] - target_y))
            return valid_numbers.pop(closest_idx)['value']

        # If we found labels, assign the numbers right next to them
        if 'sys' in labels_y:
            systolic = pop_closest_number(labels_y['sys'])
        if 'dia' in labels_y:
            diastolic = pop_closest_number(labels_y['dia'])
        if 'bpm' in labels_y:
            heart_rate = pop_closest_number(labels_y['bpm'])
            
        # STEP 3: Fallback mechanism
        # If any labels were missing due to glare, sort whatever numbers are left top-to-bottom
        valid_numbers = sorted(valid_numbers, key=lambda k: k['y'])
        
        if systolic is None and valid_numbers:
            systolic = valid_numbers.pop(0)['value']
        if diastolic is None and valid_numbers:
            diastolic = valid_numbers.pop(0)['value']
        if heart_rate is None and valid_numbers:
            heart_rate = valid_numbers.pop(0)['value']
            
        print(f"[SERVER] Parsed -> SYS: {systolic}, DIA: {diastolic}, BPM: {heart_rate}")
        
        # Ensure we at least got SYS and DIA before uploading
        if systolic and diastolic:
            health_data = {
                'patient_id': 'patient_001',
                'systolic': systolic,
                'diastolic': diastolic,
                'heart_rate': heart_rate,
                'timestamp': datetime.datetime.now()
            }
            
            db.collection('readings').add(health_data)
            print("[SERVER] SUCCESS: Uploaded to Firebase!")
        else:
            print("[SERVER] Error: Failed to assign Systolic and Diastolic values.")
    else:
        print(f"[SERVER] Error: Not enough valid numbers found. Found: {[n['value'] for n in valid_numbers]}")

# --- 3. THE LISTENER ---
@app.route('/upload', methods=['POST'])
def handle_upload():
    print("\n[SERVER] Receiving connection from app...")
    
    file_data = request.get_data()
    
    if not file_data:
        return "No image data received", 400
        
    temp_image_path = "temp_patient_reading.jpg"
    
    with open(temp_image_path, "wb") as f:
        f.write(file_data)
        
    process_and_upload(temp_image_path)
    
    return "Upload and Processing Complete!", 200

if __name__ == '__main__':
    print("=========================================")
    print(" MediSnap Server is RUNNING and LISTENING ")
    print("=========================================")
    app.run(host='0.0.0.0', port=5000)
