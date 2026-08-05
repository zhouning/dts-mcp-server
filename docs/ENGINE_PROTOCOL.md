# DTS Engine — reverse-engineered automation contract

Status: **protocol recovered by static analysis. No live run yet.**

Supersedes the "DTS Engine: no SDK, no scripting, undocumented CLI — skip"
verdict in `README.md`. That verdict was wrong on the first two counts and
incomplete on the third: there is no SDK, but there *is* a stable, fully
recoverable command-line contract with 93 flags, a JSON job-file mode, a UDP
progress channel, and a documented exit-code enum.

Version analysed: `6.1.0525.497`, install tree at
`%APPDATA%\DTS Engine\6.1`.

## How this was recovered

`Engine.exe` (5.4 MB) is a **managed .NET WinForms assembly** — `Engine,
Version=1.0.0.0` — and decompiles cleanly with `ilspycmd`. It is only a GUI
shell. All work happens in `EngineWorker.exe` (87 MB, native C++), which the
shell launches via `Process.Start` with a composed argument string.

That makes the shell the specification: every flag, every default, and the
JSON job schema are readable C#. Decompiled source is under `_decompiled/`
(gitignored — regenerate with the command in "Reproducing" below).

String-scanning `EngineWorker.exe` directly does **not** work; its literals are
packed. All 115 binaries in the tree fail a plain `grep` for known flag names
like `shpColorFieldName`. Go through the managed shell instead.

## Invocation

Two modes, both `Process.Start(EngineWorker.exe, args)`:

```
EngineWorker.exe -jsonPath="<job.aconf>"                       # job-file mode
EngineWorker.exe -handleType=<type> -inpath=… -outpath=… …      # direct-flag mode
```

Job files are `.aconf` — plain JSON, `Newtonsoft.Json`, `Formatting.Indented`,
`NullValueHandling.Ignore` (see `JSON.cs`). The shell writes one per data type
and passes its path.

Templates lifted verbatim from `FormMain.cs`:

| Pipeline | Command |
|---|---|
| Shapefile | `-inpath="…" -outpath="…" -xshpoffset=… -yshpoffset=… -handleType=shp` |
| OSGB LOD | `-handleType=osgbLod -inpath="…" -outpath="…" -xshpoffset=… -yshpoffset=…` |
| Road | `-handleType=road -roadShp="…" -xshpoffset=… -yshpoffset=… -outpath="…" -roadRGB={r},{g},{b} -roadSize=…` |
| VTPK | `-handleType=vtpk -roadShp="…" -jsonPath="…" -styleJsonPath="…" -outpath="…"` |
| Terrain | `-dtmjsonpath="…" -demPath="…" -domPath="…"` |
| CRS transform | `-inWkt="…" -outWkt="…"` |

Publishing OSGB is **two sequential processes**, not one: tiling first, then
pyramid/LOD. `checkBoxAsyncPyramid` decides whether the second starts on the
first's `Exited` event or after a fixed `Thread.Sleep(2000)`. A harness must
model both stages — the job is not done when the first process exits.

## Exit codes

`ProcessErrorCode` in the shell covers only the common prefix:

```
0 OK  1 InvalidParam  2 TypeNotSupport  3 CreatePak
4 ReadFile  5 FileContent  6 InitGDALReader  7 FileNotFound  255 Undefined
```

`ErrorCode_en.txt` in the install root is the full table and is far richer —
~90 codes in functional bands. Parse that file rather than hardcoding the enum;
it is the authoritative, shipped, English-language error contract:

| Band | Meaning |
|---|---|
| 0–14 | parameters, file I/O, 3DT publishing, GDAL init |
| 100–117 | LOD creation, mesh reduction, texture atlas, voxelization, tree build |
| 200–245 | terrain publish, DEM/DOM registration, road & river overlay, incremental publish |
| 300–302 | resource (vegetation) build |
| 400–405 | coordinate-system resolution and transformation |
| 500–502 | VTPK |
| 600 | DEM optimization |
| 65535 | undefined |

## Progress channel

The shell binds a **UDP socket** (`FormMain.cs:1096`, default port `9527`, via
`NetUtils.GetAPort`) and passes it as `-udp=<port>`. Every pipeline appends this
flag — 16 call sites. The worker streams log lines back over UDP; this is how a
harness gets progress without screen-scraping.

