import asyncio
import logging
import multiprocessing as mp
import os
import signal
import socket
import sys
import time
import atexit
import psutil
from multiprocessing import shared_memory
from typing import Generator
import json
import weakref
import subprocess
import aiohttp
import platform
from datetime import datetime

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from websockets_audio_receiver.server import WebSocketServer, Session, SESSION_TIMEOUT
from tests.process_manager import ProcessManager, ManagedProcess

# Настройка event loop для Windows
if platform.system() == 'Windows':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Добавляем корневую директорию проекта в путь импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Глобальный список для отслеживания всех процессов
_cleanup_processes = weakref.WeakSet()

def cleanup_all_processes():
    """Завершает все процессы при выходе"""
    for process in _cleanup_processes:
        try:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
        except (AttributeError, ProcessLookupError):
            continue

# Регистрируем функцию очистки
atexit.register(cleanup_all_processes)

@pytest.fixture(scope="session")
def event_loop():
    """Создает и настраивает event loop для тестов"""
    # Создаем новый event loop
    if platform.system() == 'Windows':
        loop = asyncio.WindowsSelectorEventLoopPolicy().new_event_loop()
    else:
        loop = asyncio.new_event_loop()
    
    # Устанавливаем его как текущий
    asyncio.set_event_loop(loop)
    
    yield loop
    
    # Закрываем loop после всех тестов
    try:
        # Отменяем все задачи
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        
        # Запускаем loop до завершения всех задач
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        
        # Закрываем loop
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
    except Exception as e:
        logger.error(f"Ошибка при закрытии event loop: {e}")

@pytest.fixture(autouse=True)
async def cleanup_after_test(event_loop):
    """Очищает все задачи после каждого теста"""
    yield
    
    # Получаем все задачи, кроме текущей
    current_task = asyncio.current_task(event_loop)
    tasks = [t for t in asyncio.all_tasks(event_loop) if t is not current_task]
    
    # Отменяем все задачи
    for task in tasks:
        task.cancel()
    
    # Ждем завершения всех задач
    if tasks:
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Ошибка при отмене задач: {e}")
    
    # Даем время на завершение задач
    await asyncio.sleep(0.1)

def run_server(host: str, port: int):
    """Запускает сервер в отдельном процессе"""
    server = WebSocketServer()
    
    @server.app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        session_id = str(id(websocket))
        session = server.sessions[session_id] = Session()
        logger.info(f"Новое подключение: {session_id}")

        try:
            while session.is_connected:
                data = await websocket.receive()
                if isinstance(data, dict) and "type" in data:
                    if data["type"] == "websocket.disconnect":
                        break
                    data = data.get("text", data.get("bytes", ""))
                
                try:
                    if isinstance(data, str):
                        command = json.loads(data)
                        if command.get("command") == "start_audio_receiver":
                            status = await session.handle_start_audio_receiver()
                            if status == "receiver_started":
                                await websocket.send_json({"status": "receiver_started"})
                            elif status == "receiver_already_running":
                                await websocket.send_json({"status": "receiver_already_running"})
                            else:
                                await websocket.send_json({"error": "Failed to start receiver"})
                        else:
                            await websocket.send_json({"error": "Invalid command"})
                    elif isinstance(data, bytes):
                        await session.handle_audio_data(websocket, data)
                except json.JSONDecodeError:
                    await websocket.send_json({"error": "Invalid JSON"})
                except Exception as e:
                    await websocket.send_json({"error": str(e)})
        finally:
            session.cleanup()
            del server.sessions[session_id]
    
    uvicorn.run(server.app, host=host, port=port, log_level="info")

def kill_process_on_port(port: int):
    """Убивает процесс, занимающий указанный порт"""
    logger.info(f"Попытка освободить порт {port}")
    
    # Сначала пробуем найти процесс по порту
    for proc in psutil.process_iter(['pid', 'name', 'connections']):
        try:
            for conn in proc.connections():
                if conn.laddr.port == port:
                    logger.info(f"Найден процесс {proc.pid} ({proc.name()}), занимающий порт {port}")
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                        logger.info(f"Процесс {proc.pid} успешно завершен")
                    except psutil.TimeoutExpired:
                        logger.warning(f"Процесс {proc.pid} не завершился за 5 секунд, принудительное завершение")
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                            logger.info(f"Процесс {proc.pid} принудительно завершен")
                        except psutil.TimeoutExpired:
                            logger.error(f"Не удалось завершить процесс {proc.pid}")
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    # Если не нашли по порту, ищем процессы Python с uvicorn
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.name().lower().startswith('python'):
                cmdline = proc.cmdline()
                if any('uvicorn' in cmd.lower() for cmd in cmdline):
                    logger.info(f"Найден процесс uvicorn {proc.pid}")
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                        logger.info(f"Процесс {proc.pid} успешно завершен")
                    except psutil.TimeoutExpired:
                        logger.warning(f"Процесс {proc.pid} не завершился за 5 секунд, принудительное завершение")
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                            logger.info(f"Процесс {proc.pid} принудительно завершен")
                        except psutil.TimeoutExpired:
                            logger.error(f"Не удалось завершить процесс {proc.pid}")
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    logger.info(f"Не найдено процессов, занимающих порт {port}")
    return False

