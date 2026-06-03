if not reading.is_valid:
        log.error("Could not extract a valid SYS/DIA pair.")
        return {
            'success':   False,
            'reason':    'Incomplete reading',
            'warnings':  reading.warnings,
            'reading':   None,
        }

    log.info(
        "Parsed → SYS: %s  DIA: %s  BPM: %s  (confidence: %.0f%%)",
        reading.systolic, reading.diastolic, reading.heart_rate,
        reading.confidence * 100,
    )

    data = reading.to_dict(patient_id)
    db.collection('readings').add(data)
    log.info("Uploaded to Firestore ✓")

    return {
        'success':    True,
        'patient_id': patient_id,
        'reading': {
            'systolic':   reading.systolic,
            'diastolic':  reading.diastolic,
            'heart_rate': reading.heart_rate,
            'confidence': reading.confidence,
        },
        'warnings': reading.warnings,
    }


# --- 3. THE LISTENER ---

@app.route('/upload', methods=['POST'])
def handle_upload():
    log.info("Incoming upload request")

    # ── Patient ID validation ─────────────────────────────────────────────────
    raw_id     = request.args.get('patient_id', '')
    patient_id = sanitize_patient_id(raw_id)
    valid, reason = validate_patient_id(patient_id)

    if not valid:
        log.warning("Rejected upload — %s", reason)
        return jsonify({'error': reason}), 400

    # ── Image data ────────────────────────────────────────────────────────────
    file_data = request.get_data()
    if not file_data:
        return jsonify({'error': 'No image data received'}), 400

    # Safe temp filename (patient_id is already sanitised)
    temp_path = f"/tmp/medisnap_{patient_id}_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}.jpg"

    try:
        with open(temp_path, 'wb') as f:
            f.write(file_data)

        result = process_and_upload(temp_path, patient_id)

    except RuntimeError as e:
        log.error("Processing error: %s", e)
        return jsonify({'error': str(e)}), 500

    finally:
        # Always clean up temp file
        if os.path.exists(temp_path):
            os.remove(temp_path)
            log.debug("Temp file removed: %s", temp_path)

    status_code = 200 if result['success'] else 422
    return jsonify(result), status_code


@app.route('/health', methods=['GET'])
def health_check():
    """Simple liveness probe."""
    return jsonify({'status': 'ok', 'timestamp': datetime.datetime.utcnow().isoformat()}), 200


if name == 'main':
    print("=" * 50)
    print("  MediSnap Server  —  RUNNING ON :5000")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000, debug=False)
