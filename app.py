import streamlit as st
import google.generativeai as genai  # 👈 Gemini 라이브러리
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

# Secrets에서 키 가져오기
try:
    openai_api_key = st.secrets["OPENAI_API_KEY"]
    supabase_url = st.secrets["SUPABASE_URL"]
    supabase_key = st.secrets["SUPABASE_KEY"]
    google_api_key = st.secrets["GOOGLE_API_KEY"] # 👈 구글 키 로드
except Exception:
    st.error("❌ API 키 설정이 필요합니다. Streamlit Cloud Secrets에 OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY, GOOGLE_API_KEY가 모두 있는지 확인하세요.")
    st.stop()

# 클라이언트 초기화
client = OpenAI(api_key=openai_api_key)
supabase: Client = create_client(supabase_url, supabase_key)
genai.configure(api_key=google_api_key) # 👈 Gemini 설정

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
        "current_options": None # 퀴즈 보기 고정용
    }

# ==========================================
# 2. Supabase DB 관리 함수
# ==========================================

def get_user_data(user_id):
    """사용자 정보 가져오기"""
    response = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if response.data:
        return response.data[0]
    return None

def create_new_user(user_id):
    """신규 사용자 생성"""
    data = {
        "user_id": user_id,
        "current_level": None,
        "total_complete_count": 0,
        "last_test_count": 0,
        "streak": 0,
        "last_visit_date": None
    }
    supabase.table("users").insert(data).execute()

def update_attendance(user_id):
    """출석 체크 및 Streak 로직"""
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
            
        supabase.table("users").update({
            "last_visit_date": today_str,
            "streak": new_streak
        }).eq("user_id", user_id).execute()
        
    return new_streak, msg

def update_level_and_test_log(user_id, new_level):
    """레벨 테스트 후 결과 저장"""
    user = get_user_data(user_id)
    current_total = user.get("total_complete_count", 0)
    
    supabase.table("users").update({
        "current_level": new_level,
        "last_test_count": current_total
    }).eq("user_id", user_id).execute()

def complete_daily_mission(user_id):
    """학습 완료 처리 (+1 카운트)"""
    user = get_user_data(user_id)
    new_count = user.get("total_complete_count", 0) + 1
    supabase.table("users").update({"total_complete_count": new_count}).eq("user_id", user_id).execute()
    
    supabase.table("study_logs").insert({
        "user_id": user_id,
        "study_date": date.today().isoformat(),
        "completed_at": datetime.datetime.now().isoformat()
    }).execute()

def save_wrong_word_db(user_id, word_obj):
    """틀린 단어 DB 저장"""
    res = supabase.table("wrong_words").select("*").eq("user_id", user_id).eq("word", word_obj['en']).execute()
    
    if res.data:
        row_id = res.data[0]['id']
        new_cnt = res.data[0]['wrong_count'] + 1
        supabase.table("wrong_words").update({"wrong_count": new_cnt}).eq("id", row_id).execute()
    else:
        supabase.table("wrong_words").insert({
            "user_id": user_id,
            "word": word_obj['en'],
            "meaning": word_obj['ko'],
            "wrong_count": 1
        }).execute()

# ==========================================
# 3. AI 및 유틸리티 함수
# ==========================================
def set_focus_js():
    # 마지막 입력창(현재 문제)에 자동으로 포커스를 맞춤
    components.html(
        """
        <script>
            setTimeout(function() {
                var inputs = window.parent.document.querySelectorAll("input[type=text]");
                if (inputs.length > 0) {
                    inputs[inputs.length - 1].focus();
                }
            }, 100); 
        </script>
        """,
        height=0,
    )

def generate_tts(text):
    try:
        response = client.audio.speech.create(model="tts-1", voice="alloy", input=text)
        return response.content
    except: return None

def run_level_test_ai(text):
    prompt = "학생의 영어 답변을 보고 실력을 'Low', 'Mid', 'High' 중 하나로만 평가하세요."
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system", "content":prompt}, {"role":"user", "content":text}]
    )
    return res.choices[0].message.content.strip()

