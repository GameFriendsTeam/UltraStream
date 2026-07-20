"""
Менеджер RTMP-приёма поверх ffmpeg: можно поднять несколько независимых
"серверов" приёма (каждый — свой ffmpeg -listen), у каждого свой
stream_key (человекочитаемое имя/ключ для OBS) и свой stream_id
(внутренний идентификатор для управления: остановить, посмотреть статус).

Как это работает:
	ffmpeg -listen 1 умеет слушать RTMP только на ОДНОМ порту и принимает
	ОДНО подключение за раз. Поэтому у каждого запущенного стрима — свой
	порт (выделяется автоматически, начиная с BASE_RTMP_PORT), и свой
	поток (thread) с циклом автоперезапуска ffmpeg, если OBS переподключается.

	Это не полноценный RTMP-сервер с множеством приложений на одном порту
	(как nginx-rtmp/MediaMTX) — это N независимых мини-серверов. Для
	нескольких одновременных трансляций с разных компов это ок и просто;
	если нужен один порт 1935 на все стримы с проверкой ключа — тогда
	обратно потребуется полноценный медиа-сервер.

Использование:
	from rtmp_manager import start_rtmp, stop_rtmp, list_streams

	info = start_rtmp("mystream")
	# info == {
	#     "stream_id": "a1b2c3d4",
	#     "stream_key": "mystream",
	#     "port": 1935,
	#     "rtmp_url": "rtmp://0.0.0.0:1935/live/mystream",
	#     "hls_path": "static/stream/a1b2c3d4/index.m3u8",
	#     "hls_url": "/static/stream/a1b2c3d4/index.m3u8",
	# }

	stop_rtmp(info["stream_id"])
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

STREAM_ROOT = "static/stream"
BASE_RTMP_PORT = 1935
RECONNECT_DELAY = 1.0  # пауза перед повторным listen после отключения OBS


@dataclass
class RtmpStream:
	stream_id: str
	stream_key: str
	port: int
	process: Optional[subprocess.Popen] = None
	thread: Optional[threading.Thread] = None
	stop_flag: threading.Event = field(default_factory=threading.Event)

	@property
	def hls_dir(self) -> str:
		return os.path.join(STREAM_ROOT, self.stream_id)

	@property
	def hls_path(self) -> str:
		return os.path.join(self.hls_dir, "index.m3u8")

	@property
	def rtmp_url(self) -> str:
		return f"rtmp://0.0.0.0:{self.port}/live/{self.stream_id}"

	@property
	def is_running(self) -> bool:
		return self.process is not None and self.process.poll() is None


_streams: dict[str, RtmpStream] = {}
_used_ports: set[int] = set()
_lock = threading.Lock()


def _allocate_port() -> int:
	port = BASE_RTMP_PORT
	while port in _used_ports:
		port += 1
	_used_ports.add(port)
	return port


def _ingest_loop(stream: RtmpStream) -> None:
	os.makedirs(stream.hls_dir, exist_ok=True)
	cmd = [
		"ffmpeg",
		"-y",

		"-fflags", "nobuffer",
		"-flags", "low_delay",
		"-analyzeduration", "0",
		"-probesize", "32",

		"-f", "flv",
		"-listen", "1",
		"-i", stream.rtmp_url,

		"-c", "copy",

		"-muxdelay", "0",
		"-muxpreload", "0",

		"-f", "hls",
		"-hls_time", "1",
		"-hls_list_size", "2",
		"-hls_flags", "delete_segments+append_list+independent_segments+omit_endlist",

		stream.hls_path,
	]
	while not stream.stop_flag.is_set():
		stream.process = subprocess.Popen(
			cmd, stdout=sys.stdout, stderr=sys.stderr
		)
		stream.process.wait()
		stream.process = None
		if stream.stop_flag.is_set():
			break
		time.sleep(RECONNECT_DELAY)  # OBS отключился/ещё не подключился — слушаем заново


def start_rtmp(stream_key: str) -> dict:
	"""Поднимает приём RTMP под заданный stream_key. Возвращает данные для OBS и плеера."""
	with _lock:
		stream_id = uuid.uuid4().hex[:8]
		port = _allocate_port()
		stream = RtmpStream(stream_id=stream_id, stream_key=stream_key, port=port)
		_streams[stream_id] = stream

	thread = threading.Thread(target=_ingest_loop, args=(stream,), daemon=True)
	stream.thread = thread
	thread.start()

	return {
		"stream_id": stream.stream_id,
		"stream_key": stream.stream_key,
		"port": stream.port,
		"rtmp_url": stream.rtmp_url,
		"hls_path": stream.hls_path,
		"hls_url": f"/{stream.hls_path}",
	}


def stop_rtmp(stream_id: str, cleanup_files: bool = True) -> bool:
	"""Останавливает поток по id. Возвращает False, если такого id нет."""
	with _lock:
		stream = _streams.pop(stream_id, None)
		if stream is not None:
			_used_ports.discard(stream.port)

	if stream is None:
		return False

	stream.stop_flag.set()
	if stream.process and stream.process.poll() is None:
		stream.process.terminate()
		try:
			stream.process.wait(timeout=5)
		except subprocess.TimeoutExpired:
			stream.process.kill()
			stream.process.wait()

	if cleanup_files:
		shutil.rmtree(stream.hls_dir, ignore_errors=True)
	return True


def list_streams() -> list[dict]:
	with _lock:
		snapshot = list(_streams.values())
	return [
		{
			"stream_id": s.stream_id,
			"stream_key": s.stream_key,
			"port": s.port,
			"running": s.is_running,
			"hls_url": f"/{s.hls_path}",
		}
		for s in snapshot
	]


def stop_all() -> None:
	"""Остановить все запущенные потоки (например, при выключении приложения)."""
	for stream_id in list(_streams.keys()):
		stop_rtmp(stream_id)