# liboqs Submodule

[liboqs](https://github.com/open-quantum-safe/liboqs) provides the ML-DSA
(FIPS 204) implementation used for post-quantum signature verification in the
TensorCash core node (Taproot v2 witnesses). It is vendored as a git submodule
at `shared-utils/liboqs`.

## Checking Out the Submodule

The submodule is registered in `.gitmodules`:

```ini
[submodule "shared-utils/liboqs"]
	path = shared-utils/liboqs
	url = https://github.com/open-quantum-safe/liboqs.git
```

When cloning the repository, pull the submodule contents at the same time:

```bash
git clone --recurse-submodules https://github.com/tensorcash/tensorcash.git
```

If the repository was cloned without `--recurse-submodules`, initialize and
fetch it afterward:

```bash
git submodule update --init --recursive
```

This populates `shared-utils/liboqs` at the commit pinned by the parent
repository. To verify the working tree is populated:

```bash
ls shared-utils/liboqs/CMakeLists.txt
```

## Build Integration

The test-runner image (`services/core-node/bcore/test-runner/Dockerfile`) builds
liboqs as a minimal static library before compiling the node:

1. Copies `shared-utils/liboqs` to `/build/bcore/src/external/liboqs`.
2. Configures with CMake for a minimal static build — only the ML-DSA
   signature algorithms are compiled (`ml_dsa_44`, `ml_dsa_65`, `ml_dsa_87`),
   OpenSSL is disabled, and constant-time testing is enabled:

   ```
   -DBUILD_SHARED_LIBS=OFF
   -DOQS_USE_OPENSSL=OFF
   -DOQS_BUILD_ONLY_LIB=ON
   -DOQS_MINIMAL_BUILD="SIG_ml_dsa_44;SIG_ml_dsa_65;SIG_ml_dsa_87"
   -DOQS_ENABLE_TEST_CONSTANT_TIME=ON
   ```

3. Runs `make install` into `/usr/local`, producing the static archive
   `liboqs.a` and the `oqs/oqs.h` headers, then `ldconfig`.

The node's own CMake (`src/crypto/CMakeLists.txt`) discovers the installed
library with `find_library(NAMES oqs)` and `find_path(NAMES oqs/oqs.h)`. When
both are found it links liboqs into the `bitcoin_crypto` target and defines
`USE_LIBOQS=1`; when they are not found, ML-DSA verification is compiled out
with a warning. The verification entry point is `crypto/mldsaverify.cpp`, which
selects `OQS_SIG_alg_ml_dsa_44/65/87` per signature type and verifies via
`OQS_SIG_verify`.

## Why liboqs

- **Mature:** long-running, actively maintained Open Quantum Safe project.
- **Audited:** subject to multiple security audits and peer review.
- **Portable:** x86_64, ARM64, and RISC-V support.
- **Constant-time:** timing-attack resistant on supported platforms (the build
  enables `OQS_ENABLE_TEST_CONSTANT_TIME`).
- **Minimal build:** can compile only ML-DSA, excluding KEMs and other
  signature schemes, keeping the linked artifact small.

## Alternative Backend: mldsa-native

`crypto/mldsaverify.cpp` also supports the CBMC-verified C90
[mldsa-native](https://github.com/pq-code-package/mldsa-native) reference
implementation as a compile-time alternative. To use it instead of liboqs,
vendor that project and configure CMake with `-DUSE_MLDSA_NATIVE=ON`; the
verifier source guards its liboqs and mldsa-native paths behind `USE_LIBOQS`
and `USE_MLDSA_NATIVE` respectively, defaulting to `USE_LIBOQS` when neither is
specified.
