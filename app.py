import streamlit as st
import yt_dlp
import google.generativeai as genai
import os
import json
import time
import subprocess
import shutil
import glob # 파일 찾기용 모듈 추가

# --- 설정 ---
DOWNLOAD_FOLDER = "downloads"
OUTPUT_FOLDER = "outputs"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# --- 함수 정의 ---

def download_video(url, cookie_path=None):
    """
    YouTube URL에서 비디오 정보를 추출하고 다운로드합니다.
    """
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(id)s.%(ext)s'),
        'noplaylist': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'ignoreerrors': True,
        'no_warnings': True,
        # [김지연 3.9] 최종 결과물을 무조건 mp4로 병합하여 확장자 혼동 방지
        'merge_output_format': 'mp4',
    }
    
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)
        if not info_dict:
            raise ValueError("영상 정보를 가져올 수 없습니다.")
            
        video_id = info_dict.get('id', 'unknown')
        video_title = info_dict.get('title', 'video')
        
        # [김지연 3.9 수정] 파일 찾기 로직 강화
        # 1. 예상되는 파일 경로 (MP4 강제 옵션 덕분에 mp4일 확률 높음)
        expected_filename = f"{video_id}.mp4"
        video_path = os.path.join(DOWNLOAD_FOLDER, expected_filename)
        
        # 2. 만약 바로 안 보이면 glob으로 다시 검색 (혹시 mkv 등으로 저장되었을 경우 대비)
        if not os.path.exists(video_path):
            search_pattern = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.*")
            found_files = glob.glob(search_pattern)
            
            # .part 파일(다운로드 중인 임시파일)은 제외
            valid_files = [f for f in found_files if not f.endswith('.part')]
            
            if valid_files:
                video_path = valid_files[0]
        
        # 절대 경로 변환
        video_path = os.path.abspath(video_path)
        
        # 3. 최종 확인 및 디버깅 정보 제공
        if not os.path.exists(video_path):
             # 현재 폴더에 무슨 파일들이 있는지 확인 (디버깅용)
             try:
                 files_in_dir = os.listdir(DOWNLOAD_FOLDER)
             except:
                 files_in_dir = "폴더 조회 불가"
                 
             raise FileNotFoundError(f"파일을 찾을 수 없습니다: {video_path}\n📂 현재 다운로드 폴더 파일 목록: {files_in_dir}")

    return video_path, video_title, video_id

def analyze_video_points(api_key, video_path, user_prompt):
    try:
        genai.configure(api_key=api_key)
        video_file = genai.upload_file(path=video_path)
        while video_file.state.name == "PROCESSING":
            time.sleep(2)
            video_file = genai.get_file(video_file.name)
        
        if video_file.state.name == "FAILED":
            raise ValueError("Video processing failed.")

        model = genai.GenerativeModel(model_name="gemini-2.5-flash")
        
        system_prompt = """
        당신은 전문 영상 편집자입니다. 요청에 맞춰 적절한 숏폼 구간을 찾으세요.
        [규칙]
        1. 최대 5개 구간 선정.
        2. JSON 리스트 형식 응답.
        3. 시간은 '분:초' (MM:SS).
        """
        
        request = f"사용자 요청: {user_prompt}"
        response = model.generate_content([video_file, system_prompt, request])
        
        text_response = response.text
        start_index = text_response.find('[')
        end_index = text_response.rfind(']') + 1
        
        if start_index == -1: return [{"error": "JSON 파싱 실패"}]
        
        return json.loads(text_response[start_index:end_index])

    except Exception as e:
        return [{"error": str(e)}]

def parse_time_str(time_str):
    try:
        parts = time_str.split(':')
        if len(parts) == 2: return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return 0
    except: return 0

