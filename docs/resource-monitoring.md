# Per-run resource logs

On Linux, each automation run writes a `resources.jsonl` file in its run folder:

```text
logs/runs/<run_id>/resources.jsonl
```

It records one sample every five seconds, with a UTC timestamp, available and
used system RAM, swap usage, the bot process, its Chrome/ChromeDriver process
tree, Ollama's `llama-server`, and NVIDIA GPU memory/utilization when
`nvidia-smi` is available. Per-process RSS is included; summed PSS helps account
for shared pages without counting them fully for every Chrome process.

To follow the latest run while it is active:

```bash
ls -td logs/runs/* | head -1
tail -f "$(ls -td logs/runs/* | head -1)/resources.jsonl" | jq '{time: .timestamp, available_ram_mb: .system.ram_available_mb, swap_used_mb: .system.swap_used_mb, chrome_pss_mb: .processes.chrome_tree.pss_mb, chrome_processes: .processes.chrome_tree.process_count, ollama_rss_mb: .processes.ollama_runner.rss_mb, gpus: .gpus}'
```

The logger is diagnostic only. If sampling is unavailable or fails, automation
continues without the resource sample.
