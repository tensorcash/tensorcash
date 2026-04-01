"""Mock VLLM server for E2E testing"""
import asyncio
import json
import time
import uuid
from aiohttp import web
import logging
import random

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Store received requests for verification
received_requests = []

async def handle_completions(request):
    """Mock /v1/completions endpoint"""
    try:
        data = await request.json()
        received_requests.append(data)
        
        # Log received PoW data
        if "extra_sampling_params" in data and "pow" in data["extra_sampling_params"]:
            pow_data = data["extra_sampling_params"]["pow"]
            logger.info(f"Received PoW data: block_hash={pow_data.get('block_hash', 'none')[:16]}...")
        
        # Simulate processing time
        await asyncio.sleep(random.uniform(0.1, 0.3))
        
        # Generate mock response
        completion_id = f"cmpl-{uuid.uuid4().hex[:8]}"
        
        # Check if streaming is requested
        if data.get("stream", False):
            # Return streaming response
            response = web.StreamResponse(
                status=200,
                headers={
                    'Content-Type': 'text/event-stream',
                    'Cache-Control': 'no-cache'
                }
            )
            response.enable_chunked_encoding()
            await response.prepare(request)
            
            # Send mock chunks
            for i in range(3):
                chunk = {
                    "id": completion_id,
                    "object": "text_completion",
                    "created": int(time.time()),
                    "choices": [{
                        "text": f" chunk{i}",
                        "index": 0,
                        "finish_reason": None if i < 2 else "stop"
                    }]
                }
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                await asyncio.sleep(0.1)
            
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        else:
            # Return regular response
            response_data = {
                "id": completion_id,
                "object": "text_completion",
                "created": int(time.time()),
                "model": data.get("model", "mock-model"),
                "choices": [{
                    "text": f"Mock response to: {data.get('prompt', 'no prompt')[:50]}",
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30
                }
            }
            
            # Include proof data if present (for testing proof collection)
            if "extra_sampling_params" in data and "pow" in data["extra_sampling_params"]:
                response_data["proof"] = {
                    "completion_id": completion_id,
                    "block_hash": data["extra_sampling_params"]["pow"]["block_hash"],
                    "timestamp": int(time.time()),
                    "data": "mock_proof_data_base64"
                }
            
            return web.json_response(response_data)
            
    except Exception as e:
        logger.error(f"Error handling completion: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_chat_completions(request):
    """Mock /v1/chat/completions endpoint"""
    try:
        data = await request.json()
        received_requests.append(data)
        
        # Convert chat format to completion format for processing
        messages = data.get("messages", [])
        last_message = messages[-1]["content"] if messages else "no message"
        
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        
        if data.get("stream", False):
            # Streaming chat response
            response = web.StreamResponse(
                status=200,
                headers={
                    'Content-Type': 'text/event-stream',
                    'Cache-Control': 'no-cache'
                }
            )
            response.enable_chunked_encoding()
            await response.prepare(request)
            
            for i in range(3):
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": data.get("model", "mock-model"),
                    "choices": [{
                        "index": 0,
                        "delta": {"content": f" response_chunk_{i}"},
                        "finish_reason": None if i < 2 else "stop"
                    }]
                }
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                await asyncio.sleep(0.1)
            
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        else:
            # Regular chat response
            response_data = {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": data.get("model", "mock-model"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Mock response to: {last_message[:50]}"
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 25,
                    "total_tokens": 40
                }
            }
            return web.json_response(response_data)
            
    except Exception as e:
        logger.error(f"Error handling chat completion: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_models(request):
    """Mock /v1/models endpoint"""
    models_data = {
        "object": "list",
        "data": [
            {
                "id": "Qwen/Qwen3-8B",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "test",
                "model_hash": "0" * 64,
                "model_commit": "9c925d64d72725edaf899c6cb9c377fd0709d9c5",
                "difficulty": 1000000,
                "ipfs_cid": "bafybeihosbewxanqruo7br4va7vul3j3ntnudynczfckohya7yo4mqa3oy"
            },
            {
                "id": "test-model-2",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "test",
                "model_hash": "1" * 64,
                "model_commit": "test_commit",
                "difficulty": 2000000,
                "ipfs_cid": "Qmtest123"
            }
        ]
    }
    return web.json_response(models_data)

async def handle_status(request):
    """Status endpoint for mock server"""
    return web.json_response({
        "status": "healthy",
        "requests_received": len(received_requests),
        "timestamp": int(time.time())
    })

async def handle_get_requests(request):
    """Debug endpoint to retrieve received requests"""
    return web.json_response({
        "requests": received_requests[-10:],  # Last 10 requests
        "total": len(received_requests)
    })

async def create_app():
    """Create the mock server application"""
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
    app.router.add_post('/v1/completions', handle_completions)
    app.router.add_post('/v1/chat/completions', handle_chat_completions)
    app.router.add_get('/v1/models', handle_models)
    app.router.add_get('/status', handle_status)
    app.router.add_get('/debug/requests', handle_get_requests)
    
    return app

if __name__ == '__main__':
    import asyncio
    
    logger.info("Starting mock VLLM server on port 8000")
    web.run_app(create_app(), host='0.0.0.0', port=8000)