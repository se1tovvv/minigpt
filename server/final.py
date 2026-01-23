import socket
import json
import threading
import config
from vosk import Model, KaldiRecognizer
from openai import OpenAI
import subprocess
import urllib.parse
import time
import queue
import urllib.request

client = OpenAI(api_key=config.OPENAI_API_KEY)

# ===== MODELS =====
MODEL_RU = ""
MODEL_EN = ""
SAMPLE_RATE = 16000

# ===== TCP CONFIG =====
HOST = "0.0.0.0"
PORT = 6000

print("Loading RU model...")
model_ru = Model(MODEL_RU)
print("Loading EN model...")
model_en = Model(MODEL_EN)

current_lang = "ru"
rec = KaldiRecognizer(model_ru, SAMPLE_RATE)

SPEAK_QUEUE = queue.Queue(maxsize=10)


# ===== SIMPLE MEMORY =====
conversation_history = []
HISTORY_LIMIT = 10

# ===== WAKE/SLEEP WORDS =====
WAKE_WORDS_EN = {"jarvis", "assistant"}
WAKE_WORDS_RU = {"джарвис", "жарвис", "ассистент", "тардис", "джервис"}

SLEEP_WORDS_EN = {"sleep"}
SLEEP_WORDS_RU = {"слип", "усни", "спи", "засни", "спать"}

is_awake = False
skip_next_final_after_wake = False


# ====================================================================================================
# MAC CONTROL FUNCTIONS
# ====================================================================================================

# Whitelist: spoken name -> macOS app name (AppleScript)
APP_ALIASES = {
    # Browsers
    "safari": "Safari",
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    # Dev
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    # Chat
    "telegram": "Telegram",
    "discord": "Discord",
    # System / common
    "finder": "Finder",
    "notes": "Notes",
    "music": "Music",
    "terminal": "Terminal",
}

# Russian -> English alias keys
RU_APP_ALIASES = {
    "сафари": "safari",
    "гугл": "chrome",
    "гугл хром": "google chrome",
    "вс код": "vscode",
    "видео студио код": "vscode",
    "телеграмм": "telegram",
    "дискорд": "discord",
    "файндер": "finder",
    "заметки": "notes",
    "музыку": "music",
    "терминал": "terminal",
}

KEY_ALIASES_EN = {
    "enter": "return",
    "return": "return",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
    "space": "space",
    "backspace": "delete",
    "delete": "delete",
}
KEY_ALIASES_RU = {
    "энтер": "return",
    "интер": "return",
    "таб": "tab",
    "эскейп": "escape",
    "эск": "escape",
    "пробел": "space",
    "бэкспейс": "delete",
    "делит": "delete",
}


def run_osascript(script: str) -> bool:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            if err:
                print("osascript error:", err)
            return False
        return True
    except Exception as e:
        print("osascript exception:", e)
        return False


def run_osascript_out(script: str) -> str:
    """
    Выполняет AppleScript и возвращает stdout (строкой).
    Если ошибка — возвращает текст ошибки.
    """
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if p.returncode != 0:
            return (p.stderr or "").strip()
        return (p.stdout or "").strip()
    except Exception as e:
        return f"EXCEPTION: {e}"


def _as_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def mac_open_url(url: str) -> bool:
    try:
        subprocess.run(["open", url], check=True)
        return True
    except Exception as e:
        print("open url error:", e)
        return False


def mac_key_code(code: int, times: int = 1, delay: float = 0.05) -> bool:
    # presses a virtual key code N times
    script = 'tell application "System Events"\n'
    for _ in range(max(1, times)):
        script += f"  key code {code}\n  delay {delay}\n"
    script += "end tell"
    return run_osascript(script)


def mac_press_tab(times: int = 1) -> bool:
    # tab key code = 48
    return mac_key_code(48, times=times, delay=0.05)


def mac_press_enter() -> bool:
    # return key code = 36
    return mac_key_code(36, times=1, delay=0.05)


def mac_open_app(app_name: str) -> bool:
    # Activate (brings to front)
    script = f'tell application "{app_name}" to activate'
    return run_osascript(script)


def mac_type_text(text: str) -> bool:
    # Types into the currently focused app.
    # Requires Accessibility permission.
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    script = 'tell application "System Events"\n' f'  keystroke "{safe}"\n' "end tell"
    return run_osascript(script)


