import base64

async def test_invalid_command(websocket_client):
    """Тест обработки неверной команды."""
    await websocket_client.send_json({"command": "invalid_command"})
    response = await websocket_client.receive_json()
    assert response["error"] == "Invalid command"

async def test_base64_validation(websocket_client):
    """Тест валидации base64 данных."""
    # Тест с неверным base64
    await websocket_client.send_json({"audio_data": "invalid_base64"})
    response = await websocket_client.receive_json()
    assert response["error"] == "Invalid base64 data"
    
    # Тест с слишком большими данными
    large_data = "A" * (BUFFER_SIZE * 3)  # Создаем строку больше максимального размера
    await websocket_client.send_json({"audio_data": base64.b64encode(large_data.encode()).decode()})
    response = await websocket_client.receive_json()
    assert response["error"] == "Base64 data too large"

async def test_receiver_already_running(websocket_client):
    """Тест запуска ресивера, когда он уже запущен."""
    # Первый запуск
    await websocket_client.send_json({"command": "start_audio_receiver"})
    response = await websocket_client.receive_json()
    assert response["status"] == "receiver_started"
    
    # Второй запуск
    await websocket_client.send_json({"command": "start_audio_receiver"})
    response = await websocket_client.receive_json()
    assert response["status"] == "receiver_already_running" 