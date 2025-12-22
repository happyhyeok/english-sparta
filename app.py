import streamlit as st
from openai import OpenAI
from audio_recorder_streamlit import audio_recorder
import streamlit.components.v1 as components
import json
import random
import time

# ==========================================
# 1. 설정 및 초기화
# ==========================================
# ⚠️ [중요] 여기에 발급받은 OpenAI API Key를 입력하세요.

try:
    openai_api_key = st.secrets["OPENAI_API_KEY"]
except Exception:
    # 로컬(내 컴퓨터)에서 테스트할 때를 위한 예외 처리 (secrets.toml이 없을 경우 등)
    openai_api_key = "여기에_키를_넣지_마세요_로컬은_secrets_toml로_관리합니다" 

client = OpenAI(api_key=openai_api_key)

st.set_page_config(page_title="중등 영어 스파르타", layout="centered")

# 세션 상태(Session State) 초기화
if "user_level" not in st.session_state:
    st.session_state.user_level = None 
if "mission" not in st.session_state:
    st.session_state.mission = None
if "step" not in st.session_state:
    st.session_state.step = "learning" # learning -> practice -> drill

# 단어 발음 데이터 저장소
if "word_audios" not in st.session_state:
    st.session_state.word_audios = {}

# 퀴즈(드릴) 상태 관리 저장소
if "quiz_state" not in st.session_state:
    st.session_state.quiz_state = {
        "phase": "ready",    # ready -> mc(객관식) -> writing(주관식) -> end
        "current_idx": 0,
        "shuffled_words": [],
        "wrong_words": [],   # 틀린 단어를 모으는 리스트
        "loop_count": 1      # 반복 회차 (1회차, 2회차...)
    }

# ==========================================
# 2. 유틸리티 및 AI 함수
# ==========================================

def set_focus_js():
    """
    [기능] 화면이 로드될 때 텍스트 입력창(text input)에 
    자동으로 커서를 위치시키는 JavaScript를 주입합니다.
    """
    components.html(
        """
        <script>
            var input = window.parent.document.querySelector("input[type=text]");
            if (input) {
                input.focus();
            }
        </script>
        """,
        height=0,
    )

def generate_tts(text):
    """OpenAI TTS 모델을 사용하여 텍스트를 음성으로 변환"""
    try:
        response = client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input=text
        )
        return response.content
    except Exception as e:
        st.error(f"TTS 생성 오류: {e}")
        return None

def run_level_test(user_audio_text):
    """레벨 테스트 결과 분석 (Low/Mid/High)"""
    prompt = """
    당신은 중학교 영어 교사입니다. 학생의 답변을 보고 실력을 평가하세요.
    - Low: 영어를 거의 못하거나 단어만 나열함.
    - Mid: 문장을 만들 수 있으나 문법 오류가 있음.
    - High: 자연스러운 문장 구사 가능.
    결과는 오직 'Low', 'Mid', 'High' 중 하나만 출력하세요.
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"학생 답변: {user_audio_text}"}
        ]
    )
    return response.choices[0].message.content.strip()

def generate_curriculum(level):
    """레벨에 맞는 커리큘럼(단어 20개, 문장 20개) 생성"""
    prompt = f"""
    당신은 대한민국 중학교 영어 교육 전문가입니다. 학생 레벨 '{level}'에 맞는 오늘의 미션을 JSON으로 만드세요.
    
    [필수 요구사항]
    1. **문법 설명은 반드시 100% 한국어로**, 중학생이 이해하기 쉽게 작성하세요.
    2. 단어는 20개이며, 영어 철자와 한국어 뜻을 포함하세요.
    3. **실전 연습 문장(practice_sentences)을 20개** 만드세요.
       - 오늘의 문법과 단어를 활용한 문장이어야 합니다.
       - 'hint_structure': 문장의 구조나 포함될 주요 단어를 제시 (예: 평서문 / I, go, school)
       - 'hint_grammar': 정답을 알려주지 말고 문법적 힌트만 제공 (예: 주어가 3인칭 단수입니다.)
       - 'en': 정답 영어 문장

    형식:
    {{
        "topic": "주제",
        "grammar": {{"title": "문법 제목", "description": "설명", "rule": "공식", "example": "예문"}},
        "words": [ {{"en": "apple", "ko": "사과"}}, ... ],
        "practice_sentences": [
            {{
                "ko": "나는 학교에 갑니다.", 
                "en": "I go to school.",
                "hint_structure": "...",
                "hint_grammar": "..."
            }},
            ... (총 20개)
        ]
    }}
    오직 JSON 데이터만 출력하세요.
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": prompt}],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

