import datetime
import json
import os
from pathlib import Path
import subprocess
import json
from typing import Any, Optional
from thumbnail import generate_thumbnail_from_video

def in_B_not_in_A(A: list, B: list) -> list:
    return [x for x in B if not x in set(A)]

def resolution_sorts(ls: list) -> list:
    return sorted(ls, key=lambda x: int(x.rstrip('p')), reverse=True)


def get_aspect_ratio(width: int, height: int) -> str:
    """
    Определяет аспект-рейтио видео.
    
    Возвращает:
        'vertical' — для вертикальных видео (высота > ширина)
        'horizontal' — для горизонтальных видео (ширина > высота)  
        'square' — для квадратных видео
    """
    if width == height:
        return 'square'
    elif height > width:
        return 'vertical'
    else:
        return 'horizontal'


def calculate_resolution_for_format(height: int, aspect_ratio: str) -> tuple[int, int]:
    """
    Рассчитывает ширину и высоту для заданной высоты и аспект-рейтио.
    
    Параметры:
        height (int): желаемая высота видео в пикселях
        aspect_ratio (str): тип формата ('horizontal', 'vertical', 'square')
    
    Возвращает:
        tuple[int, int]: (ширина, высота)
    """
    if aspect_ratio == 'vertical':
        # 9:16 для вертикального видео
        width = int(height * 9 / 16)
    elif aspect_ratio == 'square':
        width = height
    else:  # horizontal
        # 16:9 для горизонтального видео
        width = int(height * 16 / 9)
    
    return (width, height)


def get_decoder_lib_by_codec_name(codec_name: str, prefer_hw: str = "cuda") -> str:
    """
    Возвращает имя декодера FFmpeg для указанного кодека.
    
    Параметры:
        codec_name (str): название кодека ('hevc', 'h264', 'av1' и т.д.)
        prefer_hw (str): 'cuda' — пытаться использовать NVIDIA CUDA/cuvid,
                         'none'  — только программный декодер
    
    Возвращает:
        str — имя декодера, которое можно подставить в -c:v
    """
    codec = codec_name.lower().strip()
    
    if prefer_hw == "cuda":
        # NVIDIA CUDA / CUVID декодеры
        hw_decoders = {
            "h264":        "h264_cuvid",
            "hevc":        "hevc_cuvid",
            "av1":         "av1_cuvid",
            "vp9":         "vp9_cuvid",
            "mpeg2video":  "mpeg2_cuvid",
            "mpeg4":       "mpeg4_cuvid",
            "vc1":         "vc1_cuvid",
            "h264_cuvid":  "h264_cuvid",   # на случай, если уже пришло
            "hevc_cuvid":  "hevc_cuvid",
        }
        if codec in hw_decoders:
            return hw_decoders[codec]
    
    # По умолчанию возвращаем программный декодер (или сам кодек)
    # Для большинства кодеков имя декодера совпадает с названием кодека
    return codec


def get_codec_lib_by_codec_name(
    codec_name: str, 
    prefer_hw: str = "nvenc", 
    is_10bit: bool = False
) -> str:
    """
    Возвращает имя кодера (encoder) FFmpeg по названию кодека.
    
    Параметры:
        codec_name (str): 'hevc', 'h264', 'av1' и т.д.
        prefer_hw (str):  'nvenc' — NVIDIA NVENC (рекомендуется),
                          'none'  — только программное кодирование
        is_10bit (bool):  True, если видео 10-битное (важно для выбора кодера)
    
    Возвращает:
        str — имя кодера для параметра -c:v
    """
    codec = codec_name.lower().strip()
    
    if prefer_hw == "nvenc":
        # NVIDIA NVENC — лучший выбор для большинства случаев
        if codec in ("hevc", "h265") or is_10bit:
            # Для 10-битного видео почти всегда лучше hevc_nvenc
            return "hevc_nvenc"
        
        if codec == "h264":
            return "h264_nvenc"
        
        if codec == "av1":
            return "av1_nvenc"
        
        # Если неизвестный кодек — fallback на hevc_nvenc
        return "hevc_nvenc"
    
    # Программное кодирование (медленнее, но работает везде)
    software_encoders = {
        "hevc": "libx265",
        "h265": "libx265",
        "h264": "libx264",
        "av1":  "libsvtav1",      # или libaom-av1
        "vp9":  "libvpx-vp9",
    }
    
    return software_encoders.get(codec, "libx264")



def get_video_metadata_ffprobe(video_path: str) -> dict[str, Any]:
    """
    Получает подробные метаданные видео с помощью ffprobe.
    Включает битность (bit depth), пиксельный формат и безопасный парсинг FPS.
    """
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
    
    # Находим видео и аудио потоки
    video_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    audio_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), None)
    fmt = data.get('format', {})
    
    # Безопасный парсинг FPS (вместо eval)
    def parse_fps(r_frame_rate: str) -> float:
        try:
            if '/' in r_frame_rate:
                num, den = map(int, r_frame_rate.split('/'))
                return round(num / den, 3) if den != 0 else 0.0
            return float(r_frame_rate)
        except (ValueError, ZeroDivisionError):
            return 0.0
    
    # Определяем битность
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
        # Общая информация о файле
        'duration': float(fmt.get('duration', 0)),
        'size_bytes': int(fmt.get('size', 0)),
        'bitrate': int(fmt.get('bit_rate', 0)),
        'format_name': fmt.get('format_name'),
        
        # Видео
        'video_codec': video_stream.get('codec_name') if video_stream else None,
        'width': int(video_stream.get('width', 0)) if video_stream else None,
        'height': int(video_stream.get('height', 0)) if video_stream else None,
        'fps': parse_fps(video_stream.get('r_frame_rate', '0/1')) if video_stream else None,
        'pix_fmt': pix_fmt,
        'bit_depth': bit_depth,
        'is_10bit': bit_depth == 10,
        
        # Аудио
        'audio_codec': audio_stream.get('codec_name') if audio_stream else None,
        'sample_rate': int(audio_stream.get('sample_rate', 0)) if audio_stream else None,
        'audio_channels': int(audio_stream.get('channels', 0)) if audio_stream else None,
    }
    
    return metadata


