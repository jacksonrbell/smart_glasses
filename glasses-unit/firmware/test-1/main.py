# main.py - Glasses unit first power-on test (with file logging)
# Target: Seeed XIAO ESP32S3 Sense, MicroPython (ESP32_GENERIC_S3 build)
# Requires: ssd1306.py driver on the board (Thonny: Tools > Manage packages > "ssd1306")
# Log: /log.txt on the board's flash (previous log rotated to /log_old.txt)

from machine import Pin, I2C, I2S
import machine, time, math, struct, os, sys, io

# ---------------- 1. Mute amps immediately ----------------
PIN_SD_MODE = 4                            # D3 (low = both amps shutdown/mute)
sd_mode = Pin(PIN_SD_MODE, Pin.OUT, value=0)

# ---------------- Settings ----------------
STARTUP_HOLD_MS = 1000     # settle time after power switch before init
BATT_POLL_MS    = 2000     # MAX17048 polling interval
BUZZ_PERIOD_MS  = 5000     # buzz every 5 s
BUZZ_MS         = 200      # buzz length
BUZZ_FREQ       = 441      # Hz (exactly 50 samples/cycle at 22.05 kHz)
BUZZ_AMP        = 8000     # 16-bit peak (~-12 dBFS); raise for louder
I2C_FREQ        = 100_000  # conservative for first bring-up
AUDIO_RATE      = 22050
I2S_IBUF        = 8000     # bytes (~90 ms of 16-bit stereo at 22.05 kHz)

LOG_FILE     = "/log.txt"
LOG_OLD      = "/log_old.txt"
LOG_MAX      = 64 * 1024   # bytes before rotating
BOOTCOUNT    = "/bootcount.txt"

# ---------------- Pin map (control PCB, Oct 21 2026) ----------------
PIN_BCLK, PIN_LRCLK, PIN_DIN = 1, 2, 3     # D0, D1, D2
PIN_SCL, PIN_SDA = 5, 6                    # D4, D5
PIN_BTN_MUTE, PIN_BTN_MODE, PIN_BTN_ACTION = 7, 8, 9   # D8, D9, D10

MAX17048_ADDR = 0x36
OLED_ADDRS = (0x3C, 0x3D)

# ---------------- Logging ----------------
def _rotate():
    try:
        if os.stat(LOG_FILE)[6] > LOG_MAX:
            try:
                os.remove(LOG_OLD)
            except OSError:
                pass
            os.rename(LOG_FILE, LOG_OLD)
    except OSError:
        pass  # no log yet

def log(msg):
    line = "%9d ms | %s" % (time.ticks_ms(), msg)
    print(line)
    try:
        with open(LOG_FILE, "a") as f:   # open/close each line: survives power-off
            f.write(line + "\n")
    except OSError as e:
        print("LOG WRITE FAILED:", e)

def log_exception(e):
    buf = io.StringIO()
    sys.print_exception(e, buf)
    log("EXCEPTION:\n" + buf.getvalue())

def next_boot_number():
    n = 0
    try:
        with open(BOOTCOUNT) as f:
            n = int(f.read())
    except (OSError, ValueError):
        pass
    n += 1
    try:
        with open(BOOTCOUNT, "w") as f:
            f.write(str(n))
    except OSError:
        pass
    return n

RESET_NAMES = {
    machine.PWRON_RESET: "PWRON",
    machine.HARD_RESET: "HARD",
    machine.WDT_RESET: "WDT",
    machine.DEEPSLEEP_RESET: "DEEPSLEEP",
    machine.SOFT_RESET: "SOFT",
}

_rotate()
log("===== BOOT %d | reset cause: %s =====" % (
    next_boot_number(),
    RESET_NAMES.get(machine.reset_cause(), str(machine.reset_cause()))))
log("Amps muted (SD_MODE low)")

