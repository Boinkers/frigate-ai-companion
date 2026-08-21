import streamlit as st
import sqlite3
import pandas as pd
import os
import requests
import uuid

DB_File = "companion.db"

st.set_page_config(page_title="Frigate Companion", page_icon="🛡️", layout="wide", initial_sidebar_state="expanded")

# ==============================================================================
# 🎨 CUSTOM CSS
# ==============================================================================
st.markdown("""
    <style>
    .block-container {
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
    }
    [data-testid="stSidebar"] {
        background-color: #f8f9fa;
        border-right: 1px solid #e9ecef;
    }
    [data-testid="stExpander"] {
        border: 1px solid #e9ecef;
        border-radius: 8px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }
    .stButton>button {
        border-radius: 6px;
    }
    </style>
""", unsafe_allow_html=True)


# ==============================================================================
# 🛠️ HELPER FUNCTIONS & DIALOGS
# ==============================================================================
def get_db_connection():
    conn = sqlite3.connect(DB_File)
    conn.row_factory = sqlite3.Row
    return conn

def delete_person_completely(person_id):
    conn = get_db_connection()
    events = conn.execute("SELECT snapshot_path FROM events WHERE person_id = ?", (person_id,)).fetchall()
    for ev in events:
        if os.path.exists(ev['snapshot_path']):
            try:
                os.remove(ev['snapshot_path'])
            except:
                pass
    conn.execute("DELETE FROM events WHERE person_id = ?", (person_id,))
    conn.execute("DELETE FROM faces WHERE person_id = ?", (person_id,))
    conn.commit()
    conn.close()

def delete_single_event(event_id, snapshot_path):
    if os.path.exists(snapshot_path):
        try:
            os.remove(snapshot_path)
        except:
            pass
    conn = get_db_connection()
    conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
    conn.commit()
    conn.close()

def merge_identities(target_id, source_id):
    if target_id == source_id:
        return False, "Cannot merge an identity into itself."
        
    conn = get_db_connection()
    # 1. Transfer all events to the target
    conn.execute("UPDATE events SET person_id = ? WHERE person_id = ?", (target_id, source_id))
    
    if source_id != "Unknown":
        src_row = conn.execute("SELECT pass_count, embedding FROM faces WHERE person_id = ?", (source_id,)).fetchone()
        target_row = conn.execute("SELECT embedding FROM faces WHERE person_id = ?", (target_id,)).fetchone()
        
        if src_row:
            # 2. Transfer pass counts
            conn.execute("UPDATE faces SET pass_count = pass_count + ? WHERE person_id = ?", (src_row['pass_count'], target_id))
            
            # 3. THE FIX: If the target profile is missing face data, steal it from the source!
            if target_row and target_row['embedding'] is None and src_row['embedding'] is not None:
                conn.execute("UPDATE faces SET embedding = ? WHERE person_id = ?", (src_row['embedding'], target_id))
                
        # 4. Delete the source profile
        conn.execute("DELETE FROM faces WHERE person_id = ?", (source_id,))
        
    conn.commit()
    conn.close()
    return True, "Merged successfully!"

def get_frigate_cameras(api_url):
    """Fetches the list of configured cameras directly from Frigate's API."""
    try:
        if not api_url.startswith("http"):
            return []
        resp = requests.get(f"{api_url}/api/config", timeout=3)
        if resp.status_code == 200:
            return list(resp.json().get("cameras", {}).keys())
    except Exception:
        pass
    return []