def mac_press_key(key_code_name: str) -> bool:
    # "return", "tab", "escape", "space", "delete"
    script = (
        'tell application "System Events"\n'
        f"  key code {keycode_from_name(key_code_name)}\n"
        "end tell"
    )
    return run_osascript(script)


def keycode_from_name(name: str) -> int:
    # macOS virtual key codes (US layout)
    mapping = {
        "return": 36,
        "tab": 48,
        "space": 49,
        "delete": 51,
        "escape": 53,
    }
    return mapping.get(name, 53)


def mac_search_web(query: str) -> bool:
    q = urllib.parse.quote(query)
    url = f"https://www.google.com/search?q={q}"
    # Open in default browser
    try:
        subprocess.run(["open", url], check=True)
        return True
    except Exception as e:
        print("open url error:", e)
        return False


def mac_screenshot() -> bool:
    # Saves to Desktop by default
    try:
        subprocess.run(["screencapture", "-x", "~/Desktop/Screens Trash"], check=False)
        return True
    except Exception as e:
        print("screencapture error:", e)
        return False


def chrome_execute_js(js: str) -> str:
    js_escaped = js.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""
    tell application "Google Chrome"
        if (count of windows) = 0 then return "NO_WINDOW"
        set t to active tab of front window
        return execute t javascript "{js_escaped}"
    end tell
    """
    return run_osascript_out(script)


def chrome_activate() -> bool:
    return run_osascript('tell application "Google Chrome" to activate')


def mac_media(action: str) -> bool:
    global ACTIVE_PLAYER

    if action == "playpause":
        if ACTIVE_PLAYER == "youtube":
            return youtube_toggle_play_pause()
        return run_osascript('tell application "Music" to playpause')

    if action == "next":
        if ACTIVE_PLAYER == "music":
            return run_osascript('tell application "Music" to next track')

    if action == "previous":
        if ACTIVE_PLAYER == "music":
            return run_osascript('tell application "Music" to previous track')

    return False


def mac_volume(delta: int = 0, mute: bool = False) -> bool:
    # delta: +6/-6 etc; mute toggles by setting output muted true
    if mute:
        return run_osascript("set volume with output muted")
    if delta != 0:
        # Get current output volume (0-100), then set within bounds
        # AppleScript can read it via output volume of (get volume settings)
        script = (
            "set v to output volume of (get volume settings)\n"
            f"set v to v + ({delta})\n"
            "if v > 100 then set v to 100\n"
            "if v < 0 then set v to 0\n"
            "set volume output volume v\n"
        )
        return run_osascript(script)
    return False


def chrome_close_tab() -> bool:
    # CMD+W (close tab) — layout-independent via key code
    script = """
    tell application "Google Chrome" to activate
    delay 0.05
    tell application "System Events"
        key code 13 using {command down} -- 13 = W
    end tell
    """
    return run_osascript(script)


def chrome_close_window() -> bool:
    # CMD+SHIFT+W (close window)
    script = """
    tell application "Google Chrome" to activate
    delay 0.05
    tell application "System Events"
        key code 13 using {command down, shift down} -- W
    end tell
    """
    return run_osascript(script)


def chrome_close_all_tabs() -> bool:
    # CMD+Q (quit Chrome) — closes everything
    script = """
    tell application "Google Chrome" to activate
    delay 0.05
    tell application "System Events"
        key code 12 using {command down} -- 12 = Q
    end tell
    """
    return run_osascript(script)


def normalize_text(s: str) -> str:
    return (s or "").strip().lower()


def tokens(s: str):
    s = s.replace(",", " ").replace(".", " ").replace("!", " ").replace("?", " ")
    s = s.replace(":", " ").replace(";", " ").replace("-", " ")
    return [t for t in s.split() if t]


def contains_any_token(text: str, vocab: set) -> bool:
    ts = set(tokens(text))
    return any(w in ts for w in vocab)


def detect_wake(text: str) -> bool:
    t = normalize_text(text)
    return contains_any_token(t, WAKE_WORDS_EN) or contains_any_token(t, WAKE_WORDS_RU)


def detect_sleep(text: str) -> bool:
    t = normalize_text(text)
    return contains_any_token(t, SLEEP_WORDS_EN) or contains_any_token(
        t, SLEEP_WORDS_RU
    )


def strip_leading_wake(text: str) -> str:
    tks = tokens(text)
    if not tks:
        return ""
    wake_vocab = WAKE_WORDS_EN.union(WAKE_WORDS_RU)
    i = 0
    while i < len(tks) and tks[i] in wake_vocab:
        i += 1
    return " ".join(tks[i:]).strip()


def reset_recognizer():
    global rec, current_lang
    if current_lang == "ru":
        rec = KaldiRecognizer(model_ru, SAMPLE_RATE)
    else:
        rec = KaldiRecognizer(model_en, SAMPLE_RATE)


def send_line(conn: socket.socket, s: str):
    try:
        conn.sendall((s + "\n").encode("utf-8"))
    except OSError:
        pass


def handle_lang_markers(conn: socket.socket, data: bytes):
    global current_lang, rec

    if b"__lang_ru__" in data:
        data = data.replace(b"__lang_ru__", b"")
        current_lang = "ru"
        rec = KaldiRecognizer(model_ru, SAMPLE_RATE)
        print("LANG -> RU")
        send_line(conn, "LANG_RU_OK")

    if b"__lang_en__" in data:
        data = data.replace(b"__lang_en__", b"")
        current_lang = "en"
        rec = KaldiRecognizer(model_en, SAMPLE_RATE)
        print("LANG -> EN")
        send_line(conn, "LANG_EN_OK")

    return data


def set_awake(conn: socket.socket, awake: bool):
    global is_awake, conversation_history, skip_next_final_after_wake

    is_awake = awake
    conversation_history = []

    if is_awake:
        print("STATE -> AWAKE")
        send_line(conn, "__awake__")
        send_line(conn, "__listening_off__")
        skip_next_final_after_wake = True
        reset_recognizer()
    else:
        print("STATE -> SLEEPING")
        send_line(conn, "__sleeping__")
        send_line(conn, "__listening_off__")
        skip_next_final_after_wake = False
        reset_recognizer()


def generate_reply(text: str) -> str:
    text = text.strip()
    if not text:
        return ""

    conversation_history.append({"role": "user", "content": text})
    recent = conversation_history[-HISTORY_LIMIT:]

    system_prompt = (
        "You are a real-time voice assistant. "
        "Use the same language as the user. "
        "If unsure about facts, clearly say you don't know. "
        "Do not invent people, games or places if you are not sure. "
        "Short, clear sentences. Year is 2026. No markdown, no lists."
        "Answer in one short sentence. Max 10 words"
    )

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}, *recent],
            temperature=0.1,
            max_tokens=30,
        )
        reply = completion.choices[0].message.content.strip()
        return reply.replace("\n", " ")
    except Exception as e:
        print("LLM error:", e)
        return "Кешір, жауап генерациясында қате болды."


def tts_bytes(text: str) -> bytes:
    text = text.strip()
    if not text:
        return b""
    try:
        resp = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="onyx",
            input=text,
            response_format="pcm",
        )
        audio_bytes = resp.read()
        print(f"TTS: {len(audio_bytes)} bytes")
        return audio_bytes
    except Exception as e:
        print("TTS ERROR:", e)
        return b""


def speak(conn, text):
    text = (text or "").strip()
    if not text:
        return
    # drop backlog: keep only latest
    try:
        while True:
            SPEAK_QUEUE.get_nowait()
            SPEAK_QUEUE.task_done()
    except queue.Empty:
        pass

    try:
        SPEAK_QUEUE.put_nowait((conn, text))
    except queue.Full:
        pass


def wait_js(predicate_js: str, timeout: float = 2.0, step: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = chrome_execute_js(predicate_js)
        if (res or "").strip() in ("1", "true", "TRUE", "OK"):
            return True
        time.sleep(step)
    return False


def get_weather_wttr(location: str, lang: str) -> str:
    location = (location or "").strip()
    if not location:
        location = "Astana"

    loc = urllib.parse.quote(location)
    url = f"https://wttr.in/{loc}?format=3"

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            text = resp.read().decode("utf-8", errors="ignore").strip()

            if not text:
                return "Пустой ответ от сервиса погоды."

            # ---- локализация ----
            if lang == "ru":
                text = (
                    text.replace("Feels like", "Ощущается как")
                    .replace("Clear", "Ясно")
                    .replace("Sunny", "Солнечно")
                    .replace("Partly cloudy", "Переменная облачность")
                    .replace("Cloudy", "Облачно")
                    .replace("Overcast", "Пасмурно")
                    .replace("Rain", "Дождь")
                    .replace("Snow", "Снег")
                    .replace("Mist", "Туман")
                    .replace("Wind", "Ветер")
                )
                return f"Погода: {text}"

            # EN
            return f"Weather: {text}"

    except Exception as e:
        print("WEATHER ERROR:", e)
        return (
            "Не удалось получить погоду."
            if lang == "ru"
            else "I couldn't fetch the weather right now."
        )


# ===== LANG SWITCH =======================================================
LANG_EN_WORDS = {
    "english",
    "eng",
    "inglish",
    "english mode",
    "speak english",
    "switch to english",
    "change to english",
    "in english",
    "change to russian",
}

LANG_RU_WORDS = {
    "russian",
    "rus",
    "русский",
    "по русски",
    "по-русски",
    "russian mode",
    "speak russian",
    "switch to russian",
    "in russian",
    "говори по русски",
    "говори по-русски",
    "переключись на русский",
    "переключи на русский",
    "русский режим",
}

# Optional: very common RU phrase for EN
LANG_EN_WORDS_RU = {
    "английский",
    "по английски",
    "по-английски",
    "переключись на английский",
    "переключи на английский",
    "смена",
    "английский режим",
}


def set_language(conn: socket.socket | None, lang: str) -> bool:
    """
    lang: 'ru' or 'en'
    Updates recognizer immediately.
    Optionally notifies client (OLED) with a short marker line.
    """
    global current_lang, rec

    lang = (lang or "").strip().lower()
    if lang not in ("ru", "en"):
        return False

    if current_lang == lang:
        return True

    current_lang = lang
    rec = KaldiRecognizer(model_ru if lang == "ru" else model_en, SAMPLE_RATE)

    print(f"LANG -> {current_lang.upper()}")

    # Optional: tell ESP32 (OLED) about language change
    if conn is not None:
        send_line(conn, "LANG_RU_OK" if lang == "ru" else "LANG_EN_OK")

    return True


def speak_worker():
    while True:
        conn, text = SPEAK_QUEUE.get()
        try:
            if conn is None:
                continue

            # show text on OLED
            try:
                conn.sendall((text + "\n").encode("utf-8"))
            except OSError:
                pass

            audio = tts_bytes(text)
            if audio:
                send_line(conn, "__speaking_on__")
                header = f"__audio_len__ {len(audio)}\n"
                try:
                    conn.sendall(header.encode("utf-8"))
                    conn.sendall(audio)
                except OSError:
                    pass
                send_line(conn, "__speaking_off__")
        finally:
            SPEAK_QUEUE.task_done()


# ====================================================================================================
# MUSIC PLAY
# ====================================================================================================
ACTIVE_PLAYER = "youtube"  # "music" (Apple Music) or (YouTube)

# YouTube


def youtube_toggle_play_pause() -> bool:
    js = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return "NO_VIDEO";
  if (v.paused) { v.play(); return "PLAY"; }
  v.pause(); return "PAUSE";
})();
"""
    res = chrome_execute_js(js)
    print("YT TOGGLE:", res)
    return any(x in (res or "") for x in ("PLAY", "PAUSE"))