def is_port_available(host: str, port: int) -> bool:
    """Проверяет, доступен ли порт (возвращает True, если порт свободен)"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
            return True  # Если мы смогли привязаться к порту, значит он свободен
    except OSError:
        return False  # Если возникла ошибка, значит порт занят

async def check_server_health(host, port):
    """Проверить работоспособность сервера через HTTP эндпоинт /health"""
    url = f"http://{host}:{port+1}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=2) as response:
                if response.status == 200:
                    return True
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return False

async def wait_for_port(host: str, port: int, timeout: float = 30.0, interval: float = 1.0) -> bool:
    """Ожидает, пока порт станет доступен (свободен)"""
    logger.info(f"Ожидание порта {host}:{port} в течение {timeout} секунд")
    start_time = time.time()
    while time.time() - start_time < timeout:
        # Проверяем, занят ли порт
        if not is_port_available(host, port):
            # Проверяем доступность /health эндпоинта
            if await check_server_health(host, port):
                logger.info(f"Порт {port} занят и сервер работает")
                return True
            else:
                logger.info(f"Порт {port} занят, но сервер еще инициализируется, ждем...")
                
        await asyncio.sleep(interval)
    return False

class TestServer:
    """Тестовый WebSocket сервер"""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 8004, start_timeout: float = 20.0, check_interval: float = 1.0):
        self.host = host
        self.port = port
        self.start_timeout = start_timeout
        self.check_interval = check_interval
        self.process = None
        self.process_manager = ProcessManager()
        self.url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self._health_check_url = f"http://{host}:{port}/health"
        self._server_output = []
        self.sessions = {}
        self.active_connections = set()
        self.should_exit = False
        
        logger.info(f"Инициализация TestServer на {host}:{port}")
        logger.info(f"Таймаут запуска: {self.start_timeout}с")
        logger.info(f"Интервал проверки: {self.check_interval}с")
        
    async def start(self):
        """Запускает тестовый сервер"""
        logger.info("="*50)
        logger.info("Начало запуска тестового сервера")
        logger.info(f"Хост: {self.host}, Порт: {self.port}")

        try:
            # Проверяем, не занят ли порт
            if not is_port_available(self.host, self.port):
                logger.warning(f"Порт {self.port} уже занят, проверяем работоспособность сервера")
                if await check_server_health(self.host, self.port):
                    logger.info("Сервер уже запущен и работает корректно")
                    return True
                logger.warning(f"Порт {self.port} занят, но сервер не отвечает. Занятый порт может помешать тестам.")
                # Даем время на освобождение порта
                await asyncio.sleep(1)

            # Запускаем сервер
            logger.info("Запуск сервера...")
            self.process = self.process_manager.start_process(
                [sys.executable, "-m", "websockets_audio_receiver.server"],
                env={"PYTHONPATH": os.getcwd()}
            )

            # Ждем, пока сервер запустится
            start_time = time.time()
            server_started = False

            while time.time() - start_time < self.start_timeout:
                try:
                    # Проверяем, что процесс все еще работает
                    if not self.process.is_alive():
                        logger.error("Процесс сервера завершился")
                        logger.error(f"Код возврата: {self.process.returncode}")
                        if self.process.stdout:
                            output = self.process.stdout.read()
                            if isinstance(output, bytes):
                                output = output.decode()
                            logger.error(f"Вывод: {output}")
                        raise RuntimeError("Сервер не запустился: процесс завершился")

                    # Проверяем доступность порта - если порт занят, значит сервер, скорее всего, запущен
                    if not is_port_available(self.host, self.port):
                        logger.info("Порт занят, проверяем доступность /health")
                        # Даем серверу дополнительное время на инициализацию
                        await asyncio.sleep(3)
                        
                        # Проверяем эндпоинт /health
                        if await check_server_health(self.host, self.port):
                            server_started = True
                            logger.info("Сервер успешно запущен и отвечает на /health")
                            break
                        logger.info("Сервер запускается, но еще не отвечает на /health")

                    await asyncio.sleep(self.check_interval)

                except Exception as e:
                    logger.debug(f"Ожидание запуска сервера: {e}")
                    await asyncio.sleep(self.check_interval)

            if not server_started:
                logger.error("Сервер не запустился за отведенное время")
                if self.process.stdout:
                    output = self.process.stdout.read()
                    if isinstance(output, bytes):
                        output = output.decode()
                    logger.error(f"Вывод сервера: {output}")
                else:
                    logger.error("Нет вывода от сервера")
                raise RuntimeError("Сервер не запустился за отведенное время")

        except Exception as e:
            logger.error("="*50)
            logger.error(f"ОШИБКА ЗАПУСКА СЕРВЕРА: {str(e)}")
            logger.error(f"Тип ошибки: {type(e).__name__}")
            logger.error("="*50)
            await self.stop()
            raise
            
    async def stop(self):
        """Останавливает тестовый сервер"""
        if self.process:
            logger.info("Остановка тестового сервера")
            self.should_exit = True
            self.process_manager.stop_process(self.process)
            self.process_manager.cleanup()
            self.process = None
            logger.info("Сервер остановлен")
            
    async def handle_websocket_connection(self, websocket: WebSocket):
        """Обработка WebSocket подключения"""
        await websocket.accept()
        session_id = str(id(websocket))
        session = Session()
        self.sessions[session_id] = session
        self.active_connections.add(websocket)
        logger.info(f"Новое подключение: {session_id}")

        try:
            while session.is_connected and not self.should_exit:
                try:
                    data = await asyncio.wait_for(websocket.receive(), timeout=SESSION_TIMEOUT)
                    session.last_activity = datetime.now()
                    
                    if isinstance(data, dict) and "type" in data:
                        if data["type"] == "websocket.disconnect":
                            break
                        data = data.get("text", data.get("bytes", ""))
                    
                    try:
                        if isinstance(data, str):
                            command = json.loads(data)
                            if command.get("command") == "start_audio_receiver":
                                status = await session.handle_start_audio_receiver()
                                await websocket.send_json({"status": status})
                            else:
                                await websocket.send_json({"error": "Invalid command"})
                        elif isinstance(data, bytes):
                            await session.handle_audio_data(websocket, data)
                    except json.JSONDecodeError:
                        await websocket.send_json({"error": "Invalid JSON"})
                    except Exception as e:
                        logger.error(f"Ошибка обработки данных: {e}")
                        await websocket.send_json({"error": str(e)})
                except asyncio.TimeoutError:
                    logger.warning(f"Таймаут сессии: {session_id}")
                    break
                except Exception as e:
                    logger.error(f"Ошибка в цикле обработки: {e}")
                    break
        finally:
            session.cleanup()
            del self.sessions[session_id]
            self.active_connections.remove(websocket)
            logger.info(f"Соединение закрыто: {session_id}")

@pytest.fixture
async def process_manager():
    """Фикстура для менеджера процессов"""
    manager = ProcessManager()
    yield manager
    try:
        await manager.cleanup_async()
    except Exception as e:
        logger.error(f"Ошибка при очистке process_manager: {e}")

@pytest.fixture
async def test_server(process_manager):
    """Фикстура для тестового сервера"""
    server = TestServer()
    try:
        await server.start()
        yield server
    finally:
        await server.stop()

@pytest.fixture(scope="function")
async def ws_client(test_server):
    """Фикстура для создания WebSocket клиента"""
    from tests.unit.test_audio_receiver import TestWebSocketClient
    client = TestWebSocketClient(uri="ws://127.0.0.1:8004/ws")
    try:
        await client.connect()
        yield client
    finally:
        try:
            await client.disconnect()
            # Даем время на закрытие соединения
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка при отключении клиента: {e}")

@pytest.fixture(scope="function")
def buffer1():
    """Фикстура для создания первого буфера"""
    buffer = shared_memory.SharedMemory(create=True, size=1024)
    yield buffer
    buffer.close()
    buffer.unlink()

@pytest.fixture(scope="function")
def buffer2():
    """Фикстура для создания второго буфера"""
    buffer = shared_memory.SharedMemory(create=True, size=1024)
    yield buffer
    buffer.close()
    buffer.unlink()

@pytest.fixture(scope="function")
def data_ready():
    """Фикстура для создания события готовности данных"""
    event = mp.Event()
    yield event
    event.set()

@pytest.fixture(scope="function")
def data_processed():
    """Фикстура для создания события обработки данных"""
    event = mp.Event()
    yield event
    event.set()

@pytest.fixture(scope="function")
def receiver_ready():
    """Фикстура для создания события готовности приёмника"""
    event = mp.Event()
    yield event
    event.set()

@pytest.fixture(scope="function")
def receiver_process(buffer1, buffer2, data_ready, data_processed, receiver_ready):
    """Фикстура для создания процесса приёмника"""
    from websockets_audio_receiver.receiver import run_receiver
    process = mp.Process(
        target=run_receiver,
        args=(buffer1.name, buffer2.name, 1024, data_ready, data_processed, receiver_ready)
    )
    process.start()
    yield process
    process.terminate()
    process.join() 