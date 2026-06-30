import datetime
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional
from thumbnail import generate_thumbnail_from_video

# Минимальные размеры для NVENC (аппаратный энкодер NVIDIA)
NVENC_MIN_DIM = 128  # пикселей — жёсткое ограничение драйвера

# Кеш результата проверки доступности NVIDIA GPU (вычисляется один раз за процесс)
_gpu_available_cache: Optional[bool] = None


def is_nvidia_gpu_available(force_recheck: bool = False) -> bool:
    """
    Проверяет, доступен ли NVIDIA GPU с поддержкой NVENC/CUDA в текущем окружении.

    Проверка реальная (а не основанная на предположениях):
    1. nvidia-smi должен быть доступен и успешно отработать (значит, драйвер и GPU есть).
    2. ffmpeg должен быть собран с поддержкой hwaccel=cuda.
    3. ffmpeg должен иметь хотя бы один из энкодеров *_nvenc.

    Результат кешируется в рамках процесса, чтобы не дёргать subprocess на каждый вызов.
    Используйте force_recheck=True, если окружение могло измениться (например, после
    подключения/отключения GPU в виртуализированном контейнере).
    """
    global _gpu_available_cache
    if _gpu_available_cache is not None and not force_recheck:
        return _gpu_available_cache

    # 1. Проверяем наличие и работоспособность nvidia-smi
    try:
        result = subprocess.run(
            ['nvidia-smi'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            _gpu_available_cache = False
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _gpu_available_cache = False
        return False

    # 2. Проверяем, что ffmpeg собран с поддержкой cuda hwaccel
    try:
        hwaccels = subprocess.run(
            ['ffmpeg', '-hide_banner', '-hwaccels'],
            capture_output=True, text=True, timeout=5
        )
        if 'cuda' not in hwaccels.stdout.lower():
            _gpu_available_cache = False
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _gpu_available_cache = False
        return False

    # 3. Проверяем наличие nvenc-энкодеров в сборке ffmpeg
    try:
        encoders = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=5
        )
        if 'nvenc' not in encoders.stdout.lower():
            _gpu_available_cache = False
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _gpu_available_cache = False
        return False

    _gpu_available_cache = True
    return True


def in_B_not_in_A(A: list, B: list) -> list:
    return [x for x in B if x not in set(A)]


def resolution_sorts(ls: list) -> list:
    return sorted(ls, key=lambda x: int(x.rstrip('p')), reverse=True)


def get_aspect_ratio(width: int, height: int) -> str:
    if width == height:
        return 'square'
    elif height > width:
        return 'vertical'
    else:
        return 'horizontal'


def calculate_resolution_for_format(height: int, aspect_ratio: str) -> tuple[int, int]:
    if aspect_ratio == 'vertical':
        width = int(height * 9 / 16)
    elif aspect_ratio == 'square':
        width = height
    else:
        width = int(height * 16 / 9)
    # Выравниваем до чётного (требование большинства кодеков)
    width = width if width % 2 == 0 else width + 1
    height = height if height % 2 == 0 else height + 1
    return (width, height)


def get_decoder_lib_by_codec_name(codec_name: str, prefer_hw: Optional[str] = None) -> str:
    codec = codec_name.lower().strip()

    # Если не указано явно, решаем на основе реальной доступности GPU
    if prefer_hw is None:
        prefer_hw = "cuda" if is_nvidia_gpu_available() else "cpu"

    if prefer_hw == "cuda":
        hw_decoders = {
            "h264":        "h264_cuvid",
            "hevc":        "hevc_cuvid",
            # AV1 cuvid не поддерживает нестандартный chroma format
            # (yuv420p10le и др.) — используем программный декодер
            # "av1":      "av1_cuvid",  # ОТКЛЮЧЕНО
            "vp9":         "vp9_cuvid",
            "mpeg2video":  "mpeg2_cuvid",
            "mpeg4":       "mpeg4_cuvid",
            "vc1":         "vc1_cuvid",
            "h264_cuvid":  "h264_cuvid",
            "hevc_cuvid":  "hevc_cuvid",
        }
        if codec in hw_decoders:
            return hw_decoders[codec]

    # Программный декодер
    sw_decoders = {
        "av1": "libaom-av1",  # или libdav1d если установлен
    }
    return sw_decoders.get(codec, codec)


def get_codec_lib_by_codec_name(
    codec_name: str,
    prefer_hw: Optional[str] = None,
    is_10bit: bool = False
) -> str:
    codec = codec_name.lower().strip()

    # Если не указано явно, решаем на основе реальной доступности GPU
    if prefer_hw is None:
        prefer_hw = "nvenc" if is_nvidia_gpu_available() else "software"

    if prefer_hw == "nvenc":
        if codec in ("hevc", "h265") or is_10bit:
            return "hevc_nvenc"
        if codec == "h264":
            return "h264_nvenc"
        if codec == "av1":
            return "av1_nvenc"
        return "hevc_nvenc"

    software_encoders = {
        "hevc": "libx265",
        "h265": "libx265",
        "h264": "libx264",
        "av1":  "libsvtav1",
        "vp9":  "libvpx-vp9",
    }
    return software_encoders.get(codec, "libx264")


def get_video_metadata_ffprobe(video_path: str) -> dict[str, Any]:
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe error: {result.stderr}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ошибка парсинга JSON от ffprobe: {e}")

    video_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    audio_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), None)
    fmt = data.get('format', {})

    def parse_fps(r_frame_rate: str) -> float:
        try:
            if '/' in r_frame_rate:
                num, den = map(int, r_frame_rate.split('/'))
                return round(num / den, 3) if den != 0 else 0.0
            return float(r_frame_rate)
        except (ValueError, ZeroDivisionError):
            return 0.0

    def get_bit_depth(stream: Optional[dict]) -> Optional[int]:
        if not stream:
            return None
        bits = stream.get('bits_per_raw_sample')
        if bits:
            return int(bits)
        pix_fmt = stream.get('pix_fmt', '')
        if '10' in pix_fmt:
            return 10
        if '12' in pix_fmt:
            return 12
        if '16' in pix_fmt:
            return 16
        return 8

    pix_fmt = video_stream.get('pix_fmt') if video_stream else None
    bit_depth = get_bit_depth(video_stream)

    metadata = {
        'duration': float(fmt.get('duration', 0)),
        'size_bytes': int(fmt.get('size', 0)),
        'bitrate': int(fmt.get('bit_rate', 0)),
        'format_name': fmt.get('format_name'),
        'video_codec': video_stream.get('codec_name') if video_stream else None,
        'width': int(video_stream.get('width', 0)) if video_stream else None,
        'height': int(video_stream.get('height', 0)) if video_stream else None,
        'fps': parse_fps(video_stream.get('r_frame_rate', '0/1')) if video_stream else None,
        'pix_fmt': pix_fmt,
        'bit_depth': bit_depth,
        'is_10bit': bit_depth == 10,
        'audio_codec': audio_stream.get('codec_name') if audio_stream else None,
        'sample_rate': int(audio_stream.get('sample_rate', 0)) if audio_stream else None,
        'audio_channels': int(audio_stream.get('channels', 0)) if audio_stream else None,
    }
    return metadata


