# SPDX-License-Identifier: Apache-2.0
import pytest
import time
import threading
from unittest.mock import Mock, patch, MagicMock
from utils.proof import ValidationRequest, ResponseValue


def test_enqueue_error_path_missing_hash_id_extended():
    """Test additional error paths in enqueue_request"""
    import main as m
    
    with patch('main.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        with patch('main.ZmqSendBroker') as mock_broker_class:
            mock_broker = Mock()
            mock_broker_class.return_value = mock_broker
            
            validator = m.AsyncValidator(pull_port=6000, push_host="127.0.0.1", push_port=7000)
            
            # Test with completely malformed message
            invalid_message = b"not_a_flatbuffer"
            
            # This should handle gracefully without crashing
            validator.enqueue_request(invalid_message)


def test_dependency_tracking_simple():
    """Test basic dependency tracking without complex scenarios"""
    import main as m
    
    with patch('main.zmq') as mock_zmq:
        mock_ctx = Mock()  
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        with patch('main.ZmqSendBroker') as mock_broker_class:
            mock_broker = Mock()
            mock_broker_class.return_value = mock_broker
            
            validator = m.AsyncValidator(pull_port=6000, push_host="127.0.0.1", push_port=7000)
            
            # Test basic functionality without triggering complex dependency logic  
            hash_id = b"\x01" * 32  # Exactly 32 bytes
            assert len(hash_id) == 32
            
            # Test basic set_phase_result 
            validator.set_phase_result(hash_id, 'quick', ResponseValue.ResponseValue.Quick_OK, None)


def test_validation_status_edge_cases():
    """Test validation status tracking edge cases"""  
    import main as m
    
    with patch('main.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        with patch('main.ZmqSendBroker') as mock_broker_class:
            mock_broker = Mock()
            mock_broker_class.return_value = mock_broker
            
            validator = m.AsyncValidator(pull_port=6000, push_host="127.0.0.1", push_port=7000)
            
            hash_id = b"\x02" * 32
            
            # Test is_already_processed
            result = validator.is_already_processed(hash_id)
            assert result is False
            
            # Set a status and test again
            with validator.status_lock:
                st = validator.validation_status.setdefault(hash_id, {})
                st['full'] = ResponseValue.ResponseValue.Full_Green
                
            result = validator.is_already_processed(hash_id)
            assert result is True


def test_send_error_response_different_kinds():
    """Test send_error_response with different error kinds"""
    import main as m
    
    with patch('main.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        with patch('main.ZmqSendBroker') as mock_broker_class:
            mock_broker = Mock()
            mock_broker_class.return_value = mock_broker
            
            validator = m.AsyncValidator(pull_port=6000, push_host="127.0.0.1", push_port=7000)
            
            hash_id = b"\x02" * 32
            
            # Test different error kinds
            validator.send_error_response(hash_id, kind='full')
            validator.send_error_response(hash_id, kind='model') 
            validator.send_error_response(hash_id, kind='quick')


def test_wait_for_quick_validation_timeout():
    """Test wait_for_quick_validation timeout scenarios"""
    import main as m
    
    with patch('main.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        with patch('main.ZmqSendBroker') as mock_broker_class:
            mock_broker = Mock()
            mock_broker_class.return_value = mock_broker
            
            validator = m.AsyncValidator(pull_port=6000, push_host="127.0.0.1", push_port=7000)
            
            hash_id = b"\x02" * 32
            
            # Test timeout (should return False)
            result = validator.wait_for_quick_validation(hash_id, timeout=0.01)
            assert result is False
            
            # Test with existing quick result
            with validator.status_lock:
                st = validator.validation_status.setdefault(hash_id, {})
                st['quick'] = ResponseValue.ResponseValue.Quick_OK
                
            result = validator.wait_for_quick_validation(hash_id, timeout=0.01)
            assert result is True


def test_signal_and_event_management():
    """Test event signaling and cleanup"""
    import main as m
    
    with patch('main.zmq') as mock_zmq:
        mock_ctx = Mock()
        mock_socket = Mock()
        mock_ctx.socket.return_value = mock_socket
        mock_zmq.Context.return_value = mock_ctx
        
        with patch('main.ZmqSendBroker') as mock_broker_class:
            mock_broker = Mock()
            mock_broker_class.return_value = mock_broker
            
            validator = m.AsyncValidator(pull_port=6000, push_host="127.0.0.1", push_port=7000)
            
            hash_id = b"\x02" * 32
            
            # Test signal creation and cleanup
            validator._signal_validation_complete(hash_id)
            validator._clear_event(hash_id)
            
            # Test multiple signals
            validator._signal_validation_complete(hash_id)
            validator._signal_validation_complete(hash_id)  # Should handle duplicate
            
            validator._clear_event(hash_id)