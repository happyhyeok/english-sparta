import streamlit as st
import requests
import json
import random
import time
from datetime import date
import datetime
from openai import OpenAI
from audio_recorder_streamlit import audio_recorder
import streamlit.components.v1 as components
from supabase import create_client, Client

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
    div[data-testid="stForm"] {
        border: 1px solid #f0f2f6;
        padding: 20px;
        border-radius: 10px;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.2rem;
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

# 세션 상태 초기화
if "user_info" not in st.session_state: st.session_state.user_info = None 
if "mission" not in st.session_state: st.session_state.mission = None
if "audio_cache" not in st.session_state: st.session_state.audio_cache = {}
if "practice_results" not in st.session_state: st.session_state.practice_results = {}
if "last_processed_audio" not in st.session_state: st.session_state.last_processed_audio = {} 
if "quiz_state" not in st.session_state:
    st.session_state.quiz_state = {
        "phase": "ready", "current_idx": 0, "shuffled_words": [], 
        "wrong_words": [], "loop_count": 1, "current_options": None
    }

# ==========================================
# 2. DB 및 유틸리티 함수
# ==========================================
def fetch_user_from_db(user_id):
    response = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if response.data: return response.data[0]
    return None

def create_new_user(user_id):
    data = { "user_id": user_id, "current_level": None, "total_complete_count": 0, "last_test_count": 0, "streak": 0, "last_visit_date": None }
    supabase.table("users").insert(data).execute()

def login_and_update_attendance(user_id):
    if st.session_state.user_info and st.session_state.user_info['user_id'] == user_id:
        return st.session_state.user_info['streak']

    user = fetch_user_from_db(user_id)
    if not user:
        create_new_user(user_id)
        user = fetch_user_from_db(user_id)
    
    today_str = date.today().isoformat()
    last_visit = user.get("last_visit_date")
    streak = user.get("streak", 0)
    
    if last_visit != today_str:
        if last_visit:
            delta = (date.today() - datetime.date.fromisoformat(last_visit)).days
            streak = streak + 1 if delta == 1 else 1
        else: 
            streak = 1
        
        supabase.table("users").update({ "last_visit_date": today_str, "streak": streak }).eq("user_id", user_id).execute()
        user = fetch_user_from_db(user_id)

    st.session_state.user_info = user
    return streak

def complete_daily_mission(user_id):
    user = fetch_user_from_db(user_id)
    new_cnt = user.get("total_complete_count", 0) + 1
    supabase.table("users").update({"total_complete_count": new_cnt}).eq("user_id", user_id).execute()
    supabase.table("study_logs").insert({ "user_id": user_id, "study_date": date.today().isoformat(), "completed_at": datetime.datetime.now().isoformat() }).execute()
    
    if st.session_state.user_info:
        st.session_state.user_info['total_complete_count'] = new_cnt

def save_wrong_word_db(user_id, word_obj):
    res = supabase.table("wrong_words").select("*").eq("user_id", user_id).eq("word", word_obj['en']).execute()
    if res.data:
        supabase.table("wrong_words").update({"wrong_count": res.data[0]['wrong_count'] + 1}).eq("id", res.data[0]['id']).execute()
    else:
        supabase.table("wrong_words").insert({ "user_id": user_id, "word": word_obj['en'], "meaning": word_obj['ko'], "wrong_count": 1 }).execute()

def update_level_and_test_log(user_id, new_level):
    cnt = st.session_state.user_info.get("total_complete_count", 0)
    supabase.table("users").update({ "current_level": new_level, "last_test_count": cnt }).eq("user_id", user_id).execute()
    st.session_state.user_info['current_level'] = new_level
    st.session_state.user_info['last_test_count'] = cnt

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
    res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system", "content":"Evaluate English level (Low/Mid/High) based on user input."}, {"role":"user", "content":text}])
    return res.choices[0].message.content.strip()

@st.cache_data(show_spinner=False, ttl=3600)
def generate_curriculum(level, _today_str, user_progress_count):
    model_candidates = ["gemini-flash-latest", "gemini-pro-latest", "gemini-2.0-flash-exp"]
    headers = {'Content-Type': 'application/json'}
    
    grammar_syllabus = [
        "Be동사의 현재형 (am, are, is)", "일반동사의 현재형 (3인칭 단수 s/es)", "명사와 관사 (a/an, the, 복수형 s)",
        "대명사 (주격, 소유격, 목적격)", "형용사와 부사의 역할", "Be동사의 부정문과 의문문",
        "일반동사의 부정문과 의문문 (do/does)", "진행형 시제 (be + v-ing)", "미래 시제 (will, be going to)",
        "조동사 1 (can, may)", "조동사 2 (must, should, have to)", "의문사 의문문 (Who, What, Where...)",
        "과거 시제 (Be동사 was/were)", "과거 시제 (일반동사 규칙 -ed)", "과거 시제 (일반동사 불규칙)",
        "To 부정사의 명사적 용법", "동명사 (v-ing)", "명령문과 제안문 (Let's)", "전치사 (시간: at, on, in)", "전치사 (장소: at, on, in)"
    ]
    
    today_grammar = grammar_syllabus[user_progress_count % len(grammar_syllabus)]
    
    topics_by_day = ["School Life", "Hobbies", "Nature & Animals", "Food & Cooking", "Travel", "Health & Feelings", "My Dream Job"]
    today_topic_hint = topics_by_day[datetime.datetime.now().weekday()]

    prompt_text = f"""
    You are an expert English Curriculum Designer for Korean Middle School Grade 1.
    
    **CRITICAL INSTRUCTION - SENTENCE GENERATION:**
    1. **NO DECORATIVE ADJECTIVES:** Do NOT add words like 'big', 'fast', 'happy', 'new' unless absolutely necessary for the grammar rule.
       - ❌ Bad: "Dad drives a big truck." (Where did 'big' come from?)
       - ✅ Good: "Dad drives a truck."
    2. **1:1 Match:** The Korean translation MUST match the English sentence exactly word-for-word.
    3. **Target Grammar:** Use **"{today_grammar}"**.
    4. **Simplicity:** Keep sentences under 8 words.
    
    **CONTENT GUIDELINES:**
    1. **Target:** CEFR A2-B1 (Middle School).
    2. **Mix:** 30% Easy, 50% Medium (Core), 20% Challenge.
    3. **Topic:** {today_topic_hint}.

    Output JSON Schema:
    {{
        "topic": "Topic Name ({today_topic_hint})",
        "grammar": {{ "title": "{today_grammar}", "description": "Easy Korean Explanation", "rule": "English Rule", "example": "English Example" }},
        "words": [{{ "en": "...", "ko": "..." }}],
        "practice_sentences": [
            {{ 
                "ko": "Korean Translation", 
                "en": "English Sentence (No hidden adjectives)", 
                "hint_structure": "Subject + Verb + Object", 
                "hint_grammar": "Korean Tip" 
            }}
        ]
    }}
    Create exactly 20 words and 20 sentences.
    """
    
    payload = { "contents": [{"parts": [{"text": prompt_text}]}], "generationConfig": {"response_mime_type": "application/json"} }
    
    last_error_details = []
    for model_name in model_candidates:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={google_api_key}"
        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                result = response.json()
                return json.loads(result['candidates'][0]['content']['parts'][0]['text'])
            else:
                last_error_details.append(f"{model_name}: {response.status_code}")
                continue
        except Exception as e:
            last_error_details.append(str(e))
            continue
    
    return {"error": "\n".join(last_error_details)}

def transcribe_audio(audio_bytes):
    import io
    f = io.BytesIO(audio_bytes)
    f.name = "input.wav"
    return client.audio.transcriptions.create(model="whisper-1", file=f).text

# [수정됨] 채점 로직: 영어 절대 금지 & 한국어 피드백 강제
def evaluate_practice(target, user_input):
    prompt = f"""
    Role: Kind English Teacher for Korean Middle School Students.
    Task: Check the student's input against the Target Sentence.
    
    Target: "{target}"
    Input: "{user_input}"
    
    **STRICT RULES:**
    1. **NO HALLUCINATION:** Do NOT penalize missing adjectives (big, new) unless they are in the Target.
    2. **LANGUAGE: KOREAN ONLY (한국어)**. 
       - ❌ BAD: "Missing preposition 'for'."
       - ✅ GOOD: "전치사 'for'가 빠졌어요."
       - ✅ GOOD: "주어와 동사의 수 일치가 틀렸습니다."
    3. **Evaluation:**
       - If strict match (punctuation ignored): Output 'PASS'.
       - If mismatch: Output 'FAIL' followed by a friendly KOREAN explanation.
    
    Output Format:
    PASS
    or
    FAIL [한국어 피드백]
    """
    try:
        # Temperature를 0.2로 낮춰서 지시사항을 더 철저히 따르게 함
        res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system", "content":prompt}], temperature=0.2)
        return res.choices[0].message.content
    except Exception as e: return f"FAIL 오류: {str(e)}"

# ==========================================
# 3. 메인 화면 로직
# ==========================================
with st.sidebar:
    st.header("⚙️ 설정")
    with st.expander("🛠️ 연결 상태 확인", expanded=False):
        if st.button("모델 확인"):
            try:
                test_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={google_api_key}"
                res = requests.get(test_url).json()
                models = [m['name'] for m in res.get('models', []) if 'generateContent' in m['supportedGenerationMethods']]
                st.success(f"성공: {len(models)}개")
            except Exception as e: st.error(f"실패: {e}")
    st.divider()
    user_id = st.text_input("아이디", value="student1")

if not user_id: st.warning("아이디를 입력하세요."); st.stop()

streak = login_and_update_attendance(user_id)
user_data = st.session_state.user_info 

current_level = user_data.get('current_level')
total_complete = user_data.get('total_complete_count', 0)
last_test_cnt = user_data.get('last_test_count', 0)

st.title("🏫 AI 중학 영어 스파르타")
col1, col2, col3 = st.columns(3)
with col1: st.metric("👤 학생", user_id)
with col2: st.metric("🔥 연속 학습", f"{streak}일")
with col3: st.metric("🏆 누적 완료", f"{total_complete}회")
st.divider()

if current_level is None:
    st.subheader("📝 레벨 테스트"); st.write("Q. What do you usually do on weekends?")
    aud = audio_recorder(text="", key="lvl_rec", neutral_color="#6aa36f", recording_color="#e8b62c")
    if aud:
        aud_hash = hash(aud)
        if "lvl_test_audio" not in st.session_state or st.session_state.lvl_test_audio != aud_hash:
            txt = transcribe_audio(aud)
            st.session_state.lvl_test_audio = aud_hash
            st.write(f"답변: {txt}")
            if len(txt) > 1:
                lvl = run_level_test_ai(txt)
                update_level_and_test_log(user_id, lvl)
                st.success(f"완료: {lvl}")
                time.sleep(1.5)
                st.rerun()
    st.stop()

if not st.session_state.mission:
    with st.status("🚀 오늘의 미션을 생성하고 있습니다... (중1 문법 커리큘럼 적용)", expanded=True) as status:
        today_key = date.today().isoformat()
        mission_data = generate_curriculum(current_level, today_key, total_complete)
        
        if mission_data and "error" in mission_data:
            status.update(label="오류", state="error"); st.error(mission_data["error"]); st.stop()
        elif mission_data:
            st.session_state.mission = mission_data; status.update(label="완료!", state="complete", expanded=False)
        else: status.update(label="오류", state="error"); st.stop()

mission = st.session_state.mission
st.subheader(f"Topic: {mission['topic']}")

tab1, tab2, tab3, tab4 = st.tabs(["📘 오늘의 문법", "🍎 오늘의 단어", "✍️ 문장 연습", "⚔️ 실전 테스트"])

with tab1:
    gr = mission['grammar']
    st.subheader(gr['title'])
    st.markdown(gr['description'])
    st.info(f"📌 공식: {gr.get('rule', '')}")
    st.markdown(f"💡 예문: *{gr['example']}*")
    st.divider()
    if st.button("🔊 문법