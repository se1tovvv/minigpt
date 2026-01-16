# minigpt
MiniGPT is a **real-time voice assistant** built using an ESP32, an I2S microphone and speaker,
and a Python server running OpenAI APIs.  
It listens to the user, live-transcribes speech using Vosk, sends the text to GPT,
and replies back **with both OLED text and spoken audio

Features
- Live speech recognition using I2S INMP441 microphone
- Real-time GPT replies using OpenAI API
- Answer displayed on **128x64 OLED**
- Answer spoken through **I2S MAX98357A + speaker**
- Two language modes: **Russian & English**
- Scrollable OLED output text
- Voice activity LED indicator
- Fully offline STT (0 internet required after model download)
- Modular design — swap ESP32 or peripherals via headers


## Software used:
- Arduino IDE
- VS code

## Hardware used:
- INMP441 I2S Microphone module
- MAX98357A I2S Speaker module
- Speaker
- OLED I2C Display 128x64 IIC
- ESP32-32U
- Power swithc
- SMD Buttons x2
- LED x2
- Female Header & Male connectors
- Solder plate 7x9cm
- TP4056 Battery charge module
- Li-po 3.7V 250 mAh battery

##  Usage Flow
1. ESP32 connects to Wi-Fi and server
2. OLED asks user to select language (`RU` or `EN`)
3. User speaks into microphone
4. Vosk picks up speech → sends text to GPT
5. GPT returns a short answer
6. ESP32:
   - prints text on OLED
   - streams 24kHz PCM to MAX98357A
   - LED indicates speech vs. silence
7. User scrolls multi-line responses with scroll SMD button

## 🏁 Installation (Quick Guide)

### 1. Clone repo
```bash
git clone https://github.com/se1tovvм/minigpt.git
2. Download Vosk models

Place in /models.

3. Install Python deps
pip install vosk openai

4. Add OpenAI key

Create config.py:

OPENAI_API_KEY="your key"

5. Flash ESP32 firmware (Arduino IDE)
6. Run server
python server/minigpt_server.py



##WIRING

| INMP441 Pin | ESP32 Pin | Notes              |
| ----------- | --------- | ------------------ |
| VCC         | 3V3       | Power              |
| GND         | GND       | Ground             |
| SCK (BCLK)  | 26     | Bit clock          |
| WS (LRCLK)  | 25     | Left/right clock   |
| SD          | 22      | Data               |
| L/R         | GND       | Force left channel |

| MAX98357A Pin | ESP32 Pin | Notes       |
| ------------- | --------- | ----------- |
| VIN           | 3V3       | Power       |
| GND           | GND       | Ground      |
| BCLK          | 14      | Bit clock   |
| LRCLK/WSEL    | 27      | Word select |
| DIN           | 13      | Data        |
| SD_MODE       | not used       | Mono        |
| GAIN          |not used     | Default     |
| SPK+          | Speaker+  | Output      |
| SPK−          | Speaker−  | Output      |


| OLED Pin | ESP32 Pin | Notes     |
| -------- | --------- | --------- |
| VCC      | 3V3       | Power     |
| GND      | GND       | Ground    |
| SDA      | 21      | I2C data  |
| SCL      | 23      | I2C clock |


| Button          | ESP32 Pin | Notes                        |
| --------------- | --------- | ---------------------------- |
| Language  RU select | 16    | One pin → ESP32, other → GND |
| Scroll+EN text     | 4      | One pin → ESP32, other → GND |


| LED            | ESP32 Pin | Notes                                   |
| -------------- | --------- | --------------------------------------- |
| Wi-Fi/Thinking | 2      | Anode → resistor → ESP32, cathode → GND |
| Mic Active     | 15       | Same wiring                             |


| TP4056 Pin | Connects To                   |
| ---------- | ----------------------------- |
| B+         | Li-Po +                       |
| B−         | Li-Po −                       |
| OUT+       | Power switch → ESP32 3V3 rail |
| OUT−       | GND                           |



Note : There is a schematic in main folder.It was done in Kicad, where exact model from esp32u can be different from this table for some GPIOS like LEDs. Feel free to use ESP32 pinout to assign the GPIOS as needed.

