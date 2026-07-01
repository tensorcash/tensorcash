#!/bin/bash
# SPDX-License-Identifier: Apache-2.0

# Verification API Test Runner
# Rebuilds the Docker container and executes test suite on CUDA-enabled host
#
# Usage:
#   ./run_tests.sh                     # Run all tests with full verification
#   ./run_tests.sh --quick             # Quick mode (skip stats computation)
#   ./run_tests.sh --no-build          # Skip Docker rebuild
#   MODELS_DIR=/path/to/models ./run_tests.sh  # Custom models directory
#   DATA_DIR=/path/to/data ./run_tests.sh      # Custom data directory

# Don't use set -e as it causes issues with pipelines

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
CONTAINER_NAME="verification-api-test"
IMAGE_NAME="verification-api:test"
DOCKERFILE_PATH="services/verification-api/generic.Dockerfile"
CONTEXT_PATH="."
CUDA_VERSION="12.6.0"
CUDA_INDEX="cu126"

# Parse command line arguments
SKIP_BUILD=0
QUICK_MODE=0
for arg in "$@"; do
    case $arg in
        --quick)
            QUICK_MODE=1
            shift
            ;;
        --no-build)
            SKIP_BUILD=1
            shift
            ;;
    esac
done
export QUICK_MODE

# Function to print colored messages
print_status() {
    echo -e "${BLUE}[*]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

# Check if running from correct directory
if [ ! -f "$DOCKERFILE_PATH" ]; then
    print_error "Error: Must run from project root directory (where services/ directory exists)"
    echo "Current directory: $(pwd)"
    echo "Please run from the tensorcash2 root directory"
    exit 1
fi

# Check for NVIDIA Docker runtime
print_status "Checking for NVIDIA Docker runtime..."
if ! sudo docker info 2>/dev/null | grep -q nvidia; then
    print_warning "NVIDIA Docker runtime not detected. Tests require CUDA support."
    print_warning "Install nvidia-docker2 for GPU support or tests may fail."
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    print_success "NVIDIA Docker runtime detected"
fi

# Check for running container and stop it
print_status "Checking for existing container..."
if sudo docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    print_status "Stopping and removing existing container..."
    sudo docker stop "$CONTAINER_NAME" 2>/dev/null || true
    sudo docker rm "$CONTAINER_NAME" 2>/dev/null || true
    print_success "Existing container removed"
fi

# Build the Docker image (unless skipped)
if [ $SKIP_BUILD -eq 0 ]; then
    print_status "Building Docker image..."
    print_status "This may take several minutes on first build..."

    BUILD_ARGS="--build-arg CUDA_VERSION=${CUDA_VERSION} --build-arg CUDA_INDEX=${CUDA_INDEX}"

    if sudo docker build $BUILD_ARGS -t "$IMAGE_NAME" -f "$DOCKERFILE_PATH" "$CONTEXT_PATH"; then
        print_success "Docker image built successfully"
    else
        print_error "Docker build failed"
        exit 1
    fi
else
    print_status "Skipping Docker build (--no-build flag)"
fi

# Create test results directory
RESULTS_DIR="services/verification-api/test_results"
mkdir -p "$RESULTS_DIR"
print_status "Test results will be saved to: $RESULTS_DIR"

# Show volume mount configuration
print_status "Volume mount configuration:"
print_status "  Models: ${MODELS_DIR:-./models} → /models"
print_status "  Data: ${DATA_DIR:-/data} → /data"
if [ $QUICK_MODE -eq 1 ]; then
    print_warning "  Quick mode enabled - stats computation will be skipped"
fi

# Function to run a test in the container
run_test() {
    local test_name=$1
    local test_file=$2
    local output_file="${RESULTS_DIR}/${test_name}_$(date +%Y%m%d_%H%M%S).log"
    
    print_status "Running $test_name..."
    
    # Determine test arguments based on test type
    local test_args=""
    if [[ "$test_file" == "test.py" ]]; then
        # Add quick mode for main test if QUICK_MODE is set
        if [[ "${QUICK_MODE:-0}" == "1" ]]; then
            test_args="--quick"
            print_warning "Running in QUICK MODE (smell test disabled)"
        fi
    fi
    
    # Run test in a fresh container each time with model volumes
    sudo docker run --rm \
            --gpus all \
            -v "$(pwd)/services/verification-api/src/tests:/app/src/tests:ro" \
            -v "$(pwd)/shared-utils:/shared-utils:ro" \
            -v "${MODELS_DIR:-./models}:/models:rw" \
            -v "${DATA_DIR:-./data}:/data:rw" \
            -e CUDA_VISIBLE_DEVICES=0 \
            -e PYTHONUNBUFFERED=1 \
            -e HF_HOME=/models \
            "$IMAGE_NAME" \
            python "/app/src/tests/${test_file}" $test_args 2>&1 | tee "$output_file"
    
    # Check exit status using PIPESTATUS
    local test_result=${PIPESTATUS[0]}
    
    if [ $test_result -eq 0 ]; then
        print_success "$test_name completed successfully"
        echo "Output saved to: $output_file"
        return 0
    else
        print_error "$test_name failed"
        echo "Error log saved to: $output_file"
        return 1
    fi
}

# Verify CUDA availability in container
print_status "Verifying CUDA availability in container..."
sudo docker run --rm \
    --gpus all \
    "$IMAGE_NAME" \
    python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}'); print(f'CUDA device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"None\"}'); print(f'CUDA version: {torch.version.cuda}')" || {
    print_warning "CUDA verification failed - tests may not use GPU acceleration"
}

