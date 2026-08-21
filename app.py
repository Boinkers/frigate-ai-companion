import json
import os
import requests
import cv2
import numpy as np
import sqlite3
import base64
import uuid
import paho.mqtt.client as mqtt
from datetime import datetime
from insightface.app import FaceAnalysis

DB_File = "companion.db"
SNAPSHOT_DIR = "snapshots"
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

def get_db():
    return sqlite3.connect(DB_File, timeout=15.0)

# --- INITIALIZE DB & DEFAULT CONFIGS ---
def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faces (
            person_id TEXT PRIMARY KEY,
            name TEXT,
            embedding BLOB,
            pass_count INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            person_id TEXT,
            timestamp TEXT,
            snapshot_path TEXT,
            clothing_description TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS configs (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    defaults = {
            "frigate_api_url": "http://YOUR_FRIGATE_IP:5000",
            "mqtt_host": "YOUR_MQTT_IP",
            "mqtt_user": "admin",
            "mqtt_password": "password",
            "attribute_engine": "external_vlm",
            "vlm_api_url": "http://YOUR_MAC_IP:8080/v1/chat/completions",
            "vlm_model_name": "Qwen3-VL",
            "monitored_cameras": "" 
        }
    
    for k, v in defaults.items():
        cursor.execute("INSERT OR IGNORE INTO configs (key, value) VALUES (?, ?)", (k, v))
    
    conn.commit()
    conn.close()

init_db()

def get_config(key):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM configs WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

print("📁 SQLite Database & Configs initialized!")

print("🧠 Loading Face Recognition AI...")
face_app = FaceAnalysis(name='buffalo_l')
face_app.prepare(ctx_id=-1, det_size=(640, 640)) 
print("✅ AI Loaded!")

def analyze_clothing_with_vlm(image_path):
    if not os.path.exists(image_path):
        return "Image not found"
        
    vlm_url = get_config("vlm_api_url")
    model_name = get_config("vlm_model_name")
    
    try:
        with open(image_path, "rb") as img_file:
            img_base64 = base64.b64encode(img_file.read()).decode('utf-8')
            
        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe this person's complete clothing in precise detail. Mention specific items (e.g., black hoodie, black jeans, black boots, white sneakers, blue jacket) and their colors. Keep it under 15 words."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_base64}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 50
        }
        
        response = requests.post(vlm_url, json=payload, timeout=45)
        if response.status_code == 200:
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()
        else:
            return f"llama.cpp Error (Status {response.status_code})"
    except Exception as e:
        return f"Connection Failed: {e}"

def on_message(client, userdata, msg):
    payload = json.loads(msg.payload.decode('utf-8'))
    
    if payload.get('type') == 'end':
        after = payload.get('after', {})
        
        if after.get('label') == 'person' and after.get('has_snapshot'):
            event_id = after.get('id')
            camera_name = after.get('camera', '')
            
            monitored_cams_str = get_config("monitored_cameras")
            if monitored_cams_str:
                allowed_cams = [c.strip() for c in monitored_cams_str.split(",")]
                if camera_name not in allowed_cams:
                    print(f"[{event_id}] 🚫 Ignored event from unmonitored camera: {camera_name}")
                    return 
            
            current_frigate_url = get_config("frigate_api_url")
            snapshot_url = f"{current_frigate_url}/api/events/{event_id}/snapshot.jpg?crop=1"
            
            try:
                response = requests.get(snapshot_url, timeout=10)
                if response.status_code == 200:
                    image_bytes = np.frombuffer(response.content, np.uint8)
                    img = cv2.imdecode(image_bytes, cv2.IMREAD_COLOR)
                    
                    if img is not None:
                        snapshot_path = os.path.join(SNAPSHOT_DIR, f"{event_id}.jpg")
                        cv2.imwrite(snapshot_path, img)
                        
                        faces = face_app.get(img)
                        matched_id = None
                        
                        conn = get_db()
                        cursor = conn.cursor()
                        
                        if len(faces) > 0:
                            target_face = faces[0]
                            embedding = target_face.normed_embedding
                            
                            # THE FIX: Only pull faces that actually have embeddings!
                            cursor.execute("SELECT person_id, embedding FROM faces WHERE embedding IS NOT NULL")
                            rows = cursor.fetchall()
                            
                            if rows:
                                known_ids = [r[0] for r in rows]
                                embeddings_list = [np.frombuffer(r[1], dtype=np.float32) for r in rows]
                                similarities = np.dot(np.array(embeddings_list), embedding)
                                best_idx = np.argmax(similarities)
                                if similarities[best_idx] > 0.5:
                                    matched_id = known_ids[best_idx]
                                    
                            if not matched_id:
                                short_uuid = uuid.uuid4().hex[:5].upper()
                                matched_id = f"Person_{short_uuid}"
                                cursor.execute("INSERT INTO faces (person_id, name, embedding, pass_count) VALUES (?, ?, ?, ?)", 
                                               (matched_id, matched_id, embedding.tobytes(), 1))
                                conn.commit()
                                print(f"[{event_id}] ✨ NEW IDENTITY SAVED: {matched_id}")
                            else:
                                cursor.execute("UPDATE faces SET pass_count = pass_count + 1 WHERE person_id = ?", (matched_id,))
                                conn.commit()
                                print(f"[{event_id}] 🤝 RECOGNIZED: {matched_id}")
                        else:
                            matched_id = "Unknown"

                        conn.close()

                        clothing_desc = "VLM Disabled"
                        current_engine = get_config("attribute_engine")
                        if current_engine == "external_vlm":
                            print(f"[{event_id}] 🤖 Querying Mac llama.cpp server...")
                            clothing_desc = analyze_clothing_with_vlm(snapshot_path)
                            print(f"   🧥 VLM Result: {clothing_desc}")

                        conn = get_db()
                        cursor = conn.cursor()
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        cursor.execute('''
                            INSERT OR REPLACE INTO events (event_id, person_id, timestamp, snapshot_path, clothing_description)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (event_id, matched_id, timestamp, snapshot_path, clothing_desc))
                        conn.commit()
                        conn.close()
                        print("-" * 40)
            except Exception as e:
                print(f"Error processing event: {e}")

def start_mqtt():
    mqtt_host = get_config("mqtt_host")
    mqtt_user = get_config("mqtt_user")
    mqtt_password = get_config("mqtt_password")
    
    client = mqtt.Client()
    client.username_pw_set(mqtt_user, mqtt_password)
    
    def on_connect(client, userdata, flags, rc):
        print("✅ Successfully connected to MQTT broker!")
        client.subscribe("frigate/events")
        print("🎧 Listening for events on: frigate/events...")

    client.on_connect = on_connect
    client.on_message = on_message
    
    client.connect(mqtt_host, 1883, 60)
    client.loop_forever()

if __name__ == "__main__":
    start_mqtt()