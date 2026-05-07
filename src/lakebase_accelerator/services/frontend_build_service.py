"""Frontend Build Service — Builds React frontend using a Databricks serverless job.

Since the accelerator runs in a Databricks App (Python-only, no Node.js),
we offload the npm build to a serverless Databricks Job.

Validated flow:
1. Write React source files to a UC Volume
2. Submit a serverless job that downloads npm and runs npm install + build
3. The job writes dist/ output back to the Volume via /Volumes/ FUSE mount
4. Read dist/ files back via SDK and return them as static/ for the bundle
"""

import asyncio
import base64
import time
from io import BytesIO

from lakebase_accelerator.settings import get_settings
from lakebase_accelerator.utils.exceptions import PipelineStepError
from lakebase_accelerator.utils.exceptions.error_codes import FRONTEND_GENERATION_FAILED
from lakebase_accelerator.utils.logger import logger

BUILD_POLL_INTERVAL_SECONDS = 10
NPM_VERSION = "10.9.2"


def _build_script(catalog: str, schema: str, volume: str, build_id: str, npm_version: str) -> str:
    """Generate the Python build script with paths baked in.

    Uses string concatenation (not f-strings) for the JS code to avoid
    curly brace conflicts between Python and JavaScript.
    """
    return (
        '"""Frontend Build — Databricks serverless."""\n'
        'import subprocess, os, sys, time, tempfile\n'
        '\n'
        'SOURCE = "/Volumes/' + catalog + '/' + schema + '/' + volume + '/' + build_id + '/source"\n'
        'OUTPUT = "/Volumes/' + catalog + '/' + schema + '/' + volume + '/' + build_id + '/dist"\n'
        'W = tempfile.mkdtemp(prefix="fb_")\n'
        'BD = os.path.join(W, "build")\n'
        'ND = os.path.join(W, "npm")\n'
        'NPM_VERSION = "' + npm_version + '"\n'
        '\n'
        'def run_cmd(cmd, cwd=None):\n'
        '    display = " ".join(cmd) if isinstance(cmd, list) else cmd\n'
        '    print("  > " + display)\n'
        '    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180, shell=isinstance(cmd, str))\n'
        '    if r.stdout.strip(): print(r.stdout[-2000:])\n'
        '    if r.returncode != 0: print("  FAILED: " + r.stderr[-2000:]); sys.exit(1)\n'
        '\n'
        'def install_npm():\n'
        '    cli = os.path.join(ND, "package", "bin", "npm-cli.js")\n'
        '    if os.path.exists(cli): return cli\n'
        '    print("Downloading npm " + NPM_VERSION)\n'
        '    os.makedirs(ND, exist_ok=True)\n'
        '    js = os.path.join(W, "dl.js")\n'
        '    tgz = os.path.join(W, "npm.tgz")\n'
        '    js_code = (\n'
        '        "const https=require(\'https\'),fs=require(\'fs\');"\n'
        '        "function dl(u){https.get(u,r=>{if(r.statusCode===302||r.statusCode===301){dl(r.headers.location);return;}"\n'
        '        "r.pipe(fs.createWriteStream(\'" + tgz + "\')).on(\'finish\',()=>process.exit(0));}).on(\'error\',e=>{process.exit(1);});}"\n'
        '        "dl(\'https://registry.npmjs.org/npm/-/npm-" + NPM_VERSION + ".tgz\');"\n'
        '    )\n'
        '    with open(js, "w") as f: f.write(js_code)\n'
        '    run_cmd(["node", js])\n'
        '    for _ in range(15):\n'
        '        if os.path.exists(tgz) and os.path.getsize(tgz) > 1000: break\n'
        '        time.sleep(1)\n'
        '    else: print("npm download timeout"); sys.exit(1)\n'
        '    run_cmd("tar -xzf " + tgz + " -C " + ND)\n'
        '    if not os.path.exists(cli): print("npm-cli.js not found"); sys.exit(1)\n'
        '    return cli\n'
        '\n'
        'def main():\n'
        '    print("=== Frontend Build Started ===")\n'
        '    npm = install_npm()\n'
        '    run_cmd(["node", npm, "--version"])\n'
        '    os.makedirs(BD, exist_ok=True)\n'
        '    sp = None\n'
        '    for c in [SOURCE, SOURCE.replace("/Volumes/", "/dbfs/Volumes/")]:\n'
        '        if os.path.exists(c): sp = c; break\n'
        '    if not sp: print("ERROR: source not found"); sys.exit(1)\n'
        '    print("Source: " + sp)\n'
        '    fc = 0\n'
        '    for root, _, files in os.walk(sp):\n'
        '        for fname in files:\n'
        '            s = os.path.join(root, fname)\n'
        '            rel = os.path.relpath(s, sp)\n'
        '            d = os.path.join(BD, rel)\n'
        '            os.makedirs(os.path.dirname(d), exist_ok=True)\n'
        '            open(d, "wb").write(open(s, "rb").read())\n'
        '            fc += 1\n'
        '    print("Copied " + str(fc) + " files")\n'
        '    if fc == 0: print("ERROR: no files"); sys.exit(1)\n'
        '    run_cmd(["node", npm, "install"], cwd=BD)\n'
        '    run_cmd(["node", npm, "run", "build"], cwd=BD)\n'
        '    dd = os.path.join(BD, "dist")\n'
        '    if not os.path.exists(dd): print("ERROR: no dist/"); sys.exit(1)\n'
        '    op = None\n'
        '    for c in [OUTPUT, OUTPUT.replace("/Volumes/", "/dbfs/Volumes/")]:\n'
        '        try: os.makedirs(c, exist_ok=True); op = c; break\n'
        '        except: continue\n'
        '    if not op: print("ERROR: cant write output"); sys.exit(1)\n'
        '    print("Output: " + op)\n'
        '    oc = 0\n'
        '    for root, _, files in os.walk(dd):\n'
        '        for fname in files:\n'
        '            s = os.path.join(root, fname)\n'
        '            rel = os.path.relpath(s, dd)\n'
        '            d = os.path.join(op, rel)\n'
        '            os.makedirs(os.path.dirname(d), exist_ok=True)\n'
        '            open(d, "wb").write(open(s, "rb").read())\n'
        '            oc += 1\n'
        '    print("Wrote " + str(oc) + " files")\n'
        '    print("=== Frontend Build Complete ===")\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    main()\n'
    )