cache = {}
def get_videos(dir: str = './videos') -> dict[str, dict[str, Any]]:
    global cache
    videos = {}
    for item in Path(dir).iterdir():
        if not item.is_dir():
            continue
        v_name = item.name
        if v_name in cache:
            videos[v_name] = cache[v_name]
            continue

        v_data = {}
        with open(item / 'data.json', 'r', encoding='utf-8') as f:
            v_data = json.load(f)

        qualities = {}
        for q_item in (item).iterdir():
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
            'format': v_data.get('format', 'horizontal'),  # ← Получаем формат из data.json
            'qualities': list(resolution_sorts(qualities.keys())),
            'files': qualities
        }
        cache[v_name] = videos[v_name]

    return videos



def get_bitrate(height: int, width: int, fps: int, bpp: float):
    return height*width*fps*bpp

def make_multi_output_cmd(video_file: str, target_dir: str, resolutions: list, video_meta: dict, aspect_ratio: str = 'horizontal') -> tuple[str, list[str]]:
    cmd = [
        'ffmpeg',
        '-hwaccel', 'cuda',
        '-hwaccel_output_format', 'cuda',
        '-c:v', get_decoder_lib_by_codec_name(video_meta['video_codec']),   # декодирование на GPU
        '-i', video_file,
    ]
    
    # Получаем исходные размеры для предотвращения upscale
    original_height = video_meta.get('height', 0)

    for res in resolutions:
        target_height = int(res[:-1])
        # Не масштабируем вверх — используем минимум между целевым и исходным
        actual_height = min(target_height, original_height)
        width, _ = calculate_resolution_for_format(actual_height, aspect_ratio)
        target_br = get_bitrate(actual_height, width, int(video_meta.get('fps', 30)), 0.085)
        cmd += [
            '-vf', f'scale_cuda=-2:{actual_height}',  # масштабирование на GPU (без upscale)
            '-c:v', get_codec_lib_by_codec_name(video_meta['video_codec'], is_10bit=video_meta.get('is_10bit', False)),
            '-b:v', str(int(target_br/1000))+'k',
            '-cq', '23',
            '-preset', 'p4',
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
    
    # Определяем аспект-рейтио видео
    video_width = video_meta.get('width', 0)
    video_height = video_meta.get('height', 0)
    aspect_ratio = get_aspect_ratio(video_width, video_height)
    
    original_scale = f"{video_height}p" if aspect_ratio == 'vertical' else f"{video_width}p" if aspect_ratio == 'horizontal' else f"{video_height}p"
    if original_scale == '0p':
        raise Exception('Не удалось получить метаданные видео')
    print(f"Original video scale: {original_scale}")
    print(f"Aspect ratio: {aspect_ratio} ({video_width}x{video_height})")

    resolutions = ['2160p', '1440p', '1080p', '720p', '480p', '360p']
    resolutions = [r for r in resolutions if int(r[:-1]) <= int(original_scale[:-1])]

    if len(resolutions) < 1:
        resolutions = [original_scale[:-1]]

    cmd, video_files = make_multi_output_cmd(video_file, target_dir, resolutions, video_meta, aspect_ratio)
    print(video_files)

    os.makedirs(target_dir, exist_ok=True)
    
    # Автогенерация миниатюры, если она не была предоставлена
    final_thumbnail = thumbnail
    if thumbnail == '' or not os.path.exists(thumbnail):
        print("Миниатюра не предоставлена, генерируем из видео...")
        video_id = Path(target_dir).name
        try:
            # Размер миниатюры зависит от формата видео
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
            'format': aspect_ratio,  # ← Сохраняем формат видео
            'qualities': resolutions,
            'files': {res: str(Path(target_dir) / Path(res+'.mp4')) for res in resolutions}
        }, f, ensure_ascii=False, indent=4)

    print(cmd)
    subprocess.run(cmd, check=True, text=True, encoding='utf-8')



if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Video manager')
    parser.add_argument('--upload', "-ul", action='store_true', help='Upload a new video')

    parser.add_argument('--target-dir', '-td', type=str, help='Target directory for the video (e.g. \'videos\')')
    parser.add_argument('--video-file', -"v", type=str, help='Path to the video file to upload')
    parser.add_argument('--title', "-ti", type=str, help='Title of the video')
    parser.add_argument('--description', "-d", type=str, default='', help='Description of the video')
    parser.add_argument('--thumbnail', "-th", type=str, default='', help='Path to thumbnail')


    args = parser.parse_args()
    if not args.upload and args.target_dir and args.video_file and args.title and args.description:
        parser.error('--target-dir, --video-file, --title and --description are required when using --upload')
    if args.upload and not args.target_dir and not args.video_file and not args.title and not args.description:
        parser.error('When using --upload, you must provide --target-dir, --video-file, --title and --description')

    upload_video(args.target_dir, args.video_file, args.title, args.description, args.hw_accel)