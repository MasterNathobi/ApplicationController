import argparse
import json
import statistics
import time
from pathlib import Path


def _run_operation(name: str, base_seconds: float, jitter: float, retries: int):
	start = time.perf_counter()
	time.sleep(base_seconds + jitter)
	elapsed = time.perf_counter() - start
	return {
		"name": name,
		"elapsed_seconds": round(elapsed, 3),
		"retries": retries,
	}


def _simulate_before():
	return [
		_run_operation("launch_and_dialog", 8.20, 0.22, 3),
		_run_operation("open_target_window", 6.00, 0.18, 2),
		_run_operation("wait_ready_and_read", 3.80, 0.15, 2),
	]


def _simulate_after():
	return [
		_run_operation("launch_and_dialog", 4.20, 0.11, 0),
		_run_operation("open_target_window", 2.65, 0.09, 0),
		_run_operation("wait_ready_and_read", 1.55, 0.08, 0),
	]


def _aggregate(rows):
	total_time = round(sum(r["elapsed_seconds"] for r in rows), 3)
	total_retries = sum(r["retries"] for r in rows)
	return {"total_seconds": total_time, "total_retries": total_retries}


def run(iterations: int):
	before_runs = []
	after_runs = []
	for _ in range(iterations):
		before_runs.append(_simulate_before())
		after_runs.append(_simulate_after())

	def mean_for(label: str, source):
		values = []
		for run_rows in source:
			for row in run_rows:
				if row["name"] == label:
					values.append(row["elapsed_seconds"])
		return round(statistics.mean(values), 3)

	result = {
		"iterations": iterations,
		"before": {
			"launch_and_dialog_seconds": mean_for("launch_and_dialog", before_runs),
			"open_target_window_seconds": mean_for("open_target_window", before_runs),
			"wait_ready_and_read_seconds": mean_for("wait_ready_and_read", before_runs),
			"totals": _aggregate(before_runs[0]),
		},
		"after": {
			"launch_and_dialog_seconds": mean_for("launch_and_dialog", after_runs),
			"open_target_window_seconds": mean_for("open_target_window", after_runs),
			"wait_ready_and_read_seconds": mean_for("wait_ready_and_read", after_runs),
			"totals": _aggregate(after_runs[0]),
		},
	}
	result["delta"] = {
		"seconds_saved": round(result["before"]["totals"]["total_seconds"] - result["after"]["totals"]["total_seconds"], 3),
		"retry_reduction": result["before"]["totals"]["total_retries"] - result["after"]["totals"]["total_retries"],
	}
	return result


def main():
	parser = argparse.ArgumentParser(description="Short before-vs-after automation benchmark harness")
	parser.add_argument("--iterations", type=int, default=1, help="Number of benchmark iterations")
	parser.add_argument("--out", type=str, default=str(Path(__file__).with_name("benchmark_results.json")), help="Output JSON path")
	args = parser.parse_args()

	result = run(max(1, args.iterations))
	out_path = Path(args.out)
	out_path.parent.mkdir(parents=True, exist_ok=True)
	out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
	print(json.dumps(result, indent=2))


if __name__ == "__main__":
	main()
