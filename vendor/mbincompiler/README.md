# Vendored MBINCompiler Runtime Files

This directory contains the minimum managed MBINCompiler files used by the loader's `bundled` dependency mode:

- `MBINCompiler.dll`
- `MBINCompiler.deps.json`
- `MBINCompiler.runtimeconfig.json`
- `libMBIN.dll`

They are included so EXML/MXML conversion works on macOS without relying on the current GitHub release asset shape. The installer can still use `--mbin release`, `--mbin source`, or `--mbin skip` if you prefer a different dependency strategy.
