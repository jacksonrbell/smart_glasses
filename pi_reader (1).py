#!/usr/bin/env python3

import argparse
import getpass
import html
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from urllib.parse import urlsplit

import cv2
import numpy as np
from PIL import Image
import requests
from rapidocr import RapidOCR, ModelType, OCRVersion

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 8_000_000


def fetch_photo(session, url):
    # No automatic retry of /capture: one user trigger means one photo request.
    with session.get(url, timeout=(5, 45), stream=True) as response:
        if not response.ok:
            message = next(response.iter_content(512), b'').decode('utf-8', errors='replace')
            raise RuntimeError(f'Camera HTTP {response.status_code}: {message}')
        if 'image/jpeg' not in response.headers.get('Content-Type', '').lower():
            raise RuntimeError('Camera did not return a JPEG. Check its address and firmware.')
        data = bytearray()
        for block in response.iter_content(65536):
            if len(data) + len(block) > MAX_BYTES:
                raise ValueError('JPEG exceeds the 8 MiB limit.')
            data.extend(block)
        return bytes(data), response.headers.get('X-Capture-Id')


def decode_photo(data, mirror, rotate):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Image is empty or exceeds 8 MiB.')
    with Image.open(io.BytesIO(data)) as header:
        if header.width * header.height > MAX_PIXELS:
            raise ValueError('Image exceeds the 8 megapixel limit.')
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError('Image could not be decoded.')
    rotations = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    if rotate:
        frame = cv2.rotate(frame, rotations[rotate])
    if mirror:
        frame = cv2.flip(frame, 1)
    return frame


def translate_lines(session, lines, key, target):
    translated = []
    # Preserve each OCR region separately; avoid merging unrelated menu columns.
    for offset in range(0, len(lines), 50):
        batch = lines[offset:offset + 50]
        response = session.post(
            'https://translation.googleapis.com/language/translate/v2',
            headers={'X-Goog-Api-Key': key},
            json={'q': batch, 'target': target, 'format': 'text'}, timeout=(5, 30))
        if not response.ok:
            # Do not include credentials or request headers in terminal errors.
            raise RuntimeError(f'Google Translation HTTP {response.status_code}. '
                               'Check API enablement, billing, key restrictions and target code.')
        values = response.json()['data']['translations']
        if len(values) != len(batch):
            raise RuntimeError('Unexpected number of translations returned.')
        translated.extend(html.unescape(item['translatedText']) for item in values)
    return translated


