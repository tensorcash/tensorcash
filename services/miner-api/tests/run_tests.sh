#!/bin/bash
# Main test runner for miner-api tests

set -e

# Configuration
COMPOSE_FILE="docker-compose.test.yml"
PYTHON_CMD=${PYTHON_CMD:-python3}
ACTION=${1:-help}

# Always use sudo for docker (adjust as needed for your system)
DOCKER_CMD="sudo docker"
COMPOSE_CMD="sudo docker-compose"

case $ACTION in
    all)
        echo "Starting services..."
        $COMPOSE_CMD -f $COMPOSE_FILE down 2>/dev/null || true
        $COMPOSE_CMD -f $COMPOSE_FILE up -d
        
        echo "Waiting for services (30s)..."
        sleep 30
        
        echo "Running unit tests..."
        cd ..
        $PYTHON_CMD -m pytest tests/unit -v --tb=short
        cd tests
        
        echo "Running E2E tests..."
        $PYTHON_CMD -m pytest e2e_tests.py -v --tb=short
        
        echo "Stopping services..."
        $COMPOSE_CMD -f $COMPOSE_FILE down
        ;;
        
    unit)
        echo "Running unit tests..."
        cd ..
        $PYTHON_CMD -m pytest tests/unit -v --tb=short
        ;;
        
    e2e)
        echo "Starting services..."
        $COMPOSE_CMD -f $COMPOSE_FILE down 2>/dev/null || true
        $COMPOSE_CMD -f $COMPOSE_FILE up -d
        
        echo "Waiting for services (30s)..."
        sleep 30
        
        echo "Running E2E tests..."
        $PYTHON_CMD -m pytest e2e_tests.py -v --tb=short
        
        echo "Stopping services..."
        $COMPOSE_CMD -f $COMPOSE_FILE down
        ;;
        
    start)
        echo "Starting services..."
        $COMPOSE_CMD -f $COMPOSE_FILE down 2>/dev/null || true
        $COMPOSE_CMD -f $COMPOSE_FILE up -d
        echo "Services started. Use './run_tests.sh stop' to stop them."
        ;;
        
    stop)
        echo "Stopping services..."
        $COMPOSE_CMD -f $COMPOSE_FILE down
        ;;
        
    help|*)
        echo "Usage: ./run_tests.sh [command]"
        echo ""
        echo "Commands:"
        echo "  all   - Run all tests (unit + E2E)"
        echo "  unit  - Run unit tests only"
        echo "  e2e   - Run E2E tests only"
        echo "  start - Start services only"
        echo "  stop  - Stop services"
        echo "  help  - Show this help"
        ;;
esac