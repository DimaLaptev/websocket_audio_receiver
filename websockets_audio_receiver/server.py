import asyncio
import json
import logging
import multiprocessing as mp
from multiprocessing import shared_memory
from typing import Dict, Optional, Set
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
import signal
import sys
import os
import platform
import uvicorn
import socket
from contextlib import asynccontextmanager
import time
import aiohttp
from aiohttp import web
import argparse  # Добавляем для обработки аргументов командной строки
import numpy as np

# Настройка event loop для Windows
if platform.system() == 'Windows':
    # Используем SelectorEventLoop вместо ProactorEventLoop для лучшей совместимости
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def setup_event_loop():
    """Настройка и возврат event loop"""
    if platform.system() == 'Windows':
        loop = asyncio.WindowsSelectorEventLoopPolicy().new_event_loop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop

# Определяем путь к модулю receiver
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if current_dir not in sys.path:
    sys.path.append(current_dir)
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

try:
    # Попытка импорта с использованием полного пути
    from websockets_audio_receiver.receiver import run_receiver
except ImportError:
    try:
        # Попытка импорта из того же пакета
        from .receiver import run_receiver
    except ImportError:
        try:
            # Попытка импорта как из того же каталога
            import receiver
            run_receiver = receiver.run_receiver
        except ImportError:
            # Последняя попытка - импорт по относительному пути
            from receiver import run_receiver

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Константы
BUFFER_SIZE = 1024
SESSION_TIMEOUT = 5
DATA_TIMEOUT = 1.0
STARTUP_TIMEOUT = 5
SHUTDOWN_TIMEOUT = 5
MAX_STARTUP_ATTEMPTS = 3

# Системные типы сообщений WebSocket
WS_SYSTEM_TYPES = ["websocket.receive", "websocket.disconnect", "websocket.close", "websocket.error"]

# Получаем порт из переменных окружения или используем значения по умолчанию
PORT = int(os.environ.get("PORT", "8004"))
HOST = os.environ.get("HOST", "127.0.0.1")

def is_port_available(host: str, port: int, timeout: float = 1.0) -> bool:
    """Проверяет, доступен ли порт для подключения"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            return result != 0  # Порт доступен, если подключение не удалось
    except Exception as e:
        logger.error(f"Ошибка при проверке порта {host}:{port}: {e}")
        return False

def wait_for_port_release(host: str, port: int, timeout: float = 10.0, interval: float = 0.5) -> bool:
    """Ожидает освобождения порта"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if is_port_available(host, port):
            return True
        time.sleep(interval)
    return False

class AudioCommand(BaseModel):
    command: str = Field(..., pattern="^start_audio_receiver$")

class AudioData(BaseModel):
    audio_data: str

