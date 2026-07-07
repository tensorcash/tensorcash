# SPDX-License-Identifier: Apache-2.0
"""
Enhanced logging and reporting utilities for ProofVerifier and AsyncValidator
"""

import logging
import sys
import os
import json
import pickle
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
import matplotlib.pyplot as plt
import numpy as np
import traceback
from dataclasses import dataclass, asdict
from contextlib import contextmanager


@dataclass
class FailureData:
    """Data structure for capturing failure information"""
    timestamp: str
    failure_type: str
    hash_id: Optional[str]
    error_message: str
    verification_step: Optional[str]
    model_identifier: Optional[str]
    proof_data: Optional[Dict[str, Any]]
    metrics: Optional[Dict[str, Any]]
    stack_trace: Optional[str]
    charts_data: Optional[Dict[str, Any]]


class VerificationLogger:
    """
    Enhanced logger for verification processes with failure data collection
    """
    
    def __init__(self, 
                 name: str = "verification",
                 log_level: int = logging.INFO,
                 log_file: Optional[str] = None,
                 reports_dir: str = "verification_reports",
                 console_output: bool = True):
        """
        Initialize the verification logger
        
        Args:
            name: Logger name
            log_level: Logging level
            log_file: Optional log file path
            reports_dir: Directory for failure reports
            console_output: Whether to output to console
        """
        self.name = name
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True,exist_ok=True)
        
        # Failure data collection
        self.failure_data: List[FailureData] = []
        self.current_context: Dict[str, Any] = {}
        
        # Setup logger
        self.logger = self._setup_logger(name, log_level, log_file, console_output)
        
    def _setup_logger(self, name: str, level: int, log_file: Optional[str], console: bool) -> logging.Logger:
        """Setup the actual logger instance"""
        logger = logging.getLogger(name)
        logger.setLevel(level)
        
        logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)
        logging.getLogger('matplotlib.pyplot').setLevel(logging.WARNING)
                
        # Clear any existing handlers
        logger.handlers.clear()
        
        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s'
        )
        
        # Console handler
        if console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        # File handler
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        
        # Prevent propagation to avoid duplicate logs
        logger.propagate = False
        
        return logger
    
    @contextmanager
    def verification_context(self, **context_data):
        """Context manager for tracking verification context"""
        old_context = self.current_context.copy()
        self.current_context.update(context_data)
        try:
            yield
        finally:
            self.current_context = old_context
    
    def debug(self, message: str, **kwargs):
        """Debug level logging"""
        self.logger.debug(message, **kwargs)
    
    def info(self, message: str, **kwargs):
        """Info level logging"""
        self.logger.info(message, **kwargs)
    
    def warning(self, message: str, **kwargs):
        """Warning level logging"""
        self.logger.warning(message, **kwargs)
    
    def error(self, message: str, 
              failure_type: str = "verification_error",
              hash_id: Optional[str] = None,
              proof_data: Optional[Dict[str, Any]] = None,
              metrics: Optional[Dict[str, Any]] = None,
              charts_data: Optional[Dict[str, Any]] = None,
              **kwargs):
        """
        Error level logging with failure data collection
        
        Args:
            message: Error message
            failure_type: Type of failure (e.g., 'block_sanity', 'sequence_verification')
            hash_id: Hash ID if available
            proof_data: Relevant proof data
            metrics: Numerical metrics related to failure
            charts_data: Data for generating charts
        """
        self.logger.error(message, **kwargs)
        
        # Collect failure data
        ctx = self.current_context or {}
        # Prefer explicit params; fall back to context values for richer reports
        resolved_hash_id = hash_id or ctx.get('hash_id') or ctx.get('block_hash')
        resolved_step = ctx.get('step') or ctx.get('verification_type') or ctx.get('phase')
        resolved_model = ctx.get('model_identifier') or ctx.get('model')

        # Only capture stack trace if within an active exception context
        has_exc = any(sys.exc_info())

        failure_data = FailureData(
            timestamp=datetime.now().isoformat(),
            failure_type=failure_type,
            hash_id=resolved_hash_id,
            error_message=message,
            verification_step=resolved_step,
            model_identifier=resolved_model,
            proof_data=proof_data,
            metrics=metrics,
            stack_trace=traceback.format_exc() if has_exc else None,
            charts_data=charts_data
        )
        
        self.failure_data.append(failure_data)
        
        # Generate report for this failure
        report_path = self._generate_failure_report(failure_data)
        self.logger.error(f"Failure report generated: {report_path}")
    
    def critical(self, message: str, **kwargs):
        """Critical level logging - always treated as failure"""
        self.error(message, failure_type="critical_error", **kwargs)
    
    def _generate_failure_report(self, failure_data: FailureData) -> str:
        """Generate a detailed failure report"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_failure_type = failure_data.failure_type.replace(" ", "_").replace("/", "_")
        
        report_name = f"failure_{safe_failure_type}_{timestamp}"
        report_dir = self.reports_dir / report_name
        suffix = 1
        while report_dir.exists():
            suffix += 1
            report_dir = self.reports_dir / f"{report_name}_{suffix}"
        report_dir.mkdir(exist_ok=True)
        
        # Save failure data as JSON
        json_path = report_dir / "failure_data.json"
        with open(json_path, 'w') as f:
            # Convert to dict and handle non-serializable objects
            data_dict = asdict(failure_data)
            json.dump(data_dict, f, indent=2, default=str)
        
        # Save full failure data as pickle for complete recovery
        pickle_path = report_dir / "failure_data.pkl"
        with open(pickle_path, 'wb') as f:
            pickle.dump(failure_data, f)
        
        # Generate charts if chart data is available
        if failure_data.charts_data:
            self._generate_charts(failure_data.charts_data, report_dir)
        
        # Generate summary report
        self._generate_summary_report(failure_data, report_dir)
        
        return str(report_dir)
    
    def _generate_charts(self, charts_data: Dict[str, Any], report_dir: Path):
        """Generate charts from failure data"""
        try:
            for chart_name, data in charts_data.items():
                if isinstance(data, dict) and 'values' in data:
                    plt.figure(figsize=(10, 6))
                    
                    values = np.array(data['values'])
                    chart_type = data.get('type', 'histogram')
                    
                    if chart_type == 'histogram':
                        bins = data.get('bins', 30)
                        
                        # Plot main distribution
                        plt.hist(values, bins=bins, alpha=0.7, density=True, 
                                label='Actual', color='blue', edgecolor='black', linewidth=0.5)
                        
                        # Plot reference distribution if provided
                        if 'values_ref' in data:
                            values_ref = np.array(data['values_ref'])
                            plt.hist(values_ref, bins=bins, alpha=0.5, density=True,
                                    label='Reference', color='green', edgecolor='darkgreen', linewidth=0.5)
                        
                        plt.title(f"{chart_name} Distribution")
                        plt.xlabel(data.get('xlabel', 'Value'))
                        plt.ylabel('Density')
                        
                        # Add legend if we have reference data or thresholds
                        if 'values_ref' in data or 'thresholds' in data:
                            plt.legend()
                    
                    elif chart_type == 'line':
                        plt.plot(values, label='Actual', color='blue', linewidth=2)
                        
                        # Plot reference line if provided
                        if 'values_ref' in data:
                            values_ref = np.array(data['values_ref'])
                            plt.plot(values_ref, label='Reference', color='green', 
                                    linewidth=2, linestyle='--', alpha=0.8)
                        
                        plt.title(f"{chart_name} Over Time")
                        plt.xlabel(data.get('xlabel', 'Index'))
                        plt.ylabel(data.get('ylabel', 'Value'))
                        
                        # Add legend if we have reference data or thresholds
                        if 'values_ref' in data or 'thresholds' in data:
                            plt.legend()
                    
                    elif chart_type == 'scatter':
                        x_values = data.get('x_values', range(len(values)))
                        plt.scatter(x_values, values, alpha=0.6, label='Actual', color='blue')
                        
                        # Plot reference scatter if provided
                        if 'values_ref' in data:
                            values_ref = np.array(data['values_ref'])
                            x_values_ref = data.get('x_values_ref', x_values)
                            plt.scatter(x_values_ref, values_ref, alpha=0.6, 
                                      label='Reference', color='green', marker='^')
                        
                        plt.title(f"{chart_name} Scatter Plot")
                        plt.xlabel(data.get('xlabel', 'X'))
                        plt.ylabel(data.get('ylabel', 'Y'))
                        
                        # Add legend if we have reference data or thresholds
                        if 'values_ref' in data or 'thresholds' in data:
                            plt.legend()
                    
                    # Add threshold lines if provided
                    if 'thresholds' in data:
                        for threshold_name, threshold_value in data['thresholds'].items():
                            plt.axhline(y=threshold_value, color='red', linestyle='--', 
                                      label=f'{threshold_name}: {threshold_value:.4f}')
                        plt.legend()
                    
                    plt.grid(True, alpha=0.3)
                    plt.tight_layout()
                    
                    chart_path = report_dir / f"{chart_name}.png"
                    plt.savefig(chart_path, dpi=150, bbox_inches='tight')
                    plt.close()
                    
        except Exception as e:
            self.logger.error(f"Failed to generate charts: {e}")
    
    def _generate_summary_report(self, failure_data: FailureData, report_dir: Path):
        """Generate a human-readable summary report"""
        summary_path = report_dir / "summary.txt"
        
        with open(summary_path, 'w') as f:
            f.write("VERIFICATION FAILURE REPORT\n")
            f.write("=" * 50 + "\n\n")
            
            f.write(f"Timestamp: {failure_data.timestamp}\n")
            f.write(f"Failure Type: {failure_data.failure_type}\n")
            f.write(f"Hash ID: {failure_data.hash_id or 'N/A'}\n")
            f.write(f"Model: {failure_data.model_identifier or 'N/A'}\n")
            f.write(f"Verification Step: {failure_data.verification_step or 'N/A'}\n\n")
            
            f.write("ERROR MESSAGE:\n")
            f.write("-" * 20 + "\n")
            f.write(f"{failure_data.error_message}\n\n")
            
            if failure_data.metrics:
                f.write("METRICS:\n")
                f.write("-" * 20 + "\n")
                for key, value in failure_data.metrics.items():
                    f.write(f"{key}: {value}\n")
                f.write("\n")
            
            if failure_data.charts_data:
                f.write("CHARTS GENERATED:\n")
                f.write("-" * 20 + "\n")
                for chart_name in failure_data.charts_data.keys():
                    f.write(f"- {chart_name}.png\n")
                f.write("\n")
            
            if failure_data.stack_trace:
                f.write("STACK TRACE:\n")
                f.write("-" * 20 + "\n")
                f.write(failure_data.stack_trace)
    
    def get_failure_summary(self) -> Dict[str, Any]:
        """Get summary of all failures"""
        failure_types = {}
        for failure in self.failure_data:
            failure_types[failure.failure_type] = failure_types.get(failure.failure_type, 0) + 1
        
        return {
            'total_failures': len(self.failure_data),
            'failure_types': failure_types,
            'latest_failure': self.failure_data[-1].timestamp if self.failure_data else None,
            'reports_directory': str(self.reports_dir)
        }
    
    def clear_failure_data(self):
        """Clear collected failure data"""
        self.failure_data.clear()


def create_logger(name: str = "verification",
                 log_level: Union[int, str] = logging.INFO,
                 log_file: Optional[str] = None,
                 reports_dir: str = "verification_reports",
                 console_output: bool = True) -> VerificationLogger:
    """
    Factory function to create a VerificationLogger
    
    Args:
        name: Logger name
        log_level: Logging level (can be int or string like 'DEBUG', 'INFO')
        log_file: Optional log file path
        reports_dir: Directory for failure reports
        console_output: Whether to output to console
    
    Returns:
        VerificationLogger instance
    """
    # Convert string log level to int if needed
    if isinstance(log_level, str):
        log_level = getattr(logging, log_level.upper())
    
    return VerificationLogger(
        name=name,
        log_level=log_level,
        log_file=log_file,
        reports_dir=reports_dir,
        console_output=console_output
    )


# Convenience function for backward compatibility
def setup_logging(log_level: Union[int, str] = logging.INFO,
                 log_file: Optional[str] = None,
                 reports_dir: str = "verification_reports") -> VerificationLogger:
    """
    Setup logging with failure reporting capabilities
    
    Returns:
        VerificationLogger instance
    """
    return create_logger(
        name="verification",
        log_level=log_level,
        log_file=log_file,
        reports_dir=reports_dir,
        console_output=True
    )
