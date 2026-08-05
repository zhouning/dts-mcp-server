# Test plan — cli-anything-dts-engine

Written before the test code, per the cli-anything SOP. Results are appended
after execution.

## Constraint that shapes this plan

DTS Engine is a licensed Windows GUI product. The E2E tests invoke the real
`EngineWorker.exe` and **fail rather than skip** when it is absent, per the
harness rule against graceful degradation. Unit tests are pure and run anywhere.

`DTS_ENGINE_DIR` must point at the install (or the standard `%APPDATA%\DTS Engine\6.1`
location must exist) for the E2E tier.

## Test inventory plan

| File | Tier | Planned |
|---|---|---|
| `test_core.py` | unit, no Engine needed | ~34 |
| `test_full_e2e.py` | E2E, real Engine required | ~12 |

## Unit test plan

### `core/errors.py`
- `parse_table` on the real shipped `ErrorCode_en.txt`: parses ~90 entries, handles
  blank lines and the trailing separator-less entries.
- `parse_table` on synthetic input: malformed lines skipped, not raised on.
- `band_of`: each band boundary (0, 19, 100, 119, 200, 249, 300, 400, 500, 600),
  plus an out-of-band code returning `"unknown"`.
- `describe`: code 0 is `ok=True`; code 201 maps to band `terrain`; the
  `4294967295` → `-1` normalization for EngineMaster's unsigned return.
- `load_table` with `engine_dir=None` falls back to the enum subset.

### `core/pipelines.py`
- `get` on each of the 8 known pipelines; `KeyError` (listing known names) on a
  bogus name.
- `validate`: missing required flag reported; unknown flag reported; a correct
  flag set returns `[]`; common flags (`inWkt`, `license_server`) accepted on
  every pipeline; injected flags (`handleType`, `udp`, `window`, `title`) never
  reported as unknown.
- `to_flags`: merges `handleType` for pipelines that have one; leaves it absent
  for `terrain` (which is job-file driven).

### `core/job.py`
- `new_job`: applies the C# constructor defaults (`Vector` gets
  `ColorSource=0`, `RoadWidth=0.0`; `Osgb` gets `Collision=False`); `Type`
  ordinal matches `DATA_TYPES`; `KeyError` on an unknown type.
- `validate_job`: unknown field flagged for a type with a transcribed schema;
  returns `[]` for a type whose schema is not transcribed (do not guess).
- `write_job` / `read_job` round-trip; `None` values dropped; UTF-8 with
  non-ASCII preserved; parent directory created.

### `utils/udp_log.py`
- `decode`: GBK bytes, UTF-8 bytes, and undecodable bytes (never raises).
- `parse_line`: a real Engine error line yields level `ERROR`, the timestamp,
  and `retcode=201`; a bare line yields level `INFO` and `retcode=None`.
- `UdpLogListener`: binds an ephemeral port; receives a datagram sent to it;
  `retcode` returns the last RetCode seen; `errors()` filters to ERROR;
  context-manager entry/exit does not leak the socket.

### `utils/engine_backend.py`
- `build_args`: drops `None`; renders booleans lowercase; produces `-key=value`.
- `collect_outputs`: empty list for a missing directory; reports name/size/suffix
  for real files.

### `core/install.py`
- `find_engine_dir` honours an explicit path; raises `EngineNotFound` with the
  tried paths listed when nothing matches.

## E2E test plan

Every case below runs the real `EngineWorker.exe`.

1. **Install discovery** — `describe_install` reports version `6.1.x`,
   `EngineWorker.exe` present, and `gdal305.dll` bundled.
2. **Error table is the shipped one** — the parsed table contains the
   band-boundary codes observed live (201, 302, 6) with non-placeholder text.
3. **`road` publish succeeds** — shapefile + DOM + DEM, exit 0, a `.3dt`
   artifact > 1 MB and a `DataInfor.txt` produced. Artifact paths printed.
