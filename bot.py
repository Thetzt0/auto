import asyncio
import glob
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import edge_tts
import google.generativeai as genai

# ---------------- CONFIG ----------------
API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")

OUTPUT_FINAL = "final_recap.mp4"
USER_SCRIPT_FILE = "text.txt"
SUBTITLE_FILE = "subtitles.srt"

VOICE_NAME = "my-MM-ThihaNeural"
VOICE_SPEED = "+40%"
PAUSE_DURATION = 0.1
MAX_CHARS_PER_LINE = 48
MAX_CHARS_PER_BLOCK = 78

MIN_VIDEO_SPEED = 0.2
MAX_VIDEO_SPEED = 1.3

VIDEO_FPS = int(os.getenv("VIDEO_FPS", "60"))
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "medium")
VIDEO_CRF = os.getenv("VIDEO_CRF", "18")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")

# Subtitle timing uses Edge-TTS WordBoundary directly, like the original bot.py.
# SRT_OFFSET_SEC is kept only for backward compatibility and is not applied.
SRT_OFFSET_SEC = float(os.getenv("SRT_OFFSET_SEC", "0"))

generation_config = {
    "temperature": 0.2,
    "top_p": 0.95,
    "max_output_tokens": 65000,
    "response_mime_type": "application/json",
}

script_generation_config = {
    "temperature": 0.5,
    "top_p": 0.95,
    "max_output_tokens": 65000,
}


def configure_gemini():
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY မတွေ့ပါ။ .env ထဲမှာ GEMINI_API_KEY ထည့်ပါ။")
    genai.configure(api_key=API_KEY)


def read_user_script():
    if not os.path.exists(USER_SCRIPT_FILE):
        raise FileNotFoundError(f"{USER_SCRIPT_FILE} not found.")
    with open(USER_SCRIPT_FILE, "r", encoding="utf-8") as f:
        return f.read()