@st.dialog("👤 Assign Identity to Event")
def assign_identity_dialog(event_id):
    st.write("Who is in this snapshot?")
    
    conn = get_db_connection()
    faces_df = pd.read_sql("SELECT person_id, name FROM faces", conn)
    conn.close()
    
    options = ["-- Select Existing Profile --"]
    name_to_id = {}
    for _, r in faces_df.iterrows():
        d_name = r['name'] if r['name'] else r['person_id']
        options.append(d_name)
        name_to_id[d_name] = r['person_id']
        
    selected_existing = st.selectbox("Assign to known person:", options)
    st.markdown("**OR**")
    new_name = st.text_input("Create a completely new profile (type name):")
    
    if st.button("💾 Save Assignment", type="primary", use_container_width=True):
        conn = get_db_connection()
        if new_name.strip():
            new_p_id = f"Person_{uuid.uuid4().hex[:5].upper()}"
            conn.execute("INSERT INTO faces (person_id, name, pass_count) VALUES (?, ?, 1)", (new_p_id, new_name.strip()))
            conn.execute("UPDATE events SET person_id = ? WHERE event_id = ?", (new_p_id, event_id))
        elif selected_existing != "-- Select Existing Profile --":
            target_p_id = name_to_id[selected_existing]
            conn.execute("UPDATE events SET person_id = ? WHERE event_id = ?", (target_p_id, event_id))
            conn.execute("UPDATE faces SET pass_count = pass_count + 1 WHERE person_id = ?", (target_p_id,))
        
        conn.commit()
        conn.close()
        st.rerun()

# ==============================================================================
# 🧭 SIDEBAR NAVIGATION
# ==============================================================================
with st.sidebar:
    st.title("🛡️ Companion")
    st.markdown("---")
    nav_selection = st.radio(
        "Navigation", 
        ["👥 Faces & People", "🔀 Merge Tool", "⚙️ Configuration"],
        label_visibility="collapsed"
    )

conn = get_db_connection()
faces_df = pd.read_sql("SELECT person_id, name, pass_count FROM faces ORDER BY pass_count DESC", conn)
events_df = pd.read_sql("SELECT * FROM events ORDER BY timestamp DESC", conn)
conn.close()

# ==============================================================================
# 🟢 VIEW 1: FACES & PEOPLE
# ==============================================================================
if nav_selection == "👥 Faces & People":
    
    col_filter, col_search, col_actions = st.columns([3, 2, 1])
    
    filter_options = ["All"]
    for _, row in faces_df.iterrows():
        d_name = row['name'] if row['name'] else row['person_id']
        filter_options.append(f"{d_name} ({row['pass_count']})")
    unknown_count = len(events_df[events_df['person_id'] == 'Unknown'])
    if unknown_count > 0:
        filter_options.append(f"Unknown ({unknown_count})")
        
    with col_filter:
        selected_filter = st.radio("Filter View:", filter_options, horizontal=True, label_visibility="collapsed")
        
    with col_search:
        search_query = st.text_input("Search", placeholder="🔍 Search descriptions (e.g. 'cap', 'blue')...", label_visibility="collapsed")
        
    with col_actions:
        if st.button("🗑️ Clear Unknowns", use_container_width=True):
            delete_person_completely("Unknown")
            st.rerun()
            
    st.markdown("---")
    
    filtered_events = events_df
    
    if selected_filter != "All":
        if selected_filter.startswith("Unknown"):
            filtered_events = events_df[events_df['person_id'] == 'Unknown']
        else:
            target_name = selected_filter.rsplit(" (", 1)[0]
            match = faces_df[(faces_df['name'] == target_name) | (faces_df['person_id'] == target_name)]
            if not match.empty:
                p_id = match.iloc[0]['person_id']
                filtered_events = events_df[events_df['person_id'] == p_id]
                
                with st.container(border=True):
                    st.markdown("**Profile Management**")
                    with st.form(key=f"rename_form_{p_id}"):
                        c1, c2, c3 = st.columns([2, 1, 1])
                        with c1:
                            new_n = st.text_input("Display Name", value=target_name, label_visibility="collapsed")
                        with c2:
                            if st.form_submit_button("💾 Save Name", use_container_width=True):
                                conn = get_db_connection()
                                conn.execute("UPDATE faces SET name = ? WHERE person_id = ?", (new_n, p_id))
                                conn.commit()
                                conn.close()
                                st.success("Saved!")
                                st.rerun()
                        with c3:
                            if st.form_submit_button("🗑️ Delete Profile Entirely", use_container_width=True):
                                delete_person_completely(p_id)
                                st.rerun()
                st.write("") 

    if search_query:
        filtered_events = filtered_events[filtered_events['clothing_description'].fillna("").str.contains(search_query, case=False)]

    if filtered_events.empty:
        if search_query:
            st.info(f"No captures found matching '{search_query}'.")
        else:
            st.info("No captures found for this selection.")
    else:
        cols = st.columns(5) 
        for i, (_, ev_row) in enumerate(filtered_events.iterrows()):
            with cols[i % 5]:
                with st.container(border=True):
                    if os.path.exists(ev_row['snapshot_path']):
                        st.image(ev_row['snapshot_path'], use_container_width=True)
                    else:
                        st.warning("Missing")
                        
                    desc = ev_row['clothing_description']
                    display_desc = desc if desc else "No description"
                    
                    st.markdown(f"<div style='font-size: 0.85rem; line-height: 1.2; margin-bottom: 6px; font-weight: 500;'>{display_desc}</div>", unsafe_allow_html=True)
                    st.caption(f"{ev_row['timestamp'].split()[1]}") 
                    
                    btn_c1, btn_c2 = st.columns(2)
                    with btn_c1:
                        if st.button("👤", key=f"a_{ev_row['event_id']}", help="Assign Identity", use_container_width=True):
                            assign_identity_dialog(ev_row['event_id'])
                    with btn_c2:
                        if st.button("🗑️", key=f"d_{ev_row['event_id']}", help="Delete Snapshot", use_container_width=True):
                            delete_single_event(ev_row['event_id'], ev_row['snapshot_path'])
                            st.rerun()