cache = {}


def get_videos(dir: str = './videos') -> dict[str, dict[str, Any]]:
    videos = {}
    if not Path(dir).exists():
        return videos
    for item in Path(dir).iterdir():
        if not item.is_dir():
            continue
        v_name = item.name
        if v_name in cache:
            videos[v_name] = cache[v_name]
            continue

        data_file = item / 'data.json'
        if not data_file.exists():
            continue

        v_data = {}
        with open(data_file, 'r', encoding='utf-8') as f:
            v_data = json.load(f)

        qualities = {}
        for q_item in item.iterdir():
            if not q_item.is_file():
                continue
            if q_item.name == 'data.json':
                continue
            q_name = q_item.stem
            qualities[q_name] = f"{dir}/{v_name}/{q_name}.mp4"

        videos[v_name] = {
            'title': v_data.get('title', ''),
            'description': v_data.get('description', ''),
            'year': v_data.get('year', 0),
            'thumbnail': v_data.get('thumbnail', ''),
            'format': v_data.get('format', 'horizontal'),
            'qualities': list(resolution_sorts(qualities.keys())),
            'files': qualities
        }
        cache[v_name] = videos[v_name]

    return videos


def get_bitrate(height: int, width: int, fps: int, bpp: float):
    return height * width * fps * bpp


