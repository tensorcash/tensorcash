# SPDX-License-Identifier: Apache-2.0
import pytest
import queue
import time
import threading
from unittest.mock import Mock, patch, MagicMock


def test_broker_start_already_running():
    """Test starting broker when already running (line 39)"""
    from zmq_send_broker import ZmqSendBroker
    
    with patch('zmq_send_broker.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        broker = ZmqSendBroker("tcp://127.0.0.1:5555", max_queue=10)
        
        # Start broker
        broker.start()
        assert broker.running is True
        original_thread = broker.thread
        
        # Try to start again - should return early (line 39)
        broker.start()
        
        # Should still be the same thread and running
        assert broker.thread == original_thread
        assert broker.running is True
        
        broker.stop()


def test_broker_stop_queue_operations():
    """Test stop() queue manipulation (lines 51-60)"""
    from zmq_send_broker import ZmqSendBroker
    
    with patch('zmq_send_broker.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()  
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        broker = ZmqSendBroker("tcp://127.0.0.1:5555", max_queue=2)
        
        # Test the queue full scenario - manually manipulate the queue
        with patch.object(broker, 'q') as mock_queue:
            # Simulate queue.Full on first put_nowait, success on second
            mock_queue.put_nowait.side_effect = [queue.Full(), None]
            mock_queue.get_nowait.return_value = b"removed"
            
            broker.running = True
            broker.stop(timeout=0.1)
            
            # Should have tried to put twice and get once
            assert mock_queue.put_nowait.call_count == 2
            mock_queue.get_nowait.assert_called_once()


def test_broker_stop_empty_queue():
    """Test stop() when get_nowait raises Empty (lines 55-56)"""  
    from zmq_send_broker import ZmqSendBroker
    
    with patch('zmq_send_broker.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        broker = ZmqSendBroker("tcp://127.0.0.1:5555", max_queue=2)
        
        with patch.object(broker, 'q') as mock_queue:
            # First put fails, get raises Empty, second put fails too
            mock_queue.put_nowait.side_effect = [queue.Full(), Exception("put failed")]
            mock_queue.get_nowait.side_effect = queue.Empty()
            
            broker.running = True  
            broker.stop(timeout=0.1)  # Should handle gracefully


def test_broker_thread_join_alive():
    """Test thread join when thread is still alive (lines 71-72)"""
    from zmq_send_broker import ZmqSendBroker
    
    with patch('zmq_send_broker.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        broker = ZmqSendBroker("tcp://127.0.0.1:5555", max_queue=10)
        
        # Mock thread
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        broker.thread = mock_thread
        
        broker.stop(timeout=0.05)
        
        # Should call join with timeout
        mock_thread.join.assert_called_with(timeout=0.05)


def test_broker_cleanup_socket_error():
    """Test socket cleanup error handling (lines 79-80)"""
    from zmq_send_broker import ZmqSendBroker
    
    with patch('zmq_send_broker.zmq') as mock_zmq:
        mock_ctx = Mock() 
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        broker = ZmqSendBroker("tcp://127.0.0.1:5555", max_queue=10)
        broker.socket = mock_socket
        broker.context = mock_ctx
        
        # Make socket.close raise exception
        mock_socket.close.side_effect = Exception("Socket close failed")
        
        # Should handle exception gracefully
        broker.stop()


def test_broker_cleanup_context_error():
    """Test context cleanup error handling (lines 86-87)"""
    from zmq_send_broker import ZmqSendBroker
    
    with patch('zmq_send_broker.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        broker = ZmqSendBroker("tcp://127.0.0.1:5555", max_queue=10)
        broker.socket = mock_socket
        broker.context = mock_ctx
        
        # Make context.term raise exception  
        mock_ctx.term.side_effect = Exception("Context term failed")
        
        # Should handle exception gracefully
        broker.stop()