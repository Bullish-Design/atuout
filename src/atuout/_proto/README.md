# Vendored atuin daemon protos + generated gRPC stubs

`semantic.proto` and `history.proto` are copied verbatim from the atuin repo at commit
`3f08db6b84bd2ff151d9e6560bb057dd55e3bc53` (originally
`crates/atuin-daemon/proto/{semantic,history}.proto`). These schemas are internal/unversioned in
atuin — re-verify them on atuin upgrades.

The `*_pb2.py` / `*_pb2_grpc.py` files are **generated** and checked in. Do not edit by hand.
Regenerate with `scripts/gen_proto.sh` (needs the `dev` extra's `grpcio-tools`).