def make_multi_output_cmd(
        video_file: str,
        target_dir: str,
        resolutions: list,
        video_meta: dict,
        aspect_ratio: str = 'horizontal'
        ) -> tuple[str, list[str]]:
    """
    Строим FFmpeg команду для мульти-выходного транскодинга.

    Фиксы:
    1. AV1 cuvid → программный декодер (chroma format несовместимость)
    2. Минимальный размер NVENC = 128px (иначе InitializeEncoder failed)
    3. Чётные размеры для всех кодеков
    4. NVIDIA GPU используется только если он реально доступен в окружении —
       иначе автоматически используется программный декодер/scale/энкодер.
    """
    codec_name = video_meta.get('video_codec', 'h264')
    is_av1 = 'av1' in codec_name.lower()

    gpu_available = is_nvidia_gpu_available()
    # GPU-путь применим только если он доступен и кодек не AV1
    # (AV1 cuvid-декодер несовместим с нестандартным chroma format)
    use_gpu = gpu_available and not is_av1

    if use_gpu:
        decoder = get_decoder_lib_by_codec_name(codec_name, prefer_hw="cuda")
        cmd = [
            'ffmpeg',
            "-hide_banner",
            '-hwaccel', 'cuda',
            '-hwaccel_output_format', 'cuda',
            '-c:v', decoder,
            '-i', video_file,
        ]
    else:
        decoder = get_decoder_lib_by_codec_name(codec_name, prefer_hw="cpu")
        cmd = [
            'ffmpeg',
            "-hide_banner",
            '-c:v', decoder,
            '-i', video_file,
        ]
        if not gpu_available:
            print("NVIDIA GPU не обнаружен — используется программный декодер/энкодер.")

    original_height = video_meta.get('height', 0)
    original_width = video_meta.get('width', 0)

    for res in resolutions:
        target_height = int(res[:-1])
        actual_height = min(target_height, original_height)
        width, actual_height = calculate_resolution_for_format(actual_height, aspect_ratio)

        if use_gpu:
            # Гарантируем минимум NVENC
            actual_height = max(actual_height, NVENC_MIN_DIM)
            width = max(width, NVENC_MIN_DIM)
            encoder = get_codec_lib_by_codec_name(codec_name, prefer_hw="nvenc", is_10bit=video_meta.get('is_10bit', False))
        else:
            encoder = get_codec_lib_by_codec_name(codec_name, prefer_hw="software", is_10bit=video_meta.get('is_10bit', False))

        target_br = get_bitrate(actual_height, width, int(video_meta.get('fps', 30) or 30), 0.085)

        if use_gpu:
            cmd += [
                '-vf', f'scale_cuda=-2:{actual_height}',
                '-c:v', encoder,
                '-b:v', str(int(target_br / 1000)) + 'k',
                '-cq', '23',
                '-preset', 'p4',
                '-movflags', '+faststart',
                '-c:a', 'copy',
                f'{target_dir}/{res}.mp4',
            ]
        else:
            # Программное масштабирование (CPU): scale вместо scale_cuda
            cmd += [
                '-vf', f'scale={width}:{actual_height}:flags=lanczos',
                '-c:v', encoder,
                '-b:v', str(int(target_br / 1000)) + 'k',
                '-crf', '23',
                '-preset', 'medium',
                '-movflags', '+faststart',
                '-c:a', 'copy',
                f'{target_dir}/{res}.mp4',
            ]

    video_files = [f'{target_dir}/{res}.mp4' for res in resolutions]
    return cmd, video_files


def upload_video(target_dir: str, video_file: str, title: str, description: str = '', thumbnail: str = '') -> None:
    if not os.path.exists(video_file):
        raise Exception(f"Видео файл \"{video_file}\" не найден")

    video_meta = get_video_metadata_ffprobe(video_file)

    video_width = video_meta.get('width', 0)
    video_height = video_meta.get('height', 0)
    aspect_ratio = get_aspect_ratio(video_width, video_height)

    original_scale = (
        f"{video_height}p"
        if aspect_ratio == 'vertical'
        else f"{video_width}p"
        if aspect_ratio == 'horizontal'
        else f"{video_height}p"
    )
    if original_scale == '0p':
        raise Exception('Не удалось получить метаданные видео')
    print(f"Original video scale: {original_scale}")
    print(f"Aspect ratio: {aspect_ratio} ({video_width}x{video_height})")

    resolutions = ['2160p', '1440p', '1080p', '720p', '480p', '360p']
    resolutions = [r for r in resolutions if int(r[:-1]) <= int(original_scale[:-1])]

    # Фильтруем слишком маленькие разрешения (актуально только для NVENC)
    if is_nvidia_gpu_available():
        resolutions = [r for r in resolutions if int(r[:-1]) >= NVENC_MIN_DIM]

    if len(resolutions) < 1:
        resolutions = [original_scale]

    cmd, video_files = make_multi_output_cmd(video_file, target_dir, resolutions, video_meta, aspect_ratio)
    print("Resolutions:", resolutions)

    os.makedirs(target_dir, exist_ok=True)

    final_thumbnail = thumbnail
    if thumbnail == '' or not os.path.exists(thumbnail):
        print("Миниатюра не предоставлена, генерируем из видео...")
        video_id = Path(target_dir).name
        try:
            thumb_size = "202:360" if aspect_ratio == 'vertical' else "360:202"
            final_thumbnail = generate_thumbnail_from_video(video_file, video_id, size=thumb_size)
            print(f"Миниатюра успешно создана: {final_thumbnail}")
        except Exception as e:
            print(f"Ошибка при генерации миниатюры: {e}")
            final_thumbnail = ''

    with open(f'{target_dir}/data.json', 'w', encoding='utf-8') as f:
        json.dump({
            'title': title,
            'description': description,
            'year': datetime.date.today().year,
            'thumbnail': final_thumbnail,
            'format': aspect_ratio,
            'qualities': resolutions,
            'files': {res: str(Path(target_dir) / Path(res + '.mp4')) for res in resolutions}
        }, f, ensure_ascii=False, indent=4)

    print("FFmpeg cmd:", cmd)
    subprocess.run(cmd, check=True, text=True, encoding='utf-8')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Video manager')
    parser.add_argument('--upload', "-ul", action='store_true')
    parser.add_argument('--target-dir', '-td', type=str)
    parser.add_argument('--video-file', "-v", type=str)
    parser.add_argument('--title', "-ti", type=str)
    parser.add_argument('--description', "-d", type=str, default='')
    parser.add_argument('--thumbnail', "-th", type=str, default='')
    args = parser.parse_args()
    if args.upload:
        if not all([args.target_dir, args.video_file, args.title]):
            parser.error('--target-dir, --video-file, --title обязательны при --upload')
        upload_video(args.target_dir, args.video_file, args.title, args.description, args.thumbnail)