# ==============================================================================
# 🟡 VIEW 2: MERGE IDENTITIES
# ==============================================================================
elif nav_selection == "🔀 Merge Tool":
    st.header("🔀 Identity Merge Tool")
    st.markdown("Select a **Source** (duplicate or unknown) to merge into a **Target** (the correct profile).")
    
    with st.container(border=True):
        face_options = {}
        for _, row in faces_df.iterrows():
            d_name = row['name'] if row['name'] else row['person_id']
            face_options[f"{d_name} ({row['person_id']})"] = row['person_id']
        
        source_options = face_options.copy()
        source_options["👻 Unknown Events (Unknown)"] = "Unknown"

        if not face_options:
            st.info("Not enough known identities to perform a merge.")
        else:
            col_target, col_source, col_btn = st.columns([2, 2, 1])
            with col_target:
                target_selection = st.selectbox("Keep this Identity (Target):", list(face_options.keys()), key="merge_target")
            with col_source:
                source_selection = st.selectbox("Merge & Delete this Identity (Source):", list(source_options.keys()), key="merge_source")
            with col_btn:
                st.write("")
                st.write("")
                if st.button("🚀 Merge", type="primary", use_container_width=True):
                    t_id = face_options[target_selection]
                    s_id = source_options[source_selection]
                    success, msg = merge_identities(t_id, s_id)
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)


