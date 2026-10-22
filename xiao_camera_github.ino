
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "esp_camera.h"
#include <ESP32_OV5640_AF.h>

/
const char* WIFI_SSID = "";
const char* WIFI_PASSWORD = "";
const char* PI_BASE_URL = "";


const framesize_t PHOTO_SIZE = FRAMESIZE_QSXGA; // 2560 x 1920, OV5640.
const int JPEG_QUALITY = 10;
const int BUTTON_PIN = 2; // D1/GPIO2 -> normally-open button -> GND.
const unsigned long FOCUS_TIMEOUT_MS = 12000;
const unsigned long RESULT_TIMEOUT_MS = 300000; // At most 5 min for Pi processing.
const size_t MAX_RESULT_BYTES = 65536;

OV5640 autofocus;
sensor_t* cameraSensor = nullptr;
camera_fb_t* latestFrame = nullptr;
bool cameraReady = false;
bool autofocusReady = false;
uint32_t captureId = 0;
const char* lastError = "";
String translatedText;  // Future OLED code can render this UTF-8 text.
String audioPath;       // Pi URL path only; playback hardware is not implemented yet.
int lastReading = HIGH;
int stableButton = HIGH;
unsigned long changedAt = 0;

bool afWrite(int reg, int value) {
  int rc = cameraSensor->set_reg(cameraSensor, reg, 0xff, value);
  if (rc != 0) Serial.printf("AF write failed: register=0x%04x value=0x%02x rc=%d\n", reg, value, rc);
  return rc == 0;
}

bool waitAfRegister(int reg, int expected, unsigned long timeoutMs) {
  unsigned long started = millis();
  do {
    int value = cameraSensor->get_reg(cameraSensor, reg, 0xff);
    if (value < 0) {
      Serial.printf("AF read failed: register=0x%04x\n", reg);
      return false;
    }
    if (value == expected) return true;
    delay(10);
  } while (millis() - started < timeoutMs);
  Serial.printf("AF wait timeout: register=0x%04x expected=0x%02x actual=0x%02x\n",
    reg, expected, cameraSensor->get_reg(cameraSensor, reg, 0xff));
  return false;
}

bool focusForPhoto() {
  if (!autofocusReady) {
    lastError = "Autofocus not initialized; check startup log and reset.";
    return false;
  }
  Serial.println("Focusing... hold still and aim at the text.");
  // Release the previous focus state, wait for idle, then start ONE focus scan.
  if (!afWrite(OV5640_CMD_ACK, 1) || !afWrite(OV5640_CMD_MAIN, 0x08) ||
      !waitAfRegister(OV5640_CMD_ACK, 0, 3000) ||
      !waitAfRegister(OV5640_CMD_FW_STATUS, FW_STATUS_S_IDLE, 3000)) {
    lastError = "Autofocus reset/idle failed; try resetting the board.";
    return false;
  }
  if (!afWrite(OV5640_CMD_ACK, 1) ||
      !afWrite(OV5640_CMD_MAIN, AF_TRIG_SINGLE_AUTO_FOCUS)) {
    lastError = "Autofocus register write failed; see the AF write error above.";
    return false;
  }
  // ACK=1 means the operation is still running, NOT a rejected command.
  // FW_STATUS=0x10 means the scan completed; it does not prove optical sharpness.
  unsigned long started = millis();
  unsigned long lastLog = 0;
  int previousState = -2;
  int previousAck = -2;
  while (millis() - started < FOCUS_TIMEOUT_MS) {
    int state = cameraSensor->get_reg(cameraSensor, OV5640_CMD_FW_STATUS, 0xff);
    int ack = cameraSensor->get_reg(cameraSensor, OV5640_CMD_ACK, 0xff);
    int command = cameraSensor->get_reg(cameraSensor, OV5640_CMD_MAIN, 0xff);
    if (state < 0 || ack < 0 || command < 0) {
      Serial.printf("AF read error: status=%d ack=%d command=%d\n", state, ack, command);
      lastError = "Autofocus register read failed.";
      return false;
    }
    if (state != previousState || ack != previousAck || millis() - lastLog >= 1000) {
      Serial.printf("AF t=%lu ms: status=0x%02x ack=0x%02x command=0x%02x\n",
        millis() - started, state, ack, command);
      previousState = state;
      previousAck = ack;
      lastLog = millis();
    }
    if (state == FW_STATUS_S_FOCUSED && ack == 0) {
      Serial.println("AF scan completed. Check the photo for sharpness.");
      delay(150);
      return true;
    }
    delay(50);
  }
  afWrite(OV5640_CMD_MAIN, 0x06); // Pause a scan that has not finished.
  lastError = "Autofocus timed out after 12 seconds. Send the AF status lines for diagnosis.";
  return false;
}

