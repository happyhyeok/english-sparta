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

# [중요 변경 1] 캐싱 적용 (@st.cache_data) - API 호출 낭비 방지
@st.cache_data(show_spinner=False, ttl=3600) # 1시간 동안 저장
def generate_curriculum(level, _today_str): # _today_str은 매일 새로운 미션을 위해 넣은 더미 인자
    model_candidates = ["gemini-1.5-flash", "gemini-1.5-flash-002", "gemini-1.5-flash-001", "gemini-flash-latest", "gemini-pro"]
    headers = {'Content-Type': 'application/json'}
    
    prompt_text = f"""
    You are an expert English Curriculum Designer for Korean Middle School students.
    Create a JSON curriculum for level '{level}'.
    
    **CRITICAL RULES for 'practice_sentences':**
    1. **hint_structure**: MUST show the **ENGLISH Word Order** (Subject + Verb + Object/Modifer).
       - ❌ BAD: "주어(My brother) + 부사(late) + 동사(sleeps)" (Korean Order)
       - ✅ GOOD: "주어(My brother) + 동사(sleeps) + 부사(late)" (English Order)
       - Use Korean terms for parts of speech: 주어, 동사, 목적어, 보어, 형용사, 부사, 전치사구.
    2. **hint_grammar**: Explain the specific grammatical rule used in this sentence in Korean.
    
    Output JSON Schema:
    {{
        "topic": "English Topic Name",
        "grammar": {{
            "title": "문법 제목 (한국어)",
            "description": "문법 상세 설명 (한국어). Why & How 포함.",
            "rule": "Rule (English)",
            "example": "Example (English)"
        }},
        "words": [{{ "en": "English Word", "ko": "한국어 뜻" }}],
        "practice_sentences": [
            {{ 
                "ko": "한글 문장", 
                "en": "English Sentence", 
                "hint_structure": "주어(...) + 동사(...) + ... (English Order)", 
                "hint_grammar": "문법 포인트 (한국어)" 
            }}
        ]
    }}
    Create exactly 20 words and 20 sentences.
    """
    
    payload = { "contents": [{"parts": [{"text": prompt_text}]}], "generationConfig": {"response_mime_type": "application/json"} }
    
    last_error_msg = ""
    
    for model_name in model_candidates:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={google_api_key}"
        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                result = response.json()
                text_content = result['candidates'][0]['content']['parts'][0]['text']
                return json.loads(text_content)
            else:
                # [중요 변경 2] 에러 내용 기록
                last_error_msg = f"Model {model_name} Error: {response.status_code} - {response.text}"
                continue 
        except Exception as e: 
            last_error_msg = str(e)
            continue
    
    # 실패 시 None 대신 에러 메시지 반환 (디버깅용)
    return {"error": last_error_msg} 

def transcribe_audio(audio_bytes):
    import io
    f = io.BytesIO(audio_bytes)
    f.name = "input.wav"
    return client.audio.transcriptions.create(model="whisper-1", file=f).text

def evaluate_practice(target, user_input):
    prompt = f"""
    You are an expert English teacher for Korean middle school students.
    Task: Analyze student input vs target sentence. Provide specific feedback in **KOREAN**.

    Target: "{target}"
    Student Input: "{user_input}"

    Guidelines:
    1. Language: ALL output in Korean.
    2. Hallucination: Do NOT claim a word is missing if present.
    3. Priorities: Wrong Word > Word Order > Prepositions > Articles > Tense.
    4. Spelling: Minor typos -> PASS.

    Output Rules:
    - Correct: Output 'PASS'.
    - Incorrect: Output 'FAIL' followed by detailed explanation.
    
    Format:
    PASS
    or
    FAIL [Korean Feedback]
    """
    try:
        res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system", "content":prompt}], temperature=0.3)
        return res.choices[0].message.content
    except Exception as e: return f"FAIL 오류: {str(e)}"

# ==========================================
# 3. 메인 화면
# ==========================================
st.title("🏫 AI 중학 영어 스파르타")

# 진단 도구 (유지)
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
    with st.status("🚀 오늘의 미션을 생성하고 있습니다... (Gemini)", expanded=True) as status:
        # 날짜를 넣어 매일 변경되도록 하되, 같은 날엔 캐싱 사용
        today_key = date.today().isoformat()
        mission_data = generate_curriculum(current_level, today_key)
        
        # 에러 체크
        if mission_data and "error" in mission_data:
            st.error("🚨 연결 실패! 아래 원인을 확인하세요.")
            st.code(mission_data["error"]) # 화면에 상세 에러 출력
            status.update(label="오류 발생", state="error")
            st.stop()
        elif mission_data:
            st.session_state.mission = mission_data
            status.update(label="준비 완료!", state="complete", expanded=False)
        else:
            status.update(label="알 수 없는 오류", state="error")
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
    st.markdown("### ✍️ 문장 만들기 연습")
    st.caption("힌트를 보고 문장을 완성하세요. 틀리면 내용을 수정해서 다시 제출하면 됩니다.")
    
    for idx, q in enumerate(mission['practice_sentences']):
        result_key = f"res_{idx}"
        input_key = f"input_{idx}"
        
        is_pass = (result_key in st.session_state.practice_results and st.session_state.practice_results[result_key]['status'] == 'PASS')
        
        with st.expander(f"Q{idx+1}. {q['ko']}", expanded=not is_pass):
            st.caption(f"💡 구조: {q.get('hint_structure','')} | 🔑 문법: {q.get('hint_grammar','')}")
            
            mic_col, _ = st.columns([1, 5])
            with mic_col:
                audio_val = audio_recorder(text="", key=f"mic_{idx}", icon_size="lg", neutral_color="#6aa36f", recording_color="#e8b62c")
            
            if audio_val:
                transcribed_text = transcribe_audio(audio_val)
                st.session_state[input_key] = transcribed_text
                st.rerun()

            with st.form(key=f"form_p_{idx}"):
                user_val = st.text_input("영어 문장 입력", key=input_key)
                submit_btn = st.form_submit_button("제출 및 채점")
                
                if submit_btn:
                    if not user_val.strip():
                        st.warning("내용을 입력해주세요.")
                    else:
                        if user_val.lower().replace(".","").strip() == q['en'].lower().replace(".","").strip():
                            st.session_state.practice_results[result_key] = {'status': 'PASS', 'input': user_val}
                        else:
                            with st.spinner("AI 선생님이 채점 중입니다..."):
                                feedback_res = evaluate_practice(q['en'], user_val)
                            
                            if "PASS" in feedback_res:
                                st.session_state.practice_results[result_key] = {'status': 'PASS', 'input': user_val}
                            else:
                                clean_feedback = feedback_res.replace("FAIL", "").strip()
                                st.session_state.practice_results[result_key] = {'status': 'FAIL', 'input': user_val, 'feedback': clean_feedback}
            
            if result_key in st.session_state.practice_results:
                res_data = st.session_state.practice_results[result_key]
                if res_data['status'] == 'PASS':
                    st.success(f"🎉 정답입니다! : {res_data['input']}")
                else:
                    st.error(f"❌ 다시 시도해보세요!")
                    st.info(f"💡 피드백: {res_data['feedback']}")

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