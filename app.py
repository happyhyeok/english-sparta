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
# 1. 설정 및 초기화
# ==========================================
st.set_page_config(page_title="AI 중학 영어 스파르타", layout="centered")

# [디버깅] 라이브러리 버전 확인 (화면 맨 위에 표시됨)
try:
    st.caption(f"🔧 Google Generative AI Library Version: {genai.__version__}")
except:
    st.caption("🔧 Version check failed")

# Secrets에서 키 가져오기
try:
    openai_api_key = st.secrets["OPENAI_API_KEY"]
    supabase_url = st.secrets["SUPABASE_URL"]
    supabase_key = st.secrets["SUPABASE_KEY"]
    google_api_key = st.secrets["GOOGLE_API_KEY"]
except Exception as e:
    st.error(f"❌ API 키 설정 오류: {e}")
    st.stop()

# 클라이언트 초기화
client = OpenAI(api_key=openai_api_key)
supabase: Client = create_client(supabase_url, supabase_key)
genai.configure(api_key=google_api_key)

# 세션 상태 초기화
if "user_level" not in st.session_state: st.session_state.user_level = None 
if "mission" not in st.session_state: st.session_state.mission = None
if "step" not in st.session_state: st.session_state.step = "learning"
if "word_audios" not in st.session_state: st.session_state.word_audios = {}
if "quiz_state" not in st.session_state:
    st.session_state.quiz_state = {
        "phase": "ready", 
        "current_idx": 0, 
        "shuffled_words": [], 
        "wrong_words": [], 
        "loop_count": 1,
        "current_options": None
    }

# ==========================================
# 2. Supabase DB 관리 함수
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
    last_visit_str = user.get("last_visit_date")
    current_streak = user.get("streak", 0)
    msg = ""
    new_streak = current_streak
    
    if last_visit_str == today_str:
        msg = f"오늘도 오셨군요! 현재 {current_streak}일 연속 학습 중입니다. 🔥"
    else:
        if last_visit_str:
            last_date = datetime.date.fromisoformat(last_visit_str)
            delta = (date.today() - last_date).days
            if delta == 1:
                new_streak += 1
                msg = f"대단해요! {new_streak}일째 연속 출석 중입니다! 🚀"
            else:
                new_streak = 1
                msg = f"앗! {delta-1}일 결석하여 스트릭이 초기화되었습니다 ㅠㅠ 다시 시작해봐요! 💪"
        else:
            new_streak = 1
            msg = "환영합니다! 오늘부터 1일! 🎉"
        supabase.table("users").update({ "last_visit_date": today_str, "streak": new_streak }).eq("user_id", user_id).execute()
    return new_streak, msg

def complete_daily_mission(user_id):
    user = get_user_data(user_id)
    new_count = user.get("total_complete_count", 0) + 1
    supabase.table("users").update({"total_complete_count": new_count}).eq("user_id", user_id).execute()
    supabase.table("study_logs").insert({ "user_id": user_id, "study_date": date.today().isoformat(), "completed_at": datetime.datetime.now().isoformat() }).execute()

def save_wrong_word_db(user_id, word_obj):
    res = supabase.table("wrong_words").select("*").eq("user_id", user_id).eq("word", word_obj['en']).execute()
    if res.data:
        row_id = res.data[0]['id']
        new_cnt = res.data[0]['wrong_count'] + 1
        supabase.table("wrong_words").update({"wrong_count": new_cnt}).eq("id", row_id).execute()
    else:
        supabase.table("wrong_words").insert({ "user_id": user_id, "word": word_obj['en'], "meaning": word_obj['ko'], "wrong_count": 1 }).execute()
        
def update_level_and_test_log(user_id, new_level):
    user = get_user_data(user_id)
    current_total = user.get("total_complete_count", 0)
    supabase.table("users").update({ "current_level": new_level, "last_test_count": current_total }).eq("user_id", user_id).execute()


# ==========================================
# 3. AI 및 유틸리티 함수
# ==========================================
def set_focus_js():
    components.html(
        """<script>setTimeout(function() { var inputs = window.parent.document.querySelectorAll("input[type=text]"); if (inputs.length > 0) { inputs[inputs.length - 1].focus(); } }, 100);</script>""",
        height=0,
    )

def generate_tts(text):
    try:
        response = client.audio.speech.create(model="tts-1", voice="alloy", input=text)
        return response.content
    except: return None

def run_level_test_ai(text):
    prompt = "학생의 영어 답변을 보고 실력을 'Low', 'Mid', 'High' 중 하나로만 평가하세요."
    res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system", "content":prompt}, {"role":"user", "content":text}])
    return res.choices[0].message.content.strip()

