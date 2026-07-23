# Slurm jobs
Submitting, checking, and getting notified about Slurm jobs from Slack.

## Submit template

```bash
cat > job.sh <<'SH'
#!/bin/bash
#SBATCH --job-name=myrun
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=slurm-%j.out

source ~/miniconda3/etc/profile.d/conda.sh && conda activate myenv
srun python train.py --config cfg.yaml
SH
sbatch job.sh          # prints: Submitted batch job <JOBID>
```

Capture the job ID from the output and report it back.

## Status & diagnosis

```bash
squeue -u $USER -o "%.10i %.20j %.8T %.10M %.6D %R"   # my queue: state, runtime, reason
sacct -j <JOBID> --format=JobID,State,ExitCode,Elapsed,MaxRSS   # after it ends
tail -n 30 slurm-<JOBID>.out                            # live log
scancel <JOBID>                                         # kill
```

Common states worth explaining in replies: `PD (Resources)` = waiting for GPUs, `PD (Priority)` = queued behind others, `CG` = finishing, `F`/`OOM` in sacct = check `MaxRSS` vs requested `--mem`.

## Notify the thread when a job finishes

Wrap the wait in tmux (the `--wait` flag blocks until the job ends; never block your own reply on it). Channel/thread IDs are at the top of your prompt:

```bash
tmux new-session -d -s slurm_<JOBID> \
  'sbatch --wait job.sh; \
   python <BOT_DIR>/notify.py "✅ slurm job finished" --log slurm-$(squeue -h -u $USER -o %i | tail -1).out \
     --channel <CHANNEL_ID> --thread <THREAD_TS>'
```

Or, for an already-submitted job:

```bash
tmux new-session -d -s slurm_<JOBID> \
  'while squeue -h -j <JOBID> | grep -q .; do sleep 60; done; \
   python <BOT_DIR>/notify.py "✅ job <JOBID> left the queue" --log slurm-<JOBID>.out \
     --channel <CHANNEL_ID> --thread <THREAD_TS>'
```

## Array jobs

```bash
#SBATCH --array=0-9%4        # 10 tasks, max 4 at once; use $SLURM_ARRAY_TASK_ID inside
squeue -u $USER -r           # one row per array task
sacct -j <JOBID> --format=JobID,State,ExitCode | awk '$2!="COMPLETED"'   # failures only
```

When reporting an array, summarize (`7/10 done, 1 failed: task 3, OOM`) instead of listing every task.
