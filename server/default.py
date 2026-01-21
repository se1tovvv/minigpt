#The following model allows user to use the wake word to activate the system and ask questions. To be able to control PC/laptop, see advanced.py

import socket
import json
import threading

import config
from vosk import Model, KaldiRecognizer
from openai import OpenAI

client = OpenAI(api_key=config.OPENAI_API_KEY)

# ===== MODELS =====
MODEL_RU = "PATH TO MODEL , ex: /Users/sam/Downloads/vosk-model-ru-0.22"
MODEL_EN = "PATH TO MODEL"
SAMPLE_RATE = 16000

# ===== TCP CONFIG =====
HOST = "0.0.0.0"
PORT = 6000

# ===== TCP CONFIG =====
HOST = "0.0.0.0"
PORT = 6000

print("Loading RU model...")
model_ru = Model(MODEL_RU)

print("Loading EN model...")
model_en = Model(MODEL_EN)

# Current STT language (chosen by ESP32 language buttons)
current_lang = "ru"
rec = KaldiRecognizer(model_ru, SAMPLE_RATE)

# ===== SIMPLE MEMORY =====
conversation_history = []
HISTORY_LIMIT = 10

# ===== WAKE/SLEEP WORDS =====
WAKE_WORDS_EN = {"jarvis", "assistant"}
WAKE_WORDS_RU = {"джарвис", "жарвис", "ассистент"}

SLEEP_WORDS_EN = {"sleep"}
SLEEP_WORDS_RU = {"слип", "усни", "спи", "засни"}

# Start sleeping by default
is_awake = False

# When we wake, we skip the next FINAL if it still contains only wake word.
skip_next_final_after_wake = False


def normalize_text(s: str) -> str:
    return (s or "").strip().lower()


def tokens(s: str):
    # simple tokenization
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
    return contains_any_token(t, SLEEP_WORDS_EN) or contains_any_token(t, SLEEP_WORDS_RU)


def strip_leading_wake(text: str) -> str:
    """
    If user says: "jarvis what's time" -> "what's time"
    If user says only: "jarvis" -> ""
    Works for RU/EN wake words.
    """
    tks = tokens(text)
    if not tks:
        return ""

    wake_vocab = WAKE_WORDS_EN.union(WAKE_WORDS_RU)

    # Remove wake words from the beginning (sometimes STT repeats: "jarvis jarvis ...")
    i = 0
    while i < len(tks) and tks[i] in wake_vocab:
        i += 1

    return " ".join(tks[i:]).strip()


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
        "Short, clear sentences. Year is 2026. No markdown, no lists. "
    )

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}, *recent],
            temperature=0.1,
            max_tokens=120,
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


def send_line(conn: socket.socket, s: str):
    try:
        conn.sendall((s + "\n").encode("utf-8"))
    except OSError:
        pass


def reset_recognizer():
    """
    Critical: clears buffered audio inside Vosk so wake word doesn't show up as next FINAL.
    """
    global rec, current_lang
    if current_lang == "ru":
        rec = KaldiRecognizer(model_ru, SAMPLE_RATE)
    else:
        rec = KaldiRecognizer(model_en, SAMPLE_RATE)


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
        reset_recognizer()  # IMPORTANT
    else:
        print("STATE -> SLEEPING")
        send_line(conn, "__sleeping__")
        send_line(conn, "__listening_off__")
        skip_next_final_after_wake = False
        reset_recognizer()  # optional but keeps state clean


def speak_ack(conn: socket.socket, text: str):
    # also prints to OLED as text line
    try:
        conn.sendall((text + "\n").encode("utf-8"))
    except OSError:
        return

    audio = tts_bytes(text)
    if audio:
        send_line(conn, "__speaking_on__")
        header = f"__audio_len__ {len(audio)}\n"
        try:
            conn.sendall(header.encode("utf-8"))
            conn.sendall(audio)
        except OSError:
            return
        send_line(conn, "__speaking_off__")


def handle_client(conn: socket.socket, addr):
    global current_lang, rec, is_awake, skip_next_final_after_wake

    print(f"Client {addr} connected")
    listening_led_on = False

    # Start sleeping by default
    set_awake(conn, False)

    try:
        while True:
            data = conn.recv(2048)
            if not data:
                break

            # handle language markers
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

                # If sleeping: only react to wake words
                if not is_awake:
                    if detect_wake(norm):
                        set_awake(conn, True)
                        ack = "Да, слушаю." if current_lang == "ru" else "Yes. I'm listening."
                        speak_ack(conn, ack)
                    continue

                # If awake: suppress a leftover final right after wake
                if skip_next_final_after_wake:
                    # If this final is still just wake word (or starts with it), ignore it.
                    remainder = strip_leading_wake(norm)
                    if remainder == "":
                        # consume one final and stop skipping
                        skip_next_final_after_wake = False
                        continue
                    # If there is actual content after wake word, use it (but don't skip anymore)
                    skip_next_final_after_wake = False
                    norm = remainder
                    text = remainder  # feed cleaned text to GPT

                # If awake: check sleep command first
                if detect_sleep(norm):
                    set_awake(conn, False)
                    ack = "Ок. Сплю." if current_lang == "ru" else "Okay. Going to sleep."
                    speak_ack(conn, ack)
                    continue

                # If the user starts with wake word while already awake: strip it
                stripped = strip_leading_wake(norm)
                if stripped != norm and stripped.strip() != "":
                    text = stripped
                elif stripped == "":
                    # user said only "jarvis" while already awake -> don't send to GPT
                    ack = "Да?" if current_lang == "ru" else "Yes?"
                    speak_ack(conn, ack)
                    continue

                # Normal GPT reply
                reply = generate_reply(text)

                # text to OLED
                try:
                    conn.sendall((reply + "\n").encode("utf-8"))
                except OSError:
                    break

                # TTS
                audio = tts_bytes(reply)
                if audio:
                    send_line(conn, "__speaking_on__")
                    header = f"__audio_len__ {len(audio)}\n"
                    try:
                        conn.sendall(header.encode("utf-8"))
                        conn.sendall(audio)
                    except OSError:
                        break
                    send_line(conn, "__speaking_off__")

            else:
                pres = json.loads(rec.PartialResult())
                ptext = (pres.get("partial", "") or "").strip()
                if not ptext:
                    continue

                pnorm = normalize_text(ptext)

                # While sleeping: detect wake early, but don't spam LEDs
                if not is_awake:
                    if detect_wake(pnorm):
                        set_awake(conn, True)
                        ack = "Да, слушаю." if current_lang == "ru" else "Yes. I'm listening."
                        speak_ack(conn, ack)
                    continue

                # Awake: show listening LED when partial appears
                if not listening_led_on:
                    send_line(conn, "__listening_on__")
                    listening_led_on = True

                print(f"[{current_lang}] PARTIAL: {pnorm}", end="\r")

    finally:
        conn.close()
        print("\nClient disconnected")


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(1)
        print(f"Server listening on {HOST}:{PORT}")

        while True:
            conn, addr = s.accept()
            threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True,
            ).start()


if __name__ == "__main__":
    main()