i2s = None
try:
    # ---------------- 2. Button pull-ups ----------------
    btn_mute   = Pin(PIN_BTN_MUTE,   Pin.IN, Pin.PULL_UP)
    btn_mode   = Pin(PIN_BTN_MODE,   Pin.IN, Pin.PULL_UP)
    btn_action = Pin(PIN_BTN_ACTION, Pin.IN, Pin.PULL_UP)
    log("Button pull-ups enabled (D8-D10)")

    # ---------------- 3. Startup hold ----------------
    time.sleep_ms(STARTUP_HOLD_MS)
    log("Startup hold done (%d ms)" % STARTUP_HOLD_MS)

    # ---------------- 4. I2C + device discovery ----------------
    i2c = I2C(0, sda=Pin(PIN_SDA), scl=Pin(PIN_SCL), freq=I2C_FREQ)
    found = i2c.scan()
    log("I2C scan: %s" % [hex(a) for a in found])

    gauge_ok = MAX17048_ADDR in found
    log("MAX17048 (0x36): %s" % ("found" if gauge_ok else "NOT FOUND"))

    oled = None
    oled_addr = next((a for a in OLED_ADDRS if a in found), None)
    if oled_addr is None:
        log("OLED (0x3C/0x3D): NOT FOUND")
    else:
        try:
            import ssd1306
            # external_vcc=True: internal charge pump disabled (12V VCC from external converter)
            oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=oled_addr, external_vcc=True)
            oled.fill(0)
            oled.text("TEST", 48, 8)
            oled.show()
            log("OLED OK at %s, wrote TEST" % hex(oled_addr))
        except Exception as e:
            log("OLED init failed")
            log_exception(e)
            oled = None

    def read_soc():
        d = i2c.readfrom_mem(MAX17048_ADDR, 0x04, 2)   # SOC register
        return d[0] + d[1] / 256                         # percent

    def show_batt(text):
        if oled is None:
            return
        oled.fill_rect(0, 32, 128, 16, 0)
        oled.text(text, 0, 32)
        oled.show()

    # ---------------- 5. I2S + buzz buffers ----------------
    i2s = I2S(0, sck=Pin(PIN_BCLK), ws=Pin(PIN_LRCLK), sd=Pin(PIN_DIN),
              mode=I2S.TX, bits=16, format=I2S.STEREO,
              rate=AUDIO_RATE, ibuf=I2S_IBUF)
    log("I2S init OK (%d Hz, 16-bit stereo)" % AUDIO_RATE)

    period = AUDIO_RATE // BUZZ_FREQ                 # samples per cycle
    cycle = bytearray(period * 4)
    for n in range(period):
        s = int(BUZZ_AMP * math.sin(2 * math.pi * n / period))
        struct.pack_into("<hh", cycle, n * 4, s, s)  # L, R identical
    reps = max(1, BUZZ_MS * BUZZ_FREQ // 1000)
    tone = bytearray(len(cycle) * reps)
    for r in range(reps):
        tone[r * len(cycle):(r + 1) * len(cycle)] = cycle

    lead_silence = bytearray(int(AUDIO_RATE * 0.02) * 4)    # 20 ms (amp turn-on ~7 ms)
    tail_silence = bytearray(I2S_IBUF + 4 * 512)            # flush buffer with zeros
    drain_ms = int(len(tail_silence) / (AUDIO_RATE * 4) * 1000) + 30

    def buzz():
        sd_mode.value(1)            # unmute
        i2s.write(lead_silence)
        i2s.write(tone)
        i2s.write(tail_silence)
        time.sleep_ms(drain_ms)     # let queued audio finish
        sd_mode.value(0)            # mute

    # ---------------- 6. Main loop ----------------
    log("Entering main loop")
    next_batt = time.ticks_ms()
    next_buzz = time.ticks_add(time.ticks_ms(), BUZZ_PERIOD_MS)
    buzz_count = 0

    while True:
        now = time.ticks_ms()

        if time.ticks_diff(now, next_batt) >= 0:
            next_batt = time.ticks_add(now, BATT_POLL_MS)
            if gauge_ok:
                try:
                    soc = read_soc()
                    log("Battery: %.1f%%" % soc)
                    show_batt("BAT: %d%%" % int(soc))
                except OSError as e:
                    log("Gauge read failed: %s" % e)
                    show_batt("BAT: ERR")
            else:
                show_batt("BAT: N/A")

        if time.ticks_diff(now, next_buzz) >= 0:
            next_buzz = time.ticks_add(now, BUZZ_PERIOD_MS)
            buzz_count += 1
            buzz()
            log("Buzz #%d" % buzz_count)

        time.sleep_ms(20)

except KeyboardInterrupt:
    log("Stopped by user (Ctrl-C)")
except Exception as e:
    log("FATAL")
    log_exception(e)
finally:
    sd_mode.value(0)
    if i2s is not None:
        i2s.deinit()
    log("Amps muted, exiting")