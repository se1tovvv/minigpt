#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <Wire.h>
#include <U8g2lib.h>
#include "driver/i2s.h"
#include <math.h>

// ======== WIFI CONFIG ========
const char* WIFI_SSID     = "YOUR SSID";
const char* WIFI_PASSWORD = "YOUR PASSWORD";

// IP and server's port (PC / Mac)
const char*   SERVER_IP    = "YOUR IP";
const uint16_t SERVER_PORT = 6000;

WiFiClient client;

// ======== I2S MIC (INMP441) ========
#define I2S_WS   25   // LRCL / WS
#define I2S_SCK  26   // BCLK / SCK
#define I2S_SD   22   // DOUT from INMP441
#define I2S_PORT I2S_NUM_0

// ======== I2S SPEAKER (MAX98357A) ========
#define I2S_SPK_PORT  I2S_NUM_1
#define I2S_SPK_BCLK  14      // BCLK
#define I2S_SPK_LRCLK 27      // LRC
#define I2S_SPK_DOUT  13      // DIN
#define SPEAKER_SAMPLE_RATE 24000   // OpenAI PCM 24 kHz

// ======== LEDS & BUTTONS =========
#define LED_PIN         2     // onboard LED
#define LED_LISTEN_PIN 15     // "speaking/listening" LED
#define BTN_SCROLL_PIN  4     // scroll button (RIGHT), to GND, INPUT_PULLUP
#define BTN_LANG_PIN    16    // language button (LEFT), to GND, INPUT_PULLUP

// ======== I2C OLED ========
#define I2C_SDA 21
#define I2C_SCL 23

U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(
  U8G2_R0,
  U8X8_PIN_NONE,
  I2C_SCL,
  I2C_SDA
);

bool g_oledOK = false;

// ======== AUDIO CONFIG (MIC) ========
#define SAMPLE_RATE       16000
#define SAMPLES_PER_BLOCK 512

// ======== TEXT / SCROLL STATE ========
String g_lastReply   = "";
int    g_scrollOffset = 0;
int    g_totalLines   = 0;

const int LINE_HEIGHT        = 8;
const int TEXT_TOP           = 12;


// ======== STREAM / AUDIO PLAYBACK FLAGS ========
bool   g_pauseStream         = false;  // true -> do not send mic (used only while TTS speaking)
bool   g_audioPlaying        = false;  // true -> reading PCM from server
size_t g_audioBytesRemaining = 0;

// ======== LANGUAGE STATE ========
bool   lang_ru       = true;   // true = Russian, false = English
bool   lang_selected = false;  // language chosen on startup

// ======== WAKE/SLEEP DISPLAY STATE ========
bool   g_isAwake = false;      // server-controlled (display only). mic still streams for wake word detection.

// -------- prototypes --------
void showOledMessage(const char* header, const String& body = "");
int  countWrappedLines(const String& text);
void drawWrappedUTF8_fromOffset(const String& text, int x, int y, int lineHeight, int startLine);
void renderReply();
void i2s_install_mic();
void i2s_set_pins_mic();
void i2s_install_speaker();
void ensure_connection();
void speaker_test_beep();
void selectLanguageOnce();

// ================== SETUP ==================
void setup() {
  Serial.begin(115200);
  delay(1000);

  // I2C + OLED
  Wire.begin(I2C_SDA, I2C_SCL);
  u8g2.begin();
  g_oledOK = true;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_5x7_t_cyrillic);
  u8g2.drawUTF8(0, 14, "Загрузка...");
  u8g2.sendBuffer();

  // LEDs
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  pinMode(LED_LISTEN_PIN, OUTPUT);
  digitalWrite(LED_LISTEN_PIN, LOW);

  // Buttons
  pinMode(BTN_SCROLL_PIN, INPUT_PULLUP);
  pinMode(BTN_LANG_PIN,   INPUT_PULLUP);

  // WiFi
  if (g_oledOK) {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_5x7_t_cyrillic);
    u8g2.drawUTF8(0, 14, "WiFi подключение");
    u8g2.sendBuffer();
  }

  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
  }
  Serial.println();
  Serial.println("WiFi connected!");
  Serial.print("ESP32 IP: ");
  Serial.println(WiFi.localIP());
  digitalWrite(LED_PIN, HIGH);

  if (g_oledOK) {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_5x7_t_cyrillic);
    u8g2.drawUTF8(0, 14, "WiFi OK");
    String ip = "IP: " + WiFi.localIP().toString();
    u8g2.drawUTF8(0, 32, ip.c_str());
    u8g2.sendBuffer();
  }

  // I2S mic + speaker
  i2s_install_mic();
  i2s_set_pins_mic();
  i2s_zero_dma_buffer(I2S_PORT);

  i2s_install_speaker();
  speaker_test_beep();   // short beep to verify speaker

  // TCP
  ensure_connection();

  // ====== LANGUAGE SELECTION ON STARTUP ======
  selectLanguageOnce();

  // Start in "sleeping" UI until server wakes it
  g_isAwake = false;
  showOledMessage("Mode:", "Sleeping. Say: Jarvis / Assistant");
}

