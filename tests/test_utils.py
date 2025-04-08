import asyncio
import base64
import json
import logging
import os
import random
import string
import time
from typing import Optional, Tuple

import pytest
import websockets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from multiprocessing import shared_memory

from websockets_audio_receiver.server import WebSocketServer, Session, BUFFER_SIZE

# Настройка логирования для тестов
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class TestWebSocketClient:
    def __init__(self, uri: str = "ws://127.0.0.1:8004/ws"):
        self.uri = uri
        self.ws = None
        self.received_messages = []
        self._connect_timeout = 15  # Увеличиваем таймаут подключения
        self._receive_timeout = 15   # Увеличиваем таймаут получения
        self._max_size = BUFFER_SIZE * 2  # Увеличиваем максимальный размер сообщения

    async def connect(self):
        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(self.uri),
                timeout=self._connect_timeout
            )
            logger.info(f"Подключено к {self.uri}")
            
            # Ждем приветственное сообщение
            try:
                welcome_msg = await asyncio.wait_for(
                    self.ws.recv(),
                    timeout=self._receive_timeout
                )
                logger.info(f"Получено приветственное сообщение: {welcome_msg}")
            except asyncio.TimeoutError:
                logger.warning("Не получено приветственное сообщение")
            
            return self
        except asyncio.TimeoutError:
            logger.error(f"Таймаут подключения к {self.uri}")
            raise
        except Exception as e:
            logger.error(f"Ошибка подключения к {self.uri}: {e}")
            raise

    async def disconnect(self):
        if self.ws:
            await self.ws.close()
            self.ws = None
            logger.info("Отключено от сервера")

    async def send_command(self, command: str) -> dict:
        """Отправляет команду на сервер и ждет ответа"""
        if not self.ws:
            raise ConnectionError("Нет подключения к серверу")
        
        logger.info(f"Отправка команды: {command}")
        # Отправляем команду в простом JSON формате
        await self.ws.send(json.dumps({"command": command}))
        logger.info("Команда отправлена, ожидание ответа...")
        
        start_time = time.time()
        max_wait_time = 30  # Максимальное время ожидания ответа в секундах
        
        while time.time() - start_time < max_wait_time:
            try:
                response = await asyncio.wait_for(
                    self.ws.recv(),
                    timeout=self._receive_timeout
                )
                
                if isinstance(response, str):
                    try:
                        json_response = json.loads(response)
                        logger.info(f"Получен ответ: {json_response}")
                        
                        # Игнорируем приветственное сообщение
                        if json_response.get("status") == "connected":
                            logger.info("Получено приветственное сообщение, продолжаем ожидание ответа на команду...")
                            continue
                        
                        # Проверяем наличие ошибки
                        if "error" in json_response:
                            logger.error(f"Получена ошибка от сервера: {json_response['error']}")
                            raise ValueError(json_response["error"])
                        
                        # Проверяем наличие статуса
                        if "status" not in json_response:
                            logger.error(f"В ответе отсутствует поле 'status': {json_response}")
                            continue
                        
                        # Проверяем, что ответ содержит ожидаемые поля
                        if command == "start_audio_receiver":
                            if json_response["status"] not in ["receiver_started", "receiver_already_running"]:
                                logger.error(f"Неверный статус для команды start_audio_receiver: {json_response['status']}")
                                continue
                            logger.info(f"Получен корректный ответ на команду start_audio_receiver: {json_response}")
                        elif command == "get_receiver_status":
                            if json_response["status"] not in ["running", "stopped"]:
                                logger.error(f"Неверный статус для команды get_receiver_status: {json_response['status']}")
                                continue
                            logger.info(f"Получен корректный ответ на команду get_receiver_status: {json_response}")
                        
                        return json_response
                    except json.JSONDecodeError:
                        logger.error(f"Не удалось разобрать JSON: {response}")
                        raise ValueError("Invalid JSON response")
                else:
                    logger.error(f"Неожиданный тип ответа: {type(response)}")
                    raise ValueError("Unexpected response type")
                    
            except asyncio.TimeoutError:
                logger.error(f"Таймаут при получении ответа на команду {command}")
                if time.time() - start_time >= max_wait_time:
                    raise TimeoutError(f"Сервер не ответил в течение {max_wait_time} секунд после отправки команды {command}")
                continue
            except Exception as e:
                logger.error(f"Ошибка при отправке команды: {e}")
                raise
        
        raise TimeoutError(f"Сервер не ответил в течение {max_wait_time} секунд после отправки команды {command}")

    async def send_audio_data(self, audio_data: bytes) -> bytes:
        """Отправляет аудио данные на сервер и ждет ответа"""
        if not self.ws:
            raise ConnectionError("Нет подключения к серверу")
        
        logger.info(f"Отправка аудио данных размером {len(audio_data)} байт")
        await self.ws.send(audio_data)
        
        while True:
            try:
                response = await asyncio.wait_for(
                    self.ws.recv(),
                    timeout=self._receive_timeout
                )
                
                if isinstance(response, bytes):
                    return response
                elif isinstance(response, str):
                    try:
                        json_response = json.loads(response)
                        # Игнорируем приветственное сообщение
                        if json_response.get("status") == "connected":
                            logger.info("Получено приветственное сообщение, ожидание ответа на аудио данные...")
                            continue
                        # Если получили подтверждение получения данных, ждем бинарный ответ
                        if json_response.get("status") == "data_received":
                            logger.info("Получено подтверждение получения данных, ожидание бинарного ответа...")
                            continue
                        if "error" in json_response:
                            raise ValueError(json_response["error"])
                        logger.warning(f"Получен неожиданный JSON ответ: {json_response}")
                        continue
                    except json.JSONDecodeError:
                        logger.error(f"Не удалось разобрать JSON: {response}")
                        raise ValueError("Invalid JSON response")
                else:
                    logger.error(f"Неожиданный тип ответа: {type(response)}")
                    raise ValueError("Unexpected response type")
                    
            except asyncio.TimeoutError:
                logger.error("Таймаут при получении ответа на аудио данные")
                raise TimeoutError(f"Сервер не ответил в течение {self._receive_timeout} секунд после отправки аудио данных")
            except Exception as e:
                logger.error(f"Ошибка при отправке аудио данных: {e}")
                raise

    async def receive_messages(self):
        while True:
            try:
                message = await self.ws.recv()
                self.received_messages.append(json.loads(message))
            except websockets.exceptions.ConnectionClosed:
                break