# [수정] 모델명 변경 (latest) 및 디버깅 메시지 추가
def generate_curriculum(level):
    try:
        # 모델명을 '-latest' 붙여서 시도
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash-latest",
            generation_config={"response_mime_type": "application/json"}
        )
        
        prompt = f"""
        You are an English education expert for Korean middle school students.
        Create a JSON curriculum for level '{level}'.
        
        Output JSON Schema:
        {{
            "topic": "English Topic Name",
            "grammar": {{
                "title": "문법 제목 (한국어)",
                "description": "문법 상세 설명 (한국어). Why & How 포함.",
                "rule": "Rule (English)",
                "example": "Example (English)"
            }},
            "words": [ {{ "en": "English Word", "ko": "한국어 뜻" }} ],
            "practice_sentences": [ {{ "ko": "한글 문장", "en": "English Sentence", "hint_structure": "구조 힌트", "hint_grammar": "문법 힌트" }} ]
        }}
        Create exactly 20 words and 20 sentences.
        """
        
        response = model.generate_content(prompt)
        return json.loads(response.text)
        
    except Exception as e:
        st.error(f"⚠️ Gemini API Error Details: {str(e)}")
        # 만약 최신 모델도 안되면 구형 모델로 폴백 시도 (임시 방편)
        try:
            st.warning("⚠️ 최신 모델 실패. 기본 모델(gemini-pro)로 재시도합니다...")
            model_fallback = genai.GenerativeModel("gemini-pro")
            response = model_fallback.generate_content(prompt + "\nResponse must be valid JSON string.")
            # gemini-pro는 json 모드가 약하므로 텍스트 파싱 시도
            txt = response.text
            if "```json" in txt:
                txt = txt.split("```json")[1].split("```")[0]
            elif "```" in txt:
                txt = txt.split("```")[1].split("```")[0]
            return json.loads(txt)
        except Exception as e2:
            st.error(f"❌ Fallback failed: {str(e2)}")
            return None

def transcribe_audio(audio_bytes):
    import io
    f = io.BytesIO(audio_bytes)
    f.name = "input.wav"
    return client.audio.transcriptions.create(model="whisper-1", file=f).text

def evaluate_practice(target, user_input):
    prompt = f"목표: '{target}', 답변: '{user_input}'. 의미 일치 시 PASS, 아니면 FAIL. FAIL시 구체적 피드백(한글, 관사/시제/수일치 등 포함)."
    res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system", "content":prompt}])
    return res.choices[0].message.content

# ==========================================
# 4. 화면 구성
# ==========================================
st.title("🏫 AI 중학 영어 스파르타")

with st.sidebar:
    st.header("🔑 로그인")
    user_id = st.text_input("아이디(ID)", value="student1")
    if user_id:
        streak, msg = update_attendance(user_id)
        user_data = get_user_data(user_id)
        st.divider()
        st.metric("🔥 연속 학습", f"{streak}일")
        if "초기화" in msg: st.error(msg)
        else: st.success(msg)
        st.info(f"🏆 누적 완료: {user_data.get('total_complete_count', 0)}회")
    else:
        st.warning("아이디를 입력해주세요.")
        st.stop()

current_level = user_data.get('current_level')
total_complete = user_data.get('total_complete_count', 0)
last_test_cnt = user_data.get('last_test_count', 0)
should_test = (current_level is None) or ((total_complete - last_test_cnt) >= 5)

if should_test:
    st.subheader("📝 레벨 테스트")
    st.write("Q. What do you usually do on weekends?")
    aud = audio_recorder(text="", key="lvl_rec", neutral_color="#6aa36f", recording_color="#e8b62c")
    if aud:
        txt = transcribe_audio(aud)
        st.write(f"답변: {txt}")
        if len(txt) > 1:
            lvl = run_level_test_ai(txt)
            update_level_and_test_log(user_id, lvl)
            st.success(f"레벨: {lvl}")
            time.sleep(1)
            st.rerun()