// ================== LOOP ==================
void loop() {
  static int32_t i2s_buffer[SAMPLES_PER_BLOCK];
  static int16_t pcm16[SAMPLES_PER_BLOCK];

  ensure_connection();
  if (!client.connected()) {
    delay(1000);
    return;
  }

  // ---- MIC -> SERVER ----
  // Keep streaming even while "sleeping" so server can detect wake word.
  // Only pause during TTS playback (g_audioPlaying) or server speaking markers (g_pauseStream).
  if (!g_pauseStream && !g_audioPlaying) {
    size_t bytes_read = 0;
    esp_err_t res = i2s_read(
        I2S_PORT,
        (void*)i2s_buffer,
        SAMPLES_PER_BLOCK * sizeof(int32_t),
        &bytes_read,
        10
    );

    if (res == ESP_OK && bytes_read > 0) {
      size_t n = bytes_read / sizeof(int32_t);
      for (size_t i = 0; i < n; i++) {
        int32_t s = i2s_buffer[i] >> 12;
        if (s > 32767)  s = 32767;
        if (s < -32768) s = -32768;
        pcm16[i] = (int16_t)s;
      }
      size_t to_send = n * sizeof(int16_t);
      client.write((uint8_t*)pcm16, to_send);
    }
  }

  // ---- RECEIVE PCM FOR SPEAKER ----
  if (g_audioPlaying && g_audioBytesRemaining > 0 && client.available() > 0) {
    uint8_t buf[256];
    size_t toRead = g_audioBytesRemaining;
    if (toRead > sizeof(buf)) toRead = sizeof(buf);
    if (toRead > (size_t)client.available()) toRead = client.available();

    int actuallyRead = client.read(buf, toRead);
    if (actuallyRead > 0) {
      size_t written = 0;
      i2s_write(I2S_SPK_PORT, buf, actuallyRead, &written, 50);

      if (g_audioBytesRemaining >= (size_t)actuallyRead)
        g_audioBytesRemaining -= (size_t)actuallyRead;
      else
        g_audioBytesRemaining = 0;

      if (g_audioBytesRemaining == 0) {
        g_audioPlaying = false;
      }
    }
  }

  // ---- TEXT COMMANDS / REPLIES FROM SERVER ----
  if (!g_audioPlaying) {
    while (client.available()) {
      String line = client.readStringUntil('\n');
      line.trim();
      if (line.length() == 0) continue;

      Serial.print("SERVER (text): ");
      Serial.println(line);

      if (line == "LANG_RU_OK" || line == "LANG_EN_OK") {
        continue;
      }

      // Server wake/sleep markers (NEW)
      if (line == "__awake__") {
        g_isAwake = true;
        digitalWrite(LED_LISTEN_PIN, LOW);
        showOledMessage("Mode:", "Awake. Speak now.");
        continue;
      }
      if (line == "__sleeping__") {
        g_isAwake = false;
        digitalWrite(LED_LISTEN_PIN, LOW);
        showOledMessage("Mode:", "Sleeping. Say: Jarvis / Assistant");
        continue;
      }

      if (line == "__listening_on__") {
        // Only show listen LED if awake
        if (g_isAwake) digitalWrite(LED_LISTEN_PIN, HIGH);
        continue;
      }
      if (line == "__listening_off__") {
        digitalWrite(LED_LISTEN_PIN, LOW);
        continue;
      }

      if (line == "__speaking_on__") {
        g_pauseStream = true;
        continue;
      }
      if (line == "__speaking_off__") {
        g_pauseStream = false;
        continue;
      }

      if (line.startsWith("__audio_len__")) {
        int spacePos = line.indexOf(' ');
        if (spacePos > 0) {
          String numStr = line.substring(spacePos + 1);
          numStr.trim();
          g_audioBytesRemaining = (size_t)numStr.toInt();
          if (g_audioBytesRemaining > 0) {
            g_audioPlaying = true;
            Serial.print("Expect audio bytes: ");
            Serial.println((unsigned long)g_audioBytesRemaining);
          }
        }
        break;  // let PCM handler read raw bytes
      }

      // normal GPT reply -> OLED
      g_lastReply    = line;
      g_totalLines   = countWrappedLines(g_lastReply);
      g_scrollOffset = 0;
      renderReply();
    }
  }

  // ---- SCROLL BUTTON ----
  static uint32_t lastScrollTime = 0;
  static bool     lastScrollState = HIGH;

  bool curScroll = digitalRead(BTN_SCROLL_PIN);
  uint32_t now = millis();

  if (lastScrollState == HIGH && curScroll == LOW && (now - lastScrollTime > 150)) {
    lastScrollTime = now;

    int visibleLines = (64 - TEXT_TOP) / LINE_HEIGHT;
    if (visibleLines < 1) visibleLines = 1;

    int maxOffset = 0;
    if (g_totalLines > visibleLines) {
      maxOffset = g_totalLines - visibleLines;
    }

    if (g_scrollOffset < maxOffset) {
      g_scrollOffset++;
      renderReply();
    }
  }

  lastScrollState = curScroll;
}

