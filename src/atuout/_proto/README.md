# Vendored atuin daemon protos + generated gRPC stubs

`semantic.proto` and `history.proto` are copied verbatim from the atuin repo at commit
`2f9357e7246dbfb342a8807089e8dac3e60afec9` (originally
`crates/atuin-daemon/proto/{semantic,history}.proto`). This is the same commit devenv builds
atuin from (`devenv.yaml` `atuin-src`) — keep the two in sync. These schemas are
internal/unversioned in atuin; re-verify them on atuin upgrades.

The `*_pb2.py` / `*_pb2_grpc.py` files are **generated** and checked in. Do not edit by hand.
Regenerate with `scripts/gen_proto.sh` (needs the `dev` extra's `grpcio-tools`).
