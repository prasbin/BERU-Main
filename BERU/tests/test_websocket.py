"""Tests for the WebSocket channel and frontend serving."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(create_app())


class TestWebSocketManager:
    """Tests for the WebSocket connection manager."""

    def test_manager_initialization(self):
        from backend.api.routers.websocket import WebSocketManager

        manager = WebSocketManager()
        assert manager.active_connections == 0

    def test_manager_disconnect_nonexistent(self):
        from backend.api.routers.websocket import WebSocketManager

        manager = WebSocketManager()
        # Should not raise
        manager.disconnect("nonexistent")

    async def test_manager_send_message_no_connection(self):
        from backend.api.routers.websocket import WebSocketManager

        manager = WebSocketManager()
        # Should not raise
        await manager.send_message("nonexistent", {"test": "data"})


class TestFrontendServing:
    """Tests for frontend file serving."""

    def test_root_returns_html(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "BERU" in response.text
        assert "<!DOCTYPE html>" in response.text

    def test_serve_app_js(self, client):
        response = client.get("/app.js")
        assert response.status_code == 200
        assert "connectWebSocket" in response.text


class TestWebSocketEndpoint:
    """Tests for the WebSocket endpoint configuration."""

    def test_websocket_route_exists(self, client):
        # Verify the WebSocket route is registered
        routes = [r.path for r in client.app.routes]
        assert "/ws/{client_id}" in routes


class TestHealthEndpoint:
    """Test health endpoint still works."""

    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
