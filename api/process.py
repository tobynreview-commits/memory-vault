import os
from flask import Flask, request, jsonify
from google import genai
from google.genai import types
import requests
import datetime
import json

app = Flask(__name__)

@app.route('/api/process', methods=['POST'])
def handle_stateless_pipeline():
    # Grab the user's keys sent from the screen
    user_gemini_key = request.headers.get("X-User-Gemini-Key")
    google_token = request.headers.get("X-Google-Access-Token")
    
    payload = request.get_json() or {}
    voice_input = payload.get("voice_input", "").strip()

    # REMOVED user_gemini_key from this strict gate check
    if not google_token or not voice_input:
        return jsonify({"error": "Missing Google authorization token or voice input payload"}), 400

    # FALLBACK ENGINE GATE: Determine which API Key to assign
    # Clean up the key if it's sent as a blank string or "null" from frontend
    user_key_clean = (user_gemini_key or "").strip()
    if not user_key_clean or user_key_clean.lower() == "null":
        active_api_key = os.environ.get("MASTER_GEMINI_KEY")
    else:
        active_api_key = user_key_clean

    if not active_api_key:
        return jsonify({"error": "Server configuration fault: No active AI Engine key setup found."}), 400

    try:
        # Start the AI engine using the dynamically resolved key
        ai_client = genai.Client(api_key=active_api_key)
        # Tell the AI exactly what today's date is so it can understand time
        current_moment = datetime.datetime.now()
        current_date_str = current_moment.strftime("%Y-%m-%d")
        current_day_name = current_moment.strftime("%A")

        system_instruction = f"""
        You are the cognitive layer of a personal memory vault. Today's date is exactly {current_date_str} ({current_day_name}).
        Determine the user's intent:
        1. If they are stating a fact or note to store, set intent to "STORE".
        2. If they are asking a question about things they previously told you, set intent to "RETRIEVE".
        
        For "STORE": Target date is today ({current_date_str}).
        For "RETRIEVE": Translate relative terms (like 'yesterday') into exact YYYY-MM-DD formats using the current date.
        """

        # Force the AI to answer in a strict format so it never breaks
        response_schema = {
            "type": "OBJECT",
            "properties": {
                "intent": {"type": "STRING", "enum": ["STORE", "RETRIEVE"]},
                "sanitized_content": {"type": "STRING"},
                "target_date": {"type": "STRING"}
            },
            "required": ["intent", "sanitized_content", "target_date"]
        }

        ai_analysis = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=voice_input,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=response_schema
            )
        )
        
        parsed_intent = json.loads(ai_analysis.text)
        intent = parsed_intent.get("intent")
        target_date = parsed_intent.get("target_date", current_date_str)
        cleaned_text = parsed_intent.get("sanitized_content", voice_input)

        # Talk to the user's personal Google Drive using their login token
        auth_header = {"Authorization": f"Bearer {google_token}"}
        search_url = "https://www.googleapis.com/drive/v3/files?q=name='My Memory Vault' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        search_res = requests.get(search_url, headers=auth_header).json()
        files = search_res.get('files', [])
        
        # If the user doesn't have the spreadsheet yet, build it automatically!
        if not files:
            create_url = "https://www.googleapis.com/drive/v3/files"
            meta = {"name": "My Memory Vault", "mimeType": "application/vnd.google-apps.spreadsheet"}
            sheet_creation = requests.post(create_url, headers=auth_header, json=meta).json()
            spreadsheet_id = sheet_creation['id']
            init_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/Sheet1!A1:B1:append?valueInputOption=USER_ENTERED"
            requests.post(init_url, headers=auth_header, json={"values": [["Timestamp", "Log_Entry"]]})
        else:
            spreadsheet_id = files[0]['id']

        # If the user is saving data, write a new row to the sheet
        if intent == "STORE":
            append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/Sheet1!A:B:append?valueInputOption=USER_ENTERED"
            row_data = [[current_date_str, cleaned_text]]
            requests.post(append_url, headers=auth_header, json={"values": row_data})
            return jsonify({"intent": "STORE", "output_text": f"Saved to your timeline: {cleaned_text}"}), 200
        
        # If the user is asking a question, read the sheet and let the AI summarize it
        else:
            read_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/Sheet1!A:B"
            sheet_data = requests.get(read_url, headers=auth_header).json()
            rows = sheet_data.get('values', [])
            matched_entries = [row[1] for row in rows if len(row) > 1 and row[0] == target_date]
            
            if not matched_entries:
                return jsonify({"intent": "RETRIEVE", "output_text": f"No records found for the date {target_date}"}), 200
                
            context_blob = "\n".join([f"- {entry}" for entry in matched_entries])
            summary_prompt = f"The user asked: '{voice_input}'. Here are their saved logs for the date {target_date}:\n{context_blob}\nAnswer their question directly and conversationally using only these logs."
            summary_analysis = ai_client.models.generate_content(model='gemini-2.5-flash', contents=summary_prompt)
            return jsonify({"intent": "RETRIEVE", "output_text": summary_analysis.text}), 200

    except Exception as error:
        return jsonify({"error": str(error)}), 500
