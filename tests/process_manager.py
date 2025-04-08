import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
import platform
import psutil
from typing import List, Optional, Dict, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class ProcessManager:
    """Менеджер процессов для тестов"""
    
    def __init__(self):
        self.processes: List[subprocess.Popen] = []
        self._cleanup_timeout = 5
        
    def start_process(self, cmd: List[str], env: Optional[Dict[str, str]] = None) -> subprocess.Popen:
        """Запускает процесс с заданными параметрами"""
        try:
            # Подготавливаем окружение
            process_env = os.environ.copy()
            if env:
                process_env.update(env)
            
            # Запускаем процесс
            process = subprocess.Popen(
                cmd,
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            # Добавляем в список отслеживаемых процессов
            self.processes.append(process)
            logger.info(f"Запущен процесс {process.pid}: {' '.join(cmd)}")
            
            return process
            
        except Exception as e:
            logger.error(f"Ошибка при запуске процесса: {e}")
            raise
            
    def stop_process(self, process: subprocess.Popen) -> bool:
        """Останавливает процесс"""
        if process is None:
            return True
            
        try:
            # Получаем процесс и все его дочерние процессы
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            
            # Останавливаем все процессы
            for proc in [parent] + children:
                try:
                    proc.terminate()
                except psutil.NoSuchProcess:
                    continue
                    
            # Ждем завершения процессов
            gone, alive = psutil.wait_procs([parent] + children, timeout=self._cleanup_timeout)
            
            # Если процессы все еще живы, убиваем их
            for proc in alive:
                try:
                    proc.kill()
                except psutil.NoSuchProcess:
                    continue
                    
            # Удаляем процесс из списка отслеживаемых
            if process in self.processes:
                self.processes.remove(process)
                
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при остановке процесса {process.pid}: {e}")
            return False
            
    def cleanup(self):
        """Останавливает все процессы"""
        for process in self.processes[:]:
            self.stop_process(process)
        self.processes.clear()

    # Добавляем асинхронную версию метода cleanup
    async def cleanup_async(self):
        """Асинхронная версия метода cleanup"""
        self.cleanup()  # используем синхронную версию
        
    def kill_process_on_port(self, port: int) -> bool:
        """Убивает процесс, занимающий указанный порт"""
        try:
            if platform.system() == "Windows":
                # Для Windows используем netstat
                cmd = f'netstat -ano | findstr :{port}'
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                
                if result.stdout:
                    # Получаем PID из вывода netstat
                    for line in result.stdout.splitlines():
                        if f":{port}" in line:
                            parts = line.strip().split()
                            if len(parts) >= 5:
                                pid = int(parts[-1])
                                try:
                                    process = psutil.Process(pid)
                                    process.terminate()
                                    process.wait(timeout=self._cleanup_timeout)
                                    logger.info(f"Процесс {pid} на порту {port} завершен")
                                    return True
                                except psutil.NoSuchProcess:
                                    continue
                                    
            else:
                # Для Unix-подобных систем используем lsof
                cmd = f"lsof -i :{port} -t"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                
                if result.stdout:
                    pid = int(result.stdout.strip())
                    try:
                        process = psutil.Process(pid)
                        process.terminate()
                        process.wait(timeout=self._cleanup_timeout)
                        logger.info(f"Процесс {pid} на порту {port} завершен")
                        return True
                    except psutil.NoSuchProcess:
                        pass
                        
            return False
            
        except Exception as e:
            logger.error(f"Ошибка при освобождении порта {port}: {e}")
            return False

@contextmanager
def managed_process(cmd: List[str], env: Optional[Dict[str, str]] = None):
    """Контекстный менеджер для управления процессом"""
    manager = ProcessManager()
    process = None
    
    try:
        process = manager.start_process(cmd, env)
        yield process
    finally:
        if process:
            manager.stop_process(process)
        manager.cleanup()

class ManagedProcess:
    """Контекстный менеджер для управления процессом"""
    
    def __init__(self, cmd: List[str], env: Optional[Dict[str, str]] = None):
        self.cmd = cmd
        self.env = env
        self.process_manager = ProcessManager()
        self.process = None
        
    async def __aenter__(self) -> subprocess.Popen:
        self.process = await self.process_manager.start_process(self.cmd, self.env)
        return self.process
        
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.process:
            await self.process_manager.stop_process(self.process)
        await self.process_manager.cleanup() 