elif current_level:
    st.session_state.user_level = current_level
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

    if st.session_state.step == "learning":
        st.markdown("### 📖 Step 1. 오늘의 학습")
        if not st.session_state.word_audios:
            pb = st.progress(0, "발음 준비 중...")
            total = len(mission['words'])
            for i, w in enumerate(mission['words']):
                st.session_state.word_audios[i] = generate_tts(w['en'])
                pb.progress(min((i+1)/total, 1.0))
            pb.empty()
            
        st.info(f"📘 {mission['grammar']['title']}\n\n{mission['grammar']['description']}")
        
        for i, w in enumerate(mission['words']):
            c1, c2 = st.columns([4,1])
            c1.markdown(f"**{i+1}. {w['en']}** ({w['ko']})")
            if i in st.session_state.word_audios: c2.audio(st.session_state.word_audios[i], format='audio/mp3')

        if st.button("연습하러 가기 👉", type="primary"):
            st.session_state.step = "practice"
            st.rerun()

    elif st.session_state.step == "practice":
        st.markdown("### ✍️ Step 2. 문장 만들기")
        for idx, q in enumerate(mission['practice_sentences']):
            st.divider()
            st.markdown(f"**Q{idx+1}. {q['ko']}**")
            with st.expander("힌트"): st.write(f"{q.get('hint_structure','')} / {q.get('hint_grammar','')}")
            
            c1, c2 = st.columns([1,2])
            user_res = None
            with c1:
                aud = audio_recorder(text="", key=f"p_rec_{idx}")
                if aud: user_res = transcribe_audio(aud)
            with c2:
                with st.form(f"p_form_{idx}", clear_on_submit=True):
                    inp = st.text_input("입력", key=f"p_inp_{idx}")
                    if st.form_submit_button("제출"): user_res = inp
            
            if user_res:
                st.write(f"답안: {user_res}")
                if user_res.lower().strip().replace(".","") == q['en'].lower().strip().replace(".",""):
                    st.success("정답!")
                else:
                    with st.spinner("채점 중..."):
                        res = evaluate_practice(q['en'], user_res)
                    if "PASS" in res: st.success("통과!")
                    else: st.warning(res.replace("FAIL","").strip())

        if st.button("실전 퀴즈 도전 ⚔️", type="primary"):
            st.session_state.step = "drill"
            st.session_state.quiz_state = { "phase": "ready", "current_idx": 0, "shuffled_words": random.sample(mission['words'], 20), "wrong_words": [], "loop_count": 1, "current_options": None }
            st.rerun()

    elif st.session_state.step == "drill":
        qs = st.session_state.quiz_state
        words = qs["shuffled_words"]
        total = len(words)
        st.markdown(f"### ⚔️ Step 3. 실전 ({qs['loop_count']}회차)")
        
        if qs["phase"] == "ready":
            st.info(f"문제 수: {total}개")
            if st.button("시작"): qs["phase"] = "mc"; qs["current_options"] = None; st.rerun()
                
        elif qs["phase"] == "mc":
            target = words[qs["current_idx"]]
            st.subheader(f"객관식: {target['en']}")
            if not qs["current_options"]:
                opts = [target['ko']]
                while len(opts) < 4:
                    r = random.choice(mission['words'])['ko']
                    if r not in opts: opts.append(r)
                random.shuffle(opts)
                qs["current_options"] = opts
            else: opts = qs["current_options"]
            
            with st.form(f"mc_{qs['loop_count']}_{qs['current_idx']}"):
                sel = st.radio("뜻 선택", opts)
                if st.form_submit_button("확인"):
                    if sel == target['ko']: st.success("정답")
                    else:
                        st.error("오답")
                        if target not in qs["wrong_words"]: 
                            qs["wrong_words"].append(target)
                            save_wrong_word_db(user_id, target)
                    time.sleep(0.5)
                    qs["current_options"] = None
                    if qs["current_idx"]+1 < total: qs["current_idx"] += 1; st.rerun()
                    else: qs["phase"] = "writing"; qs["current_idx"] = 0; random.shuffle(qs["shuffled_words"]); st.rerun()

        elif qs["phase"] == "writing":
            set_focus_js()
            target = qs["shuffled_words"][qs["current_idx"]]
            st.subheader(f"주관식: {target['ko']}")
            with st.form(f"wr_{qs['loop_count']}_{qs['current_idx']}", clear_on_submit=True):
                inp = st.text_input("영어 입력")
                if st.form_submit_button("제출"):
                    if inp.strip().lower() == target['en'].lower(): st.success("정답")
                    else:
                        st.error("오답")
                        if target not in qs["wrong_words"]: 
                            qs["wrong_words"].append(target)
                            save_wrong_word_db(user_id, target)
                    time.sleep(0.5)
                    if qs["current_idx"]+1 < total: qs["current_idx"] += 1; st.rerun()
                    else:
                        if qs["wrong_words"]:
                            qs["shuffled_words"] = qs["wrong_words"][:]; qs["wrong_words"] = []; qs["current_idx"] = 0; qs["phase"] = "ready"; qs["loop_count"] += 1; st.rerun()
                        else: qs["phase"] = "end"; st.rerun()

        elif qs["phase"] == "end":
            st.balloons()
            st.success("학습 완료!")
            if st.button("메인으로"):
                complete_daily_mission(user_id)
                for key in ["mission", "step", "word_audios", "quiz_state"]: del st.session_state[key]
                st.rerun()