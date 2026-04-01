# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for enhanced_logger.py
"""

import pytest
import logging
import json
import pickle
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime

# Import the module under test
try:
    from enhanced_logger import (
        VerificationLogger,
        FailureData,
        create_logger,
        setup_logging
    )
except ImportError:
    # If direct import fails, try with sys.path manipulation
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))
    from enhanced_logger import (
        VerificationLogger,
        FailureData,
        create_logger,
        setup_logging
    )


class TestFailureData:
    """Test FailureData dataclass"""
    
    def test_failure_data_creation(self):
        """Test FailureData can be created with all fields"""
        timestamp = "2024-01-01T12:00:00"
        failure_data = FailureData(
            timestamp=timestamp,
            failure_type="test_failure",
            hash_id="abc123",
            error_message="Test error",
            verification_step="step1",
            model_identifier="model1",
            proof_data={"key": "value"},
            metrics={"accuracy": 0.95},
            stack_trace="Test stack trace",
            charts_data={"chart1": {"values": [1, 2, 3]}}
        )
        
        assert failure_data.timestamp == timestamp
        assert failure_data.failure_type == "test_failure"
        assert failure_data.hash_id == "abc123"
        assert failure_data.error_message == "Test error"
        assert failure_data.verification_step == "step1"
        assert failure_data.model_identifier == "model1"
        assert failure_data.proof_data == {"key": "value"}
        assert failure_data.metrics == {"accuracy": 0.95}
        assert failure_data.stack_trace == "Test stack trace"
        assert failure_data.charts_data == {"chart1": {"values": [1, 2, 3]}}
    
    def test_failure_data_optional_fields(self):
        """Test FailureData with minimal required fields"""
        failure_data = FailureData(
            timestamp="2024-01-01T12:00:00",
            failure_type="test",
            hash_id=None,
            error_message="Error",
            verification_step=None,
            model_identifier=None,
            proof_data=None,
            metrics=None,
            stack_trace=None,
            charts_data=None
        )
        
        assert failure_data.hash_id is None
        assert failure_data.verification_step is None
        assert failure_data.model_identifier is None
        assert failure_data.proof_data is None
        assert failure_data.metrics is None
        assert failure_data.stack_trace is None
        assert failure_data.charts_data is None


class TestVerificationLogger:
    """Test VerificationLogger class"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.reports_dir = Path(self.temp_dir) / "test_reports"
    
    def teardown_method(self):
        """Clean up test fixtures"""
        if hasattr(self, 'temp_dir'):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_logger_initialization(self):
        """Test logger can be initialized with default settings"""
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir)
        )
        
        assert logger.name == "test_logger"
        assert logger.reports_dir == self.reports_dir
        assert self.reports_dir.exists()
        assert logger.failure_data == []
        assert logger.current_context == {}
        assert logger.logger is not None
    
    def test_logger_initialization_with_log_file(self):
        """Test logger initialization with log file"""
        log_file = str(Path(self.temp_dir) / "test.log")
        logger = VerificationLogger(
            name="test_logger",
            log_file=log_file,
            reports_dir=str(self.reports_dir)
        )
        
        assert logger.logger is not None
        # Check that log file handler was added
        file_handlers = [h for h in logger.logger.handlers 
                        if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) > 0
    
    def test_basic_logging_methods(self):
        """Test basic logging methods"""
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir),
            console_output=False  # Disable console for testing
        )
        
        # These should not raise exceptions
        logger.debug("Debug message")
        logger.info("Info message")
        logger.warning("Warning message")
    
    @patch('enhanced_logger.datetime')
    @patch('enhanced_logger.traceback.format_exc')
    def test_error_logging_with_failure_data(self, mock_traceback, mock_datetime):
        """Test error logging creates failure data"""
        mock_datetime.now.return_value.isoformat.return_value = "2024-01-01T12:00:00"
        mock_traceback.return_value = "Mock stack trace"

        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir),
            console_output=False
        )

        # Mock the report generation to avoid file I/O
        with patch.object(logger, '_generate_failure_report') as mock_generate:
            mock_generate.return_value = "mock/report/path"

            # Call logger.error inside an exception context so
            # sys.exc_info() returns a real exception and stack_trace
            # gets captured via traceback.format_exc()
            try:
                raise ValueError("synthetic")
            except ValueError:
                logger.error(
                    "Test error message",
                    failure_type="test_failure",
                    hash_id="test_hash",
                    proof_data={"test": "data"},
                    metrics={"accuracy": 0.8}
                )

        assert len(logger.failure_data) == 1
        failure = logger.failure_data[0]
        assert failure.timestamp == "2024-01-01T12:00:00"
        assert failure.failure_type == "test_failure"
        assert failure.hash_id == "test_hash"
        assert failure.error_message == "Test error message"
        assert failure.proof_data == {"test": "data"}
        assert failure.metrics == {"accuracy": 0.8}
        assert failure.stack_trace == "Mock stack trace"
    
    def test_verification_context_manager(self):
        """Test verification context manager"""
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir)
        )
        
        assert logger.current_context == {}
        
        with logger.verification_context(step="test_step", model_identifier="test_model"):
            assert logger.current_context == {
                "step": "test_step",
                "model_identifier": "test_model"
            }
        
        # Context should be restored after exiting
        assert logger.current_context == {}
    
    def test_nested_context_managers(self):
        """Test nested context managers work correctly"""
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir)
        )
        
        with logger.verification_context(step="outer"):
            assert logger.current_context == {"step": "outer"}
            
            with logger.verification_context(step="inner", model="test"):
                assert logger.current_context == {
                    "step": "inner",
                    "model": "test"
                }
            
            # Should restore outer context
            assert logger.current_context == {"step": "outer"}
    
    @patch('enhanced_logger.datetime')
    def test_generate_failure_report_creates_files(self, mock_datetime):
        """Test that failure report generation creates expected files"""
        mock_datetime.now.return_value.strftime.return_value = "20240101_120000"
        
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir)
        )
        
        failure_data = FailureData(
            timestamp="2024-01-01T12:00:00",
            failure_type="test failure",
            hash_id="test_hash",
            error_message="Test error",
            verification_step="test_step",
            model_identifier="test_model",
            proof_data={"key": "value"},
            metrics={"accuracy": 0.95},
            stack_trace="Test stack trace",
            charts_data=None
        )
        
        with patch.object(logger, '_generate_charts') as mock_charts:
            report_path = logger._generate_failure_report(failure_data)
        
        report_dir = Path(report_path)
        assert report_dir.exists()
        assert (report_dir / "failure_data.json").exists()
        assert (report_dir / "failure_data.pkl").exists()
        assert (report_dir / "summary.txt").exists()
        
        # Verify JSON content
        with open(report_dir / "failure_data.json") as f:
            json_data = json.load(f)
            assert json_data["failure_type"] == "test failure"
            assert json_data["hash_id"] == "test_hash"
        
        # Verify pickle content
        with open(report_dir / "failure_data.pkl", "rb") as f:
            pkl_data = pickle.load(f)
            assert isinstance(pkl_data, FailureData)
            assert pkl_data.failure_type == "test failure"
    
    def test_get_failure_summary(self):
        """Test failure summary generation"""
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir)
        )
        
        # Initially no failures
        summary = logger.get_failure_summary()
        assert summary["total_failures"] == 0
        assert summary["failure_types"] == {}
        assert summary["latest_failure"] is None
        
        # Add some failure data manually
        failure1 = FailureData(
            timestamp="2024-01-01T12:00:00",
            failure_type="type1",
            hash_id=None,
            error_message="Error 1",
            verification_step=None,
            model_identifier=None,
            proof_data=None,
            metrics=None,
            stack_trace=None,
            charts_data=None
        )
        failure2 = FailureData(
            timestamp="2024-01-01T13:00:00",
            failure_type="type1",
            hash_id=None,
            error_message="Error 2",
            verification_step=None,
            model_identifier=None,
            proof_data=None,
            metrics=None,
            stack_trace=None,
            charts_data=None
        )
        failure3 = FailureData(
            timestamp="2024-01-01T14:00:00",
            failure_type="type2",
            hash_id=None,
            error_message="Error 3",
            verification_step=None,
            model_identifier=None,
            proof_data=None,
            metrics=None,
            stack_trace=None,
            charts_data=None
        )
        
        logger.failure_data = [failure1, failure2, failure3]
        
        summary = logger.get_failure_summary()
        assert summary["total_failures"] == 3
        assert summary["failure_types"]["type1"] == 2
        assert summary["failure_types"]["type2"] == 1
        assert summary["latest_failure"] == "2024-01-01T14:00:00"
    
    def test_clear_failure_data(self):
        """Test clearing failure data"""
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir)
        )
        
        # Add some failure data
        logger.failure_data = [MagicMock(), MagicMock()]
        assert len(logger.failure_data) == 2
        
        logger.clear_failure_data()
        assert len(logger.failure_data) == 0


