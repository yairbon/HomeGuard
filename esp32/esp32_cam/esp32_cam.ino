// ==========================================
// HomeGuard - Camera worker (board 4: ESP32-CAM)
// ==========================================
//
// This board replaces this project's original assumption that the camera
// would be a USB webcam on the PC running python/camera_worker.py — this
// one had actually been sitting pre-flashed with the stock Arduino
// "CameraWebServer" example (its own little web page, on whatever WiFi
// network someone last configured it for). That's a fine way to eyeball
// the lens, but it's a dead end for this project: nothing here would ever
// pull frames from it, the same way nothing ever called /camera/recognize
// before camera_worker.py existed (see that file's own comment).
//
// So instead of *serving* a page, this board mirrors what camera_worker.py
// already does from a PC: capture a frame and POST it straight to the
// backend's /camera/recognize, authenticated with the shared device key.
// That endpoint doesn't care whether the sender is a PC with a USB webcam
// or a board like this one — same multipart upload either way — so this
// needed zero backend or app changes, only a new frame source.
//
// Pin numbers below are the standard AI-Thinker ESP32-CAM module wiring
// (this is the common, widely-sold variant — confirmed the right one here
// because it's the same layout the pre-existing CameraWebServer firmware
// was built for, and that firmware worked). A different ESP32-CAM board
// would need different pins.

#include "esp_camera.h"
#include "esp_http_server.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <WiFi.h>
#include <HTTPClient.h>

// See esp32/secrets.h.example. Copy it into this folder as secrets.h and
// fill in the same values used for the other three boards.
#include "secrets.h"

const char* ssid = WIFI_SSID;
const char* password = WIFI_PASSWORD;
const char* backendHost = BACKEND_HOST;
const int backendPort = BACKEND_PORT;

// --- AI-Thinker ESP32-CAM pin map ---
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// How often a frame is sent. Originally matched camera_worker.py's own
// default (1500ms, tuned for face-recognition cost, not visual smoothness)
// — dropped to 500ms for a noticeably more "live" feed once the app
// actually had a way to show one. Frame capture itself has no equivalent
// of the ultrasonic sensor's echo-settling constraint, so there's no
// physical floor here the way there was for CAPTURE_INTERVAL there — the
// real ceiling is how fast the backend can process a frame (recognition,
// when enabled, is the expensive part) and how much WiFi/CPU load is
// worth spending on a live view.
const unsigned long CAPTURE_INTERVAL_MS = 500;
unsigned long lastCapture = 0;

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // Smaller/lower-quality without PSRAM because the two frame buffers
  // FRAMESIZE_VGA needs don't fit in the ESP32's plain internal RAM once
  // WiFi + HTTPClient are also using it — this is the same fallback the
  // stock CameraWebServer example uses, kept here for the same reason.
  //
  // grab_mode/fb_location weren't set explicitly before (left as whatever
  // the stack held) — that was harmless for a single 500ms-interval shot,
  // but now that /stream (startCameraServer, below) and sendFrame() are two
  // independent consumers pulling frames concurrently, it matters:
  // CAMERA_GRAB_LATEST always hands back the newest frame and drops stale
  // ones rather than queuing, so a slow consumer doesn't build up lag over
  // a long-lived stream connection. This needs fb_count>=2 to actually help
  // (two consumers sharing one buffer just serialize on it), which is why
  // the no-PSRAM fallback below is fb_count=1 only — with no PSRAM, running
  // the stream and the recognition push at once will contend for that
  // single buffer and roughly halve both.
  if (psramFound()) {
    config.frame_size = FRAMESIZE_VGA;
    config.jpeg_quality = 12;
    config.fb_count = 2;
    config.fb_location = CAMERA_FB_IN_PSRAM;
    config.grab_mode = CAMERA_GRAB_LATEST;
  } else {
    config.frame_size = FRAMESIZE_CIF;
    config.jpeg_quality = 15;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
    config.grab_mode = CAMERA_GRAB_LATEST;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return false;
  }
  return true;
}

// Builds a minimal multipart/form-data body by hand (ESP32's HTTPClient has
// no multipart helper) and POSTs it to /camera/recognize — the same field
// name ("photo") and shape python/camera_worker.py sends, so the backend
// can't tell the two apart.
void sendFrame() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("Frame capture failed");
    return;
  }

  const String boundary = "HomeGuardCamBoundary";
  const String head = "--" + boundary +
                       "\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"frame.jpg\""
                       "\r\nContent-Type: image/jpeg\r\n\r\n";
  const String tail = "\r\n--" + boundary + "--\r\n";

  const size_t totalLen = head.length() + fb->len + tail.length();
  uint8_t* body = (uint8_t*)malloc(totalLen);
  if (!body) {
    Serial.println("Out of memory building the upload body — skipping this frame");
    esp_camera_fb_return(fb);
    return;
  }

  size_t pos = 0;
  memcpy(body + pos, head.c_str(), head.length());
  pos += head.length();
  memcpy(body + pos, fb->buf, fb->len);
  pos += fb->len;
  memcpy(body + pos, tail.c_str(), tail.length());

  // The frame buffer is only needed to fill `body` above — release it before
  // the (slow, blocking) network call so the camera driver can reuse it for
  // the next capture instead of sitting idle for the round trip.
  esp_camera_fb_return(fb);

  HTTPClient http;
  http.begin("http://" + String(backendHost) + ":" + String(backendPort) + "/camera/recognize");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);

  int code = http.POST(body, totalLen);
  if (code == 401) {
    Serial.println("Frame REJECTED (401) — DEVICE_KEY in secrets.h doesn't match the backend's.");
  } else if (code <= 0) {
    Serial.printf("Error sending frame: %s\n", http.errorToString(code).c_str());
  }
  // No success message on every frame — at ~0.67 fps that would still
  // flood the log; a 401 or connection error is the only thing worth
  // interrupting the console for.

  free(body);
  http.end();
}

