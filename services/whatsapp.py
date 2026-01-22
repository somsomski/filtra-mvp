import os
import requests
from typing import List, Dict

# Environment Variables
META_TOKEN = os.environ.get("META_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")

def sanitize_argentina_number(phone_number: str) -> str:
    """
    Sanitizes Argentina Text/Sandbox numbers to the LOCAL format required by this specific Meta account.
    Target format: 54 + AreaCode (11) + 15 + Number.
    Standard International (54911...) FAILS.
    
    Logic:
    1. Remove '9' after '54'.
    2. Insert '15' after '11' if missing.
    """
    # 0. Clean basic junk
    phone = phone_number.strip().replace("+", "").replace(" ", "")

    # 1. Check if Argentina (54)
    if phone.startswith("54"):
        # 2. REMOVE '9' if present (International Mobile Token)
        # e.g. 54911... -> 5411...
        if len(phone) > 2 and phone[2] == '9':
            phone = "54" + phone[3:]
        
        # Now phone is likely 5411... (assuming BA)
        
        # 3. ADD '15' (Local Mobile Prefix) for Buenos Aires (11)
        # We need the result to be 54 11 15 xxxxxxxx
        if phone.startswith("5411"):
            # Check if '15' is already there
            # 5411 says length 4. 
            # If next chars are 15, we leave it.
            if not phone.startswith("541115"):
                # Insert 15
                phone = "541115" + phone[4:]
                
    return phone

def send_whatsapp_message(to_number: str, text: str):
    """Sends a standard text message."""
    if not META_TOKEN or not PHONE_NUMBER_ID:
        print("Error: Meta credentials not set.")
        return

    normalized_to = sanitize_argentina_number(to_number)
    
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "text",
        "text": {"body": text}
    }
    
    try:
        requests.post(url, json=payload, headers=headers).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Text to {normalized_to}: {e}")
        if e.response is not None:
             print(f"Meta details: {e.response.text}")

def send_interactive_list(to_number: str, body_text: str, button_text: str, section_title: str, rows: List[Dict]):
    """
    Sends an Interactive List Message.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return

    normalized_to = sanitize_argentina_number(to_number)
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_text,
                "sections": [
                    {
                        "title": section_title,
                        "rows": rows[:10]
                    }
                ]
            }
        }
    }
    
    try:
        requests.post(url, json=payload, headers=headers).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending List to {normalized_to}: {e}")

def send_interactive_buttons(to_number: str, body_text: str, buttons: List[Dict]):
    """
    Sends an Interactive Button Message.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return

    normalized_to = sanitize_argentina_number(to_number)
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    
    formatted_buttons = []
    for btn in buttons[:3]:
        formatted_buttons.append({
            "type": "reply",
            "reply": {
                "id": btn['id'],
                "title": btn['title']
            }
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": formatted_buttons
            }
        }
    }
    
    try:
        requests.post(url, json=payload, headers=headers).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Buttons to {normalized_to}: {e}")
