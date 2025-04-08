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

@pytest.mark.asyncio
async def test_race_conditions():
    """Тест race conditions при работе с буферами"""
    # Создание буферов
    buffer1 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    buffer2 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    lock1 = Lock()
    lock2 = Lock()
    
    try:
        # Генерация тестовых данных
        test_data = [generate_test_audio_data(1024) for _ in range(5)]
        
        # Функции для имитации race conditions
        async def write_data(data):
            with lock1:
                buffer1.buf[:len(data)] = data
                await asyncio.sleep(0.1)
        
        async def read_data():
            with lock1:
                return bytes(buffer1.buf[:1024])
        
        # Параллельная запись и чтение
        tasks = []
        for data in test_data:
            tasks.append(write_data(data))
            tasks.append(read_data())
        
        results = await asyncio.gather(*tasks)
        
        # Проверка целостности данных
        read_results = results[1::2]  # Четные результаты - чтение
        assert all(data in test_data for data in read_results)
        
    finally:
        buffer1.close()
        buffer1.unlink()
        buffer2.close()
        buffer2.unlink()

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
        
        # Попытка восстановления
        try:
            buffer1.close()
            buffer1 = shared_memory.SharedMemory(name=buffer1.name)
            recovered_data = bytes(buffer1.buf[:len(test_data)])
            assert recovered_data == test_data
        except Exception as e:
            pytest.fail(f"Failed to recover from shared memory error: {e}")
            
    finally:
        buffer1.close()
        buffer1.unlink()
        buffer2.close()
        buffer2.unlink()

@pytest.mark.asyncio
async def test_process_synchronization():
    """Тест синхронизации процессов"""
    import multiprocessing as mp
    
    # Создание буферов
    buffer1 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    buffer2 = shared_memory.SharedMemory(create=True, size=BUFFER_SIZE)
    lock1 = Lock()
    lock2 = Lock()
    
    try:
        # Функция для процесса
        def process_work(buffer_name, lock):
            shm = shared_memory.SharedMemory(name=buffer_name)
            l = Lock()
            test_data = generate_test_audio_data(1024)
            
            with l:
                shm.buf[:len(test_data)] = test_data
                time.sleep(0.1)
            
            shm.close()
        
        # Запуск процессов
        processes = []
        for _ in range(5):
            p = mp.Process(target=process_work, args=(buffer1.name, lock1))
            processes.append(p)
            p.start()
        
        # Ожидание завершения
        for p in processes:
            p.join()
            
    finally:
        buffer1.close()
        buffer1.unlink()
        buffer2.close()
        buffer2.unlink() 