// ---- Live stream server (new) ------------------------------------------
//
// Runs via esp_http_server (esp_http_server.h), NOT Arduino's WebServer
// class — that distinction is what makes this coexist with the sendFrame()
// push loop above at all. httpd_start() spins up its own FreeRTOS task
// automatically, so stream_handler's infinite per-client loop runs on a
// task of its own and never touches loop() — unlike Arduino's WebServer,
// where handleClient() (and by extension loop()) would block for the
// entire streaming session inside the handler, starving sendFrame()
// completely while any client is connected.
//
// The backend (python/backend/main.py, /camera/stream) connects here and
// relays the raw multipart bytes through to the app — this board never
// talks to the app directly, same "board is a client the backend fronts"
// shape as the other three boards, just inverted for this one endpoint
// (backend pulls, instead of board pushes).

static const char* STREAM_BOUNDARY = "\r\n--HomeGuardCamBoundary\r\n";
static const char* STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=HomeGuardCamBoundary";
static const char* STREAM_PART_HEADER = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

httpd_handle_t streamHttpd = NULL;

// Same shared DEVICE_KEY the rest of the fleet uses (secrets.h) — checked
// as an X-Device-Key header first (what the backend's httpx proxy sends,
// matching sendFrame()'s own auth), falling back to a ?key= query param
// (a browser/VLC/img-tag MJPEG viewer generally can't set a custom header
// the way HTTPClient/httpx can).
bool authorized(httpd_req_t* req) {
  char hdr[64];
  if (httpd_req_get_hdr_value_str(req, "X-Device-Key", hdr, sizeof(hdr)) == ESP_OK) {
    return strcmp(hdr, DEVICE_KEY) == 0;
  }
  char query[96], key[64];
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
      httpd_query_key_value(query, "key", key, sizeof(key)) == ESP_OK) {
    return strcmp(key, DEVICE_KEY) == 0;
  }
  return false;
}

esp_err_t streamHandler(httpd_req_t* req) {
  if (!authorized(req)) {
    httpd_resp_send_err(req, HTTPD_401_UNAUTHORIZED, NULL);
    return ESP_FAIL;
  }

  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;

  char partHeader[64];
  while (true) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
      res = ESP_FAIL;
    } else {
      res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
      if (res == ESP_OK) {
        size_t hlen = snprintf(partHeader, sizeof(partHeader), STREAM_PART_HEADER, fb->len);
        res = httpd_resp_send_chunk(req, partHeader, hlen);
      }
      if (res == ESP_OK) {
        res = httpd_resp_send_chunk(req, (const char*)fb->buf, fb->len);
      }
      esp_camera_fb_return(fb);
    }
    // A closed connection (app disconnected, or the backend's proxy gave
    // up) makes httpd_resp_send_chunk fail — that's the normal, expected
    // way this loop ends, not an error to recover from.
    if (res != ESP_OK) break;
  }
  return res;
}

void startCameraServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  httpd_uri_t streamUri = {
    .uri = "/stream",
    .method = HTTP_GET,
    .handler = streamHandler,
    .user_ctx = NULL,
  };
  if (httpd_start(&streamHttpd, &config) == ESP_OK) {
    httpd_register_uri_handler(streamHttpd, &streamUri);
    Serial.println("Stream server started on /stream");
  } else {
    Serial.println("Stream server failed to start");
  }
}

void setup() {
  // Cheap USB power (a marginal cable/hub, or a port shared with other
  // devices) can sag enough during the camera's init current spike to trip
  // the brownout detector and reset the board right as it's booting — a
  // known ESP32-CAM failure mode, not specific to this sketch. Disabling it
  // trades that false-positive protection for actually being able to boot;
  // the camera drawing real excess current would still show up as a hang or
  // corrupt frames, just not as a brownout reset loop.
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);

  Serial.begin(115200);
  Serial.println();

  if (!initCamera()) {
    Serial.println("FATAL: camera init failed. Halting.");
    while (true) delay(1000);
  }
  Serial.println("Camera ready.");

  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected!");
  Serial.print("My IP: ");
  Serial.println(WiFi.localIP());
  Serial.printf("Sending a frame to %s:%d every %lums\n", backendHost, backendPort, CAPTURE_INTERVAL_MS);

  startCameraServer();
}

void loop() {
  if (millis() - lastCapture >= CAPTURE_INTERVAL_MS) {
    if (WiFi.status() == WL_CONNECTED) {
      sendFrame();
    }
    lastCapture = millis();
  }
}
