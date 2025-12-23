import streamlit as st
import google.generativeai as genai
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

# CSS: 탭 가독성 향상 및 알림창 스타일
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
genai.configure(api_key=google_api_key)

# 세션 상태 초기화
if "user_level" not in st.session_state: st.session_state.user_level = None 
if "mission" not in st.session_state: st.session_state.mission = None
if "audio_cache" not in st.session_state: st.session_state.audio_cache = {} # TTS 캐싱 (속도)
if "practice_results" not in st.session_state: st.session_state.practice_results = {} # 채점 결과 보존
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
    """TTS 생성 및 캐싱 (속도 최적화)"""
    if text in st.session_state.audio_cache:
        return st.session_state.audio_cache[text]
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

def generate_curriculum(level):
    # [핵심] 여러 모델 이름을 순서대로 시도하여 404/429 에러 방지
    model_candidates = [
        "gemini-1.5-flash",
        "gemini-1.5-flash-latest",
        "gemini-1.5-flash-001",
        "gemini-pro" # 최후의 보루 (가장 안정적)
    ]
    
    prompt = f"""
    Create a JSON curriculum for Korean middle schooler level '{level}'.
    Topic in English. Grammar explanations MUST be in Korean (Detailed, Why & How).
    Output JSON: {{ "topic": "...", "grammar": {{ "title": "...", "description": "...", "rule": "...", "example": "..." }}, "words": [{{ "en": "...", "ko": "..." }}], "practice_sentences": [{{ "ko": "...", "en": "...", "hint_structure": "...", "hint_grammar": "..." }}] }}
    Create exactly 20 words and 20 sentences.
    """

    for model_name in model_candidates:
        try:
            # JSON 모드 설정 (gemini-pro는 지원 안 할 수 있어 예외처리)
            config = {"response_mime_type": "application/json"} if "flash" in model_name else {}
            model = genai.GenerativeModel(model_name=model_name, generation_config=config)
            
            response = model.generate_content(prompt)
            
            # 응답 텍스트 파싱
            txt = response.text
            # 마크다운 json 태그 제거 (gemini-pro 대응)
            if "```json" in txt: txt = txt.split("```json")[1].split("```")[0]
            elif "```" in txt: txt = txt.split("```")[1].split("```")[0]
            
            return json.loads(txt) # 성공하면 리턴
            
        except Exception as e:
            print(f"Model {model_name} failed: {e}")
            continue # 다음 모델 시도
            
    st.error("모든 AI 모델 연결에 실패했습니다. 잠시 후 다시 시도해주세요.")
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

# 레벨 테스트 로직
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
    with st.status("🚀 오늘의 미션을 생성하고 있습니다...", expanded=True) as status:
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
# 4. 탭 구조 구현 (최적화 적용)
# ==========================================
tab1, tab2, tab3, tab4 = st.tabs(["📘 오늘의 문법", "🍎 오늘의 단어", "✍️ 문장 연습", "⚔️ 실전 테스트"])

# --- Tab 1: 오늘의 문법 ---
with tab1:
    gr = mission['grammar']
    st.subheader(gr['title'])
    st.markdown(gr['description'])
    st.info(f"📌 공식: {gr.get('rule', '')}")
    st.markdown(f"💡 예문: *{gr['example']}*")
    
    st.divider()
    if st.button("🔊 문법 설명 듣기 (AI 선생님)"):
        with st.spinner("음성 생성 중..."):
            tts_text = f"오늘 배울 문법은 {gr['title']}입니다. {gr['description']} 예를 들어, {gr['example']} 과 같이 사용합니다."
            audio = get_audio_bytes(tts_text)
            if audio: st.audio(audio, format='audio/mp3')

# --- Tab 2: 오늘의 단어 ---
with tab2:
    st.info("💡 스피커 아이콘을 누르면 발음을 들을 수 있어요.")
    for i, w in enumerate(mission['words']):
        c1, c2, c3 = st.columns([1, 4, 1])
        with c1: st.write(f"**{i+1}.**")
        with c2: st.write(f"**{w['en']}** : {w['ko']}")
        with c3:
            if st.button("🔊", key=f"tts_w_{i}"):
                audio = get_audio_bytes(w['en'])
                if audio: st.audio(audio, format='audio/mp3', autoplay=True)

