# ApexTrader Modularization/Refactor Plan (2026-04-06)

## Stage 1: Remove all `import *` usage
- Replace all `from ... import *` with explicit imports in all modules and `__init__.py` files.
- Update all references to use explicit names as needed.

## Stage 2: Flatten/remove unnecessary wrapper modules
- Identify and remove modules that only re-export or wrap other modules (e.g., `engine/scan.py`, `engine/options_executor.py`).
- Update all imports to use the canonical module path.

## Stage 3: Centralize shared utilities
- Move all general-purpose helpers to `engine/utils/`.
- Refactor duplicated or scattered utility functions/classes.

## Stage 4: Refactor submodule `__init__.py`
- Ensure each submodule (`equity`, `options`, `ti`, `execution`, `notifications`, `broker`) exposes only its intended public API in `__init__.py`.
- Remove any `import *` and unnecessary re-exports.

## Stage 5: Refactor cross-module dependencies
- Update all cross-module imports to be explicit and avoid circular dependencies.
- Use local imports where necessary to break cycles.

## Stage 6: Test and validate
- Run all main scripts and tests to ensure the refactored codebase works as expected.
- Fix any runtime or import errors.

---

**Each stage will be committed separately for traceability.**
