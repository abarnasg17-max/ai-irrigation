# ============================================================
# AI SMART IRRIGATION – FULLY FIXED VERSION
# Fixes: Rerun bug, duplicate DB inserts, SMS spam,
#        voice loop, efficiency formula, error handling
# ============================================================

import streamlit as st
import pandas as pd
import requests
import joblib
import sqlite3
import os
import plotly.express as px
from datetime import datetime
from gtts import gTTS
import time
import uuid
import gdown

st.set_page_config(page_title="AI Smart Irrigation", layout="centered")
# ------------------------------------------------------------
# DOWNLOAD MODEL FROM GOOGLE DRIVE (ONLY FIRST TIME)
# ------------------------------------------------------------
MODEL_PATH = "water_model_realistic.pkl"

if not os.path.exists(MODEL_PATH):
    with st.spinner("📥 Downloading AI model... please wait"):
        file_id = "1ltGYrPEuiNL_5m5BuzDs0mp_AYJbVWmy"
        url = f"https://drive.google.com/uc?id={file_id}"
        gdown.download(url, MODEL_PATH, quiet=False)


# ------------------------------------------------------------
# SESSION STATE INIT
# All state variables initialized here to prevent rerun issues
# ------------------------------------------------------------
defaults = {
    "voice_played": False,
    "last_sms_key": "",
    "last_input_key": "",
    "result_ready": False,
    "water": 0,
    "method": "",
    "efficiency": 0,
    "temp": 0,
    "hum": 0,
    "rain": 0,
    "icon": "01d",
    "already_saved": False,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ------------------------------------------------------------
# LOAD FILES – with error handling
# ------------------------------------------------------------
@st.cache_resource
def load_model():
    try:
        model = joblib.load(MODEL_PATH)
        encoder = joblib.load("crop_soil_encoder.pkl")
        return model, encoder
    except FileNotFoundError as e:
        st.error(f"❌ Model file not found: {e}. Make sure .pkl files are in the same folder.")
        st.stop()

@st.cache_data
def load_locations():
    try:
        return pd.read_csv("TamilNadu_38_District_Agri_Locations.csv")
    except FileNotFoundError:
        st.error("❌ Location CSV file not found. Make sure TamilNadu_38_District_Agri_Locations.csv is in the same folder.")
        st.stop()

model, encoder = load_model()
locations = load_locations()

# ------------------------------------------------------------
# DATABASE – using cache_resource so connection is reused
# safely without recreating on every rerun
# ------------------------------------------------------------
@st.cache_resource
def get_db():
    conn = sqlite3.connect("irrigation_data.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS irrigation_records(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_name TEXT,
            crop TEXT,
            soil_type TEXT,
            land_area REAL,
            temperature REAL,
            humidity REAL,
            rainfall REAL,
            water_required REAL,
            efficiency_score REAL,
            irrigation_type TEXT,
            date_time TEXT
        )
    """)
    conn.commit()
    return conn, cursor

conn, cursor = get_db()

# ------------------------------------------------------------
# WEATHER
# ------------------------------------------------------------
def get_weather(city):
    try:
        api_key = st.secrets["WEATHER_API_KEY"]
        url = f"http://api.openweathermap.org/data/2.5/weather?q={city},IN&appid={api_key}&units=metric"
        data = requests.get(url, timeout=5).json()
        temp = data["main"]["temp"]
        humidity = data["main"]["humidity"]
        rain = data.get("rain", {}).get("1h", 0)
        icon = data["weather"][0]["icon"]
        return temp, humidity, rain, icon
    except Exception as e:
        st.warning(f"⚠️ Weather fetch failed ({e}). Using default values.")
        return 30, 60, 0, "01d"

# ------------------------------------------------------------
# SMS
# ------------------------------------------------------------
def send_sms(phone, message):
    try:
        url = "https://www.fast2sms.com/dev/bulkV2"
        headers = {"authorization": st.secrets["FAST2SMS_API_KEY"]}
        payload = {
            "route": "q",
            "message": message,
            "language": "english",
            "flash": 0,
            "numbers": phone
        }
        response = requests.post(url, data=payload, headers=headers, timeout=5)
        st.write("📡 API Response:", response.text)
        return response.status_code == 200
    except Exception as e:
        st.error(f"SMS Error: {e}")
        return False

# ------------------------------------------------------------
# UI – FARMER DETAILS
# ------------------------------------------------------------
st.title("🌾 AI Smart Irrigation System")
# 🌍 LANGUAGE TOGGLE
lang = st.radio("🌍 Language / மொழி", ["English", "Tamil"], horizontal=True)
if lang == "Tamil":
    TEXT = {
        "farmer": "விவசாயி பெயர்",
        "phone": "தொலைபேசி எண்",
        "land": "நில அளவு (ஏக்கர்)",
        "location": "இடம்",
        "crop": "பயிர்",
        "soil": "மண் வகை",
        "analyze": "🤖 கணக்கிடு",
        "result": "📊 முடிவு",
        "water": "தேவையான நீர்",
        "method": "பாசன முறை",
        "efficiency": "திறன்",
        "sms": "📩 SMS அனுப்பு"
    }
else:
    TEXT = {
        "farmer": "Farmer Name",
        "phone": "Phone Number",
        "land": "Land Area (Acres)",
        "location": "Location",
        "crop": "Crop",
        "soil": "Soil Type",
        "analyze": "🤖 Analyze & Get Irrigation Plan",
        "result": "📊 Results",
        "water": "Water Required",
        "method": "Irrigation Method",
        "efficiency": "Efficiency Score",
        "sms": "📩 Send SMS"
    }

with st.expander("👨‍🌾 Farmer Details"):
    farmer_name = st.text_input(TEXT["farmer"])
    phone_input = st.text_input(TEXT["phone"])
    land_area = st.number_input(TEXT["land"], min_value=0.1)

with st.expander("📍 Location"):
    district_list = sorted(locations["district"].unique())
    district = st.selectbox("District", district_list)

    town_list = locations[locations["district"] == district]["main_town"].unique()
    town = st.selectbox("Town", town_list)

    village_list = locations[
        (locations["district"] == district) &
        (locations["main_town"] == town)
    ]["village"].unique()
    village = st.selectbox("Village", village_list)

with st.expander("🌱 Crop Details"):
    crop = st.selectbox(TEXT["crop"], ["Rice", "Wheat", "Maize", "Sugarcane", "Cotton"])
    soil_type = st.selectbox(TEXT["soil"], ["Sandy", "Loamy", "Clay"])

# ------------------------------------------------------------
# INPUT CHANGE DETECTION
# Whenever user changes inputs, reset result + voice state
# This is the KEY fix for the rerun problem
# ------------------------------------------------------------
current_input_key = f"{farmer_name}-{crop}-{soil_type}-{district}-{town}-{land_area}"

if current_input_key != st.session_state.last_input_key:
    st.session_state.last_input_key = current_input_key
    st.session_state.result_ready = False
    st.session_state.voice_played = False
    st.session_state.already_saved = False

# ------------------------------------------------------------
# ANALYZE BUTTON – AI runs only when user clicks
# This fully solves the rerun/duplicate problem
# ------------------------------------------------------------
ready = farmer_name.strip() != "" and land_area > 0

if not ready:
    st.info("ℹ️ Enter Farmer Name & Land Area to start")

if ready:
    if st.button("🤖 Analyze & Get Irrigation Plan"):
        with st.spinner("🤖 AI analyzing..."):

            temp, hum, rain, icon = get_weather(town)
            land_hectare = land_area * 0.404686

            cat = pd.DataFrame([{"Crop": crop, "Soil_Type": soil_type}])
            enc = encoder.transform(cat)
            enc_df = pd.DataFrame(enc, columns=encoder.get_feature_names_out())

            num = pd.DataFrame([{
                "Soil_Moisture": 30,        # Default – ideally from sensor
                "Temperature_C": temp,
                "Humidity": hum,
                "Rainfall_mm": rain,
                "Sunlight_Hours": 8,        # Default assumption
                "Wind_Speed_kmh": 10,       # Default assumption
                "Field_Area_hectare": land_hectare
            }])

            final = pd.concat([num, enc_df], axis=1)
            water = model.predict(final)[0]

            if rain > 5:
                water = 0
                method = "No Irrigation"
            elif water < 6000:
                method = "Drip"
            elif water < 12000:
                method = "Sprinkler"
            else:
                method = "Flood"

            # FIXED: Efficiency — less water = higher efficiency
            efficiency = 100 if water == 0 else max(0, 100 - (water / 15000) * 100)

            # Store in session state so rerun doesn't recalculate
            st.session_state.water = water
            st.session_state.method = method
            st.session_state.efficiency = efficiency
            st.session_state.temp = temp
            st.session_state.hum = hum
            st.session_state.rain = rain
            st.session_state.icon = icon
            st.session_state.result_ready = True
            st.session_state.voice_played = False
            st.session_state.already_saved = False

# ------------------------------------------------------------
# RESULTS – shown from session state (survives reruns)
# ------------------------------------------------------------
if st.session_state.result_ready:

    water     = st.session_state.water
    method    = st.session_state.method
    efficiency= st.session_state.efficiency
    temp      = st.session_state.temp
    hum       = st.session_state.hum
    rain      = st.session_state.rain
    icon      = st.session_state.icon

    st.subheader("📊 Results")

    icon_url = f"http://openweathermap.org/img/wn/{icon}@2x.png"
    st.image(icon_url, width=100, caption="Current Weather")

    col1, col2, col3 = st.columns(3)
    col1.metric("Temperature 🌡", f"{temp}°C")
    col2.metric("Humidity 💧", f"{hum}%")
    col3.metric("Rainfall 🌧", f"{rain} mm")

    st.subheader(TEXT["result"])

    st.success(f"💧 {TEXT['water']}: {int(water)} Litres")
    st.info(f"🚿 {TEXT['method']}: {method}")
    st.metric(TEXT["efficiency"], f"{efficiency:.1f}%")

    # --------------------------------------------------------
    # TAMIL VOICE – plays only once per new result
    # --------------------------------------------------------
    if not st.session_state.voice_played:
        try:
            if lang == "Tamil":
                voice_text = f"உங்கள் பயிருக்கு தேவையான நீர் அளவு {int(water)} லிட்டர் ஆகும்"
                voice_lang = "ta"
            else:
                voice_text = f"Water required is {int(water)} litres"
                voice_lang = "en"

            tts = gTTS(voice_text, lang=voice_lang)
            # Unique filename to avoid race condition
            voice_file = f"voice_{uuid.uuid4().hex}.mp3"
            tts.save(voice_file)
            st.audio(voice_file, autoplay=True)
            time.sleep(1.5)
            # Safe delete (VERY IMPORTANT)
            try:
                if os.path.exists(voice_file):
                    os.remove(voice_file)
            except Exception:
                pass
            st.session_state.voice_played = True
        except Exception as e:
            st.warning(f"⚠️ Voice output failed: {e}")

    # --------------------------------------------------------
    # SMS – manual send button to avoid spam
    # --------------------------------------------------------
    phone = phone_input.strip().replace(" ", "").replace("+91", "")
    phone = ''.join(filter(str.isdigit, phone))
    if phone.startswith("0"):
        phone = phone[1:]

    if phone != "" and len(phone) == 10:
        sms_key = f"{phone}-{int(water)}-{method}"
        
        if st.session_state.last_sms_key == sms_key:
            st.success("✅ SMS already sent for this result.")
        else:
            if st.button("📩 Send SMS to Farmer"):
                message = f"AI IRRIGATION\nWater: {int(water)}L\nMethod: {method}\nEfficiency: {efficiency:.1f}%"
                if send_sms(phone, message):
                    st.success("✅ SMS Sent Successfully!")
                    st.session_state.last_sms_key = sms_key
                else:
                    st.error("❌ SMS Failed. Check API key.")
                    # 🔥 BACKUP PLAN
                    st.info("📩 Message for Farmer:")
                    st.code(message)
    else:
        if phone != "":
            st.warning(f"⚠️ Invalid phone: {len(phone)} digits entered. Need 10 digits.")

    # --------------------------------------------------------
    # SAVE TO DB – manual button, saves only once
    # --------------------------------------------------------
    if not st.session_state.already_saved:
        if st.button("💾 Save Record to Database"):
            cursor.execute("""
                INSERT INTO irrigation_records
                (farmer_name, crop, soil_type, land_area, temperature, humidity,
                rainfall, water_required, efficiency_score, irrigation_type, date_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                farmer_name, crop, soil_type, land_area,
                temp, hum, rain, water, efficiency, method,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            conn.commit()
            st.session_state.already_saved = True
            st.success("✅ Record saved!")
    else:
        st.success("✅ Record already saved for this result.")

# ------------------------------------------------------------
# DASHBOARD
# ------------------------------------------------------------
st.subheader("📊 Recent Records")

try:
    history = pd.read_sql_query(
        "SELECT farmer_name, crop, water_required, irrigation_type, efficiency_score, date_time FROM irrigation_records ORDER BY id DESC LIMIT 10",
        conn
    )

    if not history.empty:
        st.dataframe(history, use_container_width=True)

        chart = history["irrigation_type"].value_counts().reset_index()
        chart.columns = ["Method", "Count"]
        fig = px.pie(chart, names="Method", values="Count", title="Irrigation Methods Used")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No records yet. Analyze and save your first entry!")

except Exception as e:
    st.error(f"Dashboard error: {e}") 