import asyncio
import logging
import multiprocessing as mp
from multiprocessing import Queue, shared_memory, Event
import time
import numpy as np
from concurrent.futures import ThreadPoolExecutor

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_receiver(buffer1_name: str, buffer2_name: str, buffer_size: int, data_ready: Event, data_processed: Event, receiver_ready: Event):
    """
    Запускает процесс аудио-приёмника с оптимизированной обработкой данных.
    
    Args:
        buffer1_name (str): Имя shared memory буфера для входных данных
        buffer2_name (str): Имя shared memory буфера для выходных данных
        buffer_size (int): Размер буферов
        data_ready (Event): Событие, сигнализирующее о готовности данных
        data_processed (Event): Событие, сигнализирующее об окончании обработки
        receiver_ready (Event): Событие, сигнализирующее о готовности приёмника
    """
    try:
        logger.info("Receiver process started")
        
        # Открываем shared memory буферы
        buffer1 = shared_memory.SharedMemory(name=buffer1_name)
        buffer2 = shared_memory.SharedMemory(name=buffer2_name)

        # Создаем numpy массивы как C-непрерывные для более быстрого копирования
        arr1 = np.ndarray((buffer_size,), dtype=np.uint8, buffer=buffer1.buf)
        arr2 = np.ndarray((buffer_size,), dtype=np.uint8, buffer=buffer2.buf)

        # Предварительно выделяем буфер для предотвращения фрагментации памяти
        temp_buffer = np.zeros((buffer_size,), dtype=np.uint8)

        # Устанавливаем высокий приоритет для процесса
        try:
            import psutil
            p = psutil.Process()
            p.nice(psutil.HIGH_PRIORITY_CLASS if hasattr(psutil, 'HIGH_PRIORITY_CLASS') else -10)
            
            # Установка процессорной привязки для снижения переключений контекста
            if hasattr(p, 'cpu_affinity') and len(p.cpu_affinity()) > 1:
                # Привязываем к одному ядру для уменьшения прерываний
                p.cpu_affinity([p.cpu_affinity()[0]])
        except Exception as e:
            logger.warning(f"Failed to set process priority: {e}")

        # Сигнализируем о готовности приёмника
        receiver_ready.set()
        logger.info("Receiver initialization completed")

        # Создаем пул потоков для фоновых задач
        executor = ThreadPoolExecutor(max_workers=1)
        
        # Для быстрого времени реакции используем спин-лок вместо wait с таймаутом
        spin_count = 0
        max_spin = 1000  # Максимальное количество итераций перед переключением на сон
        
        while True:
            # Используем спин-лок с периодическим переключением на сон для снижения нагрузки на CPU
            if data_ready.is_set():
                try:
                    # Используем более быстрый метод копирования с контролем выравнивания памяти
                    # Отключаем проверки для повышения производительности
                    np.copyto(arr2, arr1, casting='unsafe')
                    
                    # Сигнализируем об окончании обработки и сбрасываем флаг готовности
                    data_processed.set()
                    data_ready.clear()
                except Exception as e:
                    logger.error(f"Error during array copy: {e}")
                    try:
                        # Запасной метод с использованием memcpy из numpy
                        np.asarray(arr2).view(np.uint8)[:] = np.asarray(arr1).view(np.uint8)[:]
                        data_processed.set()
                        data_ready.clear()
                    except Exception as copy_err:
                        logger.error(f"Backup copy method also failed: {copy_err}")
                
                # Сбрасываем счетчик спина после обработки данных
                spin_count = 0
            else:
                # Используем спин-лок для снижения задержки
                spin_count += 1
                if spin_count >= max_spin:
                    # Если достигли максимального числа итераций, даем другим процессам время выполнения
                    time.sleep(0.0001)  # 100 микросекунд
                    spin_count = 0

    except Exception as e:
        logger.error(f"Error in receiver process: {e}")
    finally:
        # Закрываем shared memory буферы и освобождаем ресурсы
        try:
            buffer1.close()
            buffer2.close()
            if 'executor' in locals():
                executor.shutdown(wait=False)
        except Exception as cleanup_err:
            logger.error(f"Error during cleanup: {cleanup_err}")
        logger.info("Receiver process shutting down")

if __name__ == "__main__":
    # Этот блок используется только для тестирования
    buffer1 = shared_memory.SharedMemory(create=True, size=1024)
    buffer2 = shared_memory.SharedMemory(create=True, size=1024)
    data_ready = Event()
    data_processed = Event()
    receiver_ready = Event()
    
    try:
        run_receiver(
            buffer1.name,
            buffer2.name,
            1024,
            data_ready,
            data_processed,
            receiver_ready
        )
    finally:
        buffer1.close()
        buffer1.unlink()
        buffer2.close()
        buffer2.unlink() 