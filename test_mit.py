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

# This is your exact same OCR code, just packed into a function!
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

    raw_text = texts[0].description
    numbers = re.findall(r'\d+', raw_text)
    
    if len(numbers) >= 2:
        systolic = int(numbers[0])
        diastolic = int(numbers[1])
        heart_rate = int(numbers[2]) if len(numbers) >= 3 else None
        
        print(f"[SERVER] Parsed -> SYS: {systolic}, DIA: {diastolic}, BPM: {heart_rate}")
        
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
        print("[SERVER] Error: Not enough numbers found.")

# --- 3. THE LISTENER ---
# This waits for your phone to send the file to /upload
@app.route('/upload', methods=['POST'])
def handle_upload():
    print("\n[SERVER] Receiving connection from app...")
    
    # App Inventor sends files exactly like this
    file_data = request.get_data()
    
    if not file_data:
        return "No image data received", 400
        
    temp_image_path = "temp_patient_reading.jpg"
    
    # Save the incoming photo to your laptop temporarily
    with open(temp_image_path, "wb") as f:
        f.write(file_data)
        
    # Send it to Google Cloud and Firebase
    process_and_upload(temp_image_path)
    
    return "Upload and Processing Complete!", 200

if __name__ == '__main__':
    print("=========================================")
    print(" MediSnap Server is RUNNING and LISTENING ")
    print("=========================================")
    # 0.0.0.0 allows devices on your Wi-Fi to connect
    app.run(host='0.0.0.0', port=5000)
