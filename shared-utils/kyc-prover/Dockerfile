# Build stage
FROM golang:1.21-alpine AS builder

RUN apk add --no-cache git make

WORKDIR /build

# Copy go mod files
COPY go.mod go.sum* ./
RUN go mod download

# Copy source
COPY . .

# Build server
RUN go build -o kyc-prover ./cmd/server

# Runtime stage
FROM alpine:latest

RUN apk add --no-cache ca-certificates

WORKDIR /app

# Copy binary from builder
COPY --from=builder /build/kyc-prover .

# Copy or generate keys
# In production, mount these as volumes
COPY --from=builder /build/*.bin* ./

# Expose API port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1

# Run server
ENTRYPOINT ["./kyc-prover"]
CMD ["-port", "8080", "-pk", "proving_key.bin", "-vk", "verification_key.bin"]