bool takePhoto() {
  lastError = "";
  if (!cameraReady) { lastError = "Camera not initialized."; return false; }
  if (!focusForPhoto()) { Serial.println(lastError); return false; }

  // Original sequence: focus first, release the held photo, discard two
  // frames, then keep the next JPEG. Discarded frames are never transmitted.
  if (latestFrame) {
    esp_camera_fb_return(latestFrame);
    latestFrame = nullptr;
  }
  for (int i = 0; i < 2; ++i) {
    camera_fb_t* old = esp_camera_fb_get();
    if (!old) { lastError = "Frame drain failed."; return false; }
    esp_camera_fb_return(old);
  }
  latestFrame = esp_camera_fb_get();
  if (!latestFrame) { lastError = "Capture failed."; return false; }
  if (latestFrame->format != PIXFORMAT_JPEG || latestFrame->len < 4) {
    esp_camera_fb_return(latestFrame);
    latestFrame = nullptr;
    lastError = "Invalid JPEG frame.";
    return false;
  }
  ++captureId;
  Serial.printf("Captured #%lu: %u x %u, %u bytes\n", (unsigned long)captureId,
    (unsigned)latestFrame->width, (unsigned)latestFrame->height,
    (unsigned)latestFrame->len);
  return true;
}

// Called only during setup, before Wi-Fi starts. No per-shot camera restart.
bool startCamera() {
  if (!psramFound()) {
    Serial.println("PSRAM missing: select OPI PSRAM and upload again.");
    return false;
  }
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 15; config.pin_d1 = 17; config.pin_d2 = 18; config.pin_d3 = 16;
  config.pin_d4 = 14; config.pin_d5 = 12; config.pin_d6 = 11; config.pin_d7 = 48;
  config.pin_xclk = 10; config.pin_pclk = 13; config.pin_vsync = 38; config.pin_href = 47;
  config.pin_sccb_sda = 40; config.pin_sccb_scl = 39;
  config.pin_pwdn = -1; config.pin_reset = -1;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = PHOTO_SIZE;
  config.jpeg_quality = JPEG_QUALITY;
  config.fb_count = 1;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return false;
  }
  cameraReady = true;
  cameraSensor = esp_camera_sensor_get();
  if (!cameraSensor || cameraSensor->id.PID != OV5640_PID) {
    Serial.println("This sketch requires the OV5640 camera.");
    esp_camera_deinit();
    cameraReady = false;
    cameraSensor = nullptr;
    return false;
  }
  Serial.println("Loading autofocus firmware...");
  autofocusReady = autofocus.start(cameraSensor) && autofocus.focusInit() == 0;
  if (!autofocusReady) {
    Serial.println("Autofocus initialization failed.");
    esp_camera_deinit();
    cameraReady = false;
    cameraSensor = nullptr;
    return false;
  }
  Serial.println("Camera and autofocus ready.");
  return true;
}

bool ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.println("Waiting for Wi-Fi...");
  WiFi.reconnect();
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 20000) delay(100);
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi unavailable. No capture uploaded. Try again after reconnecting.");
    return false;
  }
  return true;
}

void waitForResult(const String& jobId) {
  String url = String(PI_BASE_URL) + "/result/" + jobId;
  Serial.println("Pi is processing the photo...");
  unsigned long started = millis();
  unsigned long lastProgress = started;
  while (millis() - started < RESULT_TIMEOUT_MS) {
    delay(1500); // Poll only during this job, never when idle.
    if (WiFi.status() != WL_CONNECTED) continue;
    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(3000);
    http.setTimeout(10000);
    if (!http.begin(client, url)) continue;
    int code = http.GET();
    if (code == 202) {
      http.end();
      if (millis() - lastProgress >= 5000) {
        Serial.printf("Pi still processing: %lu s since upload accepted.\n", (millis() - started) / 1000);
        lastProgress = millis();
      }
      continue;
    }
    if (code < 0) {
      Serial.println("Result connection interrupted; checking this same job again.");
      http.end();
      continue;
    }
    if (http.getSize() < 0 || (size_t)http.getSize() > MAX_RESULT_BYTES) {
      Serial.println("Result length missing/too large. Read the full result on the Pi.");
      http.end();
      return;
    }
    String body = http.getString();
    http.end();
    if (code != 200) {
      Serial.printf("Pi result HTTP %d: %s\n", code, body.c_str());
      return;
    }
    JsonDocument result;
    DeserializationError error = deserializeJson(result, body);
    if (error) { Serial.println("Invalid result JSON; read the Pi terminal."); return; }
    if (String(result["job_id"] | "") != jobId) {
      Serial.println("Result ID mismatch; ignoring it.");
      return;
    }
    if (String(result["status"] | "") != "done") {
      Serial.print("Pi processing error: ");
      Serial.println(result["error"] | "Unknown error; see Pi terminal.");
      return;
    }
    Serial.printf("TIMING Pi result wait: %lu ms (OCR/translation/speech + polling/network).\n",
                  millis() - started);
    translatedText = result["translated_text"].as<String>();
    audioPath = result["audio_path"].is<const char*>() ? result["audio_path"].as<String>() : String();
    Serial.print("Result: "); Serial.println(result["result_status"] | "unknown");
    if (translatedText.length()) {
      Serial.println("TRANSLATED TEXT RECEIVED:");
      Serial.println(translatedText);
      // Future integration: draw translatedText on the OLED here.
    } else {
      Serial.println("No translated text. Check OCR / translation status on the Pi.");
    }
    if (audioPath.length()) {
      Serial.print("Speech WAV is ready on Pi: ");
      Serial.println(String(PI_BASE_URL) + audioPath);
      // Future integration: check the audio-enable switch, download WAV samples,
      // and play through an external I2S DAC + your partner's amplifier.
      // This version does NOT download/play audio or drive an OLED yet.
    }
    if (result["speech_error"].is<const char*>()) Serial.println(result["speech_error"].as<const char*>());
    return;
  }
  Serial.println("Result wait timed out. Check the Pi terminal; photo is not automatically resent.");
}

