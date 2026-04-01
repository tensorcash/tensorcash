# SPDX-License-Identifier: Apache-2.0
# Mock functional utilities for testing

def get_config():
    """Mock config function"""
    return {
        'quick_validation_threads': 2,
        'full_validation_threads': 2,
        'model_validation_threads': 1,
    }

def setup_logging():
    """Mock logging setup"""
    pass

def get_device():
    """Mock device getter"""
    return 'cpu'