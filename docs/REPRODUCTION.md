# Exact reproduction

Reference runtime: CPython 3.12.14. CI also tests Windows Python 3.14.8. Both run the complete suite and exact report/manifest verification; see the repository Actions workflow.

## Offline source installation

Extract the source archive into a new folder. Create an empty venv with `python -m venv .venv`. All application/test dependencies are built into Python; there is nothing to download or install. Run with that venv interpreter:

```sh
python scripts/check.py
python run.py report --out build/report
python scripts/verify_release.py
```

`run.py` resolves imports relative to its own file, so it also works from a different working directory. The expected reviewed output is in `examples/report/`. `verify_release.py` regenerates the report and checks exact JSON agreement, synthetic fixture bytes, package/config consistency and the release file manifest. No command reads `.git`, runs Git, or requires the original repository/ignore file. Local HTTP checks create real ephemeral loopback sockets; no methods are excluded or skipped.

## Optional Python package installation

Packaging is optional and uses pinned setuptools 84.0.0. In an online build environment install `requirements-build.lock`, then run `python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist`. A prebuilt reviewed wheel can be installed offline with `python -m pip install --no-index --no-deps path/to/wheel.whl`. `bbp-demo report --out report` runs from outside the source tree; fixture/config resources are inside the wheel. Packaging tools do not become runtime requirements.

## Determinism and checks

Integer arithmetic generates the CSV; no wall clock or Git identity appears in the report. UTC timestamps, fixed parameters, config/dataset/source hashes and explicit trial records make differences inspectable. The generated HTML uses a fixed nonce for byte reproducibility; the optional HTTP server generates fresh session token and CSP nonces. API tokens are never printed or persisted in reports.

Reports must be regenerated after source/configuration/data edits. If a manifest or expected-output check fails, inspect the change before updating the reviewed artifacts. Missing/corrupt CSV or unqualified input raises an error; the demo does not silently obtain market data.

If Windows temp-folder policy blocks tests, set TEMP and TMP to a new local folder you own, then rerun the complete suite. Do not omit persistence or HTTP tests. If port 8765 is busy, choose `--port 8766`; the server remains loopback-only. A browser is optional: inspect `report.json` and `report.md` directly.

## Platform runtimes

Use Python 3.14.8 for a downloadable Windows build. The reference Python 3.12.14 may require a platform-specific build; CI uses it on Linux. The package syntax range is broader than these two exact tested targets. Both runtime versions are fixed in the CI matrix.