// ================== LANGUAGE SELECTION SCREEN ==================
void selectLanguageOnce() {
  if (lang_selected) return;

  if (g_oledOK) {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_5x7_t_cyrillic);
    u8g2.drawUTF8(0, 14, "Выбери язык:");
    u8g2.drawUTF8(0, 32, "LEFT (GPIO16) = RU");
    u8g2.drawUTF8(0, 44, "RIGHT (BTN4)  = EN");
    u8g2.sendBuffer();
  }

  Serial.println("Waiting for language selection...");

  while (!lang_selected) {
    bool left  = (digitalRead(BTN_LANG_PIN)   == LOW);
    bool right = (digitalRead(BTN_SCROLL_PIN) == LOW);

    if (left) {
      lang_ru = true;
      lang_selected = true;
    } else if (right) {
      lang_ru = false;
      lang_selected = true;
    }
    delay(10);
  }

  if (client.connected()) {
    if (lang_ru) {
      client.write((const uint8_t*)"__lang_ru__", 11);
      Serial.println("LANG -> RU (sent)");
    } else {
      client.write((const uint8_t*)"__lang_en__", 11);
      Serial.println("LANG -> EN (sent)");
    }
  }

  if (g_oledOK) {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_5x7_t_cyrillic);
    if (lang_ru) {
      u8g2.drawUTF8(0, 20, "ЯЗЫК: РУССКИЙ");
    } else {
      u8g2.drawUTF8(0, 20, "LANG: ENGLISH");
    }
    u8g2.sendBuffer();
    delay(800);
  }
}

// ================== OLED helpers ==================
int countWrappedLines(const String& text) {
  if (!g_oledOK) return 0;

  int lines = 0;
  int start = 0;
  int len = text.length();

  while (start < len) {
    int end = start;
    String line = "";

    while (end < len) {
      String test = line + text[end];
      if (u8g2.getUTF8Width(test.c_str()) > 128) {
        break;
      }
      line = test;
      end++;
    }

    if (end == start) end++;  // safety

    lines++;
    start = end;
  }

  return lines;
}


void drawWrappedUTF8_fromOffset(
  const String& text,
  int x,
  int y,
  int lineHeight,
  int startLine
) {
  if (!g_oledOK) return;

  int screenWidth = 128;
  int start = 0;
  int len = text.length();
  int currentLine = 0;

  while (start < len && y < 64) {
    int end = start;
    String line = "";

    while (end < len) {
      String test = line + text[end];
      if (u8g2.getUTF8Width(test.c_str()) > screenWidth) {
        break;
      }
      line = test;
      end++;
    }

    if (end == start) end++;  // safety

    if (currentLine >= startLine) {
      u8g2.drawUTF8(x, y, line.c_str());
      y += lineHeight;
    }

    currentLine++;
    start = end;
  }
}