def generate_speech(text, target, folder, voice=None, rate=160):
    executable = shutil.which('espeak-ng')
    if not executable:
        raise RuntimeError('eSpeak NG is missing. Run: sudo apt install espeak-ng')
    if not text.strip():
        raise ValueError('There is no translated text to speak.')
    # Google uses zh/zh-CN/zh-TW; eSpeak names its Mandarin voice cmn.
    aliases = {'zh': 'cmn', 'zh-cn': 'cmn', 'zh-tw': 'cmn'}
    selected_voice = voice or aliases.get(target.lower(), target.lower())
    folder = Path(folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / 'last_speech.wav'
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(suffix='.wav', dir=folder, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        completed = subprocess.run(
            [executable, '-b', '1', '-v', selected_voice, '-s', str(rate),
             '-w', str(temporary), '--stdin'],
            input=text, encoding='utf-8', errors='replace', capture_output=True,
            timeout=120, check=False)
        if completed.returncode:
            detail = completed.stderr.strip()[:500]
            raise RuntimeError(f'eSpeak failed for voice {selected_voice!r}: {detail}. '
                               'List installed voices with espeak-ng --voices; use --voice to select one.')
        # Read actual metadata instead of assuming a particular sample rate.
        with wave.open(str(temporary), 'rb') as audio:
            if audio.getnframes() == 0:
                raise RuntimeError('eSpeak produced an empty audio file.')
            info = {'status': 'saved', 'file': str(destination), 'voice': selected_voice,
                    'rate_wpm': rate, 'sample_rate_hz': audio.getframerate(),
                    'channels': audio.getnchannels(), 'bits_per_sample': audio.getsampwidth() * 8,
                    'duration_seconds': round(audio.getnframes() / audio.getframerate(), 3)}
        os.replace(temporary, destination)
        info['generation_seconds'] = round(time.monotonic() - started, 2)
        return info
    finally:
        temporary.unlink(missing_ok=True)


def add_speech(result, options):
    """Keep speech/playback errors separate from successful OCR/translation."""
    if not options.speech:
        return
    if result['status'] != 'translated' or not result['translated_text'].strip():
        result['speech'] = {'status': 'skipped', 'reason': 'No successful nonempty translation.'}
        print('Speech skipped: no translated text. Any previous WAV is unchanged.', flush=True)
        return
    folder = options.speech_dir or options.save_dir or Path('captures_pi')
    try:
        info = generate_speech(result['translated_text'], result['target_language'],
                               folder, options.voice, options.speech_rate)
        result['speech'] = info
        print(f"\nAUDIO SAVED: {info['file']}", flush=True)
        print(f"Voice: {info['voice']}; {info['duration_seconds']:.1f} s; "
              f"generated in {info['generation_seconds']:.2f} s", flush=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, wave.Error, EOFError) as exc:
        result['speech'] = {'status': 'error', 'error': str(exc)}
        print(f'Speech generation failed: {exc}\nTranslation is still available. '
              'Any previous WAV is unchanged.', flush=True)
        return
    if options.play:
        # pw-play uses the desktop audio server. aplay is available on many
        # minimal installations. Playback happens on the Pi, not the SSH laptop.
        player = shutil.which('pw-play') or shutil.which('aplay')
        try:
            if not player:
                raise RuntimeError('No player found. Install pipewire-bin or alsa-utils, '
                                   'or omit --play to save audio only.')
            print('Playing on the Pi; waiting for playback to finish...', flush=True)
            completed = subprocess.run([player, info['file']], capture_output=True,
                                       encoding='utf-8', errors='replace', check=False,
                                       timeout=info['duration_seconds'] + 15)
            if completed.returncode:
                raise RuntimeError(completed.stderr.strip()[:500] or 'Audio player failed.')
            info['playback_status'] = 'completed'
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            info['playback_status'] = 'error'
            info['playback_error'] = str(exc)
            print(f'Playback failed: {exc}\nThe WAV was saved successfully.', flush=True)


def process_photo(data, engine, options, translation_session, key):
    started = time.monotonic()
    frame = decode_photo(data, options.mirror, options.rotate)
    print(f'\nImage: {frame.shape[1]} x {frame.shape[0]}. Reading text...', flush=True)
    output = engine(frame)
    detections = []
    if output.boxes is not None and output.txts is not None and output.scores is not None:
        for box, text, score in zip(output.boxes, output.txts, output.scores):
            text = str(text).strip()
            accepted = float(score) >= options.confidence and any(c.isalnum() for c in text)
            detections.append({'text': text, 'confidence': float(score),
                               'accepted': accepted, 'box': np.asarray(box).tolist()})
    selected = [item for item in detections if item['accepted']]
    lines = [item['text'] for item in selected]
    result = {'text': '\n'.join(lines), 'translated_text': '', 'target_language': options.target,
              'status': 'ocr_only' if lines else 'no_text', 'detections': detections,
              'model': 'PP-OCRv6 small', 'mirror': options.mirror, 'rotation': options.rotate,
              'box_coordinates': 'corrected image'}
    print('\nDETECTED TEXT', flush=True)
    if not lines:
        print('No text passed the confidence filter. Try better light or a closer, focused view.')
    else:
        for i, item in enumerate(selected, 1):
            print(f"{i}. {item['text']}  [score {item['confidence']:.2f}]")
        if not options.ocr_only:
            print(f'\nTRANSLATION ({options.target})', flush=True)
            try:
                translations = translate_lines(translation_session, lines, key, options.target)
                result['translated_text'] = '\n'.join(translations)
                result['status'] = 'translated'
                for i, text in enumerate(translations, 1):
                    print(f'{i}. {text}')
            except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
                result['status'] = 'translation_error'
                result['error'] = str(exc)
                print(f'Translation failed: {exc}\nDetected text is shown above.')
    result['seconds'] = round(time.monotonic() - started, 2)
    print(f"\nOCR + translation: {result['seconds']:.2f} s (excludes camera focus/transfer).", flush=True)
    # Run once per processed photo, after translation; speech has its own timing.
    add_speech(result, options)
    if options.save_dir:
        folder = options.save_dir
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'last_capture.jpg').write_bytes(data)
        # Draw boxes without attempting to render all scripts using OpenCV fonts.
        preview = frame.copy()
        for i, item in enumerate(detections, 1):
            points = np.rint(item['box']).astype(np.int32)
            color = (40, 210, 40) if item['accepted'] else (0, 165, 255)
            cv2.polylines(preview, [points], True, color, 2)
            x, y = points.min(axis=0)
            cv2.putText(preview, f"{i}: {item['confidence']:.2f}",
                        (int(x), max(16, int(y)-5)), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1)
        if not cv2.imwrite(str(folder / 'last_boxes.jpg'), preview):
            raise OSError('Could not save the boxed preview.')
        (folder / 'last_result.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def button_captures(session, base):
    """Poll only tiny status JSON; download one JPEG for each new available ID.

    ESP32 retains the latest picture only. Fast presses while OCR is busy may
    replace intermediate pictures; this is not a capture queue.
    """
    seen = None
    last_error = ''
    while True:
        try:
            response = session.get(base + '/status', timeout=(5, 20))
            response.raise_for_status()
            state = response.json()
            error = state.get('last_error', '')
            if error and error != last_error:
                print('Camera:', error, flush=True)
            last_error = error
            identity = (state['boot_id'], state['capture_id'])
            if state.get('has_photo') and identity != seen:
                data, actual_id = fetch_photo(session, base + '/last.jpg?id=' + str(state['capture_id']))
                if actual_id != str(state['capture_id']):
                    raise RuntimeError('Capture changed during download; checking again.')
                seen = identity
                yield data
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
            print(f'Waiting for camera: {exc}', flush=True)
            time.sleep(2)
        time.sleep(.5)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--camera', help='ESP32 base address, e.g. http://192.168.1.123')
    source.add_argument('--image', type=Path, help='Process one local image instead of contacting the ESP32')
    parser.add_argument('--button', action='store_true', help='Watch for captures triggered on the ESP32')
    parser.add_argument('--mirror', action='store_true', help='Flip backward lettering left-to-right')
    parser.add_argument('--rotate', type=int, choices=[0, 90, 180, 270], default=0)
    parser.add_argument('--target', default='en', help='Translation destination, e.g. en, es, fr')
    parser.add_argument('--ocr-only', action='store_true')
    parser.add_argument('--detail', action='store_true', help='Larger text-detection input for small print; slower')
    parser.add_argument('--confidence', type=float, default=.55, help='Recognition score filter, default .55')
    parser.add_argument('--save-dir', type=Path, help='Optional folder for latest capture, boxed image and JSON')
    parser.add_argument('--speech', action='store_true', help='Save translated speech as last_speech.wav')
    parser.add_argument('--play', action='store_true', help='Also play speech on the Pi; implies --speech')
    parser.add_argument('--speech-dir', type=Path, help='Audio folder; default is --save-dir or captures_pi')
    parser.add_argument('--voice', help='Override eSpeak voice (see espeak-ng --voices); default follows --target')
    parser.add_argument('--speech-rate', type=int, default=160, help='Speech speed in words/minute (80-450)')
    options = parser.parse_args()
    options.speech = options.speech or options.play
    if options.speech and options.ocr_only:
        parser.error('--speech/--play require translation; remove --ocr-only')
    if not 80 <= options.speech_rate <= 450:
        parser.error('--speech-rate must be between 80 and 450 words/minute')
    if options.speech and not shutil.which('espeak-ng'):
        parser.error('Speech requires eSpeak NG. Run: sudo apt install espeak-ng')
    if options.button and not options.camera:
        parser.error('--button requires --camera')
    if not 0 <= options.confidence <= 1:
        parser.error('--confidence must be between 0 and 1')
    if options.camera:
        options.camera = options.camera.rstrip('/')
        url = urlsplit(options.camera)
        if url.scheme != 'http' or not url.hostname or url.path or url.query or url.fragment or url.username:
            parser.error('--camera must be the ESP32 base HTTP address, without /capture')
    key = ''
    if not options.ocr_only:
        key = os.environ.get('GOOGLE_TRANSLATE_API_KEY', '').strip() or getpass.getpass('Google Translation API key: ').strip()
        if not key:
            parser.error('An API key is required, or use --ocr-only')
    print('Loading local PP-OCRv6 small multilingual models...', flush=True)
    engine = RapidOCR(params={
        'Det.ocr_version': OCRVersion.PPOCRV6, 'Det.model_type': ModelType.SMALL,
        'Rec.ocr_version': OCRVersion.PPOCRV6, 'Rec.model_type': ModelType.SMALL,
        'Global.text_score': 0.0, 'Global.max_side_len': 3072,
        'Det.limit_side_len': 1920 if options.detail else 1280, 'Det.limit_type': 'max',
        'Rec.rec_batch_num': 4,
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
    })
    camera_session = requests.Session()
    camera_session.trust_env = False # Keep LAN camera traffic out of HTTP proxies.
    translation_session = requests.Session()
    def run(data):
        try:
            return process_photo(data, engine, options, translation_session, key)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f'Photo processing failed: {exc}', flush=True)
            return None
    try:
        if options.image:
            if options.image.stat().st_size > MAX_BYTES:
                raise ValueError('Image exceeds 8 MiB.')
            result = run(options.image.read_bytes())
            return 0 if (result and result['status'] != 'translation_error'
                         and result.get('speech', {}).get('status') != 'error') else 1
        if options.button:
            print('Waiting for ESP32 Serial c / D1 button. Only the latest photo is retained. Ctrl+C quits.', flush=True)
            for data in button_captures(camera_session, options.camera):
                run(data)
                print('\nReady for the next button capture.', flush=True)
        else:
            while True:
                command = input('\nPress Enter for ONE photo, or q then Enter to quit: ').strip().lower()
                if command == 'q':
                    break
                if command:
                    continue
                try:
                    data, _ = fetch_photo(camera_session, options.camera + '/capture')
                    run(data)
                except (requests.RequestException, ValueError, RuntimeError) as exc:
                    print(f'Capture failed: {exc}', flush=True)
    except (KeyboardInterrupt, EOFError):
        print('\nStopped.')
    finally:
        camera_session.close()
        translation_session.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