def get_duration(file_path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def time_to_sec(time_str):
    try:
        h, m, s = map(float, str(time_str).split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return 0.0


def format_srt_timestamp(seconds):
    milliseconds = int((seconds % 1) * 1000)
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def _strip_json_fences(raw_text):
    """Remove common Markdown fences without damaging JSON content."""
    raw_text = (raw_text or "").strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.replace("```json", "", 1).replace("```JSON", "", 1).replace("```", "", 1).strip()
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3].strip()
    return raw_text


def repair_json(raw_text):
    """Return the first valid JSON object/array from a Gemini response.

    Gemini sometimes returns valid JSON followed by another JSON block, prose, or
    duplicated data. json.loads() fails with "Extra data" in that case. This
    function uses JSONDecoder.raw_decode so only the first complete JSON value is
    parsed and any trailing text is ignored.
    """
    raw_text = _strip_json_fences(raw_text)
    if not raw_text:
        return "[]"

    decoder = json.JSONDecoder()

    # Try parsing from the first plausible JSON start. Prefer array because the
    # scene matcher prompt asks for an array, but allow dict too.
    starts = []
    for ch in ("[", "{"):
        pos = raw_text.find(ch)
        if pos != -1:
            starts.append(pos)
    starts = sorted(set(starts)) or [0]

    for start in starts:
        candidate = raw_text[start:].strip()
        try:
            obj, _end = decoder.raw_decode(candidate)
            return json.dumps(obj, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

    # Last-resort repair for truncated arrays like: [{...}, {...}
    start = raw_text.find("[")
    last_brace = raw_text.rfind("}")
    if start != -1 and last_brace != -1 and last_brace > start:
        return raw_text[start:last_brace + 1] + "]"

    raise ValueError("Gemini returned invalid JSON for scene matching. Raw preview: " + raw_text[:500])


def parse_gemini_json(raw_text):
    """Parse Gemini JSON robustly and ignore trailing extra data."""
    return json.loads(repair_json(raw_text))


async def _upload_video_to_gemini(video_path, progress=None, start_progress=0.05, wait_progress=0.12, desc="Uploading video to Gemini..."):
    configure_gemini()
    if progress is not None:
        progress(start_progress, desc=desc)

    video_upload = genai.upload_file(path=str(video_path))

    while video_upload.state.name == "PROCESSING":
        if progress is not None:
            progress(wait_progress, desc="Waiting for Gemini video processing...")
        time.sleep(10)
        video_upload = genai.get_file(video_upload.name)

    if video_upload.state.name == "FAILED":
        raise RuntimeError("Gemini video upload processing failed.")
    return video_upload


def word_range_for_video(video_path):
    """Return dynamic script word range: minutes x 100 to minutes x 100 + 200."""
    duration = get_duration(video_path)
    if duration <= 0:
        minutes = 5
    else:
        minutes = max(1, int(round(duration / 60)))
    min_words = minutes * 100
    max_words = min_words + 200
    return minutes, min_words, max_words


def build_burmese_recap_prompt(video_path):
    _minutes, min_words, max_words = word_range_for_video(video_path)
    return f"""Write a Burmese recap voiceover script based only on the uploaded video.

Style:
- Write in first-person narration.
- Use “ကျွန်တော်" or "ကျွန်မ” for the main character.
- Write like the main character is personally telling what happened.
- Use natural spoken Burmese, not formal report-style Burmese.
- Include intense, direct dialogue between characters to make the scene feel alive. Use quotation marks for direct quotes (e.g., သူorသူမက "...လို့" ပြောတော့ ကျွန်တော်orကျွန်မက "...လို့" ပြန်အော်လိုက်တယ်).

Opening hook (STRICT INSTRUCTIONS):
- Start with a "flash-forward" hook. Scan the entire video and select the most dramatic, shocking, or climactic moment (e.g., a major betrayal revealed, a huge plot twist, or a fight).
- Write exactly ONE short, direct sentence describing this intense moment to hook the audience immediately. Do not use overly emotional filler words.
- Example: "သမီးလေးကို ကြိုးတုပ်ထားတဲ့ ပြန်ပေးသမားက ငွေသုံးသိန်းမရရင် သတ်ပစ်မယ်လို့ ကျွန်မဆီဖုန်းဆက်လာတယ်။" (This is just an example, choose the best moment from the actual video).

Pacing & Scene Sequence:
- Immediately after the 1-sentence hook, transition smoothly back to the chronological beginning of the story.
- From there, follow the exact chronological sequence of the video without skipping any scenes. Keep the narrative flow continuous.

Content rules:
- Don't use "ပါတယ်" , use "တယ်".
- Retell only the events shown or clearly stated in the video, scene-by-scene.
- Blend the narration smoothly with direct character interactions and arguments. Show the actual back-and-forth arguments just like in the video.
- Do not write like a movie review, essay, article, or news report.
- Do not explain why the movie is good.
- Do not say "ကြည့်သင့်ပါတယ်", "စိတ်ဝင်စားစရာကောင်းပါတယ်", or "သင်ခန်းစာပေးထားပါတယ်".
- Stop before the final resolution.

Format:
- One paragraph only.
- No title, headings, numbering, or bullet points.
- Burmese only.
- Keep it between {min_words} – {max_words} words."""


async def generate_recap_script_from_video(video_path, progress=None, gemini_model=None):
    video_upload = await _upload_video_to_gemini(
        video_path,
        progress=progress,
        start_progress=0.05,
        wait_progress=0.15,
        desc="Uploading original video to Gemini for script generation...",
    )
    if progress is not None:
        _minutes, min_words, max_words = word_range_for_video(video_path)
        progress(0.35, desc=f"Generating script ({min_words}-{max_words} words)...")

    model = genai.GenerativeModel((gemini_model or GEMINI_MODEL), generation_config=script_generation_config)
    prompt = build_burmese_recap_prompt(video_path)
    response = model.generate_content([video_upload, prompt])
    text = (response.text or "").strip().replace("```", "").strip()
    if not text:
        raise RuntimeError("Gemini did not return a script. Try Generate Script Again.")
    return text


def generate_recap_script(video_path, progress=None, gemini_model=None):
    return asyncio.run(generate_recap_script_from_video(video_path, progress=progress, gemini_model=gemini_model))


async def get_timestamps(video_path, user_text, progress=None, gemini_model=None):
    # No low-quality compression. Upload original/cached video directly.
    video_upload = await _upload_video_to_gemini(
        video_path,
        progress=progress,
        start_progress=0.05,
        wait_progress=0.18,
        desc="Uploading original video to Gemini for scene matching...",
    )

    if progress is not None:
        progress(0.25, desc="Gemini is analyzing scenes...")

    model = genai.GenerativeModel((gemini_model or GEMINI_MODEL), generation_config=generation_config)
    prompt = f"""
    You are a Movie Recap Editor.
    TASK: Find suitable video clips for each sentence.
    RULES:
    1. **Multiple Clips Allowed:** If a sentence describes multiple actions, you CAN and SHOULD select multiple disjoint clips.
    2. **Start & End Required:** HH:MM:SS format for every clip.
    3. **No Translation:** Return `text` EXACTLY as is.
    4. **JSON ONLY:** Return one JSON array only. No markdown, no explanation, no second JSON block.
    
    USER TEXT:
    {user_text}

    JSON Output Format:
    [
        {{
            "text": "...", 
            "clips": [
                {{"start": "00:00:05", "end": "00:00:10"}},
                {{"start": "00:05:20", "end": "00:05:23"}} 
            ]
        }}
    ]
    """

    response = model.generate_content([video_upload, prompt])
    raw_text = response.text or ""
    return parse_gemini_json(raw_text)


async def generate_audio_with_boundaries(text, output_path):
    """Generate Edge-TTS audio and exact WordBoundary timings only.

    gTTS/estimated timing fallback is intentionally removed because it can make
    subtitles appear before the waveform/audio starts.
    """
    communicate = edge_tts.Communicate(
        str(text),
        VOICE_NAME,
        rate=VOICE_SPEED,
        boundary="WordBoundary",
    )
    words = []
    with open(output_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start_time = chunk["offset"] / 10_000_000
                duration = chunk["duration"] / 10_000_000
                words.append({"text": chunk["text"], "start": start_time, "end": start_time + duration})

    if not words:
        raise RuntimeError("Edge-TTS WordBoundary မရပါ။ Subtitle timing မလွဲစေဖို့ fallback TTS မသုံးပါ။")
    return words


def process_audio_file(input_path, output_path, pause_sec):
    # Output WAV, not MP3, to avoid adding another MP3 encoder delay before AAC muxing.
    # Reset audio PTS to 0 so CapCut does not see an audio start offset.
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-af",
        f"silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB,"
        f"areverse,silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB,"
        f"areverse,apad=pad_dur={pause_sec},aresample=async=1:first_pts=0",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "1",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def generate_subtitles(word_boundaries, current_global_time, final_audio_dur, srt_index):
    entries = []
    current_group = []
    current_line_length = 0
    total_block_length = 0
    line_count = 0

    def flush_group(group, idx):
        if not group:
            return None, idx
        text_items = [g for g in group if "text" in g]
        if not text_items:
            return None, idx
        start_ts = format_srt_timestamp(current_global_time + text_items[0]["start"])
        end_ts = format_srt_timestamp(min(current_global_time + text_items[-1]["end"], current_global_time + final_audio_dur))
        combined_text = ""
        for w in group:
            if "break" in w:
                combined_text += "\n"
            else:
                space = " " if combined_text and not combined_text.endswith("\n") else ""
                combined_text += space + w["text"]
        return f"{idx}\n{start_ts} --> {end_ts}\n{combined_text.strip()}\n\n", idx + 1

    for word_info in word_boundaries:
        word_text = word_info["text"]
        if "။" in word_text:
            clean_word = word_text.replace("။", "").strip()
            if clean_word:
                current_group.append({"text": clean_word, "start": word_info["start"], "end": word_info["end"]})
            entry, srt_index = flush_group(current_group, srt_index)
            if entry:
                entries.append(entry)
            current_group = []
            current_line_length = 0
            total_block_length = 0
            line_count = 0
            continue

        word_len = len(word_text) + 1
        if total_block_length + word_len > MAX_CHARS_PER_BLOCK:
            entry, srt_index = flush_group(current_group, srt_index)
            if entry:
                entries.append(entry)
            current_group = []
            current_line_length = 0
            total_block_length = 0
            line_count = 0

        if current_line_length + word_len > MAX_CHARS_PER_LINE and line_count == 0:
            current_group.append({"break": True})
            line_count = 1
            current_line_length = word_len
        else:
            current_line_length += word_len

        total_block_length += word_len
        current_group.append(word_info)

    entry, srt_index = flush_group(current_group, srt_index)
    if entry:
        entries.append(entry)
    return entries, srt_index


def normalize_scenes(raw_scenes, script_text):
    scenes = []
    if isinstance(raw_scenes, dict):
        for key in ("scenes", "items", "data", "result"):
            if isinstance(raw_scenes.get(key), list):
                raw_scenes = raw_scenes[key]
                break

    if not isinstance(raw_scenes, list):
        raw_scenes = []

    for item in raw_scenes:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        clips = item.get("clips", [])
        safe_clips = []
        if isinstance(clips, list):
            for c in clips:
                if isinstance(c, dict):
                    safe_clips.append({"start": str(c.get("start", "00:00:00")), "end": str(c.get("end", "00:00:02"))})
        if not safe_clips:
            safe_clips = [{"start": "00:00:00", "end": "00:00:02"}]
        scenes.append({"text": text, "clips": safe_clips})

    if not scenes:
        parts = [x.strip() for x in script_text.replace("။", "။\n").splitlines() if x.strip()]
        for part in parts[:50]:
            scenes.append({"text": part, "clips": [{"start": "00:00:00", "end": "00:00:02"}]})

    return scenes


def process_video(video_path, scenes, progress=None):
    final_segments = []
    total_src_dur = get_duration(video_path)
    if total_src_dur <= 0:
        raise RuntimeError("Input video duration could not be read. Please upload a valid MP4 file.")

    all_srt_entries = []
    current_global_time = 0.0
    srt_index = 1
    total_scenes = max(len(scenes), 1)

    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        if progress is not None:
            progress(0.30 + (i / total_scenes) * 0.60, desc=f"Processing scene {i + 1}/{total_scenes}...")

        raw_audio = f"raw_voice_{i}.mp3"
        audio_file = f"voice_{i}.wav"
        final_segment = f"segment_{i}.mp4"

        word_boundaries = asyncio.run(generate_audio_with_boundaries(scene.get("text", ""), raw_audio))
        process_audio_file(raw_audio, audio_file, PAUSE_DURATION)

        final_audio_dur = get_duration(audio_file)
        if final_audio_dur <= 0.1:
            final_audio_dur = 3.0

        # Use Edge-TTS WordBoundary timings directly, same as the original bot.py.
        new_entries, srt_index = generate_subtitles(word_boundaries, current_global_time, final_audio_dur, srt_index)
        all_srt_entries.extend(new_entries)

        clips = scene.get("clips", []) or [{"start": scene.get("start", "00:00:00"), "end": scene.get("end", "00:00:02")}]
        parsed_clips = []
        total_visual_dur = 0.0
        for c in clips:
            st = time_to_sec(c.get("start", "0:0:0"))
            et = time_to_sec(c.get("end", "0:0:0"))
            if et <= st:
                et = st + 2.0
            if et > total_src_dur:
                et = total_src_dur
            if st >= total_src_dur:
                st = max(0.0, total_src_dur - 2.0)
            dur = max(et - st, 0.1)
            parsed_clips.append({"start": st, "end": et, "dur": dur})
            total_visual_dur += dur

        if total_visual_dur <= 0:
            total_visual_dur = 2.0

        speed_factor = total_visual_dur / final_audio_dur
        if speed_factor > MAX_VIDEO_SPEED:
            speed_factor = MAX_VIDEO_SPEED
        elif speed_factor < MIN_VIDEO_SPEED:
            if not parsed_clips:
                parsed_clips = [{"start": 0.0, "end": min(total_src_dur, 2.0), "dur": min(total_src_dur, 2.0)}]
            req_visual_dur = final_audio_dur * MIN_VIDEO_SPEED
            expand_amount = req_visual_dur - total_visual_dur
            parsed_clips[-1]["end"] = min(total_src_dur, parsed_clips[-1]["end"] + expand_amount)
            parsed_clips[-1]["dur"] = parsed_clips[-1]["end"] - parsed_clips[-1]["start"]
            total_visual_dur = sum(c["dur"] for c in parsed_clips)
            speed_factor = max(total_visual_dur / final_audio_dur, MIN_VIDEO_SPEED)

        print(f"Scene {i + 1}/{total_scenes}: audio={final_audio_dur:.2f}s clips={len(parsed_clips)} speed={speed_factor:.2f}x", flush=True)
        for clip_i, c in enumerate(parsed_clips, start=1):
            out_dur = c["dur"] / speed_factor if speed_factor else c["dur"]
            print(
                f"  Clip {clip_i}/{len(parsed_clips)}: "
                f"source={c['start']:.2f}s-{c['end']:.2f}s | "
                f"src_dur={c['dur']:.2f}s | output≈{out_dur:.2f}s | "
                f"speed={speed_factor:.2f}x",
                flush=True
            )

        sub_clip_files = []
        for k, c in enumerate(parsed_clips):
            sub_file = f"sub_{i}_{k}.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                str(c["start"]),
                "-t",
                str(c["dur"]),
                "-i",
                str(video_path),
                "-filter_complex",
                f"[0:v]setpts=(PTS-STARTPTS)/{speed_factor},fps={VIDEO_FPS}[v]",
                "-map",
                "[v]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                VIDEO_PRESET,
                "-crf",
                str(VIDEO_CRF),
                "-pix_fmt",
                "yuv420p",
                sub_file,
            ]
            subprocess.run(cmd, check=True)
            if os.path.exists(sub_file):
                sub_clip_files.append(sub_file)

        merged_vid = f"merged_vid_{i}.mp4"
        if len(sub_clip_files) > 1:
            list_file = f"sub_list_{i}.txt"
            with open(list_file, "w", encoding="utf-8") as f:
                for sf in sub_clip_files:
                    f.write(f"file '{Path(sf).as_posix()}'\n")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", merged_vid], check=True)
        elif len(sub_clip_files) == 1:
            shutil.copy(sub_clip_files[0], merged_vid)
        else:
            continue

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-i",
            merged_vid,
            "-i",
            audio_file,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            AUDIO_BITRATE,
            "-af",
            "aresample=async=1:first_pts=0",
            "-t",
            str(final_audio_dur),
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            "-use_editlist",
            "0",
            final_segment,
        ]
        subprocess.run(cmd, check=True)

        if os.path.exists(final_segment):
            final_segments.append(final_segment)
            # Use the real muxed MP4 segment duration for the next SRT block.
            # This prevents tiny AAC/frame rounding errors from accumulating and
            # making later captions appear early in CapCut.
            muxed_dur = get_duration(final_segment)
            current_global_time += muxed_dur if muxed_dur > 0.1 else final_audio_dur

    if not final_segments:
        raise RuntimeError("No final video segments were created.")

    with open(SUBTITLE_FILE, "w", encoding="utf-8") as f:
        f.writelines(all_srt_entries)

    if progress is not None:
        progress(0.94, desc="Merging final video...")
    with open("list.txt", "w", encoding="utf-8") as f:
        for f_name in final_segments:
            f.write(f"file '{Path(f_name).as_posix()}'\n")
    concat_tmp = "concat_tmp.mp4"
    subprocess.run([
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        "list.txt",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        concat_tmp,
    ], check=True)

    # Final normalization: flatten MP4 edit lists/start offsets so CapCut,
    # extracted audio, video, and external SRT all start from the same zero PTS.
    subprocess.run([
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-i",
        concat_tmp,
        "-filter_complex",
        f"[0:v]setpts=PTS-STARTPTS,fps={VIDEO_FPS}[v];[0:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        str(VIDEO_CRF),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        "-video_track_timescale",
        "90000",
        "-use_editlist",
        "0",
        OUTPUT_FINAL,
    ], check=True)

    cleanup_files = (
        final_segments
        + glob.glob("voice_*.mp3")
        + glob.glob("voice_*.wav")
        + glob.glob("raw_voice_*.mp3")
        + glob.glob("sub_*.mp4")
        + glob.glob("merged_vid_*.mp4")
        + glob.glob("sub_list_*.txt")
        + ["list.txt", concat_tmp]
    )
    for f in cleanup_files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass


def run_video_recap(video_path, script_text, output_dir, voice_name="my-MM-ThihaNeural", voice_speed="+40%", progress=None, gemini_model=None):
    global OUTPUT_FINAL, USER_SCRIPT_FILE, SUBTITLE_FILE, VOICE_NAME, VOICE_SPEED, API_KEY

    API_KEY = os.getenv("GEMINI_API_KEY", "")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    old_cwd = os.getcwd()
    os.chdir(output_dir)
    try:
        src_video = os.path.abspath(video_path)
        local_video = os.path.join(output_dir, "input.mp4")
        if os.path.abspath(src_video) != os.path.abspath(local_video):
            shutil.copy(src_video, local_video)
        else:
            local_video = src_video

        USER_SCRIPT_FILE = os.path.join(output_dir, "text.txt")
        OUTPUT_FINAL = os.path.join(output_dir, "final_recap.mp4")
        SUBTITLE_FILE = os.path.join(output_dir, "subtitles.srt")
        VOICE_NAME = voice_name
        VOICE_SPEED = voice_speed

        with open(USER_SCRIPT_FILE, "w", encoding="utf-8") as f:
            f.write(script_text)

        user_story = read_user_script()
        scenes_data = asyncio.run(get_timestamps(local_video, user_story, progress=progress, gemini_model=gemini_model))
        scenes_data = normalize_scenes(scenes_data, user_story)
        process_video(local_video, scenes_data, progress=progress)

        if progress is not None:
            progress(1.0, desc="Done")
        return OUTPUT_FINAL, SUBTITLE_FILE
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    video = "input.mp4"
    if not os.path.exists(video):
        raise FileNotFoundError("input.mp4 not found")
    with open("text.txt", "r", encoding="utf-8") as f:
        text = f.read()
    run_video_recap(video, text, os.getcwd())