void renderReply() {
  if (!g_oledOK) return;
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_5x7_t_cyrillic);
  drawWrappedUTF8_fromOffset(g_lastReply, 0, TEXT_TOP, LINE_HEIGHT, g_scrollOffset);
  u8g2.sendBuffer();
}

void showOledMessage(const char* header, const String& body) {
  if (!g_oledOK) return;

  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_5x7_t_cyrillic);
  int y = 14;
  if (header && strlen(header) > 0) {
    u8g2.drawUTF8(0, y, header);
    y += 12;
  }
  drawWrappedUTF8_fromOffset(body, 0, y, LINE_HEIGHT, 0);
  u8g2.sendBuffer();
}

// ================== I2S MIC ==================
void i2s_install_mic() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format =
        (i2s_comm_format_t)(I2S_COMM_FORMAT_I2S | I2S_COMM_FORMAT_I2S_MSB),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 64,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };
  if (i2s_driver_install(I2S_PORT, &cfg, 0, NULL) != ESP_OK) {
    Serial.println("I2S mic driver install failed");
    while (true) delay(1000);
  }
  i2s_set_clk(I2S_PORT, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_MONO);
}

void i2s_set_pins_mic() {
  i2s_pin_config_t pin_cfg = {
    .bck_io_num = I2S_SCK,
    .ws_io_num = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_SD
  };
  if (i2s_set_pin(I2S_PORT, &pin_cfg) != ESP_OK) {
    Serial.println("I2S mic set pins failed");
    while (true) delay(1000);
  }
}

// ================== I2S SPEAKER ==================
void i2s_install_speaker() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SPEAKER_SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format =
        (i2s_comm_format_t)(I2S_COMM_FORMAT_I2S | I2S_COMM_FORMAT_I2S_MSB),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 4,
    .dma_buf_len = 256,
    .use_apll = false,
    .tx_desc_auto_clear = true,
    .fixed_mclk = 0
  };
  if (i2s_driver_install(I2S_SPK_PORT, &cfg, 0, NULL) != ESP_OK) {
    Serial.println("I2S speaker driver install failed");
    while (true) delay(1000);
  }

  i2s_pin_config_t pin_cfg = {
    .bck_io_num   = I2S_SPK_BCLK,
    .ws_io_num    = I2S_SPK_LRCLK,
    .data_out_num = I2S_SPK_DOUT,
    .data_in_num  = I2S_PIN_NO_CHANGE
  };
  if (i2s_set_pin(I2S_SPK_PORT, &pin_cfg) != ESP_OK) {
    Serial.println("I2S speaker set pins failed");
    while (true) delay(1000);
  }

  i2s_set_clk(I2S_SPK_PORT, SPEAKER_SAMPLE_RATE, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
  Serial.println("I2S speaker initialized");
}

// короткий писк для проверки динамика
void speaker_test_beep() {
  Serial.println("Speaker test: 1kHz beep");
  const int freq = 1000;
  const int sampleRate = SPEAKER_SAMPLE_RATE;
  const float duration = 0.4f;
  int totalSamples = (int)(duration * sampleRate);

  int16_t buffer[256];
  int generated = 0;

  while (generated < totalSamples) {
    int chunk = totalSamples - generated;
    if (chunk > (int)(sizeof(buffer) / sizeof(buffer[0])))
      chunk = sizeof(buffer) / sizeof(buffer[0]);

    for (int i = 0; i < chunk; ++i) {
      int sampleIndex = generated + i;
      float t = (float)sampleIndex / sampleRate;
      float s = sinf(2.0f * PI * freq * t);
      buffer[i] = (int16_t)(s * 25000.0f);
    }

    size_t written = 0;
    i2s_write(I2S_SPK_PORT, buffer, chunk * sizeof(int16_t), &written, 100);
    generated += chunk;
  }
  Serial.println("Speaker test done");
}

// ================== TCP ==================
void ensure_connection() {
  if (client.connected()) return;

  Serial.printf("Connecting to server %s:%u...\n", SERVER_IP, SERVER_PORT);
  if (client.connect(SERVER_IP, SERVER_PORT)) {
    Serial.println("Server connected");
    client.println("HELLO ESP32 PCM16 16000");
    showOledMessage("Сервер:", "подключен");
    delay(800);
  } else {
    Serial.println("Server connect failed");
    showOledMessage("Сервер:", "ошибка соединения");
    delay(1000);
  }
}