# Filter out already processed flags from remaining arguments
REMAINING_ARGS=()
for arg in "$@"; do
    if [[ "$arg" != "--quick" && "$arg" != "--no-build" ]]; then
        REMAINING_ARGS+=("$arg")
    fi
done

# Run tests based on remaining arguments
if [ ${#REMAINING_ARGS[@]} -eq 0 ]; then
    # No arguments (or only flags) - run all tests
    print_status "Running all tests..."
    
    # Track test results
    TESTS_PASSED=0
    TESTS_FAILED=0
    
    # Run ChiaVDF test (lightweight, run first)
    if run_test "ChiaVDF Verification" "chia_test.py"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
    
    # Run main proof verification test
    if run_test "Proof Verification" "test.py"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
    
    # Skip benchmark in quick mode
    if [ $QUICK_MODE -eq 0 ]; then
        print_warning "Benchmark test may take several minutes..."
        if run_test "Performance Benchmark" "benchmark.py"; then
            TESTS_PASSED=$((TESTS_PASSED + 1))
        else
            TESTS_FAILED=$((TESTS_FAILED + 1))
        fi
    else
        print_status "Skipping benchmark test in quick mode"
    fi
    
    # Print summary
    echo
    echo "========================================"
    echo "           TEST SUMMARY                 "
    echo "========================================"
    print_success "Tests passed: $TESTS_PASSED"
    if [ $TESTS_FAILED -gt 0 ]; then
        print_error "Tests failed: $TESTS_FAILED"
    fi
    echo "Results saved in: $RESULTS_DIR"
    echo "========================================"
    
else
    # Run specific tests based on arguments
    for arg in "${REMAINING_ARGS[@]}"; do
        case $arg in
            chia)
                run_test "ChiaVDF Verification" "chia_test.py"
                ;;
            proof|main)
                run_test "Proof Verification" "test.py"
                ;;
            benchmark|bench)
                if [ $QUICK_MODE -eq 1 ]; then
                    print_warning "Skipping benchmark in quick mode"
                else
                    print_warning "Benchmark test may take several minutes..."
                    run_test "Performance Benchmark" "benchmark.py"
                fi
                ;;
            shell)
                print_status "Starting interactive shell in container..."
                sudo docker run -it --rm \
                    --gpus all \
                    -v "$(pwd)/services/verification-api/src/tests:/app/src/tests:ro" \
                    -v "$(pwd)/shared-utils:/shared-utils:ro" \
                    -v "${MODELS_DIR:-./models}:/models:rw" \
                    -v "${DATA_DIR:-/data}:/data:rw" \
                    -e CUDA_VISIBLE_DEVICES=0 \
                    -e HF_HOME=/models \
                    "$IMAGE_NAME" \
                    /bin/bash
                ;;
            *)
                print_error "Unknown test: $arg"
                echo "Usage: $0 [--quick] [--no-build] [chia|proof|main|benchmark|bench|shell]"
                echo "Options:"
                echo "  --quick    - Quick mode (skip stats computation)"
                echo "  --no-build - Skip Docker image rebuild"
                echo "Tests:"
                echo "  chia       - Run ChiaVDF verification test"
                echo "  proof/main - Run main proof verification test"
                echo "  benchmark  - Run performance benchmark test"
                echo "  shell      - Start interactive shell in container"
                echo "  (no args)  - Run all tests"
                exit 1
                ;;
        esac
    done
fi

print_success "Test run complete!"