def generate_test_audio_data(size: int = 1024) -> bytes:
    """Генерирует тестовые аудиоданные заданного размера"""
    return bytes(random.getrandbits(8) for _ in range(size))

def create_test_session() -> Tuple[Session, str, str]:
    """Создает тестовую сессию и возвращает имена буферов"""
    session = Session()
    return session, session.buffer1.name, session.buffer2.name

def cleanup_test_session(session: Session):
    """Очищает ресурсы тестовой сессии"""
    session.cleanup()

@pytest.fixture
async def test_client():
    """Фикстура для создания тестового клиента"""
    client = TestWebSocketClient()
    await client.connect()
    yield client
    await client.disconnect()

@pytest.fixture
def test_session():
    """Фикстура для создания тестовой сессии"""
    session, buffer1_name, buffer2_name = create_test_session()
    yield session, buffer1_name, buffer2_name
    cleanup_test_session(session)

@pytest.fixture
def test_app():
    """Фикстура для создания тестового FastAPI приложения"""
    server = WebSocketServer()
    return server.app

def measure_latency(func):
    """Декоратор для измерения времени выполнения функции"""
    async def wrapper(*args, **kwargs):
        start_time = time.time()
        result = await func(*args, **kwargs)
        end_time = time.time()
        return result, end_time - start_time
    return wrapper

class MemoryMonitor:
    """Класс для мониторинга использования памяти"""
    def __init__(self):
        self.initial_memory = self._get_memory_usage()
        self.measurements = []

    def _get_memory_usage(self) -> float:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024  # в МБ

    def measure(self):
        current = self._get_memory_usage()
        self.measurements.append(current - self.initial_memory)
        return current - self.initial_memory

    def get_statistics(self):
        if not self.measurements:
            return None
        return {
            "min": min(self.measurements),
            "max": max(self.measurements),
            "avg": sum(self.measurements) / len(self.measurements)
        } 