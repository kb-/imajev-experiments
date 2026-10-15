# Launch profiles

Run commands from the repository root. All profiles live directly in this folder; each YAML is a complete configuration, with no inheritance or overlay files.

```sh
uv run imajev-game                                    # default.yaml: ImaJEV 4B NF4
uv run imajev-game --config configs/liquid-d1-3b-shared.yaml
```

Models and runtimes must be installed before gameplay. See [model setup](../docs/model-backends.md) and [ImaJEV deployment](../docs/deployment.md). Add `--debug-input` for logging and automatic session recording.

| Profile | Recognition | Opponent / initial settings |
| --- | --- | --- |
| [default.yaml](default.yaml) | ImaJEV 4B NF4 | ImaJEV 4B NF4; tic-tac-toe, Quoted, tactical guard enabled |
| [liquid-d1-3b-shared.yaml](liquid-d1-3b-shared.yaml) | Liquid d1, ImaJEV fallback | Liquid d1; Boku, Quoted, tactical guard enabled; one shared Liquid runtime |
| [liquid-d1-3b.yaml](liquid-d1-3b.yaml) | ImaJEV 4B NF4 | Liquid d1; tic-tac-toe, Quoted |
| [qwen3-8b.yaml](qwen3-8b.yaml), [qwen3-vl.yaml](qwen3-vl.yaml) | ImaJEV 4B NF4 | Ollama Qwen opponent |
| [decision3-mini-4b-q8.yaml](decision3-mini-4b-q8.yaml) | Liquid d1, ImaJEV fallback | Decision3 replacement for Nox; GPU verified; tic-tac-toe, Quoted, guard disabled; [strategy remains unsuitable in the tested setup](../docs/evaluation/decision3-mini-q8.md) |
| [decision3-mini-4b-shared.yaml](decision3-mini-4b-shared.yaml) | Decision3, ImaJEV fallback | Experimental shared vision/text runtime; [14/15 synthetic recognition controls passed](../docs/decision3.md); same strategy limitation |
| [decision2-nox-4b-q8.yaml](decision2-nox-4b-q8.yaml) | Liquid d1, ImaJEV fallback | Nox Q8; tic-tac-toe, Quoted, guard disabled; **unsuited for unassisted strategic play in the tested setup** ([evaluation](../docs/evaluation/decision2-nox-q8.md)); retained for research |
| [imajev-4b-nf4.yaml](imajev-4b-nf4.yaml) | ImaJEV 4B NF4 | Explicit backend settings for both ImaJEV roles |
| [imajev-2b-shared.yaml](imajev-2b-shared.yaml) | ImaJEV 2B | ImaJEV 2B; replaces the former root `config.2b.yaml` |
| [imajev-2b.yaml](imajev-2b.yaml) | ImaJEV 4B NF4 | ImaJEV 2B opponent |
| [boku.yaml](boku.yaml) | ImaJEV 4B NF4 | Boku; threshold 0.65, tactical guard disabled |
| [quoted.yaml](quoted.yaml) | ImaJEV 4B NF4 | Tic-tac-toe, Quoted, tactical guard enabled, opening suggestion disabled |
| [no-opening.yaml](no-opening.yaml) | ImaJEV 4B NF4 | Tic-tac-toe, Original prompt, opening suggestion disabled |
| [imajev-4b-nf4-unguarded.yaml](imajev-4b-nf4-unguarded.yaml) | ImaJEV 4B NF4 | Tactical guard and opening suggestion disabled; former `config.4b-nf4.yaml` |
| [coached-quoted.yaml](coached-quoted.yaml) | ImaJEV 4B NF4 | Coached quoted, shared ImaJEV coach |
| [coached-quoted-ollama.yaml](coached-quoted-ollama.yaml) | ImaJEV 4B NF4 | Coached quoted, Ollama `qwen3.6:latest` coach |

Other opponent examples: [Tev1](tev1-4b.yaml), [Nimble](nimble-9b.yaml), [Decider](decider-4b.yaml), [Decision 2 Nox](decision2-nox-4b.yaml), [Decision 2 Sol](decision2-sol-2b.yaml), [d1-omni](d1-omni-600m.yaml), [Kev](kev-4b.yaml), [lev](lev-4b.yaml), [Laya](laya.yaml), [Julia](julia-1.yaml), and [Clef Flash](clef-flash.yaml). These use ImaJEV recognition and require their own installed assets; they do not imply a validated local setup.

For Decision3, run `uv run python scripts/install_decision3.py`, then `uv run python scripts/install_decision3.py --verify` with the app closed. It installs a separate prebuilt CUDA runtime, Q8 weights and projector without requiring a compiler. See [Decision3 setup and results](../docs/decision3.md). The original Decision2 profiles remain research baselines.

For legacy Nox Q8, run `uv run python scripts/install_decision2.py`, then `uv run python scripts/install_decision2.py --verify` with the app closed. The build needs CUDA `nvcc`. See [installation and GPU validation](../docs/model-backends.md#decision-20-nox-q8-on-nvidia--wsl2); `--download-only` installs the weights independently.

Nox Q8's GPU integration is verified, but its [full-context Tic-tac-toe benchmark](../docs/evaluation/decision2-nox-q8.md) failed strategic suitability: 63/102 optimal canonical-order decisions and 11 losses in 12 games against minimax. Installation verification is not a playing-strength test. Other Decision2 variants remain unassessed by this experiment.

The GUI's **Move prompt** selector changes strategy, while the profile chooses the models. Select the next mode before **New game**, since the opponent may start immediately. Liquid's shared profile needs an explicit Ollama coach to enable Coached quoted; see [coaching configuration](../docs/model-backends.md#native-vision-recognition).

Paths inside a YAML file resolve relative to that file. Shipped profiles use `../models/` and `../runtimes/` for installed assets, `../sessions/` for recordings, and `../learning/coached-quoted/` for learned strategies. Existing local records have not been moved or removed.

The former root configs and `configs/models/*.yaml` now live here. Root `config.yaml` became `configs/default.yaml`; root `config.<preset>.yaml` generally became `configs/<preset>.yaml`, with the explicit exceptions listed above and `config.coached-quoted.ollama.yaml` becoming `configs/coached-quoted-ollama.yaml`. Update existing launch commands; missing config files are errors.
