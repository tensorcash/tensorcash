FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    tar \
    curl \
    procps \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Install IPFS with architecture detection
ARG IPFS_VERSION=v0.25.0
ARG TARGETARCH
RUN case ${TARGETARCH} in \
        "amd64")  ARCH="linux-amd64"  ;; \
        "arm64")  ARCH="linux-arm64"  ;; \
        *)        ARCH="linux-amd64"  ;; \
    esac && \
    wget -qO /tmp/ipfs.tar.gz "https://dist.ipfs.io/kubo/${IPFS_VERSION}/kubo_${IPFS_VERSION}_${ARCH}.tar.gz" && \
    tar -xzf /tmp/ipfs.tar.gz -C /tmp && \
    mv /tmp/kubo/ipfs /usr/local/bin/ipfs && \
    chmod +x /usr/local/bin/ipfs && \
    rm -rf /tmp/ipfs.tar.gz /tmp/kubo

# Create IPFS user and directories
RUN useradd -m -s /bin/bash ipfs && \
    mkdir -p /data/ipfs /models && \
    chown -R ipfs:ipfs /data/ipfs /models

# Set up Python environment
WORKDIR /app
COPY shared-utils/ipfs/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY shared-utils/ipfs/hf_to_ipfs.py /app/
COPY shared-utils/ipfs/entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# Switch to ipfs user
# USER ipfs
ENV IPFS_PATH=/data/ipfs
ENV HF_HOME=/models

EXPOSE 4001 5001 8080

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["pin-and-serve"]