class Session:
    def __init__(self):
        self.buffer1 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
        self.buffer2 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
        self.lock1 = mp.Lock()
        self.lock2 = mp.Lock()
        self.data_ready = mp.Event()
        self.data_processed = mp.Event()
        self.last_activity = datetime.now()
        self.receiver_process = None
        self.is_connected = True
        self.lock_timeout = 1  # Уменьшаем таймаут для блокировок до 1 секунды
        self.receiver_ready = mp.Event()  # Новое событие для проверки готовности приёмника
        
        # Создаем numpy массивы для более быстрого копирования данных
        self.np_buffer1 = np.ndarray((BUFFER_SIZE,), dtype=np.uint8, buffer=self.buffer1.buf)
        self.np_buffer2 = np.ndarray((BUFFER_SIZE,), dtype=np.uint8, buffer=self.buffer2.buf)

    def cleanup(self):
        if self.receiver_process and self.receiver_process.is_alive():
            self.receiver_process.terminate()
            self.receiver_process.join()
        self.buffer1.close()
        self.buffer1.unlink()
        self.buffer2.close()
        self.buffer2.unlink()

    async def acquire_lock_with_timeout(self, lock, timeout=None):
        """Приобретение блокировки с таймаутом"""
        if timeout is None:
            timeout = self.lock_timeout
        
        # Пробуем получить блокировку с таймаутом
        try:
            # Используем asyncio.wait_for для асинхронного ожидания блокировки
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, lock.acquire),
                timeout=timeout
            )
            return True
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при получении блокировки: {timeout} секунд")
            return False
        except Exception as e:
            logger.error(f"Ошибка при получении блокировки: {e}")
            return False

    def release_lock(self, lock):
        """Освобождение блокировки"""
        try:
            lock.release()
        except Exception as e:
            logger.error(f"Ошибка при освобождении блокировки: {e}")

    async def handle_audio_data(self, websocket: WebSocket, audio_bytes: bytes):
        """Обработка аудио данных"""
        try:
            if not self.receiver_process or not self.receiver_process.is_alive():
                await websocket.send_json({"error": "Receiver not running"})
                return

            # Записываем данные в буфер
            data_length = min(len(audio_bytes), BUFFER_SIZE)
            
            # Потокобезопасное копирование данных в буфер с использованием numpy
            await self.acquire_lock_with_timeout(self.lock1)
            try:
                # Используем numpy для быстрого копирования
                self.np_buffer1[:data_length] = np.frombuffer(audio_bytes[:data_length], dtype=np.uint8)
                self.data_ready.set()
            finally:
                self.release_lock(self.lock1)

            # Ждем обработки данных
            if not self.data_processed.wait(timeout=0.5):  # Уменьшаем таймаут для ускорения
                await websocket.send_json({"error": "Data processing timeout"})
                return

            # Отправляем обработанные данные обратно
            await self.acquire_lock_with_timeout(self.lock2)
            try:
                # Используем bytes напрямую из numpy массива
                processed_data = bytes(self.np_buffer2[:data_length])
            finally:
                self.release_lock(self.lock2)
                
            await websocket.send_bytes(processed_data)

        except Exception as e:
            logger.error(f"Error handling audio data: {e}")
            await websocket.send_json({"error": str(e)})

    async def handle_start_audio_receiver(self):
        """Запуск аудио-приёмника с проверкой готовности"""
        logger.info("Старт метода handle_start_audio_receiver")
        
        if not self.receiver_process or not self.receiver_process.is_alive():
            logger.info("Создаем новый процесс приёмника")
            
            # Очищаем события
            self.data_ready.clear()
            self.data_processed.clear()
            self.receiver_ready.clear()
            
            # Создаем и запускаем процесс
            try:
                from websockets_audio_receiver.receiver import run_receiver
            except ImportError:
                try:
                    from .receiver import run_receiver  # type: ignore
                except ImportError:
                    import receiver  # type: ignore
                    run_receiver = receiver.run_receiver  # type: ignore
            
            self.receiver_process = mp.Process(
                target=run_receiver,
                args=(self.buffer1.name, self.buffer2.name, BUFFER_SIZE, 
                      self.data_ready, self.data_processed, self.receiver_ready)
            )
            self.receiver_process.start()
            logger.info(f"Процесс приёмника запущен с PID: {self.receiver_process.pid}")
            
            # Ждем инициализации приёмника
            try:
                logger.info("Ожидание готовности приёмника...")
                success = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, self.receiver_ready.wait),
                    timeout=5.0
                )
                logger.info(f"Результат ожидания готовности: {success}")
                if success:
                    return "receiver_started"
                else:
                    logger.error("Приёмник не сигнализировал о готовности")
                    return "failed"
            except asyncio.TimeoutError:
                logger.error("Таймаут при ожидании инициализации приёмника")
                # Не прерываем работу, возможно приёмник просто не сигнализировал о готовности
                # но все равно работает
                return "receiver_started"
            except Exception as e:
                logger.error(f"Ошибка при запуске приёмника: {e}")
                return "failed"
        else:
            logger.info("Приёмник уже запущен")
            return "receiver_already_running"