# --- Tab 3: 문장 연습 (데이터 보존 로직 적용) ---
with tab3:
    st.markdown("### 문장 만들기 연습")
    st.caption("AI 선생님이 실시간으로 피드백을 드려요!")
    
    for idx, q in enumerate(mission['practice_sentences']):
        result_key = f"res_{idx}"
        # 정답 여부에 따라 Expander 열기/닫기 조절
        is_solved = (result_key in st.session_state.practice_results and st.session_state.practice_results[result_key]['status'] == 'PASS')
        
        with st.expander(f"Q{idx+1}. {q['ko']}", expanded=not is_solved):
            st.caption(f"힌트: {q.get('hint_structure','')} | {q.get('hint_grammar','')}")
            
            # 저장된 결과 확인
            cached_res = st.session_state.practice_results.get(result_key)
            
            # 정답인 경우
            if cached_res and cached_res['status'] == 'PASS':
                st.success(f"✅ 정답! : {cached_res['input']}")
                if st.button("다시 하기", key=f"retry_{idx}"):
                    del st.session_state.practice_results[result_key]
                    st.rerun()
            else:
                # 문제 풀이 영역
                col_mic, col_input = st.columns([1, 4])
                user_input = None
                
                with col_mic:
                    aud = audio_recorder(text="", key=f"prac_mic_{idx}", icon_size="lg", neutral_color="#6aa36f", recording_color="#e8b62c")
                    if aud: user_input = transcribe_audio(aud)
                
                with col_input:
                    with st.form(f"prac_form_{idx}", clear_on_submit=True):
                        txt_val = st.text_input("영어 문장 입력", key=f"prac_txt_{idx}")
                        if st.form_submit_button("제출"): user_input = txt_val
                
                # 오답 피드백 표시 (저장된 내용)
                if cached_res and cached_res['status'] == 'FAIL':
                    st.error(f"❌ 입력: {cached_res['input']}")
                    st.warning(cached_res['feedback'])

                # 새로운 입력 처리
                if user_input:
                    # 정답 체크
                    if user_input.lower().replace(".","").strip() == q['en'].lower().replace(".","").strip():
                        st.session_state.practice_results[result_key] = {'status': 'PASS', 'input': user_input}
                        st.rerun()
                    else:
                        with st.spinner("채점 중..."):
                            res = evaluate_practice(q['en'], user_input)
                        
                        if "PASS" in res:
                            st.session_state.practice_results[result_key] = {'status': 'PASS', 'input': user_input}
                        else:
                            feedback = res.replace("FAIL", "").strip()
                            st.session_state.practice_results[result_key] = {'status': 'FAIL', 'input': user_input, 'feedback': feedback}
                        st.rerun()

# --- Tab 4: 실전 테스트 ---
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
                    if choice == target['ko']:
                        st.success("정답! ⭕")
                    else:
                        st.error(f"오답! 정답은 '{target['ko']}' 입니다.")
                        if target not in qs["wrong_words"]: 
                            qs["wrong_words"].append(target)
                            save_wrong_word_db(user_id, target)
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
                    if inp.strip().lower() == target['en'].lower():
                        st.success("정답! ⭕")
                    else:
                        st.error(f"오답! 정답은 '{target['en']}' 입니다.")
                        if target not in qs["wrong_words"]:
                            qs["wrong_words"].append(target)
                            save_wrong_word_db(user_id, target)
                    time.sleep(0.5)
                    if curr + 1 < total: qs["current_idx"] += 1; st.rerun()
                    else:
                        if qs["wrong_words"]:
                            qs["shuffled_words"] = qs["wrong_words"][:]; qs["wrong_words"] = []; qs["current_idx"] = 0; qs["phase"] = "ready"; qs["loop_count"] += 1; st.warning("🚨 틀린 문제만 다시 도전!"); time.sleep(1); qs["phase"] = "mc"; st.rerun()
                        else: qs["phase"] = "end"; st.rerun()