##Apple Music playing logic (For MACOS). 

def mac_music_play_playlist(playlist_name: str, shuffle: bool = True) -> bool:
    global ACTIVE_PLAYER

    name = (playlist_name or "").strip()
    if not name:
        return False

    safe = _as_escape(name)
    script = f"""
    tell application "Music"
        activate
        delay 0.2
        set shuffling to {"true" if shuffle else "false"}
        set shuffle enabled to shuffling
        try
            set pl to first playlist whose name is "{safe}"
            play pl
            return "OK"
        on error errMsg number errNum
            return "ERR " & errNum & " " & errMsg
        end try
    end tell
    """

    out = run_osascript_out(script)
    if out.startswith("ERR"):
        print("MUSIC PLAYLIST ERR:", out)
        return False

    if out == "OK":
        ACTIVE_PLAYER = "music"
        return True

    return False


def mac_music_list_playlists(filter_text: str = "") -> str:
    ft = (filter_text or "").strip()
    safe = _as_escape(ft)

    script = f"""
    tell application "Music"
        set outText to ""
        try
            if "{safe}" is "" then
                set pls to playlists
            else
                set pls to (every playlist whose name contains "{safe}")
            end if

            repeat with p in pls
                set outText to outText & (name of p) & "\\n"
            end repeat
            return outText
        on error errMsg number errNum
            return "ERR " & errNum & " " & errMsg
        end try
    end tell
    """
    return run_osascript_out(script)


