# Makefile for PoW utilities test suite

CXX = g++
# Allow overriding CXXFLAGS/LDFLAGS from environment or make command line
CXXFLAGS ?= -std=c++17 -Wall -Wextra -O2 -g -Wno-deprecated-declarations
LDFLAGS ?= -lssl -lcrypto -lzmq

# v3 admission (TIP-0003): `make ARGON2=1` compiles pow_v3 with
# libargon2 (needs libargon2-dev); default builds the runtime_error fallback
# so the dev targets stay dependency-free (v2 paths unaffected).
ifeq ($(ARGON2),1)
POW_V3_CXXFLAGS := -DPOW_V3_HAVE_ARGON2
LDFLAGS += -largon2
else
POW_V3_CXXFLAGS :=
endif

# Source files
SOURCES := pow_utils.cpp pow_v3.cpp bcred_table_r1024.cpp pow_zmq_writer.cpp pow_test.cpp compile_test.cpp debug_messages.cpp
HEADERS := pow_utils.h pow_v3.h bcred_table_r1024.h pow_zmq_writer.h

# Common objects needed by all targets that use pow_utils symbols
COMMON_OBJS := pow_utils.o pow_v3.o bcred_table_r1024.o pow_zmq_writer.o
TEST_OBJ := pow_test.o
COMPILE_TEST_OBJ := compile_test.o
DEBUG_OBJ := debug_messages.o

# Target executables
TARGET = pow_test
COMPILE_TEST = compile_test
DEBUG_TEST = debug_messages

# Default target
all: $(TARGET)

# Build the test executable
$(TARGET): $(COMMON_OBJS) $(TEST_OBJ)
	$(CXX) $(COMMON_OBJS) $(TEST_OBJ) -o $(TARGET) $(LDFLAGS)

# Build the compilation test
$(COMPILE_TEST): $(COMMON_OBJS) $(COMPILE_TEST_OBJ)
	$(CXX) $(COMMON_OBJS) $(COMPILE_TEST_OBJ) -o $(COMPILE_TEST) $(LDFLAGS)

# Build the debug test
$(DEBUG_TEST): $(COMMON_OBJS) $(DEBUG_OBJ)
	$(CXX) $(COMMON_OBJS) $(DEBUG_OBJ) -o $(DEBUG_TEST) $(LDFLAGS)

# Compile source files
pow_utils.o: pow_utils.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) -c pow_utils.cpp -o pow_utils.o

pow_v3.o: pow_v3.cpp pow_v3.h bcred_table_r1024.h
	$(CXX) $(CXXFLAGS) $(POW_V3_CXXFLAGS) -c pow_v3.cpp -o pow_v3.o

bcred_table_r1024.o: bcred_table_r1024.cpp bcred_table_r1024.h
	$(CXX) $(CXXFLAGS) -c bcred_table_r1024.cpp -o bcred_table_r1024.o

pow_zmq_writer.o: pow_zmq_writer.cpp pow_zmq_writer.h $(HEADERS)
	$(CXX) $(CXXFLAGS) -c pow_zmq_writer.cpp -o pow_zmq_writer.o

pow_test.o: pow_test.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) -c pow_test.cpp -o pow_test.o

compile_test.o: compile_test.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) -c compile_test.cpp -o compile_test.o

debug_messages.o: debug_messages.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) -c debug_messages.cpp -o debug_messages.o

# Quick compilation check
check: $(COMPILE_TEST)
	@echo "Running compilation test..."
	./$(COMPILE_TEST)

# Debug message construction
debug: $(DEBUG_TEST)
	@echo "Running C++ debug..."
	./$(DEBUG_TEST)
	@echo "\nRunning Python debug..."
	python3 debug_messages.py

# Run tests
test: $(TARGET)
	@echo "Running C++ tests..."
	./$(TARGET)
	@echo "\nRunning Python tests..."
	python3 pow_python_test.py
	@echo "\nComparing results..."
	@echo "Check that the hex outputs match between C++ and Python!"

# Clean build artifacts
clean:
	rm -f $(OBJECTS) $(COMPILE_TEST_OBJ) $(DEBUG_OBJ) $(TARGET) $(COMPILE_TEST) $(DEBUG_TEST) test_vectors.json

# Phony targets
.PHONY: all test clean check debug
