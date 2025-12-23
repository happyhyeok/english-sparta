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

# [핵심] 자동 모델 찾기 함수
def generate_curriculum(level):
    model_candidates = [
        "gemini-1.5-flash",       # 표준
        "gemini-1.5-flash-002",   # 최신 안정
        "gemini-1.5-flash-001",   # 구버전
        "gemini-flash-latest",    # 별칭
        "gemini-pro"              # 1.0 Pro
    ]
    
    headers = {'Content-Type': 'application/json'}
    
    prompt_text = f"""
    Create a JSON curriculum for Korean middle schooler level '{level}'.
    Topic in English. Grammar explanations MUST be in Korean (Detailed, Why & How).
    Output JSON Schema: {{ "topic": "...", "grammar": {{ "title": "...", "description": "...", "rule": "...", "example": "..." }}, "words": [{{ "en": "...", "ko": "..." }}], "practice_sentences": [{{ "ko": "...", "en": "...", "hint_structure": "...", "hint_grammar": "..." }}] }}
    Create exactly 20 words and 20 sentences.
    """
    
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    for model_name in model_candidates:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={google_api_key}"
        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                result = response.json()
                text_content = result['candidates'][0]['content']['parts'][0]['text']
                return json.loads(text_content)
            else:
                continue 
        except Exception:
            continue
            
    st.error("❌ 모든 AI 모델 연결에 실패했습니다.")
    return None

def transcribe_audio(audio_bytes):
    import io
    f = io.BytesIO(audio_bytes)
    f.name = "input.wav"
    return client.audio.transcriptions.create(model="whisper-1", file=f).text

# [중요 변경] 교수법이 적용된 강력한 피드백 프롬프트
def evaluate_practice(target, user_input):
    prompt = f"""
    You are an expert English teacher for Korean middle school students.
    Your task is to analyze the student's input against the target sentence and provide specific, helpful feedback in **KOREAN**.

    Target Sentence: "{target}"
    Student Input: "{user_input}"

    **Analysis Guidelines (SLA-based):**
    1. **Strict Language Policy:** ALL output (explanation, feedback) MUST be in **Korean** (한국어). Never use English for explanations.
    2. **Hallucination Check:** Before giving feedback, strictly compare word-by-word. Do not claim a word is missing if it is present.
    3. **Error Prioritization:**
       - **Meaning/Vocab:** If the wrong word is used, correct it first.
       - **Grammar (Syntax):** Check Word Order > Prepositions > Articles > Tense > Subject-Verb Agreement.
       - **Spelling:** Minor typos are acceptable if meaning is clear -> Output 'PASS' but mention the typo gently.

    **Output Rules:**
    - If the sentence is correct (or close enough): Output just 'PASS'.
    - If incorrect: Output 'FAIL' followed by a detailed explanation in Korean.
    
    **Feedback Structure (for FAIL):**
    - 🚨 **오류 지적:** (Example: "전치사 'in'이 빠졌어요.")
    - 💡 **이유 설명:** Explain the grammar rule simply. (Example: "장소를 나타낼 때는 앞에 'in'을 써야 해요.")
    - ✨ **정답 유도:** Encourage them to try again.

    OUTPUT FORMAT:
    PASS
    or
    FAIL [Your Korean Feedback Here]
    """
    
    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=[{"role":"system", "content":prompt}],
            temperature=0.3 # 창의성 낮춤 -> 정확도 향상
        )
        return res.choices[0].message.content
    except Exception as e:
        return f"FAIL 채점 중 오류가 발생했습니다: {str(e)}"

# ==========================================
# 3. 메인 화면
# ==========================================
st.title("🏫 AI 중학 영어 스파르타")

# 진단 도구
with st.expander("🛠️ API 연결 문제 해결 도구", expanded=False):
    if st.button("내 API 키로 가능한 모델 확인하기"):
        try:
            test_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={google_api_key}"
            res = requests.get(test_url).json()
            models = [m['name'] for m in res.get('models', []) if 'generateContent' in m['supportedGenerationMethods']]
            st.success(f"사용 가능 모델: {', '.join(models)}")
        except Exception as e:
            st.error(f"확인 실패: {e}")

with st.sidebar:
    st.header("🔑 로그인")
    user_id = st.text_input("아이디", value="student1")
    if user_id:
        streak = update_attendance(user_id)
        user_data = get_user_data(user_id)
        st.success(f"🔥 {streak}일 연속 학습 중!")
        st.info(f"🏆 누적 완료: {user_data.get('total_complete_count', 0)}회")
    else: st.stop()

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

if not st.session_state.mission:
    with st.status("🚀 오늘의 미션을 생성하고 있습니다... (Gemini Auto-Detect)", expanded=True) as status:
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

tab1, tab2, tab3, tab4 = st.tabs(["📘 오늘의 문법", "🍎 오늘의 단어", "✍️ 문장 연습", "⚔️ 실전 테스트"])

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

with tab2:
    st.info("💡 스피커를 누르면 발음을 들을 수 있어요.")
    for i, w in enumerate(mission['words']):
        c1, c2, c3 = st.columns([1, 4, 1])
        with c1: st.write(f"**{i+1}.**")
        with c2: st.write(f"**{w['en']}** : {w['ko']}")
        with c3:
            if st.button("🔊", key=f"tts_w_{i}"):
                audio = get_audio_bytes(w['en'])
                if audio: st.audio(audio, format='audio/mp3', autoplay=True)