# [핵심 변경] Gemini 1.5 Flash로 커리큘럼 생성 (속도 대폭 개선)
def generate_curriculum(level):
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config={"response_mime_type": "application/json"}
    )
    
    prompt = f"""
    You are an English education expert for Korean middle school students.
    Create a JSON curriculum for level '{level}'.
    
    **Constraints:**
    1. 'topic' must be in English.
    2. **All explanations (grammar title, description, hints) MUST be in Korean.**
    3. Make grammar explanations detailed, easy to understand, and include 'Why' & 'How'.
    
    Output JSON Schema:
    {{
        "topic": "English Topic Name",
        "grammar": {{
            "title": "문법 제목 (한국어)",
            "description": "문법 상세 설명 (한국어). 줄글과 불렛포인트 활용하여 가독성 높게.",
            "rule": "Rule (English)",
            "example": "Example (English)"
        }},
        "words": [
            {{ "en": "English Word", "ko": "한국어 뜻" }}, 
            ... (20 items)
        ],
        "practice_sentences": [
            {{ 
                "ko": "한글 문장", 
                "en": "English Sentence", 
                "hint_structure": "구조 힌트 (한국어)", 
                "hint_grammar": "문법 힌트 (한국어)" 
            }},
            ... (20 items)
        ]
    }}
    
    Create exactly 20 words and 20 sentences.
    """
    
    try:
        response = model.generate_content(prompt)
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini Error: {e}")
        return None

def transcribe_audio(audio_bytes):
    import io
    f = io.BytesIO(audio_bytes)
    f.name = "input.wav"
    return client.audio.transcriptions.create(model="whisper-1", file=f).text

def evaluate_practice(target, user_input):
    # 채점 및 피드백은 OpenAI 유지 (디테일한 피드백 강점)
    prompt = f"""
    목표 문장: '{target}'
    학생 답변: '{user_input}'
    
    역할: 꼼꼼하고 친절한 영어 선생님.
    
    채점 기준:
    1. 의미가 통하면 'PASS' (사소한 오타 허용).
    2. 문법 오류나 의미 불일치는 'FAIL'.
    
    **FAIL 시 피드백 지침 (한국어):**
    - 단순히 정답만 주지 말고 이유를 설명하세요.
    - **관사(a/an/the)**: 사용 이유 설명.
    - **수 일치**: 3인칭 단수 변화 설명.
    - **시제**: 시제 차이 설명.
    
    출력 형식:
    - 정답: PASS
    - 오답: FAIL [구체적인 피드백]
    """
    res = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system", "content":prompt}])
    return res.choices[0].message.content

# ==========================================
# 4. 화면 구성 (메인 로직)
# ==========================================
st.title("🏫 AI 중학 영어 스파르타")

# 사이드바
with st.sidebar:
    st.header("🔑 로그인")
    user_id = st.text_input("아이디(ID)", value="student1")
    
    if user_id:
        streak, msg = update_attendance(user_id)
        user_data = get_user_data(user_id)
        
        st.divider()
        st.metric("🔥 연속 학습", f"{streak}일")
        
        if "초기화" in msg:
            st.error(msg)
        else:
            st.success(msg)
            
        total_cnt = user_data.get('total_complete_count', 0)
        st.info(f"🏆 누적 완료: {total_cnt}회")
        
    else:
        st.warning("아이디를 입력해주세요.")
        st.stop()

# ==========================================
# 레벨 테스트 여부 판단
# ==========================================
should_test = False
current_level = user_data.get('current_level')
total_complete = user_data.get('total_complete_count', 0)
last_test_cnt = user_data.get('last_test_count', 0)

# 1. 신규 유저
if current_level is None:
    should_test = True
    st.info("👋 처음 오셨군요! 레벨 테스트를 진행합니다.")

# 2. 5회 완료 주기 체크
elif (total_complete - last_test_cnt) >= 5:
    should_test = True
    st.warning(f"📅 학습 {total_complete - last_test_cnt}회 완료! 정기 레벨 점검 시간입니다.")

