"""Mock Core Node API server for E2E testing"""
import asyncio
import json
import time
from aiohttp import web
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def handle_models(request):
    """Mock /api/v1/models endpoint - returns models in Core Node format"""
    models_data = [
        {
            "model_name": "Qwen/Qwen3-8B",
            "model_hash": "0" * 64,
            "model_commit": "9c925d64d72725edaf899c6cb9c377fd0709d9c5",
            "difficulty": 1000000,
            "ipfs_cid": "bafybeihosbewxanqruo7br4va7vul3j3ntnudynczfckohya7yo4mqa3oy",
            "created": int(time.time()),
            "owned_by": "test"
        },
        {
            "model_name": "test-model-2",
            "model_hash": "1" * 64,
            "model_commit": "test_commit",
            "difficulty": 2000000,
            "ipfs_cid": "Qmtest123",
            "created": int(time.time()),
            "owned_by": "test"
        }
    ]
    logger.info(f"Core Node: Returning {len(models_data)} models")
    return web.json_response(models_data)

async def handle_health(request):
    """Health check endpoint"""
    return web.json_response({"status": "healthy"})

async def create_app():
    """Create the mock core node application"""
    app = web.Application()
    
    # Add CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = '*'
        return response
    
    app.middlewares.append(cors_middleware)
    
    # Register routes
    app.router.add_get('/api/v1/models', handle_models)
    app.router.add_get('/health', handle_health)
    
    return app

if __name__ == '__main__':
    logger.info("Starting mock Core Node API on port 8050")
    web.run_app(create_app(), host='0.0.0.0', port=8050)