def process_video(input_path, start_sec, end_sec, video_id, index, template_path=None, chroma_key=None, layout_settings=None, video_on_top=True):
    """
    [김지연 3.8] 파일 경로 검증 로직 추가
    """
    output_filename = f"{video_id}_shorts_{index+1}.mp4"
    output_path = os.path.abspath(os.path.join(OUTPUT_FOLDER, output_filename))
    temp_cut_path = os.path.abspath(os.path.join(DOWNLOAD_FOLDER, f"temp_cut_{index}.mp4"))
    
    # 입력 파일 존재 여부 재확인
    if not os.path.exists(input_path):
        st.error(f"❌ 원본 파일을 찾을 수 없습니다: {input_path}")
        return None
    
    if template_path:
        template_path = os.path.abspath(template_path)
    
    scale_pct = layout_settings.get('scale', 100) if layout_settings else 100
    v_offset = layout_settings.get('v_offset', 0) if layout_settings else 0
    
    # --- [1단계] 영상 먼저 자르기 (Input Seeking) ---
    cut_command = [
        "ffmpeg", "-y",
        "-ss", str(start_sec), "-to", str(end_sec),
        "-i", input_path,
        "-c:v", "libx264", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-strict", "experimental",
        temp_cut_path
    ]
    
    try:
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        subprocess.run(cut_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
        st.error(f"❌ 1단계(자르기) 실패:\n{error_msg}")
        return None

    # --- [2단계] 템플릿 합성 ---
    merge_command = ["ffmpeg", "-y", "-i", temp_cut_path]
    
    if template_path:
        merge_command.extend(["-loop", "1", "-i", template_path])
    
    filter_complex = ""
    target_width = int(1080 * (scale_pct / 100))
    if target_width % 2 != 0: target_width -= 1
    
    if template_path:
        if video_on_top:
            # [CASE A] 영상 > 템플릿
            filter_str = (
                f"[1:v]scale=1080:1920,setsar=1[bg];"
                f"[0:v]scale={target_width}:-2,setsar=1[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2+{v_offset}:format=auto:shortest=1,format=yuv420p"
            )
        else:
            # [CASE B] 템플릿 > 영상
            if chroma_key:
                template_filter = f"[1:v]scale=1080:1920,colorkey={chroma_key['color']}:{chroma_key['similarity']}:{chroma_key['blend']},setsar=1[template];"
            else:
                template_filter = "[1:v]scale=1080:1920,setsar=1[template];"

            filter_str = (
                f"[0:v]scale={target_width}:-2,setsar=1[scaled];"
                f"[scaled]pad=1080:1920:(ow-iw)/2:(oh-ih)/2+{v_offset}:black[vid];"
                f"{template_filter}"
                f"[vid][template]overlay=0:0:shortest=1,format=yuv420p"
            )
    else:
        filter_str = "format=yuv420p"

    if template_path or filter_str != "format=yuv420p":
        merge_command.extend(["-filter_complex", filter_str])
        
    merge_command.extend([
        "-c:v", "libx264", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-strict", "experimental",
        output_path
    ])

    try:
        subprocess.run(merge_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
        
        if os.path.exists(temp_cut_path):
            os.remove(temp_cut_path)
            
        return output_path
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
        st.error(f"❌ 2단계(합성) 실패:\n{error_msg}")
        return None

# --- UI 구성 ---

st.set_page_config(page_title="AI Shorts Maker Pro (김지연 3.9)", layout="wide")

st.title("🎬 AI 숏폼 자동 생성기 Pro (김지연 3.9)")
st.markdown("Gemini 2.5 Flash | **MP4 강제 변환 & 경로 추적 강화** | **담당자: 김지연**")

with st.sidebar:
    st.header("⚙️ 기본 설정")
    api_key = st.text_input("Gemini API Key", type="password")
    
    st.markdown("---")
    uploaded_cookies = st.file_uploader(
        "🍪 유튜브 쿠키 파일 (cookies.txt)", 
        type=["txt"], 
        help="서버 차단 시 'Get cookies.txt LOCALLY' 확장 프로그램으로 추출한 파일을 넣으세요."
    )
    
    cookie_path = None
    if uploaded_cookies:
        cookie_path = os.path.join(DOWNLOAD_FOLDER, "cookies.txt")
        with open(cookie_path, "wb") as f:
            f.write(uploaded_cookies.getbuffer())
        st.success("✅ 쿠키 적용됨")
    
    st.markdown("---")
    st.header("🎨 템플릿 설정")
    
    uploaded_template = st.file_uploader(
        "🖼️ 템플릿 오버레이 (PNG/JPG)", 
        type=["png", "jpg", "jpeg"], 
        help="가운데가 뚫려있는 1080x1920 이미지를 사용하세요."
    )
    
    template_path = None
    chroma_key_settings = None
    video_on_top = True

    if uploaded_template:
        ext = os.path.splitext(uploaded_template.name)[1]
        template_path = os.path.join(DOWNLOAD_FOLDER, f"temp_template{ext}")
        with open(template_path, "wb") as f:
            f.write(uploaded_template.getbuffer())
        
        st.success(f"✅ 템플릿 로드됨 ({ext})")
        
        st.markdown("#### 🥞 레이어 순서")
        video_on_top = st.checkbox("영상을 템플릿 '위'에 올리기", value=True, help="체크하면 영상이 템플릿을 덮습니다.")

        if not video_on_top:
            with st.expander("🪄 템플릿 투명화 (크로마키)", expanded=False):
                use_chroma = st.checkbox("배경 투명하게 만들기", value=False)
                if use_chroma:
                    col_c1, col_c2 = st.columns(2)
                    with col_c1:
                        color_picker = st.color_picker("투명하게 할 색상", "#000000")
                    with col_c2:
                        similarity = st.slider("색상 유사도", 0.01, 0.5, 0.1)
                    
                    chroma_key_settings = {
                        "color": color_picker.replace("#", "0x"),
                        "similarity": str(similarity),
                        "blend": "0.1"
                    }

    st.markdown("---")
    with st.expander("📐 영상 배치 상세 설정 (Zoom/이동)", expanded=True):
        scale_pct = st.slider("🔍 영상 크기 (Zoom)", 50, 150, 100, 5)
        v_offset = st.slider("↕️ 위아래 위치 이동", -500, 500, 0, 10)
        
        layout_settings = {
            "scale": scale_pct,
            "v_offset": v_offset
        }

if 'generated_shorts' not in st.session_state:
    st.session_state['generated_shorts'] = []

youtube_url = st.text_input("YouTube URL", placeholder="https://www.youtube.com/watch?v=...")
st.divider()

tab1, tab2 = st.tabs(["🤖 AI 자동 분석", "✍️ 수동 입력"])

mode = "AI"
user_prompt = ""
manual_segments = []

with tab1:
    user_prompt = st.text_area("편집 프롬프트", height=80)
    if st.button("🚀 AI 생성 시작", type="primary", key="btn_ai"):
        mode = "AI"
        run_process = True
    else: run_process = False

with tab2:
    for i in range(5):
        c1, c2 = st.columns(2)
        with c1: s = st.text_input(f"#{i+1} 시작 (MM:SS)", key=f"s_{i}")
        with c2: e = st.text_input(f"#{i+1} 종료 (MM:SS)", key=f"e_{i}")
        if s and e: manual_segments.append({"start_time": s, "end_time": e, "reason": "수동"})
            
    if st.button("✂️ 수동 생성 시작", type="primary", key="btn_manual"):
        mode = "Manual"
        run_process = True
    elif not run_process: run_process = False

if run_process:
    if not youtube_url:
        st.error("URL을 입력하세요.")
    else:
        st.session_state['generated_shorts'] = []
        with st.status("작업 진행 중...", expanded=True) as status:
            status.write("📥 영상 다운로드 중...")
            try:
                # 쿠키 경로 전달
                video_path, video_title, video_id = download_video(youtube_url, cookie_path)
            except Exception as e:
                st.error(f"다운로드 실패: {e}")
                st.stop()
            
            target_segments = []
            if mode == "AI":
                if not api_key: st.error("API 키 필요"); st.stop()
                status.write("🤖 AI 분석 중 (Gemini 2.5 Flash)...")
                res = analyze_video_points(api_key, video_path, user_prompt)
                if not res or (isinstance(res, list) and "error" in res[0]): st.error("분석 실패"); st.stop()
                target_segments = res
            else:
                target_segments = manual_segments

            temp_results = []
            for i, seg in enumerate(target_segments):
                s_str, e_str = seg.get("start_time"), seg.get("end_time")
                status.write(f"🎞️ Processing #{i+1}: {s_str} ~ {e_str}")
                
                s_sec, e_sec = parse_time_str(s_str), parse_time_str(e_str)
                if e_sec > s_sec:
                    out_path = process_video(
                        video_path, s_sec, e_sec, video_id, i, 
                        template_path=template_path,
                        chroma_key=chroma_key_settings,
                        layout_settings=layout_settings,
                        video_on_top=video_on_top
                    )
                    if out_path:
                        temp_results.append({"path": out_path, "label": f"Shorts #{i+1}", "reason": seg.get("reason")})
            
            st.session_state['generated_shorts'] = temp_results
            status.update(label="완료!", state="complete")

if st.session_state['generated_shorts']:
    output_files = st.session_state['generated_shorts']
    st.success(f"🎉 {len(output_files)}개 생성 완료")
    
    tabs = st.tabs([i["label"] for i in output_files])
    for i, tab in enumerate(tabs):
        with tab:
            st.write(output_files[i]["reason"])
            st.video(output_files[i]["path"])
            with open(output_files[i]["path"], "rb") as f:
                st.download_button("📥 다운로드", f, file_name=f"shorts_{i}.mp4", mime="video/mp4", key=f"d_{i}")
