from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
import uvicorn

from ontoagents.config import get_settings
from ontoagents.evals import load_cases, run_case
from ontoagents.kg.store import load_into_graphdb
from ontoagents.wiring import pipeline, runtime

app = typer.Typer(no_args_is_help=True, help="Ontology-grounded Text-to-SQL agents.")


@app.command()
def ask(question: str, show_attempts: bool = False) -> None:
    """Ask one question and print the verified answer."""

    async def run() -> None:
        async with pipeline(get_settings()) as p:
            answer = await p.ask(question)
        typer.echo(f"[{answer.outcome}] {answer.answer}")
        if answer.sql:
            typer.echo(f"\n{answer.sql}")
        if show_attempts:
            typer.echo(json.dumps([a.model_dump() for a in answer.attempts], indent=2))

    asyncio.run(run())


@app.command("load-graphdb")
def load_graphdb() -> None:
    """Create the GraphDB repository and load the ontology into it."""
    s = get_settings()
    asyncio.run(load_into_graphdb(s.graphdb_url, s.graphdb_repository, s.ontology_path))
    typer.echo(f"loaded {s.ontology_path} into {s.graphdb_url}/repositories/{s.graphdb_repository}")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the HTTP API."""
    uvicorn.run("ontoagents.api:create_app", factory=True, host=host, port=port)


@app.command("eval")
def evaluate(cases: Path = Path("evals/golden.yaml"), min_accuracy: float = 0.9) -> None:
    """Run the golden set against the live model; exit non-zero below the threshold."""

    async def run() -> float:
        async with runtime(get_settings()) as rt:
            results = [await run_case(c, rt.pipeline, rt.executor) for c in load_cases(cases)]
        for r in results:
            typer.echo(f"{'PASS' if r.passed else 'FAIL'}  {r.case.id:<28} {r.detail}")
        return sum(r.passed for r in results) / len(results)

    accuracy = asyncio.run(run())
    typer.echo(f"\naccuracy: {accuracy:.0%} (threshold {min_accuracy:.0%})")
    if accuracy < min_accuracy:
        raise typer.Exit(code=1)
