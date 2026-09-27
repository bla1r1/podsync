# calcHashAB.wasm

Not part of podsync. Vendored, unmodified, from:

- Project: https://github.com/dstaley/hashab
- Release: https://github.com/dstaley/hashab/releases/tag/2025-01-04
- File: `calcHashAB.wasm`
- SHA-256: `b565e73a7f08939ed097946ca179a2d6eb08087994ba35191578d3e8b5af79f0`
- License: The Unlicense (public domain)

A clean-room reimplementation of the HASHAB signature (iPod nano 6G/7G,
mhbd offset 0xAB) as a WebAssembly module, run through `wasmtime` by
`podsync.itdb.writer.signing.ab`. See that module for the calling
convention (20-byte SHA-1 input, 8-byte UUID input, 57-byte output).