# ====================================================================================================
# Youtube Video playing logic
# ====================================================================================================
def play_from_youtube_video(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False

    term = urllib.parse.quote(q)
    url = f"https://www.youtube.com/results?search_query={term}"

    # Open inside Chrome
    if not chrome_open_url(url, new_tab=True):
        return False

    time.sleep(2.0)

    # Click by first render
    js_click_first = r"""
(() => {
  const first =
    document.querySelector('ytd-video-renderer a#thumbnail') ||
    document.querySelector('ytd-video-renderer a#video-title');

  if (!first) return "NO_VIDEO_RENDERER";
  first.click();
  return "CLICKED_FIRST";
})();
"""
    res = chrome_execute_js(js_click_first)
    print("YT CLICK:", res)
    if "CLICKED_FIRST" not in (res or ""):
        return False

    time.sleep(2.0)

    # Check if no errors with re-directing to youtube.music
    u = chrome_active_url()
    if "music.youtube.com" in u:
        print("Redirected to YTM, trying fallback video...")
        chrome_execute_js("history.back(); 'BACK';")
        time.sleep(1.0)

        js_click_second = r"""
(() => {
  const vids = Array.from(document.querySelectorAll('ytd-video-renderer'));
  if (vids.length < 2) return "NO_SECOND";
  const a =
    vids[1].querySelector('a#thumbnail') ||
    vids[1].querySelector('a#video-title');
  if (!a) return "NO_SECOND_LINK";
  a.click();
  return "CLICKED_SECOND";
})();
"""
        res2 = chrome_execute_js(js_click_second)
        print("YT FALLBACK CLICK:", res2)
        time.sleep(2.0)

    ok = yt_force_play()
    if ok:
        global ACTIVE_PLAYER
        ACTIVE_PLAYER = "youtube"
    return ok


def chrome_open_url(url: str, new_tab: bool = True) -> bool:
    safe = _as_escape(url)
    script = f"""
    tell application "Google Chrome"
        activate
        if (count of windows) = 0 then
            make new window
        end if
        if {str(new_tab).lower()} then
            set t to make new tab at end of tabs of front window
            set URL of t to "{safe}"
            set active tab index of front window to (count of tabs of front window)
        else
            set URL of active tab of front window to "{safe}"
        end if
    end tell
    """
    return run_osascript(script)


def chrome_active_url() -> str:
    script = r"""
    tell application "Google Chrome"
        if (count of windows) = 0 then return ""
        return URL of active tab of front window
    end tell
    """
    return (run_osascript_out(script) or "").strip()


def yt_force_play() -> bool:
    js = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return "NO_VIDEO";
  if (v.paused) { v.play(); return "PLAY"; }
  return "ALREADY_PLAYING";
})();
"""
    res = chrome_execute_js(js)
    print("YT FORCE PLAY:", res)
    return "PLAY" in (res or "") or "ALREADY_PLAYING" in (res or "")


# ====================================================================================================
# Executing Commands
# ====================================================================================================


def parse_and_execute_command(user_text: str) -> str | None:
    """
    Returns a short assistant message if a command was executed.
    Returns None if this is not a command (so it should go to GPT).
    Safe: whitelist only.
    """
    t = normalize_text(user_text)
    t = normalize_text(user_text)

    # ---- EN commands ----
    if t == "weather" or t.startswith("weather "):
        loc = user_text[len("weather") :].strip()
        return get_weather_wttr(loc, current_lang)

    if t.startswith("open playlist ") and len(t) > len("open playlist "):
        pl = t[len("open playlist ") :].strip()  # use t (already cleaned)
        ok = mac_music_play_playlist(pl, shuffle=True)
        return "Opening playlist." if ok else "No results in Apple Music."

    if t.startswith("open "):
        target = t[len("open ") :].strip()
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_open_app(app_key)
            return "Opened." if ok else "I could not open it."
        return "That app is not in my allowed list."

    if t.startswith("switch to "):
        target = t[len("switch to ") :].strip()
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_open_app(app_key)
            return "Switched." if ok else "I could not switch."
        return "That app is not in my allowed list."

    if t.startswith("search for "):
        q = t[len("search for ") :].strip()
        if q:
            ok = mac_search_web(q)
            return "Searching." if ok else "I could not open the browser."
        return "Say the query."

    if t.startswith("turn on ") and len(t) > len("turn on "):
        q = t[len("turn on ") :].strip()
        ok = play_from_youtube_video(q)
        return "Ok" if ok else "Failed."

    if t.startswith("type "):
        content = user_text.strip()[len("type ") :].strip()  
        if content:
            ok = mac_type_text(content)
            return (
                "Typed." if ok else "I could not type. Check Accessibility permissions."
            )
        return "Say what to type."

    if t.startswith("press "):
        key = t[len("press ") :].strip()
        key_name = KEY_ALIASES_EN.get(key)
        if key_name:
            ok = mac_press_key(key_name)
            return "Done." if ok else "I could not press the key."
        return "Allowed keys: enter, tab, escape, space, backspace."

    if t in ("volume up", "louder"):
        ok = mac_volume(delta=6)
        return "Volume up." if ok else "I could not change volume."

    if t in ("volume down", "quieter"):
        ok = mac_volume(delta=-6)
        return "Volume down." if ok else "I could not change volume."

    if t == "mute":
        ok = mac_volume(mute=True)
        return "Muted." if ok else "I could not mute."

    if t in ("play", "pause", "post", "stop", "play/pause"):
        ok = mac_media("playpause")
        return "OK." if ok else "I could not control media."

    if t in ("close tab", "close the tab", "close this tab"):
        ok = chrome_close_tab()
        return "Closed tab." if ok else "I could not close the tab."

    if t in ("next track", "next"):
        ok = mac_media("next")
        return "Next." if ok else "I could not control media."

    if t in ("previous track", "previous", "back"):
        ok = mac_media("previous")
        return "Previous." if ok else "I could not control media."

    if t in ("screenshot", "take screenshot"):
        ok = mac_screenshot()
        return "Screenshot saved." if ok else "I could not take a screenshot."

    if t.startswith("close "):
        # --- RU: close Chrome tab (must be BEFORE "закрой <app>") ---
        if t in (
            "close tab",
            "закрой эту вкладку",
            "закрой таб",
            "закрой вкладку в хроме",
            "close chrome tab",
        ):
            ok = chrome_close_tab()
            return "ok" if ok else "failed"

        if t in (
            "close window",
            "close chrome window",
        ):
            ok = chrome_close_window()
            return "ok" if ok else "failed"

        if t in (
            "close chrome",
            "close google",
            "close google chrome",
        ):
            ok = chrome_close_all_tabs()
            return "ok" if ok else "failed"

        target = t[len("close ") :].strip()
        target = RU_APP_ALIASES.get(target, target)
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_quit_app(app_key)
            return "closed" if ok else "failed to close."
        return "this app is not in list."

    if t.startswith("quit "):
        target = t[len("quit ") :].strip()
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_quit_app(app_key)
            return "Quit." if ok else "I could not quit it."
        return "That app is not in my allowed list."

    if t.startswith("launch ") and len(t) > len("launch "):
        q = user_text[len("launch ") :].strip()
        ok = play_from_youtube_video(q)
        return "Okay" if ok else "Failed to open YouTube."

    if t.startswith("play ") and len(t) > len("play "):
        q = user_text[len("play ") :].strip()
        ok = play_from_youtube_video(q)
        return "Okay" if ok else "Failed. Check Accessibility."

    if t.startswith("turn on ") and len(t) > len("turn on "):
        q = user_text[len("turn on ") :].strip()
        ok = play_from_youtube_video(q)
        return "Okay" if ok else "Failed. Check Accessibility."

    # ---- RU commands -----------------------------------------

    if t == "погода" or t.startswith("погода "):
        loc = user_text[len("погода") :].strip()
        return get_weather_wttr(loc, current_lang)

    if t.startswith("включи ") and len(t) > len("включи "):
        q = user_text[len("включи ") :].strip()
        ok = play_from_youtube_video(q)
        return "Хорошо." if ok else "Не получилось. Проверь Accessibility."

    if t.startswith("поставь ") and len(t) > len("поставь "):
        q = user_text[len("поставь ") :].strip()
        ok = play_from_youtube_video(q)
        return "Хорошо. " if ok else "Не получилось. Проверь Accessibility."

    if t.startswith("открой плейлист ") and len(t) > len("открой плейлист "):
        pl = user_text.strip()[len("открой плейлист ") :].strip()
        ok = mac_music_play_playlist(pl, shuffle=True)
        return (
            "Включаю плейлист."
            if ok
            else "Не нашёл плейлист в Apple Music. Скажи точное название."
        )

    if t.startswith("открой "):
        target = t[len("открой ") :].strip()
        target = RU_APP_ALIASES.get(target, target)
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_open_app(app_key)
            return "Открыл." if ok else "Не получилось открыть."
        return "приложение не найдено"

    if t.startswith("переключись на "):
        target = t[len("переключись на ") :].strip()
        target = RU_APP_ALIASES.get(target, target)
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_open_app(app_key)
            return "Переключил." if ok else "Не получилось переключить."
        return "Этого приложения нет в списке разрешённых."

    if t.startswith("поиск "):
        q = user_text.strip()[len("поиск ") :].strip()
        if q:
            ok = mac_search_web(q)
            return "Ищу." if ok else "Не получилось открыть браузер."
        return "Скажи запрос."

    if t.startswith("напечатай "):
        content = user_text.strip()[len("напечатай ") :].strip()
        if content:
            ok = mac_type_text(content)
            return "Напечатал." if ok else "Не могу печатать. Проверь Accessibility."
        return "Скажи, что напечатать."

    if t.startswith("нажми "):
        key = t[len("нажми ") :].strip()
        key_name = KEY_ALIASES_RU.get(key)
        if key_name:
            ok = mac_press_key(key_name)
            return "Готово." if ok else "Не получилось нажать."
        return "Разрешённые клавиши: энтер, таб, эскейп, пробел, бэкспейс."

    if t in ("громче", "погромче"):
        ok = mac_volume(delta=6)
        return "Громче." if ok else "Не получилось."

    if t in ("тише", "потише"):
        ok = mac_volume(delta=-40)
        return "Тише." if ok else "Не получилось."

    if t in ("без звука", "мут"):
        ok = mac_volume(mute=True)
        return "Без звука." if ok else "Не получилось."

    if t in ("плей", "играй", "пауза", "плей пауза", "включи"):
        ok = mac_media("playpause")
        return "Ок" if ok else "Не получилось."

    if t in ("следующий трек", "следующая", "дальше"):
        ok = mac_media("next")
        return "Следующий." if ok else "Не получилось."

    if t in ("предыдущий трек", "предыдущая", "назад"):
        ok = mac_media("previous")
        return "Предыдущий." if ok else "Не получилось."

    if t in ("скриншот", "сделай скриншот"):
        ok = mac_screenshot()
        return "Скриншот сохранён." if ok else "Не получилось."

    if t.startswith("закрой "):
        # --- RU: close Chrome tab (must be BEFORE "закрой <app>") ---
        if t in (
            "закрой вкладку",
            "закрой эту вкладку",
            "закрой таб",
            "закрой вкладку в хроме",
            "закрой вкладку хром",
        ):
            ok = chrome_close_tab()
            return (
                "Закрыл вкладку."
                if ok
                else "Не получилось закрыть вкладку. Проверь Accessibility."
            )

        if t in ("закрой окно", "закрой окно хром", "закрой окно в хроме"):
            ok = chrome_close_window()
            return "Закрыл окно." if ok else "Повтори"

        if t in (
            "закрой хром",
            "закрой хром полностью",
            "закрой все вкладки",
            "выйди из хрома",
        ):
            ok = chrome_close_all_tabs()
            return "Ок." if ok else "Не получилось. Проверь Accessibility."

        target = t[len("закрой ") :].strip()
        target = RU_APP_ALIASES.get(target, target)
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_quit_app(app_key)
            return "Закрыл." if ok else "Не получилось закрыть."
        return "Не получилось"

    if t.startswith("выйди из "):
        target = t[len("выйди из ") :].strip()
        target = RU_APP_ALIASES.get(target, target)
        app_key = APP_ALIASES.get(target)
        if app_key:
            ok = mac_quit_app(app_key)
            return "Вышел." if ok else "Не получилось."
        return "Не найдено"

    return None


def handle_client(conn: socket.socket, addr):
    global is_awake, skip_next_final_after_wake

    print(f"Client {addr} connected")
    listening_led_on = False

    set_awake(conn, False)

    try:
        while True:
            data = conn.recv(1024)
            if not data:
                break

            data = handle_lang_markers(conn, data)
            if not data:
                continue

            if rec.AcceptWaveform(data):
                if listening_led_on:
                    send_line(conn, "__listening_off__")
                    listening_led_on = False

                res = json.loads(rec.Result())
                text = (res.get("text", "") or "").strip()
                if not text:
                    continue

                norm = normalize_text(text)
                print(f"[{current_lang}] FINAL: {norm}")

                # Sleeping: only wake word
                if not is_awake:
                    if detect_wake(norm):
                        set_awake(conn, True)
                        ack = "Да?" if current_lang == "ru" else "Yes?"
                        speak(conn, ack)
                    continue

                # Awake: suppress leftover final right after wake
                if skip_next_final_after_wake:
                    remainder = strip_leading_wake(norm)
                    if remainder == "":
                        skip_next_final_after_wake = False
                        continue
                    skip_next_final_after_wake = False
                    norm = remainder
                    text = remainder

                # Awake: sleep command
                if detect_sleep(norm):
                    set_awake(conn, False)
                    ack = "Сплю." if current_lang == "ru" else "Going to sleep."
                    speak(conn, ack)
                    continue

                # Strip wake word if user said "jarvis ..." while already awake
                stripped = strip_leading_wake(norm)
                if stripped != norm and stripped.strip() != "":
                    text = stripped
                    norm = normalize_text(stripped)
                elif stripped == "":
                    ack = "Да?" if current_lang == "ru" else "Yes?"
                    speak(conn, ack)
                    continue

                # ---- Voice language switch (works while awake) ----
                norm2 = normalize_text(text)

                if norm2 in LANG_EN_WORDS or norm2 in LANG_EN_WORDS_RU:
                    ok = set_language(conn, "en")
                    speak(
                        conn,
                        "Okay. English mode." if ok else "I couldn't switch language.",
                    )
                    continue

                if norm2 in LANG_RU_WORDS:
                    ok = set_language(conn, "ru")
                    speak(
                        conn,
                        (
                            "Ок. Русский режим."
                            if ok
                            else "Не получилось переключить язык."
                        ),
                    )
                    continue

                # Try safe command execution
                cmd_result = parse_and_execute_command(text)
                if cmd_result is not None:
                    speak(conn, cmd_result)
                    continue

                # Otherwise, normal GPT reply
                reply = generate_reply(text)
                speak(conn, reply)

            else:
                pres = json.loads(rec.PartialResult())
                ptext = (pres.get("partial", "") or "").strip()
                if not ptext:
                    continue

                pnorm = normalize_text(ptext)

                # sleeping: detect wake early, no LED spam
                if not is_awake:
                    if detect_wake(pnorm):
                        set_awake(conn, True)
                        ack = "Да?" if current_lang == "ru" else "Yes?"
                        speak(conn, ack)
                    continue

                if not listening_led_on:
                    send_line(conn, "__listening_on__")
                    listening_led_on = True

                print(f"[{current_lang}] PARTIAL: {pnorm}", end="\r")

    finally:
        conn.close()
        print("\nClient disconnected")


def mac_quit_app(app_name: str) -> bool:
    script = f'tell application "{app_name}" to quit'
    return run_osascript(script)


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(1)
        print(f"Server listening on {HOST}:{PORT}")
        threading.Thread(target=speak_worker, daemon=True).start()

        while True:
            conn, addr = s.accept()
            threading.Thread(
                target=handle_client, args=(conn, addr), daemon=True
            ).start()


if __name__ == "__main__":
    main()
