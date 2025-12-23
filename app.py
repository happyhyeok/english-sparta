import streamlit as st
import requests  # 👈 [변경] 구글 라이브러리 대신 requests 사용
from openai import OpenAI
from audio_recorder_streamlit import audio_recorder
import streamlit.components.v1 as components
from supabase import create_client, Client
import json
import random
import time
import datetime
from datetime import date

# ==========================================
# 1. 환경 설정 및 초기화
# ==========================================
st.set_page_config(page_title="AI 중학 영어 스파르타", layout="centered")

# CSS
st.markdown("""
<style>
    .stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p {
        font-size: 1.1rem;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)

# Secrets 로드
try:
    openai_api_key = st.secrets["OPENAI_API_KEY"]
    supabase_url = st.secrets["SUPABASE_URL"]
    supabase_key = st.secrets["SUPABASE_KEY"]
    google_api_key = st.secrets["GOOGLE_API_KEY"]
except Exception as e:
    st.error(f"❌ 설정 오류: Secrets를 확인해주세요. ({str(e)})")
    st.stop()

# 클라이언트 초기화
client = OpenAI(api_key=openai_api_key)
supabase: Client = create_client(supabase_url, supabase_key)
# genai.configure... (삭제: 라이브러리 사용 안 함)

# 세션 상태 초기화
if "user_level" not in st.session_state: st.session_state.user_level = None 
if "mission" not in st.session_state: st.session_state.mission = None
if "audio_cache" not in st.session_state: st.session_state.audio_cache = {}
if "practice_results" not in st.session_state: st.session_state.practice_results = {}
if "quiz_state" not in st.session_state:
    st.session_state.quiz_state = {
        "phase": "ready", "current_idx": 0, "shuffled_words": [], 
        "wrong_words": [], "loop_count": 1, "current_options": None
    }

# ==========================================
# 2. DB 및 유틸리티 함수
# ==========================================
def get_user_data(user_id):
    response = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if response.data: return response.data[0]
    return None

def create_new_user(user_id):
    data = { "user_id": user_id, "current_level": None, "total_complete_count": 0, "last_test_count": 0, "streak": 0, "last_visit_date": None }
    supabase.table("users").insert(data).execute()

def update_attendance(user_id):
    user = get_user_data(user_id)
    if not user:
        create_new_user(user_id)
        user = get_user_data(user_id)
    today_str = date.today().isoformat()
    last_visit = user.get("last_visit_date")
    streak = user.get("streak", 0)
    
    if last_visit != today_str:
        if last_visit:
            delta = (date.today() - datetime.date.fromisoformat(last_visit)).days
            streak = streak + 1 if delta == 1 else 1
        else: streak = 1
        supabase.table("users").update({ "last_visit_date": today_str, "streak": streak }).eq("user_id", user_id).execute()
    
    return streak

def complete_daily_mission(user_id):
    user = get_user_data(user_id)
    new_cnt = user.get("total_complete_count", 0) + 1
    supabase.table("users").update({"total_complete_count": new_cnt}).eq("user_id", user_id).execute()
    supabase.table("study_logs").insert({ "user_id": user_id, "study_date": date.today().isoformat(), "completed_at": datetime.datetime.now().isoformat() }).execute()

def save_wrong_word_db(user_id, word_obj):
    res = supabase.table("wrong_words").select("*").eq("user_id", user_id).eq("word", word_obj['en']).execute()
    if res.data:
        supabase.table("wrong_words").update({"wrong_count": res.data[0]['wrong_count'] + 1}).eq("id", res.data[0]['id']).execute()
    else:
        supabase.table("wrong_words").insert({ "user_id": user_id, "word": word_obj['en'], "meaning": word_obj['ko'], "wrong_count": 1 }).execute()

def update_level_and_test_log(user_id, new_level):
    cnt = get_user_data(user_id).get("total_complete_count", 0)
    supabase.table("users").update({ "current_level": new_level, "last_test_count": cnt }).eq("user_id", user_id).execute()

# --- AI 관련 함수 ---

def get_audio_bytes(text):
    if text in st.session_state.audio_cache: return st.session_state.audio_cache[text]
    try:
        response = client.audio.speech.create(model="tts-1", voice="alloy", input=text)
        st.session_state.audio_cache[text] = response.content
        return response.content
    except: return None

def set_focus_js():
    components.html("""<script>setTimeout(function() { var inputs = window.parent.document.querySelectorAll("input[type=text]"); if (inputs.length > 0) { inputs[inputs.length - 1].focus(); } }, 100);</script>""", height=0)

def run_level_test_ai(text):
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system", "content":"Evaluate English level (Low/Mid/High) based on user input."}, {"role":"user", "content":text}]
    )
    return res.choices[0].message.content.strip()

# [핵심 변경] 라이브러리 없이 HTTP 요청으로 직접 연결 (에러 해결의 열쇠 🔑)
def generate_curriculum(level):
    # 구글 API URL (Gemini 1.5 Flash)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={google_api_key}"
    
    headers = {'Content-Type': 'application/json'}
    
    prompt_text = f"""
    Create a JSON curriculum for Korean middle schooler level '{level}'.
    Topic in English. Grammar explanations MUST be in Korean (Detailed, Why & How).
    Output JSON Schema: {{ "topic": "...", "grammar": {{ "title": "...", "description": "...", "rule": "...", "example": "..." }}, "words": [{{ "en": "...", "ko": "..." }}], "practice_sentences": [{{ "ko": "...", "en": "...", "hint_structure": "...", "hint_grammar": "..." }}] }}
    Create exactly 20 words and 20 sentences.
    """
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }],
        "generationConfig": {
            "response_mime_type": "application/json"
        }
    }
    
    try:
        # HTTP POST 요청 보내기
        response = requests.post(url, headers=headers, json=payload)
        
        # 응답 확인
        if response.status_code == 200:
            result = response.json()
            # JSON 파싱 (구글 응답 구조에 맞춤)
            text_content = result['candidates'][0]['content']['parts'][0]['text']
            return json.loads(text_content)
        else:
            st.error(f"Google API Error: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        st.error(f"연결 실패: {str(e)}")
        return None

def transcribe_audio(audio_bytes):
    import io
    f = io.BytesIO(audio_bytes)
    f.name = "input.wav"
    return client.audio.transcriptions.create(model="whisper-1", file=f).text

def evaluate_practice(target, user_input):
    prompt = f"Goal: '{target}', Input: '{user_input}'. If meaning matches, output 'PASS'. Else 'FAIL' with specific Korean feedback (include reasons like article, tense, etc)."
    res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system", "content":prompt}])
    return res.choices[0].message.content

# ==========================================
# 3. 메인 화면 구성
# ==========================================
st.title("🏫 AI 중학 영어 스파르타")

with st.sidebar:
    st.header("🔑 로그인")
    user_id = st.text_input("아이디", value="student1")
    if user_id:
        streak = update_attendance(user_id)
        user_data = get_user_data(user_id)
        st.success(f"🔥 {streak}일 연속 학습 중!")
        st.info(f"🏆 누적 완료: {user_data.get('total_complete_count', 0)}회")
    else: st.stop()

# 레벨 테스트
current_level = user_data.get('current_level')
total_complete = user_data.get('total_complete_count', 0)
last_test_cnt = user_data.get('last_test_count', 0)

if current_level is None or (total_complete - last_test_cnt) >= 5:
    st.subheader("📝 레벨 테스트")
    st.write("Q. What do you usually do on weekends?")
    aud = audio_recorder(text="", key="lvl_rec", neutral_color="#6aa36f", recording_color="#e8b62c")
    if aud:
        txt = transcribe_audio(aud)
        st.write(f"답변: {txt}")
        if len(txt) > 1:
            lvl = run_level_test_ai(txt)
            update_level_and_test_log(user_id, lvl)
            st.success(f"레벨 설정 완료: {lvl}")
            time.sleep(1.5)
            st.rerun()
    st.stop()

# 미션 생성
if not st.session_state.mission:
    with st.status("🚀 오늘의 미션을 생성하고 있습니다... (Gemini)", expanded=True) as status:
        mission_data = generate_curriculum(current_level)
        if mission_data:
            st.session_state.mission = mission_data
            status.update(label="준비 완료!", state="complete", expanded=False)
        else:
            status.update(label="오류 발생", state="error")
            st.stop()

mission = st.session_state.mission
st.header(f"Topic: {mission['topic']}")
st.caption(f"Level: {current_level}")

# ==========================================
# 4. 탭 구조
# ==========================================
tab1, tab2, tab3, tab4 = st.tabs(["📘 오늘의 문법", "🍎 오늘의 단어", "✍️ 문장 연습", "⚔️ 실전 테스트"])

# --- Tab 1 ---
with tab1:
    gr = mission['grammar']
    st.subheader(gr['title'])
    st.markdown(gr['description'])
    st.info(f"📌 공식: {gr.get('rule', '')}")
    st.markdown(f"💡 예문: *{gr['example']}*")
    st.divider()
    if st.button("🔊 문법 설명 듣기"):
        with st.spinner("생성 중..."):
            tts_text = f"오늘의 문법은 {gr['title']}입니다. {gr['description']} 예를 들어 {gr['example']} 처럼 씁니다."
            audio = get_audio_bytes(tts_text)
            if audio: st.audio(audio, format='audio/mp3')

# --- Tab 2 ---
with tab2:
    st.info("💡 스피커를 누르면 발음을 들을 수 있어요.")
    for i, w in enumerate(mission['words']):
        c1, c2, c3 = st.columns([1, 4, 1])
        with c1: st.write(f"**{i+1}.**")
        with c2: st.write(f"**{w['en']}** : {w['ko']}")
        with c3:
            if st.button("🔊", key=f"tts_w_{i}"):
                audio = get_audio_bytes(w['en'])
                if audio: st.audio(audio, format='audio/mp3',