# ==========================================
# Phase 0: 레벨 테스트
# ==========================================
if should_test:
    st.subheader("📝 레벨 테스트")
    st.markdown("편안하게 답변해주세요. 단어만 말해도 됩니다!")
    
    q_text = "What do you usually do on weekends?"
    st.markdown(f"**Q. {q_text}** (주말에 보통 뭐 해요?)")
    
    with st.expander("💡 답변 팁 보기", expanded=True):
        st.markdown("""
        - **"Game"**, **"Sleep"** 처럼 단어만 말해도 됩니다.
        - 편안하게 녹음 버튼을 눌러주세요.
        """)
    
    if st.button("🔊 질문 듣기"):
        tts = generate_tts(q_text)
        if tts: st.audio(tts, format='audio/mp3')
        
    aud = audio_recorder(text="", key="lvl_rec", neutral_color="#6aa36f", recording_color="#e8b62c")
    if aud:
        with st.spinner("분석 중..."):
            txt = transcribe_audio(aud)
            st.write(f"답변: {txt}")
            if len(txt) < 2:
                st.warning("잘 안 들렸어요.")
            else:
                lvl = run_level_test_ai(txt)
                update_level_and_test_log(user_id, lvl)
                st.balloons()
                st.success(f"결과: **[{lvl}]** 레벨로 설정되었습니다!")
                time.sleep(2)
                st.rerun()

