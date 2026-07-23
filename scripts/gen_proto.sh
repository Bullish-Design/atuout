#!/usr/bin/env bash
# Regenerate the gRPC stubs for the atuin daemon services.
#
# The plain-named .proto copies live in src/atuout/_proto/ (pinned to atuin commit
# 3f08db6b84bd2ff151d9e6560bb057dd55e3bc53). grpcio-tools emits `import <name>_pb2`
# statements that assume the generated modules are importable top-level; we rewrite
# those to package-relative imports so `from atuout._proto import ...` works.
#
# Requires the dev extra: grpcio-tools. On NixOS you may need LD_LIBRARY_PATH set to
# a libstdc++-providing gcc lib dir for grpcio's C extension to import.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
proto_dir="${here}/src/atuout/_proto"

python -m grpc_tools.protoc \
  -I "${proto_dir}" \
  --python_out="${proto_dir}" \
  --grpc_python_out="${proto_dir}" \
  "${proto_dir}/semantic.proto" \
  "${proto_dir}/history.proto"

# Rewrite `import foo_pb2 as ...` -> `from atuout._proto import foo_pb2 as ...`
for f in "${proto_dir}"/*_pb2_grpc.py; do
  sed -i -E 's/^import (semantic_pb2|history_pb2) as /from atuout._proto import \1 as /' "$f"
done

echo "Generated stubs in ${proto_dir}"