with tab3:
    st.markdown("### 문장 만들기 연습")
    for idx, q in enumerate(mission['practice_sentences']):
        result_key = f"res_{idx}"
        is_solved = (result_key in st.session_state.practice_results and st.session_state.practice_results[result_key]['status'] == 'PASS')
        
        with st.expander(f"Q{idx+1}. {q['ko']}", expanded=not is_solved):
            st.caption(f"힌트: {q.get('hint_structure','')} | {q.get('hint_grammar','')}")
            cached_res = st.session_state.practice_results.get(result_key)
            if cached_res and cached_res['status'] == 'PASS':
                st.success(f"✅ 정답! : {cached_res['input']}")
                if st.button("다시 하기", key=f"retry_{idx}"):
                    del st.session_state.practice_results[result_key]
                    st.rerun()
            else:
                col_mic, col_input = st.columns([1, 4])
                user_input = None
                with col_mic:
                    aud = audio_recorder(text="", key=f"prac_mic_{idx}", icon_size="lg", neutral_color="#6aa36f", recording_color="#e8b62c")
                    if aud: user_input = transcribe_audio(aud)
                with col_input:
                    with st.form(f"prac_form_{idx}", clear_on_submit=True):
                        txt_val = st.text_input("영어 문장 입력", key=f"prac_txt_{idx}")
                        if st.form_submit_button("제출"): user_input = txt_val
                
                if cached_res and cached_res['status'] == 'FAIL':
                    st.error(f"❌ 입력: {cached_res['input']}")
                    st.warning(cached_res['feedback'])
                if user_input:
                    if user_input.lower().replace(".","").strip() == q['en'].lower().replace(".","").strip():
                        st.session_state.practice_results[result_key] = {'status': 'PASS', 'input': user_input}
                        st.rerun()
                    else:
                        with st.spinner("채점 중..."):
                            res = evaluate_practice(q['en'], user_input)
                        if "PASS" in res:
                            st.session_state.practice_results[result_key] = {'status': 'PASS', 'input': user_input}
                        else:
                            st.session_state.practice_results[result_key] = {'status': 'FAIL', 'input': user_input, 'feedback': res.replace("FAIL", "").strip()}
                        st.rerun()

with tab4:
    qs = st.session_state.quiz_state
    words = qs["shuffled_words"]
    if not words and qs["phase"] == "ready":
        if st.button("🚀 실전 테스트 시작하기"):
            qs["shuffled_words"] = random.sample(mission['words'], 20)
            qs["phase"] = "mc"
            st.rerun()
    elif qs["phase"] == "end":
        st.balloons()
        st.success(f"🎉 {qs['loop_count']}회차 학습 완료!")
        if st.button("학습 종료 및 메인으로"):
            complete_daily_mission(user_id)
            for key in ["mission", "audio_cache", "quiz_state", "practice_results"]: 
                if key in st.session_state: del st.session_state[key]
            st.rerun()
    elif words:
        total = len(words)
        curr = qs["current_idx"]
        target = words[curr]
        st.progress((curr + 1) / total, text=f"문제 {curr + 1} / {total}")
        
        if qs["phase"] == "mc":
            st.subheader(f"객관식: {target['en']}")
            if qs["current_options"] is None:
                opts = [target['ko']]
                while len(opts) < 4:
                    r = random.choice(mission['words'])['ko']
                    if r not in opts: opts.append(r)
                random.shuffle(opts)
                qs["current_options"] = opts
            with st.form(f"quiz_mc_{curr}"):
                choice = st.radio("알맞은 뜻을 고르세요", qs["current_options"])
                if st.form_submit_button("확인"):
                    if choice == target['ko']: st.success("정답! ⭕")
                    else:
                        st.error(f"오답! 정답은 '{target['ko']}' 입니다.")
                        if target not in qs["wrong_words"]: qs["wrong_words"].append(target); save_wrong_word_db(user_id, target)
                    time.sleep(0.5)
                    qs["current_options"] = None
                    if curr + 1 < total: qs["current_idx"] += 1; st.rerun()
                    else: qs["phase"] = "writing"; qs["current_idx"] = 0; random.shuffle(qs["shuffled_words"]); st.rerun()

        elif qs["phase"] == "writing":
            st.subheader(f"주관식: {target['ko']}")
            set_focus_js()
            with st.form(f"quiz_wr_{curr}", clear_on_submit=True):
                inp = st.text_input("영어 단어를 입력하세요")
                if st.form_submit_button("제출"):
                    if inp.strip().lower() == target['en'].lower(): st.success("정답! ⭕")
                    else:
                        st.error(f"오답! 정답은 '{target['en']}' 입니다.")
                        if target not in qs["wrong_words"]: qs["wrong_words"].append(target); save_wrong_word_db(user_id, target)
                    time.sleep(0.5)
                    if curr + 1 < total: qs["current_idx"] += 1; st.rerun()
                    else:
                        if qs["wrong_words"]:
                            qs["shuffled_words"] = qs["wrong_words"][:]; qs["wrong_words"] = []; qs["current_idx"] = 0; qs["phase"] = "ready"; qs["loop_count"] += 1; st.warning("🚨 틀린 문제 재도전!"); time.sleep(1); qs["phase"] = "mc"; st.rerun()
                        else: qs["phase"] = "end"; st.rerun()