# ==========================================
# Phase 1~3: 메인 학습
# ==========================================
elif current_level:
    st.session_state.user_level = current_level
    
    # 미션 생성 (Gemini 사용으로 속도 개선)
    if not st.session_state.mission:
        with st.status("🚀 오늘의 미션을 생성하고 있습니다... (Gemini)", expanded=True) as status:
            st.write("AI 선생님이 커리큘럼을 짜고 있어요...")
            mission_data = generate_curriculum(current_level)
            
            if mission_data:
                st.session_state.mission = mission_data
                status.update(label="준비 완료!", state="complete", expanded=False)
            else:
                status.update(label="오류 발생", state="error")
                st.error("⚠️ 커리큘럼 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
                st.stop()
            
    mission = st.session_state.mission
    st.header(f"Topic: {mission['topic']}")
    st.caption(f"Level: {current_level}")

    # Step 1. Learning
    if st.session_state.step == "learning":
        st.markdown("### 📖 Step 1. 오늘의 학습")
        
        if not st.session_state.word_audios:
            pb = st.progress(0, "발음 준비 중...")
            total_words = len(mission['words'])
            
            for i, w in enumerate(mission['words']):
                st.session_state.word_audios[i] = generate_tts(w['en'])
                # 프로그레스 바 에러 방지 로직
                prog_val = (i + 1) / total_words
                if prog_val > 1.0: prog_val = 1.0
                pb.progress(prog_val)
            pb.empty()
            
        with st.container(border=True):
            gr = mission['grammar']
            st.subheader(f"📘 {gr['title']}")
            st.markdown(gr['description'])
            st.info(f"공식: {gr.get('rule','')}")
            st.markdown(f"예시: *{gr['example']}*")
        
        # 매직 커맨드 에러 방지를 위해 st.columns 객체 직접 사용
        for i, w in enumerate(mission['words']):
            c1, c2 = st.columns([4,1])
            c1.markdown(f"**{i+1}. {w['en']}** ({w['ko']})")
            
            if i in st.session_state.word_audios:
                c2.audio(st.session_state.word_audios[i], format='audio/mp3')

        if st.button("연습하러 가기 👉", type="primary"):
            st.session_state.step = "practice"
            st.rerun()

    # Step 2. Practice
    elif st.session_state.step == "practice":
        st.markdown("### ✍️ Step 2. 문장 만들기")
        
        with st.container(border=True):
            gr = mission['grammar']
            st.markdown(f"**핵심 문법:** {gr['title']}")
            st.caption(gr.get('rule', ''))

        for idx, q in enumerate(mission['practice_sentences']):
            st.divider()
            st.markdown(f"**Q{idx+1}. {q['ko']}**")
            with st.expander("힌트"):
                st.write(f"{q.get('hint_structure','')} / {q.get('hint_grammar','')}")
                
            c_mic, c_txt = st.columns([1,2])
            user_res = None
            with c_mic:
                aud = audio_recorder(text="", key=f"p_rec_{idx}")
                if aud: user_res = transcribe_audio(aud)
            with c_txt:
                # 제출 시 입력창 비우기 (clear_on_submit=True)
                with st.form(f"p_form_{idx}", clear_on_submit=True):
                    inp = st.text_input("입력", key=f"p_inp_{idx}")
                    if st.form_submit_button("제출"): user_res = inp
            
            if user_res:
                st.write(f"답안: {user_res}")
                if user_res.lower().replace(".","").strip() == q['en'].lower().replace(".","").strip():
                    st.success("정답! 🎉")
                else:
                    with st.spinner("채점 및 상세 분석 중..."):
                        res = evaluate_practice(q['en'], user_res)
                    
                    if "PASS" in res:
                        st.success("통과! 👍")
                        st.caption(res.replace("PASS",""))
                    else:
                        st.error("오답 ❌")
                        st.warning(res.replace("FAIL","").strip())
                        
        if st.button("실전 퀴즈 도전 ⚔️", type="primary"):
            st.session_state.step = "drill"
            st.session_state.quiz_state = {
                "phase": "ready", "current_idx": 0,
                "shuffled_words": random.sample(mission['words'], 20),
                "wrong_words": [], "loop_count": 1,
                "current_options": None # 보기 고정용 초기화
            }
            st.rerun()

    # Step 3. Drill
    elif st.session_state.step == "drill":
        qs = st.session_state.quiz_state
        words = qs["shuffled_words"]
        total = len(words)
        
        st.markdown(f"### ⚔️ Step 3. 실전 테스트 ({qs['loop_count']}회차)")
        
        if qs["phase"] == "ready":
            st.info(f"문제 수: {total}개")
            if qs['loop_count'] > 1: st.error("틀린 문제 재도전!")
            if st.button("시작"):
                qs["phase"] = "mc"
                qs["current_options"] = None
                st.rerun()
                
        elif qs["phase"] == "mc":
            st.subheader(f"객관식 ({qs['current_idx']+1}/{total})")
            target = words[qs["current_idx"]]
            st.markdown(f"## {target['en']}")
            
            # [보기 고정 로직] 이미 생성된 보기가 없으면 새로 생성 후 저장
            if qs.get("current_options") is None:
                opts = [target['ko']]
                while len(opts) < 4:
                    r = random.choice(mission['words'])['ko']
                    if r not in opts: opts.append(r)
                random.shuffle(opts)
                qs["current_options"] = opts
            else:
                opts = qs["current_options"]
            
            with st.form(f"mc_{qs['loop_count']}_{qs['current_idx']}"):
                sel = st.radio("뜻 선택", opts)
                if st.form_submit_button("확인"):
                    if sel == target['ko']: st.success("정답 ⭕")
                    else:
                        st.error("오답 ❌")
                        if target not in qs["wrong_words"]: 
                            qs["wrong_words"].append(target)
                            save_wrong_word_db(user_id, target)
                            
                    time.sleep(0.5)
                    qs["current_options"] = None # 다음 문제를 위해 보기 초기화
                    
                    if qs["current_idx"]+1 < total:
                        qs["current_idx"] += 1
                        st.rerun()
                    else:
                        qs["phase"] = "writing"
                        qs["current_idx"] = 0
                        random.shuffle(qs["shuffled_words"])
                        st.rerun()
                        
        elif qs["phase"] == "writing":
            st.subheader(f"주관식 ({qs['current_idx']+1}/{total})")
            
            # 입력창 자동 포커스 (JS)
            set_focus_js()
            
            target = qs["shuffled_words"][qs["current_idx"]]
            st.markdown(f"## {target['ko']}")
            
            with st.form(f"wr_{qs['loop_count']}_{qs['current_idx']}", clear_on_submit=True):
                inp = st.text_input("영어 단어 입력")
                if st.form_submit_button("제출"):
                    if inp.strip().lower() == target['en'].lower(): st.success("정답 ⭕")
                    else:
                        st.error("오답 ❌")
                        if target not in qs["wrong_words"]:
                            qs["wrong_words"].append(target)
                            save_wrong_word_db(user_id, target)
                            
                    time.sleep(0.5)
                    if qs["current_idx"]+1 < total:
                        qs["current_idx"] += 1
                        st.rerun()
                    else:
                        if qs["wrong_words"]:
                            qs["shuffled_words"] = qs["wrong_words"][:]
                            qs["wrong_words"] = []
                            qs["current_idx"] = 0
                            qs["phase"] = "ready"
                            qs["loop_count"] += 1
                            st.rerun()
                        else:
                            qs["phase"] = "end"
                            st.rerun()
                            
        elif qs["phase"] == "end":
            st.balloons()
            st.success("🎉 오늘의 학습 완료!")
            
            if st.button("완료 및 메인으로"):
                complete_daily_mission(user_id)
                # 로그아웃 방지: 특정 키만 삭제
                for key in ["mission", "step", "word_audios", "quiz_state"]:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()