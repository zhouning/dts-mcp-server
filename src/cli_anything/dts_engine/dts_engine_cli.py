"""cli-anything-dts-engine — command-line interface to DTS Engine 6.1.

DTS Engine (飞渡科技 / Freedo) is a GUI-only 3D digital-twin data production
tool. It ships no SDK and no documented CLI, but its GUI shell (Engine.exe) is a
managed .NET assembly that drives EngineWorker.exe over a stable 93-flag command
line. This CLI targets that contract directly, so agents can publish 3DT tiles
headlessly.

Protocol reference: ../../../ENGINE_PROTOCOL.md
"""
from __future__ import annotations

import json as jsonlib
import sys

import click

from .core import errors as errmod
from .core import job as jobmod
from .core import pipelines
from .core.install import EngineNotFound, describe_install, find_engine_dir
from .core.publish import publish, publish_osgb

VERSION = "1.0.0"


def emit(data, as_json: bool, human=None) -> None:
    """Print machine-readable JSON or a human rendering of the same data."""
    if as_json:
        click.echo(jsonlib.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif human:
        human(data)
    else:
        click.echo(jsonlib.dumps(data, indent=2, ensure_ascii=False, default=str))


def parse_flags(pairs: tuple[str, ...]) -> dict[str, object]:
    """Parse ``key=value`` / ``-key=value`` extras into a flag mapping."""
    out: dict[str, object] = {}
    for raw in pairs:
        item = raw.lstrip("-")
        if "=" not in item:
            raise click.BadParameter(f"expected key=value, got {raw!r}")
        k, _, v = item.partition("=")
        out[k.strip()] = v.strip()
    return out


@click.group(invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.option("--engine-dir", default=None, help="DTS Engine install dir (else $DTS_ENGINE_DIR).")
@click.version_option(VERSION, prog_name="cli-anything-dts-engine")
@click.pass_context
def cli(ctx, as_json, engine_dir):
    """Headless CLI for DTS Engine 3D tile publishing."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = as_json
    ctx.obj["engine_dir"] = engine_dir
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


# ---------------------------------------------------------------- inspection

@cli.command()
@click.pass_context
def info(ctx):
    """Show the located install: version, executables, bundled GDAL."""
    try:
        data = describe_install(ctx.obj["engine_dir"])
    except EngineNotFound as e:
        emit({"ok": False, "error": str(e)}, ctx.obj["json"])
        sys.exit(1)

    def human(d):
        click.echo(f"DTS Engine {d['version'] or '?'}")
        click.echo(f"  dir:   {d['engine_dir']}")
        click.echo(f"  gdal:  {', '.join(d['gdal']) or '(none)'}")
        for name, present in d["executables"].items():
            click.echo(f"  {'OK ' if present else '-- '} {name}")

    emit({"ok": True, **data}, ctx.obj["json"], human)


@cli.command("pipelines")
@click.pass_context
def list_pipelines(ctx):
    """List available publish pipelines and their required flags."""
    data = [
        {
            "name": p.name,
            "handleType": p.handle_type,
            "required": list(p.required),
            "optional": list(p.optional),
            "verified": p.verified,
            "summary": p.summary,
            "source": p.source,
            "notes": p.notes,
        }
        for p in pipelines.PIPELINES.values()
    ]

    def human(rows):
        for r in rows:
            mark = "verified" if r["verified"] else "unverified"
            click.echo(f"\n{r['name']}  [{mark}]")
            click.echo(f"  {r['summary']}")
            click.echo(f"  required: {' '.join('-' + f for f in r['required'])}")
            if r["notes"]:
                click.echo(f"  note: {r['notes']}")

    emit(data, ctx.obj["json"], human)


@cli.command("errors")
@click.option("--code", type=int, default=None, help="Describe a single exit code.")
@click.pass_context
def show_errors(ctx, code):
    """Show the shipped error-code table, or explain one code."""
    try:
        d = find_engine_dir(ctx.obj["engine_dir"])
        table = errmod.load_table(d)
    except EngineNotFound:
        table = dict(errmod.FALLBACK)

    if code is not None:
        emit(errmod.describe(code, table), ctx.obj["json"])
        return
    data = [
        {"code": c, "band": errmod.band_of(c), "message": m}
        for c, m in sorted(table.items())
    ]

    def human(rows):
        for r in rows:
            click.echo(f"{r['code']:>6}  [{r['band']:<8}] {r['message']}")

    emit(data, ctx.obj["json"], human)


# ------------------------------------------------------------------ publish

def _run_and_report(ctx, result):
    as_json = ctx.obj["json"]

    def human(d):
        if d.get("validation_errors"):
            click.secho("validation failed:", fg="red")
            for p in d["validation_errors"]:
                click.echo(f"  - {p}")
            return
        ok = d.get("ok")
        click.secho(
            f"{'OK' if ok else 'FAILED'}  {d.get('pipeline', '')}"
            f"  ({d.get('elapsed_seconds', '?')}s)",
            fg="green" if ok else "red",
        )
        err = d.get("error")
        if err and not ok:
            click.echo(f"  code {err['code']} [{err['band']}] {err['message']}")
        for rec in d.get("log", []):
            if rec["level"] == "ERROR":
                click.echo(f"  log: {rec['body']}")
        for o in d.get("outputs", []):
            click.echo(f"  -> {o['name']}  {o['size']:,} bytes")

    emit(result, as_json, human)
    if not result.get("ok"):
        sys.exit(1)


@cli.command()
@click.argument("pipeline")
@click.option("--outpath", required=True, help="Output directory for the 3DT tiles.")
@click.option("--set", "extras", multiple=True, metavar="KEY=VALUE",
              help="Any Engine flag, repeatable (e.g. --set roadRGB=255,0,0).")
@click.option("--timeout", default=3600, show_default=True, help="Seconds before abort.")
@click.option("--strict/--no-strict", default=True, show_default=True,
              help="Validate flags against the pipeline spec before running.")
@click.option("--quiet", is_flag=True, help="Do not stream Engine log lines.")
@click.pass_context
def run(ctx, pipeline, outpath, extras, timeout, strict, quiet):
    """Run a publish PIPELINE (see `pipelines` for names and required flags).

    \b
    Example — drape vectors on terrain and publish a 3DT tile:
      cli-anything-dts-engine run road --outpath ./out \\
        --set roadShp=roads.shp --set domPath=dom.tif --set demPath=dem.tif
    """
    flags = parse_flags(extras)
    flags["outpath"] = outpath
    def stream(rec):
        click.echo(f"  [{rec['level']}] {rec['body']}", err=True)

    on_line = None if (quiet or ctx.obj["json"]) else stream
    try:
        result = publish(
            pipeline, flags, engine_dir=ctx.obj["engine_dir"],
            timeout=timeout, strict=strict, on_line=on_line,
        )
    except (EngineNotFound, KeyError) as e:
        emit({"ok": False, "error": str(e)}, ctx.obj["json"])
        sys.exit(1)
    _run_and_report(ctx, result)


@cli.command("publish-osgb")
@click.option("--inpath", required=True, help="OSGB dataset directory or file.")
@click.option("--outpath", required=True, help="Output directory.")
@click.option("--async-pyramid", is_flag=True, help="Start the pyramid stage without waiting.")
@click.option("--set", "extras", multiple=True, metavar="KEY=VALUE", help="Extra Engine flags.")
@click.option("--timeout", default=3600, show_default=True)
@click.pass_context
def publish_osgb_cmd(ctx, inpath, outpath, async_pyramid, extras, timeout):
    """Publish OSGB in both stages (tiling, then LOD pyramid).

    The raw CLI needs two separate invocations for this; stopping after the
    first leaves an incomplete dataset.
    """
    try:
        result = publish_osgb(
            inpath, outpath, engine_dir=ctx.obj["engine_dir"],
            async_pyramid=async_pyramid, timeout=timeout, **parse_flags(extras),
        )
    except EngineNotFound as e:
        emit({"ok": False, "error": str(e)}, ctx.obj["json"])
        sys.exit(1)

    def human(d):
        for s in d["stages"]:
            ok = s.get("ok")
            click.secho(f"{'OK' if ok else 'FAILED'}  stage={s['stage']}"
                        f"  ({s.get('elapsed_seconds', '?')}s)",
                        fg="green" if ok else "red")
            err = s.get("error")
            if err and not ok:
                click.echo(f"  code {err['code']} [{err['band']}] {err['message']}")
        for o in d.get("outputs", []):
            click.echo(f"  -> {o['name']}  {o['size']:,} bytes")

    emit(result, ctx.obj["json"], human)
    if not result.get("ok"):
        sys.exit(1)


# --------------------------------------------------------------- job files

@cli.group("job")
def job_group():
    """Create and inspect .aconf job files (the -jsonPath input)."""


@job_group.command("new")
@click.argument("data_type")
@click.option("-o", "--out", required=True, help="Path to write the .aconf file.")
@click.option("--set", "extras", multiple=True, metavar="Field=Value",
              help="Job field, repeatable. Uses the C# property names.")
@click.pass_context
def job_new(ctx, data_type, out, extras):
    """Create a job file for DATA_TYPE (Terrain, Osgb, Vector, BIM, ...)."""
    try:
        job = jobmod.new_job(data_type, **parse_flags(extras))
    except KeyError as e:
        emit({"ok": False, "error": str(e)}, ctx.obj["json"])
        sys.exit(1)
    problems = jobmod.validate_job(data_type, job)
    path = jobmod.write_job(job, out)
    emit(
        {"ok": True, "path": str(path), "data_type": data_type,
         "job": job, "warnings": problems},
        ctx.obj["json"],
        lambda d: click.echo(f"wrote {d['path']}"),
    )


@job_group.command("show")
@click.argument("path")
@click.pass_context
def job_show(ctx, path):
    """Print a job file."""
    emit(jobmod.read_job(path), ctx.obj["json"])


@job_group.command("types")
@click.pass_context
def job_types(ctx):
    """List job data types and their known fields."""
    data = [
        {"data_type": name, "ordinal": ordinal,
         "fields": list(jobmod.JOB_FIELDS.get(name, ())) or None}
        for name, ordinal in jobmod.DATA_TYPES.items()
    ]
    emit(data, ctx.obj["json"],
         lambda rows: [click.echo(f"{r['ordinal']}  {r['data_type']}") for r in rows])


# -------------------------------------------------------------------- repl

@cli.command()
@click.pass_context
def repl(ctx):
    """Interactive session."""
    from .utils.repl_skin import ReplSkin

    skin = ReplSkin("dts-engine", version=VERSION)
    try:
        skin.print_banner()
    except Exception:
        click.echo(f"cli-anything dts-engine v{VERSION}")
    def safe(fn, *a, fallback: str = "") -> None:
        """Call a ReplSkin method, falling back to plain output.

        ReplSkin uses Unicode icons and ANSI styling that raise
        UnicodeEncodeError on GBK consoles and NoConsoleScreenBufferError
        outside a real Windows console.
        """
        try:
            fn(*a)
        except Exception:
            if fallback:
                click.echo(fallback)

    try:
        info_data = describe_install(ctx.obj["engine_dir"])
        line = f"{info_data['version']} @ {info_data['engine_dir']}"
        safe(skin.status, "Engine", line, fallback=f"Engine: {line}")
    except EngineNotFound as e:
        first = str(e).splitlines()[0]
        safe(skin.error, first, fallback=f"error: {first}")

    commands = {
        "info": "Show install details",
        "pipelines": "List publish pipelines",
        "errors [code]": "Error-code table",
        "run <pipeline> --outpath ...": "Run a pipeline",
        "publish-osgb --inpath ... --outpath ...": "Two-stage OSGB publish",
        "job new <type> -o file": "Create a job file",
        "help": "Show this list",
        "exit": "Leave",
    }
    # prompt_toolkit needs a real Windows console; it raises
    # NoConsoleScreenBufferError under piped stdin, MSYS/Git Bash, and CI.
    # Fall back to plain stdin so scripted sessions still work.
    session = None
    try:
        session = skin.create_prompt_session()
    except Exception:
        # Avoid skin.warning here: its Unicode icons raise UnicodeEncodeError
        # on GBK consoles, which is exactly where this fallback triggers.
        click.echo("No interactive console; reading commands from stdin.", err=True)

    def read_line() -> str:
        if session is not None:
            return skin.get_input(session)
        return input("dts-engine> ")

    while True:
        try:
            line = read_line().strip()
        except (EOFError, KeyboardInterrupt):
            break
        except Exception:
            break
        if not line:
            continue
        if line in {"exit", "quit"}:
            break
        if line == "help":
            safe(
                skin.help, commands,
                fallback="\n".join(f"  {k}  -  {v}" for k, v in commands.items()),
            )
            continue
        try:
            cli.main(args=line.split(), standalone_mode=False, obj=ctx.obj)
        except SystemExit:
            pass
        except Exception as e:  # keep the REPL alive on bad input
            safe(skin.error, str(e), fallback=f"error: {e}")
    safe(skin.print_goodbye, fallback="bye")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