def transcribe_audio(audio_bytes):
    """Whisper 모델을 사용하여 음성을 텍스트로 변환"""
    import io
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "input.wav"
    transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
    return transcript.text

def evaluate_practice(target_sentence, user_input):
    """
    연습 문제 채점 및 피드백 생성
    틀렸을 경우 '왜 틀렸는지'를 상세히 설명하도록 요청
    """
    prompt = f"""
    목표 문장: "{target_sentence}"
    학생 답안: "{user_input}"
    
    당신은 친절한 중학교 영어 선생님입니다.
    1. 의미와 문법이 90% 이상 일치하면 맨 첫 줄에 'PASS'라고 적으세요.
    2. 틀렸다면 맨 첫 줄에 'FAIL'이라고 적으세요.
    3. **FAIL인 경우, 반드시 한국어로 구체적인 피드백을 주세요.**
       - 어떤 부분이 틀렸는지 (시제, 철자, 단어 선택 등) 설명하세요.
       - 정답 문장을 한 번 더 보여주세요.
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": prompt}]
    )
    return response.choices[0].message.content

# ==========================================
# 3. 화면 구성 (UI)
# ==========================================

st.title("🏫 AI 중학 영어 스파르타")

# [Phase 0] 레벨 테스트 (친절한 안내 적용)
if st.session_state.user_level is None:
    st.subheader("📝 레벨 테스트")
    st.info("AI 선생님의 질문을 듣고 편안하게 대답해 보세요.")
    
    question_text = "What do you usually do on weekends?"
    
    # 질문 섹션
    st.markdown(f"### 🎙️ Q. {question_text}")
    st.caption("해석: 주말에 보통 무엇을 하시나요?") # 한글 해석 추가
    
    # 팁 제공 (심리적 장벽 낮추기)
    with st.expander("💡 답변 팁 보기 (클릭)", expanded=True):
        st.markdown("""
        - 완벽한 문장이 아니어도 괜찮아요.
        - **"Game"**, **"Sleep"** 처럼 **단어만 말해도 됩니다!**
        - 편안하게 녹음 버튼을 눌러주세요.
        """)

    # TTS 재생
    if "level_test_audio" not in st.session_state:
        st.session_state.level_test_audio = generate_tts(question_text)
    if st.session_state.level_test_audio:
        st.audio(st.session_state.level_test_audio, format="audio/mp3")

    # 녹음기
    audio_bytes = audio_recorder(text="", recording_color="#e8b62c", neutral_color="#6aa36f", key="level_rec")
    
    if audio_bytes:
        with st.spinner("AI가 분석 중입니다..."):
            text = transcribe_audio(audio_bytes)
            st.write(f"🗣️ 당신의 답변: **{text}**")
            
            if len(text) < 2:
                st.warning("잘 안 들렸어요. 다시 한 번 말씀해 주세요! (단어 하나라도 좋아요)")
            else:
                level = run_level_test(text)
                st.session_state.user_level = level
                st.balloons()
                st.success(f"분석 완료! 당신에게 딱 맞는 **[{level}]** 코스를 준비했습니다.")
                time.sleep(2)
                st.rerun()

# [Phase 1~3] 메인 학습 루틴
else:
    # 미션 데이터 생성 (없을 경우)
    if st.session_state.mission is None:
        with st.spinner(f"Lv.{st.session_state.user_level} 맞춤 커리큘럼 생성 중..."):
            st.session_state.mission = generate_curriculum(st.session_state.user_level)
    
    mission = st.session_state.mission
    st.header(f"Topic: {mission['topic']}")

    # ===============================================
    # Step 1. 학습 모드 (Learning)
    # ===============================================
    if st.session_state.step == "learning":
        st.markdown("### 📖 Step 1. 오늘의 학습")
        
        # [수정] 발음 파일 미리 생성 (클릭 시 1회 재생용)
        if not st.session_state.word_audios:
            progress_bar = st.progress(0, text="발음 파일을 준비 중입니다...")
            total_words = len(mission['words'])
            
            for i, word in enumerate(mission['words']):
                # 반복 없이 단어만 1회 생성
                audio_data = generate_tts(word['en'])
                st.session_state.word_audios[i] = audio_data
                progress_bar.progress((i + 1) / total_words)
            
            progress_bar.empty()
            st.toast("학습 준비 완료! 🎧")

        # 문법 카드
        with st.container(border=True):
            st.subheader(f"📘 문법: {mission['grammar']['title']}")
            st.markdown(f"{mission['grammar']['description']}")
            st.info(f"**규칙:** {mission['grammar'].get('rule', '')}")
            st.markdown(f"**예시:** *{mission['grammar']['example']}*")

        # 단어 리스트
        st.subheader("🔥 필수 단어 20")
        for i, word in enumerate(mission['words']):
            col_text, col_btn = st.columns([4, 1])
            with col_text:
                st.markdown(f"**{i+1}. {word['en']}** ({word['ko']})")
            with col_btn:
                # 미리 생성된 오디오 재생
                if i in st.session_state.word_audios:
                    st.audio(st.session_state.word_audios[i], format="audio/mp3")
        
        st.divider()
        if st.button("문장 만들기 연습하러 가기 👉", type="primary"):
            st.session_state.step = "practice"
            st.rerun()

    # ===============================================
    # Step 2. 문장 만들기 연습 (Guided Practice)
    # ===============================================
    elif st.session_state.step == "practice":
        st.markdown("### ✍️ Step 2. 문장 만들기 연습 (20문항)")
        
        # 상단 문법 리마인드
        grammar = mission['grammar']
        with st.container(border=True):
            st.markdown(f"**💡 핵심 문법:** {grammar['title']}")
            st.caption(f"공식: {grammar.get('rule', '')}")

        sentences = mission['practice_sentences']
        for idx, q in enumerate(sentences):
            st.divider()
            st.markdown(f"**Q{idx+1}. {q['ko']}**")
            
            with st.expander("🕵️ 힌트 보기"):
                st.markdown(f"- **구조:** {q.get('hint_structure','')}")
                st.markdown(f"- **문법:** {q.get('hint_grammar','')}")
            
            col_mic, col_kbd = st.columns([1, 2])
            user_response = None
            
            # 1. 음성 입력
            with col_mic:
                audio_bytes = audio_recorder(text="", key=f"rec_{idx}", icon_size="lg")
                if audio_bytes: 
                    user_response = transcribe_audio(audio_bytes)
            
            # 2. 텍스트 입력
            with col_kbd:
                with st.form(key=f"form_{idx}"):
                    txt = st.text_input("정답 입력", key=f"txt_{idx}")
                    if st.form_submit_button("제출") and txt: 
                        user_response = txt

            # 채점 및 피드백 로직
            if user_response:
                st.write(f"📝 **내 답안:** {user_response}")
                
                # 1차: 단순 문자열 비교 (정확도 100%인 경우)
                if user_response.lower().replace(".","").strip() == q['en'].lower().replace(".","").strip():
                     st.success("완벽합니다! 정답입니다. 🎉")
                else:
                    # 2차: AI 정밀 채점
                    with st.spinner("AI 선생님이 채점 중입니다..."):
                        res = evaluate_practice(q['en'], user_response)
                    
                    if res.startswith("PASS"):
                        st.success("통과! 잘 하셨어요. 👍")
                        st.caption(res.replace("PASS", "").strip())
                    else:
                        st.error("틀렸습니다. ❌")
                        # [요청 반영] 틀린 이유를 상세히 출력
                        feedback_msg = res.replace("FAIL", "").strip()
                        st.warning(f"💡 **선생님 조언:**\n\n{feedback_msg}")

        st.divider()
        st.markdown("연습을 모두 마쳤나요?")
        if st.button("⚔️ 실전 퀴즈 (Drill) 도전하기", type="primary"):
            st.session_state.step = "drill"
            # 퀴즈 상태 초기화
            st.session_state.quiz_state = {
                "phase": "ready",
                "current_idx": 0,
                "shuffled_words": random.sample(mission['words'], len(mission['words'])), # 처음엔 전체 단어
                "wrong_words": [],
                "loop_count": 1
            }
            st.rerun()

    # ===============================================
    # Step 3. 실전 드릴 (무한 오답 루프 시스템)
    # ===============================================
    elif st.session_state.step == "drill":
        quiz_data = st.session_state.quiz_state
        words_list = quiz_data["shuffled_words"]
        total_q = len(words_list)
        
        st.markdown(f"### ⚔️ Step 3. 실전 테스트 (Loop {quiz_data['loop_count']})")
        
        # [Phase: Ready] 준비 화면
        if quiz_data["phase"] == "ready":
            st.info(f"이번 라운드 도전 단어: **{total_q}개**")
            
            if quiz_data['loop_count'] > 1:
                st.error(f"🚨 틀린 단어들만 모아서 다시 퀴즈를 봅니다! (재도전 {quiz_data['loop_count']}회차)")
            else:
                st.markdown("객관식 문제(뜻 맞추기)와 주관식 문제(철자 쓰기)가 이어집니다.")

            if st.button("테스트 시작! (Start)"):
                quiz_data["phase"] = "mc"
                st.rerun()

        # [Phase 1: 객관식 퀴즈]
        elif quiz_data["phase"] == "mc":
            st.subheader(f"Round 1. 객관식 ({quiz_data['current_idx'] + 1}/{total_q})")
            
            target_word = words_list[quiz_data["current_idx"]]
            st.markdown(f"## 🔤 **{target_word['en']}**")
            
            # 보기 생성 로직
            correct_ans = target_word['ko']
            all_meanings = [w['ko'] for w in mission['words']]
            distractors = [m for m in all_meanings if m != correct_ans]
            # 보기가 부족할 경우를 대비해 확장
            if len(distractors) < 3: distractors = distractors * 3 
            
            opts = random.sample(distractors, 3) + [correct_ans]
            random.shuffle(opts)

            with st.form(key=f"mc_{quiz_data['loop_count']}_{quiz_data['current_idx']}"):
                choice = st.radio("알맞은 뜻을 고르세요:", opts)
                submit = st.form_submit_button("확인")
                
                if submit:
                    if choice == correct_ans:
                        st.success("정답! ⭕")
                    else:
                        st.error(f"땡! ❌ (정답: {correct_ans})")
                        # 틀린 단어 리스트에 추가 (중복 방지)
                        if target_word not in quiz_data["wrong_words"]:
                            quiz_data["wrong_words"].append(target_word)

                    time.sleep(0.8) # 결과 확인용 딜레이
                    
                    # 다음 문제 or 다음 단계 이동
                    if quiz_data["current_idx"] + 1 < total_q:
                        quiz_data["current_idx"] += 1
                        st.rerun()
                    else:
                        # 객관식 종료 -> 주관식 준비
                        quiz_data["phase"] = "writing"
                        quiz_data["current_idx"] = 0
                        # 주관식에서는 순서를 한 번 더 섞어줌
                        random.shuffle(quiz_data["shuffled_words"]) 
                        st.rerun()

        # [Phase 2: 주관식 쓰기 퀴즈]
        elif quiz_data["phase"] == "writing":
            st.subheader(f"Round 2. 철자 쓰기 ({quiz_data['current_idx'] + 1}/{total_q})")
            
            # [요청 반영] 텍스트 박스 자동 포커스
            set_focus_js()

            target_word = quiz_data["shuffled_words"][quiz_data["current_idx"]]
            st.markdown(f"## 🇰🇷 **{target_word['ko']}**")
            st.caption("위 뜻을 가진 영어 단어를 입력하고 엔터(Enter)를 치세요.")

            with st.form(key=f"wr_{quiz_data['loop_count']}_{quiz_data['current_idx']}"):
                # key를 매번 다르게 주어 리셋 효과 & 자동 포커스 타겟팅
                user_input = st.text_input("영어 단어 입력", key=f"input_{quiz_data['loop_count']}_{quiz_data['current_idx']}") 
                submit = st.form_submit_button("제출")
                
                if submit:
                    if user_input.strip().lower() == target_word['en'].lower():
                        st.success("Correct! ⭕")
                    else:
                        st.error(f"Wrong! ❌ (정답: {target_word['en']})")
                        # 틀린 단어 추가
                        if target_word not in quiz_data["wrong_words"]:
                            quiz_data["wrong_words"].append(target_word)

                    time.sleep(0.8)
                    
                    if quiz_data["current_idx"] + 1 < total_q:
                        quiz_data["current_idx"] += 1
                        st.rerun()
                    else:
                        # [핵심 로직] 모든 라운드 종료 후 판단
                        if len(quiz_data["wrong_words"]) > 0:
                            # 틀린 문제가 있으면 -> 해당 단어들로만 구성된 새로운 루프 시작
                            quiz_data["shuffled_words"] = quiz_data["wrong_words"][:] # 복사
                            quiz_data["wrong_words"] = [] # 오답통 초기화
                            quiz_data["current_idx"] = 0
                            quiz_data["phase"] = "ready" # 다시 준비 화면으로
                            quiz_data["loop_count"] += 1
                            st.rerun()
                        else:
                            # 오답이 하나도 없으면 -> 최종 종료
                            quiz_data["phase"] = "end"
                            st.rerun()

        # [Phase: End] 최종 완료 화면
        elif quiz_data["phase"] == "end":
            st.balloons()
            st.title("🏆 미션 클리어!")
            st.success(f"축하합니다! 총 {quiz_data['loop_count']}번의 루프 끝에 모든 단어를 마스터했습니다.")
            
            if st.button("처음부터 다시 학습하기 (Reset All)"):
                st.session_state.clear()
                st.rerun()