class TestFactoryFunctions:
    """Test factory functions"""
    
    def test_create_logger(self):
        """Test create_logger factory function"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = create_logger(
                name="factory_test",
                log_level="DEBUG",
                reports_dir=temp_dir
            )
            
            assert isinstance(logger, VerificationLogger)
            assert logger.name == "factory_test"
            assert logger.logger.level == logging.DEBUG
    
    def test_create_logger_with_int_log_level(self):
        """Test create_logger with integer log level"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = create_logger(
                name="factory_test",
                log_level=logging.WARNING,
                reports_dir=temp_dir
            )
            
            assert logger.logger.level == logging.WARNING
    
    def test_setup_logging(self):
        """Test setup_logging convenience function"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = setup_logging(
                log_level="INFO",
                reports_dir=temp_dir
            )
            
            assert isinstance(logger, VerificationLogger)
            assert logger.name == "verification"
            assert logger.logger.level == logging.INFO


class TestChartGeneration:
    """Test chart generation functionality"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.reports_dir = Path(self.temp_dir) / "test_reports"
    
    def teardown_method(self):
        """Clean up test fixtures"""
        if hasattr(self, 'temp_dir'):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @patch('enhanced_logger.plt')
    def test_generate_charts_histogram(self, mock_plt):
        """Test histogram chart generation"""
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir)
        )
        
        charts_data = {
            "test_histogram": {
                "type": "histogram",
                "values": [1, 2, 3, 4, 5],
                "bins": 10,
                "xlabel": "Value",
                "ylabel": "Density"
            }
        }
        
        report_dir = self.reports_dir / "test_report"
        report_dir.mkdir(parents=True)
        
        logger._generate_charts(charts_data, report_dir)
        
        # Verify matplotlib was called appropriately
        mock_plt.figure.assert_called()
        mock_plt.hist.assert_called()
        mock_plt.title.assert_called_with("test_histogram Distribution")
        mock_plt.xlabel.assert_called_with("Value")
        mock_plt.ylabel.assert_called_with("Density")
    
    @patch('enhanced_logger.plt')
    def test_generate_charts_with_error(self, mock_plt):
        """Test chart generation handles errors gracefully"""
        mock_plt.figure.side_effect = Exception("Test error")
        
        logger = VerificationLogger(
            name="test_logger",
            reports_dir=str(self.reports_dir),
            console_output=False
        )
        
        charts_data = {"test_chart": {"values": [1, 2, 3]}}
        report_dir = self.reports_dir / "test_report"
        report_dir.mkdir(parents=True)
        
        # Should not raise exception
        logger._generate_charts(charts_data, report_dir)


class TestLoggerEdgeCases:
    """Test edge cases and error conditions"""
    
    def test_logger_with_no_console_output(self):
        """Test logger with console output disabled"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = VerificationLogger(
                name="no_console",
                console_output=False,
                reports_dir=temp_dir
            )
            
            # Should have no console handlers
            console_handlers = [h for h in logger.logger.handlers 
                              if isinstance(h, logging.StreamHandler) and 
                              not isinstance(h, logging.FileHandler)]
            assert len(console_handlers) == 0
    
    def test_critical_logging_maps_to_error(self):
        """Test that critical logging creates failure data"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = VerificationLogger(
                name="critical_test",
                reports_dir=temp_dir,
                console_output=False
            )
            
            with patch.object(logger, '_generate_failure_report') as mock_generate:
                mock_generate.return_value = "mock/path"
                logger.critical("Critical error occurred")
            
            assert len(logger.failure_data) == 1
            assert logger.failure_data[0].failure_type == "critical_error"