void captureAndUpload() {
  if (!cameraReady || !autofocusReady) {
    Serial.println("Camera/AF startup failed; reset after checking the startup log.");
    return;
  }
  if (!ensureWiFi()) return;
  translatedText = "";
  audioPath = "";
  Serial.printf("Wi-Fi RSSI: %d dBm\n", WiFi.RSSI());
  unsigned long captureStarted = millis();
  if (!takePhoto()) {
    Serial.println(lastError);
    return;
  }
  Serial.printf("TIMING focus + capture: %lu ms\n", millis() - captureStarted);
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(30000); // Pi accepts quickly; long OCR happens in a background job.
  if (!http.begin(client, String(PI_BASE_URL) + "/image")) {
    Serial.println("Invalid Pi URL.");
    return;
  }
  http.addHeader("Content-Type", "image/jpeg");
  Serial.println("Uploading one photo to the Pi...");
  const size_t uploadBytes = latestFrame->len;
  unsigned long uploadStarted = millis();
  int code = http.POST(latestFrame->buf, latestFrame->len);
  unsigned long uploadMs = millis() - uploadStarted;
  Serial.printf("TIMING upload request: %lu ms, %u bytes, HTTP %d\n",
                uploadMs, (unsigned)uploadBytes, code);
  if (code == 202 && uploadMs > 0) {
    Serial.printf("Upload effective rate: %.1f KiB/s\n", (uploadBytes / 1024.0) / (uploadMs / 1000.0));
  }
  // Retain latestFrame through upload/result-wait/idle. The next takePhoto()
  // returns it AFTER autofocus, matching the earlier capture lifecycle.
  if (code < 0) {
    Serial.print("Pi upload failed: "); Serial.println(HTTPClient::errorToString(code));
    http.end();
    Serial.println("Check Pi server, PI_BASE_URL, and network. No automatic photo retry.");
    return;
  }
  if (http.getSize() < 0 || http.getSize() > 4096) {
    Serial.println("Unexpected Pi upload response size.");
    http.end();
    return;
  }
  String body = http.getString();
  http.end();
  if (code != 202) {
    Serial.printf("Pi upload HTTP %d: %s\n", code, body.c_str());
    return;
  }
  JsonDocument response;
  if (deserializeJson(response, body) || !response["job_id"].is<const char*>()) {
    Serial.println("Pi did not return a valid job ID.");
    return;
  }
  String jobId = response["job_id"].as<String>();
  waitForResult(jobId);
}

void setup() {
  Serial.begin(115200);
  delay(2000); // Startup delay only; never wait for a USB/Serial host.
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  Serial.println("LEGACY CAPTURE + PI UPLOAD (revision 5)");
  Serial.println("Initializing camera and autofocus once at startup...");
  if (!startCamera()) {
    Serial.println("Camera startup failed. Check the messages above, then reset.");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.println("ESP32 upload client. There is NO ESP32 web server in this sketch.");
  Serial.print("Pi server: "); Serial.println(PI_BASE_URL);
  if (ensureWiFi()) { Serial.print("ESP32 IP: "); Serial.println(WiFi.localIP()); }
  Serial.println("Ready. Send c or press D1 button for ONE photo. No automatic capture at boot.");
}

void loop() {
  bool trigger = false;
  if (Serial.available()) {
    char command = Serial.read();
    trigger = command == 'c' || command == 'C';
  }
  int reading = digitalRead(BUTTON_PIN);
  if (reading != lastReading) changedAt = millis();
  lastReading = reading;
  if (millis() - changedAt >= 40 && reading != stableButton) {
    stableButton = reading;
    if (stableButton == LOW) trigger = true;
  }
  if (trigger) {
    captureAndUpload();
    // Ignore presses/Serial characters made while busy; no hidden capture queue.
    while (Serial.available()) Serial.read();
    lastReading = stableButton = digitalRead(BUTTON_PIN);
    changedAt = millis();
    Serial.println("Ready. Send c or press D1 button for the next photo.");
  }
  delay(10);
}
