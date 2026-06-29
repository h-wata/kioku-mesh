# kioku-mesh: zenohd + zenoh-backend-rocksdb Docker image
#
# Stage 1: download RocksDB backend plugin from GitHub Releases.
# The official eclipse/zenoh image (Alpine musl) does not bundle the backend;
# we fetch the matching musl-standalone binary and copy it in.
FROM alpine:3.23 AS plugin-downloader
ARG ZENOH_VERSION=1.9.0
ARG ZENOH_TARGET=x86_64-unknown-linux-musl
# Default digest is for x86_64-unknown-linux-musl @ v1.9.0.
# For aarch64, pass: --build-arg ZENOH_TARGET=aarch64-unknown-linux-musl
#   --build-arg ROCKSDB_SHA256=298734a4f50dfa12c27337cbafeb7d99949f4f3fda4c330ff6891d39fdd97112
# When upgrading ZENOH_VERSION, update this value: see README "version upgrade" section.
ARG ROCKSDB_SHA256=88b13af5ddaadff9ec55c61765db6344666ae38731257877023b07c59a0c4bd1
RUN apk add --no-cache curl unzip \
    && curl -fsSL \
       "https://github.com/eclipse-zenoh/zenoh-backend-rocksdb/releases/download/${ZENOH_VERSION}/zenoh-backend-rocksdb-${ZENOH_VERSION}-${ZENOH_TARGET}-standalone.zip" \
       -o /tmp/rocksdb.zip \
    && echo "${ROCKSDB_SHA256}  /tmp/rocksdb.zip" | sha256sum -c - \
    && unzip /tmp/rocksdb.zip -d /tmp/plugin/

# Stage 2: zenohd with the RocksDB backend plugin installed.
FROM eclipse/zenoh:1.9.0
COPY --from=plugin-downloader /tmp/plugin/libzenoh_backend_rocksdb.so /
