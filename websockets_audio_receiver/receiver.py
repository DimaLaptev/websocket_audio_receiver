import asyncio
import logging
import multiprocessing as mp
from multiprocessing import Queue, shared_memory, Event
import time
import numpy as np
from concurrent.futures import ThreadPoolExecutor

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

def run_receiver(input_buffer_name, output_buffer_name, buffer_size, data_ready_event, processing_done_event, receiver_ready_event=None, stop_event=None):
    """
    Функция для запуска аудио-приемника в отдельном процессе.
    
    Args:
        input_buffer_name (str): Имя разделяемого входного буфера в памяти
        output_buffer_name (str): Имя разделяемого выходного буфера в памяти
        buffer_size (int): Размер буфера
        data_ready_event (multiprocessing.Event): Событие, сигнализирующее о готовности данных
        processing_done_event (multiprocessing.Event): Событие, сигнализирующее о завершении обработки
        receiver_ready_event (multiprocessing.Event, optional): Событие, сигнализирующее о готовности приемника
        stop_event (multiprocessing.Event, optional): Событие для остановки приемника
    """
    logger.info(f"Аудио-приемник запущен с буфером размером {buffer_size} байт")
    
    try:
        # Подключение к разделяемой памяти
        input_buffer = shared_memory.SharedMemory(name=input_buffer_name)
        output_buffer = shared_memory.SharedMemory(name=output_buffer_name)
        logger.info(f"Подключено к разделяемой памяти: вход {input_buffer_name}, выход {output_buffer_name}")
        
        # Создание буферов numpy для эффективной обработки данных
        # Используем C_CONTIGUOUS и кэшированное выравнивание для оптимизации доступа
        input_array = np.ndarray((buffer_size,), dtype=np.uint8, buffer=input_buffer.buf, order='C')
        output_array = np.ndarray((buffer_size,), dtype=np.uint8, buffer=output_buffer.buf, order='C')
        
        # Сигнализируем о готовности приемника, если предоставлено событие
        if receiver_ready_event is not None:
            receiver_ready_event.set()
            logger.info("Сигнал о готовности приемника отправлен")
        
        # Убедимся, что события находятся в исходном состоянии
        data_ready_event.clear()
        processing_done_event.clear()
        
        # Адаптивный интервал опроса: начинаем с более высокой частоты, затем адаптируемся
        poll_interval = 0.001  # 1ms для лучшей отзывчивости
        max_poll_interval = 0.01  # Максимум 10ms
        idle_count = 0
        max_idle_before_adjust = 10
        
        # Инициализация счетчиков для мониторинга производительности
        process_count = 0
        last_time_check = time.time()
        processing_times = []
        
        # Основной цикл обработки с адаптивным ожиданием
        while stop_event is None or not stop_event.is_set():
            # Ожидание новых данных с адаптивным таймаутом
            if data_ready_event.wait(timeout=poll_interval):
                # Сброс счетчика простоя при получении данных
                idle_count = 0
                start_time = time.time()
                
                try:
                    # Используем прямое копирование с оптимизацией для непрерывных массивов
                    # Это минимизирует копирование данных и использует векторизацию CPU
                    np.copyto(output_array, input_array, casting='no')
                    
                    # Сигнализируем о завершении обработки
                    data_ready_event.clear()
                    processing_done_event.set()
                    
                    # Отслеживаем время обработки
                    end_time = time.time()
                    processing_times.append(end_time - start_time)
                    process_count += 1
                    
                    # Периодически очищаем список для предотвращения утечек памяти
                    if len(processing_times) > 100:
                        processing_times = processing_times[-50:]
                    
                    # Логируем производительность каждые 100 обработок
                    if process_count % 100 == 0:
                        current_time = time.time()
                        elapsed = current_time - last_time_check
                        avg_processing = np.mean(processing_times) * 1000 if processing_times else 0
                        logger.info(f"Производительность: {process_count/elapsed:.2f} фреймов/сек, среднее время обработки: {avg_processing:.2f}мс")
                        last_time_check = current_time
                        
                except Exception as e:
                    logger.error(f"Ошибка при обработке данных: {e}")
                    # Даже при ошибке гарантируем корректное состояние событий
                    data_ready_event.clear()
                    processing_done_event.set()
            else:
                # Увеличиваем интервал опроса при простое для снижения нагрузки на CPU
                idle_count += 1
                if idle_count >= max_idle_before_adjust:
                    poll_interval = min(poll_interval * 1.5, max_poll_interval)
                    idle_count = 0
                    # При долгом простое уменьшаем частоту записи логов
                    if process_count > 0:
                        logger.debug(f"Простой: адаптивный интервал опроса установлен на {poll_interval*1000:.2f}мс")
    
    except Exception as e:
        logger.error(f"Ошибка в аудио-приемнике: {e}")
    
    finally:
        try:
            # Очистка ресурсов
            input_buffer.close()
            output_buffer.close()
            logger.info("Аудио-приемник завершил работу и освободил ресурсы")
        except Exception as e:
            logger.error(f"Ошибка при освобождении ресурсов: {e}")

if __name__ == "__main__":
    # Код для тестирования приемника
    from multiprocessing import Event, Process
    import numpy as np
    
    # Создание разделяемой памяти для тестирования
    buffer_size = 1024
    input_mem = shared_memory.SharedMemory(create=True, size=buffer_size)
    output_mem = shared_memory.SharedMemory(create=True, size=buffer_size)
    
    # Создание событий для синхронизации
    data_ready = Event()
    processing_done = Event()
    receiver_ready = Event()
    stop_event = Event()
    
    # Запуск приемника в отдельном процессе
    receiver_process = Process(
        target=run_receiver,
        args=(input_mem.name, output_mem.name, buffer_size, 
              data_ready, processing_done, receiver_ready, stop_event)
    )
    receiver_process.start()
    
    # Ожидаем готовности приемника
    receiver_ready.wait(timeout=5.0)
    
    try:
        # Тестовое заполнение буфера
        input_array = np.ndarray((buffer_size,), dtype=np.uint8, buffer=input_mem.buf)
        output_array = np.ndarray((buffer_size,), dtype=np.uint8, buffer=output_mem.buf)
        
        # Имитация отправки данных
        for i in range(5):
            input_array[:] = np.random.randint(0, 255, size=buffer_size, dtype=np.uint8)
            data_ready.set()
            processing_done.wait()
            processing_done.clear()
            time.sleep(0.1)
            print(f"Тест {i+1}: Среднее значение обработанных данных: {np.mean(output_array):.2f}")
        
        # Остановка приемника
        stop_event.set()
        receiver_process.join(timeout=1.0)
        
    finally:
        # Очистка ресурсов
        input_mem.close()
        input_mem.unlink()
        output_mem.close()
        output_mem.unlink()