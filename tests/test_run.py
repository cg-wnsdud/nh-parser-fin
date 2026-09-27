"""The normal CLI run always reaches the VLM/P1/P3 pipeline."""

import sys

from PIL import Image

import run
from nh_parser_fin.ingest.loader import LabPage
from nh_parser_fin.parse import pipeline


def test_main_runs_full_pipeline_without_vlm_flag(monkeypatch, tmp_path):
    source = tmp_path / "sample.png"
    page = LabPage(
        doc_id="sample",
        source_file=source.name,
        page_no=1,
        image=Image.new("RGB", (100, 100), "white"),
    )
    observed = {}

    monkeypatch.setattr(run, "iter_inputs", lambda paths, exclude: [source])
    monkeypatch.setattr(run, "load_pages", lambda path, **kwargs: [page])
    monkeypatch.setattr(
        run.client,
        "call",
        lambda *args, **kwargs: {"pruned": {}, "seconds": 0.0},
    )
    monkeypatch.setattr(run, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(run, "MEDIA_DIR", tmp_path / "media")

    def fake_full_pipeline(documents, tasks, *, out, media_dir, compact_output):
        observed.update(
            documents=documents,
            tasks=tasks,
            out=out,
            media_dir=media_dir,
            compact_output=compact_output,
        )
        return [], []

    monkeypatch.setattr(pipeline, "run_full_pipeline", fake_full_pipeline)
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--run-name", "demo", "--input", str(source)],
    )

    run.main()

    assert len(observed["documents"]) == 1
    assert len(observed["tasks"]) == 1
    assert observed["out"] == tmp_path / "outputs" / "demo"
    assert observed["compact_output"] is False