class WebSocketServer:
    def __init__(self):
        logger.info("Инициализация WebSocket сервера...")
        self.app = FastAPI(lifespan=self.lifespan)
        self.sessions: Dict[str, Session] = {}
        self.active_connections: Set[WebSocket] = set()
        self.should_exit = False
        self.setup_routes()
        self.setup_signal_handlers()
        logger.info("WebSocket сервер инициализирован")

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        """Управление жизненным циклом приложения"""
        logger.info("Запуск сервера...")
        # Проверяем доступность порта
        if not is_port_available(HOST, PORT):
            logger.error(f"Порт {PORT} уже занят")
            if not wait_for_port_release(HOST, PORT):
                raise RuntimeError(f"Порт {PORT} занят и не может быть освобожден")
        
        # Код, выполняемый при запуске
        yield
        
        # Код, выполняемый при остановке
        logger.info("Остановка сервера...")
        await self.cleanup()

    def setup_signal_handlers(self):
        """Настройка обработчиков сигналов"""
        for sig in (signal.SIGTERM, signal.SIGINT):
            if platform.system() != 'Windows':
                signal.signal(sig, self.handle_shutdown)
            else:
                signal.signal(sig, signal.SIG_DFL)

    def handle_shutdown(self, signum, frame):
        """Обработчик сигналов завершения"""
        logger.info(f"Получен сигнал {signum}, начинаем корректное завершение...")
        self.should_exit = True
        asyncio.create_task(self.cleanup())

    async def cleanup(self):
        """Очистка ресурсов при завершении"""
        logger.info("Начало очистки ресурсов...")
        
        # Закрываем все WebSocket соединения
        for ws in self.active_connections.copy():
            try:
                await ws.close()
            except Exception as e:
                logger.error(f"Ошибка при закрытии WebSocket соединения: {e}")
        
        # Очищаем все сессии
        for session_id, session in list(self.sessions.items()):
            try:
                session.cleanup()
                del self.sessions[session_id]
            except Exception as e:
                logger.error(f"Ошибка при очистке сессии {session_id}: {e}")
        
        logger.info("Очистка ресурсов завершена")

    def setup_routes(self):
        """Настройка маршрутов"""
        @self.app.get("/health")
        async def health_check():
            """Проверка работоспособности сервера"""
            return {
                "status": "ok",
                "server": "websockets_audio_receiver",
                "timestamp": datetime.now().isoformat(),
                "active_sessions": len(self.sessions),
                "active_connections": len(self.active_connections)
            }
            
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            """Основной WebSocket эндпоинт для обработки аудио данных"""
            await self.handle_websocket_connection(websocket)

    async def handle_websocket_connection(self, websocket: WebSocket):
        """Обработка WebSocket подключения"""
        await websocket.accept()
        session_id = str(id(websocket))
        session = Session()
        self.sessions[session_id] = session
        self.active_connections.add(websocket)
        logger.info(f"Новое подключение: {session_id}")
        
        # Отправляем приветственное сообщение
        await websocket.send_json({"status": "connected", "session_id": session_id})

        try:
            while session.is_connected and not self.should_exit:
                try:
                    data = await asyncio.wait_for(websocket.receive(), timeout=SESSION_TIMEOUT)
                    session.last_activity = datetime.now()
                    
                    if isinstance(data, dict) and "type" in data:
                        if data["type"] == "websocket.disconnect":
                            break
                        data = data.get("text", data.get("bytes", ""))
                    
                    logger.info(f"Получены данные: {data[:100]}...")
                    
                    try:
                        if isinstance(data, str):
                            try:
                                command = json.loads(data)
                                logger.info(f"Получена команда: {command}")
                                
                                if command.get("command") == "start_audio_receiver":
                                    logger.info("Обработка команды start_audio_receiver")
                                    result = await session.handle_start_audio_receiver()
                                    logger.info(f"Результат запуска приёмника: {result}")
                                    await websocket.send_json({"status": result})
                                    logger.info(f"Ответ отправлен: {{'status': {result}}}")
                                else:
                                    logger.warning(f"Неизвестная команда: {command.get('command')}")
                                    await websocket.send_json({"error": "Invalid command"})
                            except json.JSONDecodeError as e:
                                logger.error(f"Ошибка разбора JSON: {e}")
                                await websocket.send_json({"error": "Invalid JSON"})
                        elif isinstance(data, bytes):
                            await session.handle_audio_data(websocket, data)
                        else:
                            logger.error(f"Неизвестный тип данных: {type(data)}")
                            await websocket.send_json({"error": "Unknown data type"})
                    except Exception as e:
                        logger.error(f"Ошибка обработки данных: {e}")
                        await websocket.send_json({"error": str(e)})
                        
                except asyncio.TimeoutError:
                    logger.info(f"Таймаут сессии {session_id}")
                    break
                    
        except WebSocketDisconnect:
            logger.info(f"Клиент отключился: {session_id}")
        except Exception as e:
            logger.error(f"Ошибка в WebSocket соединении: {e}")
        finally:
            self.active_connections.remove(websocket)
            session.cleanup()
            del self.sessions[session_id]
            logger.info(f"Соединение закрыто: {session_id}")

