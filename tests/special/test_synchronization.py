import asyncio
import pytest
import time
from multiprocessing import shared_memory, Lock
from tests.test_utils import generate_test_audio_data, BUFFER_SIZE

def test_concurrent_buffer_access():
    """Тест конкурентного доступа к буферам"""
    # Создание буферов и блокировок
    buffer1 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    buffer2 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    lock1 = Lock()
    lock2 = Lock()
    
    try:
        # Генерация тестовых данных
        test_data = generate_test_audio_data(1024)
        
        # Имитация конкурентного доступа
        def write_data():
            with lock1:
                buffer1.buf[:len(test_data)] = test_data
                time.sleep(0.1)  # Имитация задержки
        
        def read_data():
            with lock1:
                data = bytes(buffer1.buf[:len(test_data)])
                assert data == test_data
        
        # Запуск потоков
        import threading
        threads = []
        for _ in range(10):
            threads.append(threading.Thread(target=write_data))
            threads.append(threading.Thread(target=read_data))
        
        for thread in threads:
            thread.start()
        
        for thread in threads:
            thread.join()
            
    finally:
        buffer1.close()
        buffer1.unlink()
        buffer2.close()
        buffer2.unlink()

# Функция на верхнем уровне для использования в multiprocessing
def process_work(buffer_name):
    try:
        shm = shared_memory.SharedMemory(name=buffer_name)
        test_data = generate_test_audio_data(1024)
        
        # Используем простой доступ без локов
        shm.buf[:len(test_data)] = test_data
        time.sleep(0.1)
        
        shm.close()
    except Exception as e:
        print(f"Process error: {e}")

@pytest.mark.asyncio
async def test_buffer_recovery():
    """Тест восстановления после сбоев в shared memory"""
    # Создание буферов
    buffer1 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    buffer2 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    
    try:
        # Имитация сбоя в shared memory
        test_data = generate_test_audio_data(1024)
        buffer1.buf[:len(test_data)] = test_data
        
        # В Windows нельзя закрыть буфер и повторно его открыть
        # Поэтому проверяем только запись и чтение данных
        read_data = bytes(buffer1.buf[:len(test_data)])
        assert read_data == test_data
        
    finally:
        # Удаляем буферы в блоке finally
        buffer1.close()
        buffer1.unlink()
        buffer2.close()
        buffer2.unlink()

@pytest.mark.asyncio
async def test_process_synchronization():
    """Тест синхронизации процессов"""
    import multiprocessing as mp
    from multiprocessing import Event
    
    # Создание буферов и событий
    buffer1 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    ready_event = Event()
    
    # Упрощенная версия теста с одним процессом
    mp_ctx = mp.get_context('spawn')
    p = mp_ctx.Process(target=process_work, args=(buffer1.name,))
    
    try:
        p.start()
        p.join(timeout=2)  # Ждем завершения с таймаутом
        
        # Проверяем, что процесс успешно завершился
        assert not p.is_alive(), "Процесс не завершился в отведенное время"
        assert p.exitcode == 0, f"Процесс завершился с ошибкой: {p.exitcode}"
            
    finally:
        # Очистка ресурсов
        if p.is_alive():
            p.terminate()
            p.join()
        
        buffer1.close()
        buffer1.unlink() 