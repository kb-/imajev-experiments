"""Add a busy gate, provenance, and optional NF4 loading to the pinned upstream service.

The normal path uses the upstream model loader unchanged. When IMAJEV_QUANTIZATION=nf4,
only the base Qwen loading step is replaced; Imajev prompting, PEFT adapter, decision
readout, calibration, and HTTP handling remain upstream.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid


def _install_nf4_loader():
    """Patch the pinned TorchDecision loader for CUDA-only bitsandbytes NF4 inference."""
    import torch
    import torch_decision
    from safetensors.torch import load_file
    from transformers import BitsAndBytesConfig, Qwen3_5ForConditionalGeneration

    original = torch_decision.TorchDecision

    class NF4TorchDecision(original):
        def __init__(self, path, device, dtype=torch.bfloat16, max_length=4096, pad_multiple=0):
            if not str(device).startswith("cuda"):
                raise RuntimeError("NF4 serving requires CUDA; CPU/MPS fallback is disabled.")
            self.processor = torch_decision._LazyMultimodalProcessor(path)
            self.device = device
            self.max_length = max_length
            self.pad_multiple = pad_multiple
            quantization = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
            self.model = Qwen3_5ForConditionalGeneration.from_pretrained(
                path,
                local_files_only=True,
                dtype=torch.bfloat16,
                quantization_config=quantization,
                device_map={"": 0},
            ).eval()
            self.readout = None
            self._codebook = None
            self.codes = torch_decision.MAX_READOUT_CODES
            self.prompt_layout = torch_decision.DEFAULT_PROMPT_LAYOUT

        def enable_readout(self, adapter=None, trainable=True, codes=None):
            """Load the shipped trained readout without touching the quantized LM head."""
            if adapter is None:
                raise ValueError("NF4 serving requires an Imajev adapter with a trained decision readout.")
            path = Path(adapter) / "decision_readout.safetensors"
            manifest_path = Path(adapter) / "decision_readout.json"
            if not path.exists() or not manifest_path.exists():
                raise ValueError("NF4 serving requires decision_readout.safetensors and decision_readout.json.")

            trained = load_file(str(path), device=str(self.device))["weight"].float()
            if trained.ndim != 2 or trained.shape[0] not in (255, 256):
                raise ValueError("Decision readout must have 255 or 256 rows.")
            rows = int(trained.shape[0])
            wanted = torch_decision.check_readout_codes(codes if codes is not None else rows)
            if rows != wanted:
                raise ValueError(
                    f"NF4 serving does not synthesize readout rows from the quantized LM head; "
                    f"adapter has {rows} rows but {wanted} were requested."
                )

            self.codes = wanted
            self._codebook = None
            codebook = self._ensure_codebook(0)
            manifest = json.loads(manifest_path.read_text())
            actual = [{"code": code, "token_id": token_id} for code, token_id in codebook]
            bound = manifest.get("codes")
            if manifest.get("version") != 1 or not isinstance(bound, list) or bound != actual[:rows]:
                raise ValueError("Decision readout code/token binding does not match this tokenizer.")
            if not bool(torch.isfinite(trained).all()):
                raise ValueError("Decision readout contains non-finite values.")

            self.prompt_layout = torch_decision.check_prompt_layout(
                manifest.get("prompt_layout", torch_decision.DEFAULT_PROMPT_LAYOUT)
            )
            self.readout = torch.nn.Linear(
                trained.shape[1],
                self.codes,
                bias=False,
                device=self.device,
                dtype=torch.float32,
            )
            self.readout.weight.data.copy_(trained)
            self.readout.weight.requires_grad_(trainable)
            return True

    torch_decision.TorchDecision = NF4TorchDecision


class BusyGuard:
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        is_inference = scope['type'] == 'http' and scope['method'] == 'POST' and scope['path'] in ('/v1/systemone', '/v1/coach')
        if not is_inference:
            return await self.app(scope, receive, send)
        if self.state.serving or self.state.lock.locked():
            body = b'{"error":"busy","detail":"The previous GPU request is still running."}'
            await send({'type': 'http.response.start', 'status': 409, 'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': body})
            return
        self.state.serving = True
        try:
            return await self.app(scope, receive, send)
        finally:
            self.state.serving = False


def generate_strategy(engine, request):
    """Use the ordinary LM head while leaving the trained readout untouched."""
    import gc
    import torch
    tokenizer = engine.processor.tokenizer
    messages = [
        {'role': 'system', 'content': 'You coach O in tic-tac-toe. Revise the previous strategy using accepted moves and outcomes, retaining useful older lessons. Return only a concise actionable strategy, in at most 120 words, using six short numbered rules. No analysis or thinking.'},
        {'role': 'user', 'content': json.dumps(request, ensure_ascii=False)},
    ]
    # Actual tokenizer enforcement; always retain the triggering game.
    request = dict(request, games=list(request.get('games', [])))
    while True:
        messages[1]['content'] = json.dumps(request, ensure_ascii=False)
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = tokenizer(prompt, return_tensors='pt')
        if inputs['input_ids'].shape[-1] <= 3072:
            break
        if len(request['games']) <= 1:
            raise ValueError('Previous strategy and triggering game exceed the 3072-token input budget.')
        request['games'].pop()
        request['included_game_ids'] = [g['game_id'] for g in request['games']]
    inputs = {k: v.to(engine.device) for k, v in inputs.items()}
    output = tokens = None
    try:
        with engine.model.disable_adapter(), torch.inference_mode():
            output = engine.model.generate(**inputs, do_sample=False, max_new_tokens=512,
                                          pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
                                          use_cache=True)
        tokens = output[0, inputs['input_ids'].shape[-1]:].tolist()
        strategy = tokenizer.decode(tokens, skip_special_tokens=True).strip()
        if not strategy or len(tokens) >= 512:
            raise ValueError('Coach returned an empty or truncated strategy; previous revision retained. Output: ' + strategy[:1500])
        return {'strategy': strategy, 'truncated': False, 'prompt': messages,
                'usage': {'input_tokens': inputs['input_ids'].shape[-1], 'output_tokens': len(tokens)},
                'included_game_ids': request.get('included_game_ids', [])}
    finally:
        del output, tokens, inputs
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def main():
    root = Path.cwd()
    spec = importlib.util.spec_from_file_location('imajev_pinned_server', root / 'scripts/playground/server.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    quantization = os.environ.get("IMAJEV_QUANTIZATION", "").strip().lower()
    if quantization:
        if quantization != "nf4":
            raise SystemExit(f"Unsupported IMAJEV_QUANTIZATION={quantization!r}")
        _install_nf4_loader()

    original_create_app = module.create_app
    manifest_name = os.environ.get("IMAJEV_RUNTIME_MANIFEST", "runtime-manifest.json")
    manifest = json.loads((root.parent / manifest_name).read_text())
    runtime_id = str(uuid.uuid4())

    def create_app(*args, **kwargs):
        app = original_create_app(*args, **kwargs)
        app.state.serving = False
        app.add_middleware(BusyGuard, state=app.state)

        @app.get('/v1/status')
        def status():
            return {
                'busy': app.state.serving or app.state.lock.locked(),
                'coaching': hasattr(getattr(app.state.backend, 'engine', None), 'model') and hasattr(app.state.backend.engine.model, 'disable_adapter'),
                'runtime_id': runtime_id,
                'provenance': manifest,
            }

        from fastapi import HTTPException

        @app.post('/v1/coach')
        def coach(request: dict):
            engine = getattr(app.state.backend, 'engine', None)
            if engine is None or not hasattr(engine.model, 'disable_adapter'):
                raise HTTPException(503, 'Shared coaching requires an unmerged PEFT PyTorch model.')
            try:
                with app.state.lock:
                    result = generate_strategy(engine, request)
                return dict(result, model=app.state.backend.model, provenance=manifest)
            except Exception as exc:
                raise HTTPException(500, str(exc)) from exc

        # Upstream mounts a catch-all playground, so move our route ahead of it.
        for _ in range(2):
            route = app.router.routes.pop()
            app.router.routes.insert(0, route)
        return app

    module.create_app = create_app
    return module.main()


if __name__ == '__main__':
    raise SystemExit(main())
