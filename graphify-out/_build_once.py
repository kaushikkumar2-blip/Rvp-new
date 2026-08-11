import json
import sys
from pathlib import Path

from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.detect import detect, save_manifest
from graphify.export import to_json
from graphify.extract import collect_files, extract
from graphify.report import generate

root = Path(".")
out = root / "graphify-out"
out.mkdir(exist_ok=True)
(out / ".graphify_python").write_text(sys.executable, encoding="utf-8")

detection = detect(root)
(out / ".graphify_detect.json").write_text(
    json.dumps(detection, ensure_ascii=False), encoding="utf-8"
)
print(
    f"Corpus: {detection['total_files']} files, "
    f"~{detection.get('total_words', 0)} words"
)

code_files = []
for f in detection.get("files", {}).get("code", []):
    p = Path(f)
    code_files.extend(collect_files(p) if p.is_dir() else [p])

ast = (
    extract(code_files, cache_root=root)
    if code_files
    else {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
)
semantic = {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}

seen = {n["id"] for n in ast["nodes"]}
merged_nodes = list(ast["nodes"])
for n in semantic["nodes"]:
    if n["id"] not in seen:
        merged_nodes.append(n)
        seen.add(n["id"])

extraction = {
    "nodes": merged_nodes,
    "edges": ast["edges"] + semantic["edges"],
    "hyperedges": semantic.get("hyperedges", []),
    "input_tokens": 0,
    "output_tokens": 0,
}
(out / ".graphify_extract.json").write_text(
    json.dumps(extraction, indent=2, ensure_ascii=False), encoding="utf-8"
)

G = build_from_json(extraction, root=str(root.resolve()), directed=False)
if G.number_of_nodes() == 0:
    raise SystemExit("Graph is empty")

communities = cluster(G)
cohesion = score_all(G, communities)
labels = {cid: f"Community {cid}" for cid in communities}
gods = god_nodes(G)
surprises = surprising_connections(G, communities)
questions = suggest_questions(G, communities, labels)

to_json(G, communities, str(out / "graph.json"))
report = generate(
    G,
    communities,
    cohesion,
    labels,
    gods,
    surprises,
    detection,
    {"input": 0, "output": 0},
    str(root.resolve()),
    suggested_questions=questions,
)
(out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
save_manifest(detection.get("all_files") or detection["files"], root=str(root.resolve()))
print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
