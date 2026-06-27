import os
import subprocess
from pathlib import Path

standard_dir = "thumbnail"

def get_thumbnail_path(name: str) -> str:
    os.makedirs(standard_dir, exist_ok=True)
    return f"{standard_dir}/{name}"

def load_thumbnail(name: str, b: bytes) -> str:
    path = get_thumbnail_path(name)
    with open(path, 'wb') as f:
        f.write(b)
    return path

def generate_thumbnail_from_video(video_file: str, video_id: str, timestamp: float = 1.0, size: str = "320:180") -> str:
    """
    Генерирует миниатюру из видеофайла, извлекая кадр в указанный момент времени.
    
    Параметры:
        video_file (str): путь к видеофайлу
        video_id (str): идентификатор видео для имени файла миниатюры
        timestamp (float): время в секундах для извлечения кадра (по умолчанию 1 сек)
        size (str): размер миниатюры в формате ШИРИНАxВЫСОТА (по умолчанию 320x180)
    
    Возвращает:
        str — путь к сохранённой миниатюре
    """
    if not os.path.exists(video_file):
        raise FileNotFoundError(f"Видеофайл не найден: {video_file}")
    
    thumbnail_filename = f"{video_id}.jpg"
    thumbnail_path = get_thumbnail_path(thumbnail_filename)
    
    cmd = [
        'ffmpeg',
        '-i', video_file,
        '-ss', str(timestamp),
        '-vframes', '1',
        '-vf', f'scale={size}',
        '-y',
        thumbnail_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка при генерации миниатюры: {result.stderr}")
    
    return thumbnail_path