Two more endpoints appear in the shell's string table, used for the
server/cluster publishing path rather than local runs:
`http://{host}:{port}/upload` and `ws://{host}:{port}/status`.

A failed-file list is written to disk during OSGB publishing and read back on
process exit (`strFailedFile`), so per-tile failures are recoverable
programmatically.

## Licensing

```
-license_server=<host> -license_port=<port>       # default port 80
```

`StartupCmdLineParam.ParseCmdLineParam` accepts `licenseServer=host:port` on the
shell's own command line and forwards it to every worker invocation. Local runs
otherwise depend on the CodeMeter container, same as Explorer. Untested whether
Engine hits the same entitlement wall that keeps `DTS Explorer.exe` from
starting (exit code 3 — see `README.md`).

## Flag inventory (93)

From the UTF-16 string table of `Engine.exe`, cross-checked against the
decompiled call sites.

**I/O and job control** — `-inpath -outpath -jsonPath -dtmjsonpath
-styleJsonPath -handleType -filter -process -stream -udp -window -title
-packAllFile -allowScanDir -importHideData -data -xml -tree`

**LOD / HLOD** — `-hlod -hlodOctree -hlodMergeLeaf -hlodMergeDistance
-hlodMaxDrawDistance -hlodDistanceScale -reductionHLOD -reductionTileNode
-viewDetailLevel -mindrawresolution -cutBoxRate -gridLength -maxGridLength`

**Mesh and material** — `-mergeTexture -minMipmapSize -recalculateNormal
-forceCullFace -RemoveAdhesion -removeTooFar -instance -forceInstanceCheck
-glassMaterialConvert -unlightMaterial -specialMaterial -alphaThreshold
-castshadow -collision -apartModel -replaceWithCustomAssets -supportOSGB
-useNewImporter`

**Terrain, water, coast** — `-demPath -domPath -dtmOverlapMeter
-dtmInnerDownMeter -dtmInnerEmgenceMeter -coastDemPath -coastLength
-coastMeterWaterHeight -seaPath -seaDomPath -water -fillvalue -elevationField`

**Vector / road** — `-roadShp -roadRGB -roadSize -shpOidFieldName
-getShpColorField -useCustomOID -xshpoffset -yshpoffset -colorType`

**CRS** — `-inWkt -outWkt -projid -srsOriginX -srsOriginY -srsOriginZ -unit
-northType -originType`

**BIM references** — `-includeRefFile -includeCadFile -includeDGNRefFile`

**Database and licensing** — `-useDatabase -dbType -license_server
-license_port -useGeneralEncryptKey -author -title -viewname -Type
-Disposition -handleType`

Note `-shpColorFieldName` is referenced by error code 241 but did not appear in
the shell's table; it is likely worker-only. Treat the error-code file as a
second source of flag names.

## JSON job schema

One `Config` subclass per `DataType`, all inheriting:

```csharp
class Config {
  DataType Type;                          // enum below
  long? TimeStamp;
  string OverrideKeyInfo;
  string AdditionalParam;                 // raw flags appended to the command
  IncrementStatus IncrementImporterStatus;
}
```

`DataType` — `Terrain, Osgb, MAX, BIM, LAS, Material, Vector, Sea, Skeletal,
DTOModeData`. Corresponding classes: `ConfigTerrain, ConfigOsgb, ConfigMax,
ConfigBIM, ConfigLas, ConfigMaterial, ConfigVector, ConfigSea, ConfigSkeletal,
ConfigDTOModeData`.

`AdditionalParam` is an escape hatch: arbitrary flags pass straight through to
the worker, so a harness is never boxed in by the schema.

Two examples, field-for-field:

```csharp
class ConfigOsgb : Config {
  string InPath, OutPath, OuterShp, InnerShp, FileName;
  string OffsetX, OffsetY;
  string SRSOriginX, SRSOriginY, SRSOriginZ, InWKT, OutWKT;
  bool   ShowWindow, AsyncPyramid, Collision = false,
         CullBack, ReductionTileNode = false;
  int    ProcessCount;
}

class ConfigVector : Config {
  string RoadShpPath, OuterShp, InnerShp, OffsetX, OffsetY;
  string ShpFileRelateVTPKPath = "";
  ColorSourceType ColorSource = Custom;   // Custom | ColorFiled
  Color  RoadColor = Red;
  double RoadWidth = 0.0;
  string OutputPath, FileName;
  bool   ShowWindow;
  VectorAdvanceParam AdvanceParam;        // DOM, DEM, ElevationFill, CustomElevation
}
```

Global settings live at
`%APPDATA%\EngineSettings\v6.1\EngineGlobalSettings.aconf` — currently a single
`UsePrivateKey` bool, surfaced as `-useGeneralEncryptKey=<!UsePrivateKey>`.

## What Engine does that GDAL does not

`gdal305.dll` is **GDAL 3.0.5**, and `data/` is an unmodified GDAL data
directory (`grib2_*.csv`, `*.gfs`, `pci_datum.txt`, `osmconf.ini`,
`gdalmdiminfo_output.schema.json`). GDAL's role is the outer ring only: vector
and raster reading, and coordinate transformation — error bands `6` and
`400–405`, nothing else.

Everything Engine is actually sold for sits outside GDAL, and the error bands
name it: mesh LOD and simplification, texture atlas re-baking, voxelization,
scene-tree construction, 3DT tile publishing, DEM/DOM co-registration,
incremental republish. GDAL has no equivalent for any of it.

Also embedded, none of it GDAL: `ifcengine.dll` (IFC), `obmxsdk.dll` (OSGB),
`SketchUpAPI.dll`, `AirCity.RhinoCore.dll`, `AirCity.DGNExporter*.dll` with a
Bentley MicroStation `standards.cfg`, `e57-reader.dll` / `laz-reader.dll`
(point clouds), `PhysX3*.dll` (collision), `nvtt_64.dll` (texture
compression), `NCSEcw.dll` (ECW/JP2), `FileGDBAPI.dll` (Esri File
Geodatabase), plus per-year exporter plugins for Revit 2016–2021, Navisworks
2021–2024, AutoCAD, ArchiCAD, SolidWorks and 3ds Max.

So "skip Engine, use GDAL directly" only holds for 2D format conversion and
reprojection. For 3D tile production it is not a substitute.

## Consequences for a cli-anything harness

Engine is a **better** wrapping target than Explorer, not a worse one:

- Headless by construction — `ProcessWindowStyle.Hidden`, no GUI needed by the
  worker. Explorer needs a live Unreal viewer with a project loaded.
- Stateless per invocation — each run is one job in, files out. Explorer is
  stateful against a loaded scene.
- Machine-readable results — exit code plus a shipped ~90-entry error table,
  plus the on-disk failed-file list. Ideal for `TEST.md` assertions.
- Progress without scraping — UDP log stream on `-udp`.
- Runs anywhere the licence reaches, so it can be a normal stdio MCP server
  spawned as a subprocess, exactly like `arcgis-mcp` and `qgis-mcp`. Explorer
  cannot: its port is compiled in and bound to loopback, forcing HTTP/SSE.

Remaining unknowns, all requiring a live run:

1. Whether Engine starts at all under the current licence, or hits Explorer's
   exit-code-3 wall.
2. Exact semantics of the `-handleType` values beyond the six templates above.
3. Whether the `.aconf` field names are what the worker actually reads, or
   whether the shell renames on serialization. `JSON.Stringify` uses default
   contract resolution and no `[JsonProperty]` attributes appear on the Config
   classes, so field names should pass through verbatim — but confirm.
4. UDP log line format.

## Reproducing

```powershell
dotnet tool install --global ilspycmd
& "$env:USERPROFILE\.dotnet\tools\ilspycmd.exe" `
    "$env:APPDATA\DTS Engine\6.1\Engine.exe" -o .\_decompiled\Engine -p
```

Then read `AirCityEngine/FormMain.cs` (the argument composition, ~8700 lines),
`AirCityEngine/Config*.cs` (the job schema), and
`AirCityEngine/ProcessErrorCode.cs`.

Static analysis only — nothing above required running Engine or touching the
licence server.
