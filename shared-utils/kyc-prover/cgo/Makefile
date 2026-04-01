# Makefile for building CGO shared library
# Builds libzkprover.so (Linux), libzkprover.dylib (macOS), or zkprover.dll (Windows)

GOCMD=go
GOBUILD=$(GOCMD) build
GOCLEAN=$(GOCMD) clean

# Output library name
LIB_NAME=zkprover

# Platform-specific settings
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Linux)
    LIB_EXT=so
    LIB_PREFIX=lib
    BUILD_FLAGS=-buildmode=c-shared
endif
ifeq ($(UNAME_S),Darwin)
    LIB_EXT=dylib
    LIB_PREFIX=lib
    BUILD_FLAGS=-buildmode=c-shared
endif
ifeq ($(OS),Windows_NT)
    LIB_EXT=dll
    LIB_PREFIX=
    BUILD_FLAGS=-buildmode=c-shared
endif

OUTPUT=$(LIB_PREFIX)$(LIB_NAME).$(LIB_EXT)

.PHONY: all clean install

all: $(OUTPUT)

$(OUTPUT): bridge.go
	@echo "Building CGO shared library: $(OUTPUT)"
	$(GOBUILD) $(BUILD_FLAGS) -o $(OUTPUT) bridge.go
	@echo "Build complete: $(OUTPUT)"
	@echo ""
	@echo "To use in C++:"
	@echo "  #include \"zkprover.h\""
	@echo "  // Link with: -L. -lzkprover"

clean:
	$(GOCLEAN)
	rm -f $(OUTPUT) zkprover.h.tmp

# Install to system library directory (optional)
install: $(OUTPUT)
	@echo "Installing $(OUTPUT) to /usr/local/lib"
	sudo cp $(OUTPUT) /usr/local/lib/
	@echo "Installing zkprover.h to /usr/local/include"
	sudo cp zkprover.h /usr/local/include/
	@echo "Installation complete"
	@echo ""
	@echo "Run 'sudo ldconfig' (Linux) or update DYLD_LIBRARY_PATH (macOS) to use system-wide"

# Test the library with a simple C program
test: $(OUTPUT)
	@echo "Building test program..."
	gcc -o test_bridge test_bridge.c -L. -l$(LIB_NAME) -Wl,-rpath,.
	@echo "Run ./test_bridge to test the library"
