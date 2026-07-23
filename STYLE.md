# Writing Style Rules

Injected into the agent's prompt at the start of every thread. Re-read on every
command — edits apply immediately, no bot restart needed.

## Banned words

Never use the words on the left in any output — chat replies, code, comments, commit
messages, documents, slides, plot labels. Use the replacement instead.

| Don't write | Write instead |
|-------------|---------------|
| fleet | machines, workers, worker pool |
| canary | small pilot run, gating trial run |
| probe | few-shot target adaptation (or the precise method name) |
| rig | robot platform, workstation |
| arm (experiment sense) | experiment condition, variant, group |
| triage | failure diagnosis |
| sweet spot | optimal range |
| leverage (as a verb) | use |
| utilize | use |
| delve | examine |

Exception: "arm" is fine when it means the robot's physical arm (e.g. left arm,
right arm, arm joints). The ban is only on "arm" meaning an experiment condition.

## Tone

- Research artifacts (papers, slides, Notion pages) use precise academic terminology —
  no informal coinages.
- Plain, direct sentences. No marketing language, no hype.
- All Slack channel posts and captions in English.
