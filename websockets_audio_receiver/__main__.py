"""
Точка входа для прямого запуска пакета
"""

import uvicorn
from .server import WebSocketServer

def main():
    server = WebSocketServer()
    uvicorn.run(server.app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main() 