async def run_server(host: str, port: int):
    """Запустить сервер"""
    try:
        # Импортируем receiver внутри функции, чтобы избежать циклических импортов
        try:
            from websockets_audio_receiver.receiver import run_receiver
        except ImportError:
            try:
                from .receiver import run_receiver  # type: ignore
            except ImportError:
                import receiver  # type: ignore
                run_receiver = receiver.run_receiver  # type: ignore

        if not is_port_available(host, port):
            logger.error(f"Порт {port} уже занят")
            return 1

        # Создаем приложение aiohttp для HTTP эндпоинтов
        app = web.Application()
        
        # Добавляем эндпоинт для проверки работоспособности
        async def health_check(request):
            return web.Response(text="OK")
        
        app.router.add_get('/health', health_check)
        
        # Запускаем HTTP сервер на том же порту
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port+1)  # Используем порт + 1 для HTTP
        await site.start()
        
        logger.info(f"HTTP сервер запущен на http://{host}:{port+1}/health")
        
        # Запускаем WebSocket сервер
        logger.info("Запуск сервера с параметрами:")
        logger.info(f"- HOST: {host}")
        logger.info(f"- PORT: {port}")
        logger.info(f"- SESSION_TIMEOUT: {SESSION_TIMEOUT}")
        logger.info(f"- DATA_TIMEOUT: {DATA_TIMEOUT}")
        logger.info(f"- BUFFER_SIZE: {BUFFER_SIZE}")
        
        # Создаем и запускаем сервер
        server = WebSocketServer()
        
        config = uvicorn.Config(
            app=server.app,
            host=host,
            port=port,
            log_level="info",
            loop="asyncio",
            timeout_keep_alive=SESSION_TIMEOUT,
            access_log=True
        )
        
        server_instance = uvicorn.Server(config)
        
        # Вместо блокирующего server_instance.run(), используем асинхронный метод serve()
        await server_instance.serve()
        
    except Exception as e:
        logger.error(f"Ошибка при запуске сервера: {e}")
        return 1

if __name__ == "__main__":
    # Создаем парсер аргументов командной строки
    parser = argparse.ArgumentParser(description="Запуск WebSocket сервера для аудио данных")
    parser.add_argument("--host", type=str, default=HOST, help="Хост для запуска сервера")
    parser.add_argument("--port", type=int, default=PORT, help="Порт для запуска сервера")
    
    args = parser.parse_args()
    
    # Запускаем сервер с указанными аргументами
    # Вместо asyncio.run, создаем и запускаем цикл событий вручную
    loop = setup_event_loop()
    try:
        loop.run_until_complete(run_server(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("Сервер был остановлен пользователем")
    finally:
        loop.close() 