# Monitoring machines & jobs
How to check GPUs, watch running jobs, and wire up finish-notifications back to the Slack thread.

## Quick status

```bash
nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader
df -h / /data 2>/dev/null | tail -n +2          # disk
free -g | awk '/Mem/{print $3"G used / "$2"G"}' # RAM
tmux ls 2>/dev/null                              # running sessions
```

Reply with a compact table, not raw dumps.

## Launching long jobs (the standard pattern)

Anything over a few minutes goes in a detached tmux session with a chained notification. The channel/thread IDs to use are given at the top of your prompt:

```bash
tmux new-session -d -s train_run1 \
  'cd ~/project && python train.py 2>&1 | tee train_run1.log; \
   python <BOT_DIR>/notify.py "✅ train_run1 finished" --log train_run1.log \
     --channel <CHANNEL_ID> --thread <THREAD_TS>'
```

Then reply immediately with: the tmux session name, the log path, and how to check progress. Do not wait for the job.

## Checking progress of a running job

```bash
tail -n 30 train_run1.log                        # latest output
grep -E "loss|acc|step" train_run1.log | tail -5 # last metrics
tmux capture-pane -pt train_run1 | tail -20      # if it doesn't log to a file
```

When asked "how's training going?", parse the metric lines and **plot the curve so far** (see `plotting.md`) instead of pasting log text — one picture beats fifty log lines.

## Watchdogs

Periodic check that pings Slack only when something changes state:

```bash
tmux new-session -d -s watchdog '
while true; do
  if ! tmux has-session -t train_run1 2>/dev/null; then
    python <BOT_DIR>/notify.py "⚠️ train_run1 tmux session is GONE" --log train_run1.log \
      --channel <CHANNEL_ID> --thread <THREAD_TS>; break
  fi
  sleep 300
done'
```

Same shape works for "notify me when GPU memory frees up", "when this file appears", etc. Always `break` after notifying so watchdogs don't spam.
