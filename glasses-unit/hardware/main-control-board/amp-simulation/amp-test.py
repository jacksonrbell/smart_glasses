from machine import I2S, I2C, Pin
import struct, time, micropython
from array import array

print("Starting test...")

WAV_FILE    = "tts.wav"
SHUNT_OHMS  = 0.1
INA_ADDR    = 0x40
GAIN_GPIO   = 15
IBUF        = 20000

# ---------- INA219 ----------
i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=400_000)
i2c.writeto_mem(INA_ADDR, 0x00, struct.pack(">H", 0x1DDF))

def read_vi():
    raw_i = struct.unpack(">h", i2c.readfrom_mem(INA_ADDR, 0x01, 2))[0]
    raw_v = struct.unpack(">H", i2c.readfrom_mem(INA_ADDR, 0x02, 2))[0]
    return (raw_v >> 3) * 0.004, raw_i * 0.01 / SHUNT_OHMS   # V, mA

# ---------- WAV header ----------
f = open(WAV_FILE, "rb")
f.read(12)
while True:
    cid, clen = struct.unpack("<4sI", f.read(8))
    if cid == b"fmt ":
        tag, ch, rate, byterate, _, bits = struct.unpack("<HHIIHH", f.read(clen + (clen & 1))[:16])
    elif cid == b"data":
        data_start = f.tell(); data_len = clen
        break
    else:
        f.read(clen + (clen & 1))
assert tag == 1 and bits == 16, "need 16-bit PCM WAV"
print("WAV: %d Hz, %d ch, %.2f s" % (rate, ch, data_len / byterate))

# ---------- Compressor (compiled separately so errors can't stop the script) ----------
comp_state = array("i", [4096, 0])   # [gain_q12, envelope]

COMP_SRC = """
from micropython import const
_TGT  = const(29000)   # target block peak (~-1 dBFS)
_MAXG = const(16384)   # max boost 4.0x (+12 dB)
_GATE = const(330)     # below ~-40 dBFS: no boost
_BLK  = const(32)

@micropython.viper
def compress(buf, nbytes: int, st):
    p = ptr16(buf)
    s = ptr32(st)
    gain = int(s[0])
    env = int(s[1])
    nsamp = nbytes >> 1
    i = 0
    while i < nsamp:
        end = i + _BLK
        if end > nsamp:
            end = nsamp
        pk = 0
        j = i
        while j < end:
            x = int(p[j])
            if x > 32767:
                x = x - 65536
            if x < 0:
                x = 0 - x
            if x > pk:
                pk = x
            j += 1
        if pk > env:
            env = pk
        else:
            env = env - (env >> 6)
        if env < _GATE:
            tgt = 4096
        else:
            tgt = _MAXG
            while ((env * tgt) >> 12) > _TGT:
                tgt = tgt - (tgt >> 3)
        if tgt < gain:
            gain = tgt
        else:
            gain = gain + ((tgt - gain) >> 4)
        j = i
        while j < end:
            x = int(p[j])
            if x > 32767:
                x = x - 65536
            y = (x * gain) >> 12
            if y > 32767:
                y = 32767
            if y < (0 - 32768):
                y = 0 - 32768
            p[j] = y & 0xFFFF
            j += 1
        i = end
    s[0] = gain
    s[1] = env
"""

COMP_OK = True
try:
    exec(COMP_SRC, globals())
    _t = bytearray(128)
    compress(memoryview(_t), 128, comp_state)
    print("Compressor OK")
except Exception as e:
    COMP_OK = False
    print("Compressor unavailable:", repr(e))

# ---------- Gain control ----------
def set_gain(db):
    if db == 9:
        Pin(GAIN_GPIO, Pin.IN)               # floating -> 9 dB
    elif db == 12:
        Pin(GAIN_GPIO, Pin.OUT, value=0)     # GND -> 12 dB
    else:
        raise ValueError("gain must be 9 or 12")

# ---------- Playback ----------
buf = bytearray(4096); mv = memoryview(buf); zeros = bytearray(1024)
audio = None; playing = False; done = False; remaining = 0; use_comp = False

def feed(_):
    global remaining, done
    if not playing:
        audio.write(zeros); return
    n = f.readinto(mv[:min(len(buf), remaining)]) if remaining > 0 else 0
    if not n:
        done = True; return
    remaining -= n
    if use_comp:
        compress(mv, n, comp_state)
    audio.write(mv[:n])

def measure(stop):
    i_sum = v_sum = p_sum = i_pk = 0.0; n = 0
    while not stop():
        time.sleep_ms(9)
        v, ma = read_vi()
        i_sum += ma; v_sum += v; p_sum += v * ma; n += 1
        if ma > i_pk:
            i_pk = ma
    return i_sum / n, v_sum / n, p_sum / n, i_pk, n

def run(gain_db, comp):
    global audio, playing, done, remaining, use_comp
    if comp and not COMP_OK:
        print("\nSkipping gain %d dB with compression (compressor unavailable)" % gain_db)
        return
    print("\nRunning: gain %d dB, compression %s" % (gain_db, "ON" if comp else "OFF"))
    set_gain(gain_db)
    use_comp = comp
    comp_state[0] = 4096; comp_state[1] = 0
    f.seek(data_start); remaining = data_len; playing = False; done = False
    audio = I2S(0, sck=Pin(10), ws=Pin(11), sd=Pin(9), mode=I2S.TX, bits=16,
                format=I2S.MONO if ch == 1 else I2S.STEREO, rate=rate, ibuf=IBUF)
    try:
        audio.irq(feed)
        audio.write(zeros)

        time.sleep_ms(500)
        t0 = time.ticks_ms()
        base = measure(lambda: time.ticks_diff(time.ticks_ms(), t0) > 3000)

        playing = True
        drain_ms = int(IBUF / byterate * 1000)
        time.sleep_ms(drain_ms)              # skip silence already queued
        t_end = [None]
        def stop_play():
            if done and t_end[0] is None:
                t_end[0] = time.ticks_ms()
            return t_end[0] is not None and time.ticks_diff(time.ticks_ms(), t_end[0]) > drain_ms
        play = measure(stop_play)
    finally:
        audio.deinit()

    print("Idle (silence):  %.1f mA  %.3f V  %.1f mW  (n=%d)" % (base[0], base[1], base[2], base[4]))
    print("Playback avg:    %.1f mA  %.3f V  %.1f mW  peak %.1f mA  (n=%d)"
          % (play[0], play[1], play[2], play[3], play[4]))
    print("Audio-related:   %.1f mA  %.1f mW" % (play[0] - base[0], play[2] - base[2]))
    time.sleep_ms(1500)

try:
    run(9, False)
    run(9, True)
    run(12, False)
    run(12, True)
finally:
    f.close()
    Pin(GAIN_GPIO, Pin.IN)
    print("\nDone.")