# ==============================================================================
# ⚙️ VIEW 3: SYSTEM CONFIGURATION
# ==============================================================================
elif nav_selection == "⚙️ Configuration":
    st.header("⚙️ System Configuration")
    
    conn = get_db_connection()
    configs_df = pd.read_sql("SELECT key, value FROM configs", conn)
    conn.close()
    config_dict = {row['key']: row['value'] for _, row in configs_df.iterrows()}
    
    with st.container(border=True):
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Local Connections")
            frigate_url = st.text_input("Frigate API URL", value=config_dict.get("frigate_api_url", ""))
            mqtt_host = st.text_input("MQTT Broker Host", value=config_dict.get("mqtt_host", ""))
            mqtt_user = st.text_input("MQTT Username", value=config_dict.get("mqtt_user", ""))
            mqtt_pass = st.text_input("MQTT Password", type="password", value=config_dict.get("mqtt_password", ""))
            
            # --- NEW: MONITORED CAMERAS LOGIC ---
            st.markdown("---")
            st.subheader("Camera Filters")
            available_cameras = get_frigate_cameras(frigate_url)
            saved_cams_str = config_dict.get("monitored_cameras", "")
            saved_cams_list = [c.strip() for c in saved_cams_str.split(",")] if saved_cams_str else []
            
            # Combine available cameras with saved ones just in case Frigate is offline
            all_options = list(set(available_cameras + saved_cams_list))
            
            selected_cameras = st.multiselect(
                "Monitored Cameras (Leave empty to monitor ALL cameras):",
                options=all_options,
                default=[c for c in saved_cams_list if c in all_options],
                help="Only events from these cameras will be processed by the AI."
            )
            
        with col2:
            st.subheader("AI Engine Settings")
            engine_idx = 0 if config_dict.get("attribute_engine") == "external_vlm" else 1
            engine = st.selectbox("Attribute Engine", ["external_vlm", "local_fast"], index=engine_idx)
            
            vlm_url = st.text_input("Mac llama.cpp API URL (/chat/completions)", value=config_dict.get("vlm_api_url", ""))
            
            current_model = config_dict.get("vlm_model_name", "")
            if "available_models" not in st.session_state:
                st.session_state.available_models = [current_model] if current_model else ["Qwen3-VL"]

            if st.button("🔄 Fetch Available Models"):
                try:
                    models_endpoint = vlm_url.replace("/chat/completions", "/models")
                    response = requests.get(models_endpoint, timeout=5)
                    if response.status_code == 200:
                        data = response.json()
                        models_list = [model["id"] for model in data.get("data", [])]
                        if models_list:
                            st.session_state.available_models = models_list
                            st.success(f"Fetched {len(models_list)} model(s)!")
                    else:
                        st.error(f"Failed to fetch models. Status: {response.status_code}")
                except Exception as e:
                    st.error(f"Connection failed: {e}")

            if current_model not in st.session_state.available_models and current_model != "":
                st.session_state.available_models.append(current_model)
                
            vlm_model = st.selectbox(
                "Select VLM Model", 
                st.session_state.available_models,
                index=st.session_state.available_models.index(current_model) if current_model in st.session_state.available_models else 0
            )

        st.markdown("---")
        if st.button("💾 Save Configuration", type="primary"):
            cams_to_save = ",".join(selected_cameras)
            
            conn = get_db_connection()
            conn.execute("UPDATE configs SET value = ? WHERE key = 'frigate_api_url'", (frigate_url,))
            conn.execute("UPDATE configs SET value = ? WHERE key = 'mqtt_host'", (mqtt_host,))
            conn.execute("UPDATE configs SET value = ? WHERE key = 'mqtt_user'", (mqtt_user,))
            conn.execute("UPDATE configs SET value = ? WHERE key = 'mqtt_password'", (mqtt_pass,))
            conn.execute("UPDATE configs SET value = ? WHERE key = 'attribute_engine'", (engine,))
            conn.execute("UPDATE configs SET value = ? WHERE key = 'vlm_api_url'", (vlm_url,))
            conn.execute("UPDATE configs SET value = ? WHERE key = 'vlm_model_name'", (vlm_model,))
            # Insert or ignore logic handles if it didn't exist in older databases
            conn.execute("INSERT OR REPLACE INTO configs (key, value) VALUES ('monitored_cameras', ?)", (cams_to_save,))
            conn.commit()
            conn.close()
            st.success("Settings updated! (Restart backend app.py if changing MQTT/Frigate connection)")
            st.rerun()