class FrontendBuildService:
    """Builds React frontend via a Databricks serverless job."""

    def __init__(self, workspace_client) -> None:
        self._workspace_client = workspace_client
        self._settings = get_settings()

    async def build_frontend(self, build_id: str, source_files: dict[str, str]) -> dict[str, str]:
        """Build React frontend. Returns dict with "static/..." keys."""
        logger.info(f"Starting frontend build: {build_id}", extra={"step": "frontend_build"})

        await self._write_source_to_volume(build_id, source_files)
        await self._run_build_job(build_id)
        dist_files = await self._read_dist_from_volume(build_id)

        if not dist_files:
            raise PipelineStepError(
                step_name="frontend_build",
                message="Build succeeded but no dist/ output found",
                error_code=FRONTEND_GENERATION_FAILED,
            )

        static_files = {f"static/{path}": content for path, content in dist_files.items()}
        logger.info(f"Frontend build complete: {len(static_files)} files", extra={"step": "frontend_build"})

        asyncio.create_task(self._cleanup_volume(build_id))
        return static_files

    async def _write_source_to_volume(self, build_id: str, source_files: dict[str, str]) -> None:
        """Write React source files to UC Volume."""
        s = self._settings
        base = f"/Volumes/{s.volume_catalog}/{s.volume_schema}/{s.volume_name}/{build_id}/source"
        logger.info(f"Writing {len(source_files)} files to {base}")

        for rel_path, content in source_files.items():
            try:
                await asyncio.to_thread(
                    self._workspace_client.files.upload,
                    f"{base}/{rel_path}",
                    BytesIO(content.encode("utf-8")),
                    overwrite=True,
                )
            except Exception as e:
                raise PipelineStepError(
                    step_name="frontend_build",
                    message=f"Failed to write '{rel_path}' to Volume: {e}",
                    error_code=FRONTEND_GENERATION_FAILED,
                ) from e

    async def _run_build_job(self, build_id: str) -> None:
        """Submit and wait for the serverless build job."""
        from databricks.sdk.service.jobs import (
            SubmitTask, SparkPythonTask, Source, JobEnvironment,
        )
        from databricks.sdk.service.compute import Environment
        from databricks.sdk.service.workspace import ImportFormat

        settings = self._settings

        # Generate build script with paths baked in
        build_script = _build_script(
            catalog=settings.volume_catalog,
            schema=settings.volume_schema,
            volume=settings.volume_name,
            build_id=build_id,
            npm_version=NPM_VERSION,
        )

        # Upload to Workspace
        script_path = f"/Workspace/Shared/lakebase-accelerator-apps/_build_scripts/{build_id}_build.py"
        try:
            await asyncio.to_thread(
                self._workspace_client.workspace.mkdirs,
                "/Workspace/Shared/lakebase-accelerator-apps/_build_scripts",
            )
            encoded = base64.b64encode(build_script.encode("utf-8")).decode("ascii")
            await asyncio.to_thread(
                self._workspace_client.workspace.import_,
                path=script_path,
                content=encoded,
                format=ImportFormat.AUTO,
                overwrite=True,
            )
        except Exception as e:
            raise PipelineStepError(
                step_name="frontend_build",
                message=f"Failed to upload build script: {e}",
                error_code=FRONTEND_GENERATION_FAILED,
            ) from e

        # Submit job
        if settings.frontend_build_use_serverless:
            tasks = [
                SubmitTask(
                    task_key="build_frontend",
                    spark_python_task=SparkPythonTask(python_file=script_path, source=Source.WORKSPACE),
                    environment_key="default",
                )
            ]
            environments = [JobEnvironment(environment_key="default", spec=Environment(client="1"))]
        else:
            tasks = [
                SubmitTask(
                    task_key="build_frontend",
                    spark_python_task=SparkPythonTask(python_file=script_path, source=Source.WORKSPACE),
                    new_cluster={
                        "spark_version": settings.frontend_build_spark_version,
                        "num_workers": 0,
                        "node_type_id": settings.frontend_build_node_type,
                        "spark_conf": {"spark.master": "local[*]", "spark.databricks.cluster.profile": "singleNode"},
                        "custom_tags": {"ResourceClass": "SingleNode"},
                    },
                )
            ]
            environments = None

        logger.info(f"Submitting build job (serverless={settings.frontend_build_use_serverless})")
        try:
            kwargs = {"run_name": f"frontend-build-{build_id}", "tasks": tasks}
            if environments:
                kwargs["environments"] = environments
            run = await asyncio.to_thread(self._workspace_client.jobs.submit, **kwargs)
            run_id = run.run_id
        except Exception as e:
            raise PipelineStepError(
                step_name="frontend_build",
                message=f"Failed to submit build job: {e}",
                error_code=FRONTEND_GENERATION_FAILED,
            ) from e

        logger.info(f"Build job submitted: run_id={run_id}")
        await self._poll_job(run_id)

    async def _poll_job(self, run_id: int) -> None:
        """Poll until job completes or times out."""
        start = time.time()
        timeout = self._settings.frontend_build_timeout_seconds

        while time.time() - start < timeout:
            try:
                status = await asyncio.to_thread(self._workspace_client.jobs.get_run, run_id)
                state = status.state
                lcs = str(state.life_cycle_state) if state.life_cycle_state else ""
                rs = str(state.result_state) if state.result_state else ""

                if "TERMINATED" in lcs:
                    if "SUCCESS" in rs:
                        logger.info("Frontend build job succeeded")
                        return
                    error_msg = state.state_message or f"Job failed: {rs}"
                    raise PipelineStepError(
                        step_name="frontend_build",
                        message=f"Build job failed: {error_msg}",
                        error_code=FRONTEND_GENERATION_FAILED,
                    )
                if "INTERNAL_ERROR" in lcs or "SKIPPED" in lcs:
                    raise PipelineStepError(
                        step_name="frontend_build",
                        message=f"Build job error: {state.state_message}",
                        error_code=FRONTEND_GENERATION_FAILED,
                    )
            except PipelineStepError:
                raise
            except Exception as e:
                logger.warning(f"Poll error: {e}")

            await asyncio.sleep(BUILD_POLL_INTERVAL_SECONDS)

        try:
            await asyncio.to_thread(self._workspace_client.jobs.cancel_run, run_id)
        except Exception:
            pass
        raise PipelineStepError(
            step_name="frontend_build",
            message=f"Build job timed out after {timeout}s",
            error_code=FRONTEND_GENERATION_FAILED,
        )

    async def _read_dist_from_volume(self, build_id: str) -> dict[str, str]:
        """Read built dist/ files from Volume."""
        s = self._settings
        base = f"/Volumes/{s.volume_catalog}/{s.volume_schema}/{s.volume_name}/{build_id}/dist"
        logger.info(f"Reading built files from {base}")
        dist_files: dict[str, str] = {}

        try:
            entries = await asyncio.to_thread(
                self._workspace_client.files.list_directory_contents, base
            )
            for entry in entries:
                if entry.is_directory:
                    sub = await self._read_dir(f"{base}/{entry.name}", entry.name)
                    dist_files.update(sub)
                else:
                    content = await self._read_file(f"{base}/{entry.name}")
                    if content is not None:
                        dist_files[entry.name] = content
        except Exception as e:
            raise PipelineStepError(
                step_name="frontend_build",
                message=f"Failed to read build output: {e}",
                error_code=FRONTEND_GENERATION_FAILED,
            ) from e

        return dist_files

    async def _read_dir(self, path: str, prefix: str) -> dict[str, str]:
        """Recursively read files from a Volume directory."""
        files: dict[str, str] = {}
        try:
            entries = await asyncio.to_thread(
                self._workspace_client.files.list_directory_contents, path
            )
            for entry in entries:
                rel = f"{prefix}/{entry.name}"
                if entry.is_directory:
                    files.update(await self._read_dir(f"{path}/{entry.name}", rel))
                else:
                    content = await self._read_file(f"{path}/{entry.name}")
                    if content is not None:
                        files[rel] = content
        except Exception as e:
            logger.warning(f"Could not read {path}: {e}")
        return files

    async def _read_file(self, path: str) -> str | None:
        """Read a text file from Volume."""
        try:
            resp = await asyncio.to_thread(self._workspace_client.files.download, path)
            return resp.contents.read().decode("utf-8")
        except UnicodeDecodeError:
            return None
        except Exception as e:
            logger.warning(f"Could not read {path}: {e}")
            return None

    async def _cleanup_volume(self, build_id: str) -> None:
        """Clean up temp build files (best-effort)."""
        s = self._settings
        path = f"/Volumes/{s.volume_catalog}/{s.volume_schema}/{s.volume_name}/{build_id}"
        try:
            await asyncio.to_thread(self._workspace_client.files.delete_directory, path)
        except Exception:
            pass
