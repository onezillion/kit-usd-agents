"""Consolidate POC Test A + Test B summaries into a CSV comparison table.

Reads every *.summary.json under the run dir and produces:
  - poc_results.csv (one row per stream/case with the key metrics)
  - poc_results_summary.json (aggregate per backend)
"""
import csv
import json
import sys
from pathlib import Path


def pct(d, key):
    if not d:
        return ""
    return d.get(key, "")


def harvest(run_dir: str):
    rd = Path(run_dir)
    rows = []
    for sf in sorted(rd.glob("*.summary.json")):
        # skip the 2-pipe result (different schema) and poc_overall
        if sf.name.startswith("C_live_2pipe") or sf.name == "poc_summary.json":
            continue
        try:
            d = json.load(open(sf))
        except Exception:
            continue
        if "results" in d and isinstance(d["results"], list) and d["results"]:
            # Test A single-stream
            r = d["results"][0]
            v = d.get("verify", {}) or {}
            rows.append({
                "test": "A",
                "stream_id": d.get("stream_id", ""),
                "backend": d.get("backend", ""),
                "fps_target": d.get("fps_target", ""),
                "mode": d.get("mode", ""),
                "bitrate": d.get("bitrate", ""),
                "produced": r.get("produced", 0),
                "encoded": r.get("encoded", 0),
                "fps_encoded": round(r.get("fps_encoded", 0), 2),
                "lat_p50_ms": round(r.get("latency_s", {}).get("p50", 0) * 1000, 3),
                "lat_p95_ms": round(r.get("latency_s", {}).get("p95", 0) * 1000, 3),
                "lat_p99_ms": round(r.get("latency_s", {}).get("p99", 0) * 1000, 3),
                "lat_max_ms": round(r.get("latency_s", {}).get("max", 0) * 1000, 3),
                "enc_submit_p50_ms": round(r.get("encoder_submit_duration_s", {}).get("p50", 0) * 1000, 3),
                "enc_completion_p50_ms": round(r.get("encoder_completion_duration_s", {}).get("p50", 0) * 1000, 3),
                "au_size_p50": round(r.get("au_size_bytes", {}).get("p50", 0)),
                "queue_high_water": r.get("queue", {}).get("high_water", 0),
                "drops": r.get("queue", {}).get("dropped", 0),
                "replacements": r.get("queue", {}).get("replaced", 0),
                "verify_ok": v.get("ok", ""),
                "profile": v.get("profile_name", ""),
                "psnr_db": round(v.get("psnr_db"), 2) if v.get("psnr_db") else "",
                "hw_nvenc": d.get("hardware_proof", {}).get("hardware", ""),
            })
        elif "per_stream" in d:
            # Test B multi-encoder
            agg = d.get("aggregate", {})
            rows.append({
                "test": "B",
                "stream_id": d.get("label", ""),
                "backend": d.get("backend", ""),
                "fps_target": d.get("fps_target", ""),
                "mode": d.get("mode", ""),
                "fan_mode": d.get("fan_mode", ""),
                "n_encoders": d.get("n_encoders", ""),
                "produced": "",
                "encoded": agg.get("total_encoded", 0),
                "fps_encoded": round(agg.get("aggregate_fps", 0), 2),
                "lat_p50_ms": "",
                "lat_p95_ms": "",
                "lat_p99_ms": "",
                "lat_max_ms": "",
                "slowest_fps": round(agg.get("slowest_fps", 0), 2),
                "total_drops": agg.get("total_drops", 0),
                "total_replacements": agg.get("total_replacements", 0),
            })
    return rows


def main(run_dir):
    rows = harvest(run_dir)
    out_csv = Path(run_dir) / "poc_results.csv"
    if rows:
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in keys})
    print("wrote", out_csv, "with", len(rows), "rows")
    # also pretty-print Test A single-encoder aggregate by backend
    by_backend = {}
    for r in rows:
        if r.get("test") == "A":
            b = r["backend"]
            by_backend.setdefault(b, []).append(r)
    print("\n--- Test A per-backend (avg over cases) ---")
    for b, rs in by_backend.items():
        lats = [x["lat_p50_ms"] for x in rs if isinstance(x.get("lat_p50_ms"), (int, float))]
        fps = [x["fps_encoded"] for x in rs]
        psnrs = [x["psnr_db"] for x in rs if isinstance(x.get("psnr_db"), (int, float))]
        import statistics
        print(f"  {b:18s} cases={len(rs)} avg_fps={statistics.mean(fps):.1f} "
              f"avg_lat_p50={statistics.mean(lats):.2f}ms "
              f"avg_psnr={statistics.mean(psnrs):.1f}dB" if psnrs else
              f"  {b:18s} cases={len(rs)} avg_fps={statistics.mean(fps):.1f} "
              f"avg_lat_p50={statistics.mean(lats):.2f}ms (no PSNR)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