4. **`.3dt` output is verified, not assumed** — file exists, size > 1 MB, and is
   not a ZIP/glTF container (Engine's own binary format).
5. **`DataInfor.txt` is the 32-hex encryption key** — matches `^[0-9A-F]{32}$`.
6. **Missing DOM fails with code 201** — the exact failure observed live; the
   result's `error.band` is `terrain`, and the UDP log carries `RetCode:201`.
7. **UDP log is captured on failure** — at least one ERROR record with a
   non-empty body.
8. **Wrong input type for `shp` fails with 302** — a polygon shapefile through
   the vegetation pipeline; confirms `shp` is not a general vector converter.
9. **Strict validation short-circuits** — a missing required flag returns
   `validation_errors` and never launches a process (asserted by absence of
   `exit_code`).
10. **`--no-strict` bypasses validation** — the same call reaches the Engine and
    returns a real exit code.

### CLI subprocess tests (`TestCLISubprocess`)

Run the installed `cli-anything-dts-engine` as an agent would, resolved via
`_resolve_cli`, with no `cwd` set.

11. `--help` exits 0; `--version` reports 1.0.0.
12. `--json info` emits parseable JSON with `ok=true` and an `engine_dir`.
13. `--json pipelines` lists all 8 pipelines and marks `road` verified.
14. `--json errors --code 201` returns band `terrain`.
15. `job new Vector` writes a file whose JSON carries the C# defaults.
16. **Full publish through the installed command** — `run road` with real inputs
    exits 0 and the reported output path exists on disk with size > 1 MB.

## Realistic workflow scenarios

**Workflow A — vector-on-terrain publish.**
Simulates the core production task: taking a projected shapefile plus
orthophoto and elevation rasters and publishing a 3D tile for a viewer.
Operations chained: probe install → inspect pipeline requirements → generate DOM
and DEM from the shapefile's own extent → run `road` → verify the `.3dt`
artifact and its key file. Verified: exit code 0, artifact size, key format,
output count.

**Workflow B — failure diagnosis loop.**
Simulates an agent that hits an error and must self-correct without a human
reading a GUI dialog. Operations chained: run `road` with no DOM → receive exit
201 → resolve the code through the shipped table → add `-domPath` → succeed.
Verified: the first result's band is `terrain`, the second exits 0. This is the
loop the UDP log and error table exist to support.

**Workflow C — pre-flight validation.**
Simulates an agent composing flags it is unsure about. Operations chained:
validate a bad flag set (caught locally, no process spawned) → correct it →
publish. Verified: no `exit_code` key on the rejected call, exit 0 on the
corrected one.

---

# Test results

Executed 2026-08-05 against DTS Engine 6.1.0525.497 on Windows 11, with a
licensed install at `%APPDATA%\DTS Engine\6.1`.

```
test_core.py::test_parse_table_synthetic PASSED [  1%]
test_core.py::test_parse_table_ignores_malformed PASSED [  2%]
test_core.py::test_band_of[0-io] PASSED    [  4%]
test_core.py::test_band_of[19-io] PASSED   [  5%]
test_core.py::test_band_of[100-lod] PASSED [  6%]
test_core.py::test_band_of[119-lod] PASSED [  8%]
test_core.py::test_band_of[200-terrain] PASSED [  9%]
test_core.py::test_band_of[249-terrain] PASSED [ 11%]
test_core.py::test_band_of[302-resource] PASSED [ 12%]
test_core.py::test_band_of[400-crs] PASSED [ 13%]
test_core.py::test_band_of[500-vtpk] PASSED [ 15%]
test_core.py::test_band_of[600-dem_opt] PASSED [ 16%]
test_core.py::test_band_of[65535-unknown] PASSED [ 18%]
test_core.py::test_band_of[99-unknown] PASSED [ 19%]
test_core.py::test_describe_ok PASSED      [ 20%]
test_core.py::test_describe_maps_band_and_message PASSED [ 22%]
test_core.py::test_describe_normalizes_enginemaster_unsigned_minus_one PASSED [ 23%]
test_core.py::test_describe_unmapped_code PASSED [ 25%]
test_core.py::test_load_table_falls_back_without_engine_dir PASSED [ 26%]
test_core.py::test_load_table_falls_back_on_missing_file PASSED [ 27%]
test_core.py::test_all_pipelines_retrievable PASSED [ 29%]
test_core.py::test_get_unknown_lists_known PASSED [ 30%]
test_core.py::test_validate_reports_missing_required PASSED [ 31%]
test_core.py::test_validate_reports_unknown_flag PASSED [ 33%]
test_core.py::test_validate_clean_flagset PASSED [ 34%]
test_core.py::test_validate_accepts_common_flags PASSED [ 36%]
test_core.py::test_validate_accepts_injected_flags PASSED [ 37%]
test_core.py::test_to_flags_injects_handle_type PASSED [ 38%]
test_core.py::test_to_flags_omits_handle_type_for_terrain PASSED [ 40%]
test_core.py::test_road_is_marked_verified PASSED [ 41%]
test_core.py::test_every_pipeline_cites_its_source PASSED [ 43%]
test_core.py::test_new_job_applies_vector_defaults PASSED [ 44%]
test_core.py::test_new_job_applies_osgb_defaults PASSED [ 45%]
test_core.py::test_new_job_unknown_type PASSED [ 47%]
test_core.py::test_new_job_drops_none_fields PASSED [ 48%]
test_core.py::test_validate_job_flags_unknown_field PASSED [ 50%]
test_core.py::test_validate_job_skips_untranscribed_schema PASSED [ 51%]
test_core.py::test_write_read_job_roundtrip PASSED [ 52%]
test_core.py::test_write_job_preserves_non_ascii PASSED [ 54%]
test_core.py::test_write_job_drops_none PASSED [ 55%]
test_core.py::test_data_types_match_csharp_ordinals PASSED [ 56%]
test_core.py::test_decode_gbk PASSED       [ 58%]
test_core.py::test_decode_never_raises PASSED [ 59%]
test_core.py::test_parse_line_real_error_line PASSED [ 61%]
test_core.py::test_parse_line_strips_leading_control_byte PASSED [ 62%]
test_core.py::test_decode_strips_leading_control_byte PASSED [ 63%]
test_core.py::test_parse_line_plain PASSED [ 65%]
test_core.py::test_listener_receives_datagram PASSED [ 66%]
test_core.py::test_build_args_renders_flags PASSED [ 68%]
test_core.py::test_build_args_drops_none PASSED [ 69%]
test_core.py::test_build_args_lowercases_booleans PASSED [ 70%]
test_core.py::test_collect_outputs_missing_dir PASSED [ 72%]
test_core.py::test_collect_outputs_reports_files PASSED [ 73%]
test_core.py::test_find_engine_dir_honours_explicit PASSED [ 75%]
test_core.py::test_find_engine_dir_raises_with_tried_paths PASSED [ 76%]
test_full_e2e.py::test_install_discovered PASSED [ 77%]
test_full_e2e.py::test_shipped_error_table_is_used PASSED [ 79%]
test_full_e2e.py::test_road_publish_succeeds PASSED [ 80%]
test_full_e2e.py::test_3dt_artifact_verified PASSED [ 81%]
test_full_e2e.py::test_datainfor_is_encryption_key PASSED [ 83%]
test_full_e2e.py::test_output_totals_reported PASSED [ 84%]
test_full_e2e.py::test_missing_dom_fails_with_201 PASSED [ 86%]
test_full_e2e.py::test_udp_log_captured_on_failure PASSED [ 87%]
test_full_e2e.py::test_shp_pipeline_rejects_polygons PASSED [ 88%]
test_full_e2e.py::test_strict_validation_does_not_launch PASSED [ 90%]
test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 91%]
test_full_e2e.py::TestCLISubprocess::test_version PASSED [ 93%]
test_full_e2e.py::TestCLISubprocess::test_info_json PASSED [ 94%]
test_full_e2e.py::TestCLISubprocess::test_pipelines_json PASSED [ 95%]
test_full_e2e.py::TestCLISubprocess::test_errors_json PASSED [ 97%]
test_full_e2e.py::TestCLISubprocess::test_job_new PASSED [ 98%]
test_full_e2e.py::TestCLISubprocess::test_full_publish_through_installed_cli PASSED [100%]
============================= 72 passed in 37.05s =============================
```

## Summary statistics

| Metric | Value |
|---|---|
| Total tests | 72 |
| Passed | 72 (100%) |
| Failed | 0 |
| Unit tests (`test_core.py`) | 55 |
| E2E tests (`test_full_e2e.py`) | 17, of which 7 are CLI-subprocess |
| Execution time | ~37-49 s (dominated by real Engine runs) |
| Planned vs actual | 34 unit planned / 55 written; 12 E2E planned / 17 written |

## What the E2E tier actually proved

- A real `.3dt` tile was produced: **2,477,786 bytes**, plus the 32-hex
  `DataInfor.txt` key file. Verified by size, by not being a ZIP, and by not
  carrying a `glTF` magic — it is Engine's own binary container.
- The `road` pipeline's undocumented `-domPath` requirement was confirmed
  live: omitting it yields exit **201** in band `terrain`.
- `-handleType=shp` was confirmed **not** to be a general vector converter:
  a polygon shapefile yields exit **302** in band `resource`.
- The shipped ~90-code `ErrorCode_en.txt` table is parsed and used, not the
  9-entry enum fallback.

## Bug found by the test suite

`test_udp_log_captured_on_failure` failed on the first run and exposed a real
defect: Engine prefixes each UDP datagram with a `0x01` control byte, so the
`[ERROR]` prefix never matched and every line was mis-classified as `INFO`.
The earlier throwaway probe scripts missed this because they only `rstrip()`ed.
Fixed in `utils/udp_log.py` (`_LEAD_CONTROL`), with two regression tests
pinning the real captured frame format.

## Coverage notes

Gaps, stated plainly rather than papered over:

- **Only `road` is verified end-to-end.** `osgbLod`, `multosgb`, `max`, `vtpk`,
  `waterTerrain`, `Library`, `shp` (success path), and `terrain` have correct
  flag names transcribed from the decompiled shell, but no successful run.
  They are marked `verified=False` in `core/pipelines.py` and the CLI reports
  that status. Verifying them needs real input data this machine does not have
  (OSGB datasets, .max scenes, .vtpk packages, DTM job descriptions).
- **The two-stage OSGB path is untested against real data.** `publish_osgb()`
  models the shell's two-process sequence, but only the sequencing logic is
  exercised, not a real OSGB publish.
- **`.aconf` job-file mode is not verified end-to-end.** The schema is
  transcribed from `Config*.cs` and round-trips, but no job file has been fed to
  the worker via `-jsonPath` and confirmed to drive a publish. Only `Vector` and
  `Osgb` field lists are transcribed; the other 8 data types deliberately return
  no validation errors rather than guess.
- **No round-trip through the GUI.** The generated `.3dt` has not been opened in
  DTS Explorer, because Explorer will not start on this machine (GPU-related,
  per the user). Artifact verification is therefore structural, not visual.
- **Licence-server flags are untested.** `-license_server`/`-license_port` pass
  through unvalidated; this install uses a local CodeMeter container.

---

# Addendum — standalone repository

This project was extracted into a standalone repository (`dts-mcp-server`) with
an MCP server layer added on top of the same core. Two test files joined the
suite:

| File | Tier | Tests |
|---|---|---|
| `tests/test_mcp_server.py` | MCP tool registry and tool functions | 18 |
| `tests/test_stdio_handshake.py` | real stdio subprocess, JSON-RPC | 3 |

**Total: 93 passed.**

`test_stdio_handshake.py` launches `python -m dts_mcp_server` as an MCP client
would and drives a genuine handshake — initialize, `tools/list`, `tools/call` —
so a packaging or entry-point regression cannot pass unnoticed.

## Two further bugs the tests caught

1. **`dts_explain_error` conflated two meanings of `ok`.** The tool merged
   `describe()`'s output, whose `ok` means "this code indicates success", into a
   response envelope whose `ok` means "the call worked". Looking up a failure
   code therefore reported the tool call itself as failed — actively misleading
   to an agent. Split into `ok` and `indicates_success`, with a test pinning the
   distinction.

2. **The stdio test harness closed stdin too early.** Writing all requests at
   once and closing stdin made the server hit EOF and shut down mid-conversation;
   `tools/list` passed only by timing luck. Now each request is written and its
   reply read before the next is sent. This was a defect in the test, not the
   server, but it was masking whether `tools/call` worked at all over a real
   transport.

## Portability fix

The E2E fixture previously copied a shapefile out of an installed `libpysal`
examples directory, which exists only on the original development machine. It
now generates a 36-parcel UTM 17N shapefile and matching DOM/DEM rasters from
scratch, so a fresh clone can run the E2E tier anywhere DTS Engine is installed.
