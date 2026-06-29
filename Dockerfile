# kioku-mesh: zenohd + zenoh-backend-rocksdb Docker image
#
# Stage 1: download RocksDB backend plugin from GitHub Releases.
# The official eclipse/zenoh image (Alpine musl) does not bundle the backend;
# we fetch the matching musl-standalone binary and copy it in.
FROM alpine:3.23 AS plugin-downloader
ARG ZENOH_VERSION=1.9.0
ARG ZENOH_TARGET=x86_64-unknown-linux-musl
RUN apk add --no-cache curl unzip \
    && curl -fsSL \
       "https://github.com/eclipse-zenoh/zenoh-backend-rocksdb/releases/download/${ZENOH_VERSION}/zenoh-backend-rocksdb-${ZENOH_VERSION}-${ZENOH_TARGET}-standalone.zip" \
       -o /tmp/rocksdb.zip \
    && unzip /tmp/rocksdb.zip -d /tmp/plugin/

# Stage 2: zenohd with the RocksDB backend plugin installed.
FROM eclipse/zenoh:1.9.0
COPY --from=plugin-downloader /tmp/plugin/libzenoh_backend_rocksdb.so /
